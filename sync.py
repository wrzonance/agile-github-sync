#!/usr/bin/env python3
"""Ongoing GitHub -> AgilePlace sync (Model 2). Agnostic: derives everything from live GitHub + the
board + the GitHub Projects v2 Status, with no manifest/issue-map.

Per run: ensure a card per issue (matched by URL, then customId); move each card to the lane for its
stage (Projects v2 Status = source of truth, label/PR fallback); mirror sub-issues as parent/child
connections; mirror blocked-by as the card Blocked state; bidirectionally reconcile labels/milestone
<-> tags and planned dates <-> Project date fields. Every mutation to one card is batched into a single
versioned PATCH (optimistic concurrency).

DRY RUN by default. State is target-scoped, issue-URL-keyed, records each issue's card id (so a
re-created card resets its merge base instead of wiping GitHub), atomic, and fail-closed.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile

import agileplace
import ghkit
import ghproject
from config import STATE_FILE, env_config
from reconcile import reconcile, reconcile_value
from stages import blocked_reason, epic_key_for_task, issue_stage, normalize_status, title_key

MS_PREFIX = "milestone:"
STATE_SCHEMA = 2


def load_state(target: str, board: str) -> dict:
    if not STATE_FILE.exists():
        return {"schema": STATE_SCHEMA, "target": target, "board": board, "issues": {}}
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as err:
        raise SystemExit(f"ERROR: {STATE_FILE} is unreadable/corrupt ({err}). Refusing to run so removals "
                         f"aren't resurrected. Inspect or delete it, then re-run.")
    if state.get("target") != target or str(state.get("board")) != str(board):
        raise SystemExit(f"ERROR: {STATE_FILE} is for target {state.get('target')}/board {state.get('board')}, "
                         f"but configured for {target}/board {board}. Move or delete it, then re-run.")
    state.setdefault("schema", STATE_SCHEMA)
    state.setdefault("issues", {})
    return state


def save_state(state: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(STATE_FILE.parent), prefix=".sync-state.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, STATE_FILE)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def epic_task_numbers(cfg: dict, epic: dict, by_key: dict) -> list[int]:
    """Native sub-issues; fall back to the [KEY] title convention ONLY on a query FAILURE (None), never
    on a genuine empty result."""
    nums = ghkit.sub_issue_numbers(cfg, epic["number"])
    if nums is not None:
        return nums
    print(f"WARN  [{title_key(epic['title']) or epic['number']}] native sub-issues unavailable -- "
          f"falling back to the [KEY] title convention")
    epic_key = title_key(epic["title"])
    return [i["number"] for i in by_key.values()
            if epic_key_for_task(title_key(i["title"]) or "") == epic_key]


def issue_card_title(issue: dict) -> str:
    t = issue["title"]
    k = title_key(t)
    if k and t.startswith(f"[{k}]"):
        return t[len(f"[{k}]"):].strip() or t
    return t


def resolve_issue_stage(issue: dict, project_status: dict) -> str:
    raw = project_status.get(issue["url"])
    return (normalize_status(raw) if raw else None) or issue_stage(issue)


def _label_set(labels, ignore: frozenset) -> set[str]:
    return {l for l in labels if l not in ignore and not l.startswith(MS_PREFIX)}


def _card_milestones(card: dict) -> tuple[str | None, set[str]]:
    """(current milestone value, all raw milestone: tags). Deterministic: the current value is the
    lexicographically-first non-empty suffix; extras/empties are stale tags to be cleaned up."""
    tags = {t for t in agileplace.card_tags(card) if t.startswith(MS_PREFIX)}
    suffixes = sorted(t[len(MS_PREFIX):] for t in tags if t[len(MS_PREFIX):])
    return (suffixes[0] if suffixes else None), tags


def sync_metadata(cfg, apply, issue, card, ignore, issues_state, queue) -> None:
    url = issue["url"]
    prev = issues_state[url]

    gh_labels = _label_set(issue["labels"], ignore)
    ap_label_tags = _label_set((t for t in agileplace.card_tags(card) if not t.startswith(MS_PREFIX)), ignore)
    base_labels = _label_set(prev.get("labels", []), ignore)
    r = reconcile(base_labels, gh_labels, ap_label_tags)
    for item in sorted(r.gh_add):
        ghkit.edit_label(cfg, apply, issue["number"], item, add=True)
    for item in sorted(r.gh_remove):
        ghkit.edit_label(cfg, apply, issue["number"], item, add=False)
    tag_ops = [agileplace.op_tag(t, add=True) for t in sorted(r.ap_add)]
    tag_ops += [agileplace.op_tag(t, add=False) for t in sorted(r.ap_remove)]

    gh_ms = issue.get("milestone")
    ap_ms, ms_tags = _card_milestones(card)
    new_ms = reconcile_value(prev.get("milestone"), gh_ms, ap_ms)
    if new_ms != gh_ms:
        ghkit.set_milestone(cfg, apply, issue["number"], new_ms)
    desired_ms_tag = f"{MS_PREFIX}{new_ms}" if new_ms else None
    if {desired_ms_tag} - {None} != ms_tags:  # normalize: exactly the one desired tag, remove any others
        for stale in sorted(ms_tags - ({desired_ms_tag} - {None})):
            tag_ops.append(agileplace.op_tag(stale, add=False))
        if desired_ms_tag and desired_ms_tag not in ms_tags:
            tag_ops.append(agileplace.op_tag(desired_ms_tag, add=True))

    if tag_ops:
        queue(card, tag_ops, "tags/milestone")
    if r.gh_add or r.gh_remove or r.ap_add or r.ap_remove or new_ms != gh_ms or tag_ops:
        key = title_key(issue["title"]) or str(issue["number"])
        print(f"meta  [{key}] labels gh+{len(r.gh_add)}/-{len(r.gh_remove)} ap+{len(r.ap_add)}/-{len(r.ap_remove)}"
              f" milestone={new_ms}")
    if apply:
        prev.update({"labels": sorted(r.new_base), "milestone": new_ms})


def sync_dates(cfg, apply, issue, card, pitem, field_meta, issues_state, queue,
               unmatched_kinds: frozenset[str] = frozenset()) -> None:
    """Bidirectional planned dates (AgilePlace-wins). Only a date whose Project field id is known AND
    not flagged as unmatched (see ghproject.unmatched_date_kinds) is synced -- otherwise it is skipped
    entirely (never advanced), so a missing/mismatched field can't be read as a deletion next run.

    Merge-base gating: the GH-side merge base (prev[kind]) only advances when the GitHub value is
    already correct (new == gh_date, nothing to write) or the write is confirmed to have happened
    (ghproject.set_project_date returned True). A silently-skipped write (e.g. item_id/field_id
    missing) must never advance the base -- doing so would mask the mismatch forever, since the next
    run would compare the base against a GitHub value it never actually reached. The AgilePlace-side
    queue write is unaffected by this gating -- it always fires when the AgilePlace value needs to
    change."""
    if not pitem:
        return
    prev = issues_state[issue["url"]]
    key = title_key(issue["title"]) or str(issue["number"])
    item_id = pitem.get("item_id")
    for kind, field_id, ap_field in (("start", field_meta.get("start_field_id"), "plannedStart"),
                                     ("target", field_meta.get("target_field_id"), "plannedFinish")):
        if not field_id or kind in unmatched_kinds:
            continue  # field not resolved, or resolved-but-unmatched -> do not sync or advance this date
        gh_date = pitem.get(kind)
        ap_date = card.get(ap_field)
        new = reconcile_value(prev.get(kind), gh_date, ap_date, prefer="ap")
        gh_write_ok = True
        if new != gh_date:
            gh_write_ok = ghproject.set_project_date(cfg, apply, field_meta["project_id"], item_id, field_id, new)
        if new != ap_date:
            queue(card, [agileplace.op_planned_date(ap_field, new)], f"{ap_field}={new}")
        if new != gh_date or new != ap_date:
            print(f"date  [{key}] {kind} -> {new or 'unset'}")
        if apply and gh_write_ok:
            prev[kind] = new


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync GitHub -> AgilePlace (per-issue cards, lanes, connections, metadata)")
    parser.add_argument("--apply", action="store_true", help="actually write (default: verbose dry run)")
    args = parser.parse_args()

    cfg = env_config()
    online = bool(cfg["token"] and cfg["host"] and cfg["board_id"])
    apply = args.apply and online
    if args.apply and not online:
        print("NOTE: --apply given but AgilePlace is not fully configured (.env) -- forcing dry run")
    elif not online:
        print("DRY RUN: AgilePlace not fully configured -> no writes; printing planned actions")
    elif not apply:
        print("DRY RUN (read-only): pass --apply to create/move/connect cards + sync metadata")

    if cfg["target_repo_path"] is None:
        print("NOTE: TARGET_REPO_PATH not set (.env) -- cannot read GitHub; nothing to sync.")
        return
    target = ghkit.repo_name(cfg)
    if not target:
        print("NOTE: target repo unreachable (no remote or gh not authenticated) -- nothing to sync yet (local only).")
        return

    issues = ghkit.list_issues(cfg)
    open_pr = ghkit.open_pr_issue_numbers(cfg)
    for i in issues:
        i["has_open_pr"] = i["number"] in open_pr
    by_number = {i["number"]: i for i in issues}
    by_key = {title_key(i["title"]) or str(i["number"]): i for i in issues}
    epics = [i for i in issues if "type:epic" in i["labels"]]

    # Projects v2: tri-state. A configured-but-FAILED read must not silently fall back and mass-move lanes.
    if ghproject.configured(cfg):
        pit, raw_items = ghproject.items_and_raw(cfg)
        project_read_failed = pit is None
        project_items = pit or {}
    else:
        project_read_failed, project_items, raw_items = False, {}, []
    project_status = {u: v["status"] for u, v in project_items.items() if v.get("status")}
    field_meta = ghproject.field_meta(cfg) if (ghproject.configured(cfg) and not project_read_failed) else None
    move_lanes = not project_read_failed
    if project_read_failed:
        print("WARN  Projects v2 read FAILED -- leaving lanes untouched this run (Status is the source of truth)")
    elif ghproject.configured(cfg):
        print(f"projects v2: {len(project_status)} items carry Status{'; dates enabled' if field_meta else ''}")
    unmatched_kinds = (
        ghproject.unmatched_date_kinds(raw_items, field_meta, cfg["gh_project"]["start_field"],
                                        cfg["gh_project"]["target_field"])
        if field_meta else frozenset())
    for kind in sorted(unmatched_kinds):
        print(f"WARN  Projects v2 '{kind}' field resolved but no item ever exposed a matching key -- "
              f"skipping {kind} date sync this run")

    lanes = agileplace.board_layout(cfg) if online else []
    cards = agileplace.list_cards(cfg) if online else []
    state = load_state(target, str(cfg["board_id"])) if online else {"issues": {}}
    issues_state = state.setdefault("issues", {})
    smap = cfg.get("stage_lane_map")

    card_by_url, card_by_cid = {}, {}
    for card in cards:
        for u in agileplace.card_external_urls(card):
            card_by_url[u] = card
        cid = agileplace.custom_id_value(card)
        if cid:
            card_by_cid[cid] = card

    def card_for(issue):
        return card_by_url.get(issue["url"]) or card_by_cid.get(title_key(issue["title"]) or "")

    card_ops: dict = {}

    def queue(card, ops, note):
        entry = card_ops.setdefault(str(card["id"]), {"card": card, "ops": [], "notes": []})
        entry["ops"].extend(ops)
        entry["notes"].append(note)

    # 1) ensure a card per issue
    for issue in issues:
        if card_for(issue):
            continue
        key = title_key(issue["title"]) or str(issue["number"])
        stage = resolve_issue_stage(issue, project_status)
        lane, _ = agileplace.resolve_lane_for_stage(lanes, stage, issue.get("milestone") or "", smap)
        created = agileplace.create_card(cfg, apply, issue_card_title(issue), key, issue["url"],
                                         lane["id"] if lane else None)
        if apply and created.get("id"):
            card_by_url[issue["url"]] = created
            if key:
                card_by_cid[key] = created
        print(f"{'made ' if apply else 'DRY  '} card [{key}] stage={stage}"
              f"{' lane=' + agileplace.lane_title(lane) if lane else ''}")

    # 2) per issue: base reset if card changed; lane; metadata; dates
    for issue in issues:
        key = title_key(issue["title"]) or str(issue["number"])
        card = card_for(issue)
        if not card or not card.get("id"):
            continue  # freshly dry-run-created (no id yet), or unresolved
        cid = str(card["id"])
        st = issues_state.setdefault(issue["url"], {})
        if st.get("card_id") is None:
            st["card_id"] = cid                       # fresh / migrated -> keep any existing base
        elif st["card_id"] != cid:
            issues_state[issue["url"]] = {"card_id": cid}  # card was replaced -> reset merge base

        stage = resolve_issue_stage(issue, project_status)
        if move_lanes:
            target_lane, acceptable = agileplace.resolve_lane_for_stage(lanes, stage, issue.get("milestone") or "", smap)
            if target_lane:
                current = str(card.get("laneId") or (card.get("lane") or {}).get("id") or "")
                if current not in {str(i) for i in acceptable}:
                    queue(card, [agileplace.op_lane(target_lane["id"])], f"lane->{agileplace.lane_title(target_lane)}")
                    print(f"{'move ' if apply else 'DRY  '} [{key}] -> '{agileplace.lane_title(target_lane)}' (stage {stage})")
        sync_metadata(cfg, apply, issue, card, cfg["label_sync_ignore"], issues_state, queue)
        if field_meta:
            sync_dates(cfg, apply, issue, card, project_items.get(issue["url"]), field_meta, issues_state, queue,
                       unmatched_kinds)

    # 3) parent/child connections: make each epic card's children EQUAL its sub-issues (add + remove)
    our_card_ids = {str(c["id"]) for c in card_by_url.values() if c.get("id")}
    for epic in epics:
        parent = card_for(epic)
        if not parent or not parent.get("id"):
            continue
        key = title_key(epic["title"]) or str(epic["number"])
        task_urls = [by_number[n]["url"] for n in epic_task_numbers(cfg, epic, by_key) if n in by_number]
        desired = {str(card_by_url[u]["id"]) for u in task_urls if u in card_by_url and card_by_url[u].get("id")}
        existing = agileplace.card_child_ids(parent)
        adds = sorted(desired - existing)
        removes = sorted((existing & our_card_ids) - desired)  # only detach cards WE manage
        if adds:
            agileplace.connect_children(cfg, apply, str(parent["id"]), adds)
            print(f"{'link ' if apply else 'DRY  '} [{key}] +{len(adds)} child card(s)")
        if removes:
            agileplace.disconnect_children(cfg, apply, str(parent["id"]), removes)
            print(f"{'unlink' if apply else 'DRY  '} [{key}] -{len(removes)} child card(s)")

    # 4) dependencies -> card Blocked state (skip entirely unless the whole blocked-by snapshot is complete)
    blocked_by = ghkit.blocked_by_map(cfg, [i["number"] for i in issues]) if online else None
    if online and blocked_by is None:
        print("WARN  blocked-by snapshot incomplete -- leaving ALL card Blocked states untouched this run")
    if blocked_by is not None:
        stage_by_number = {i["number"]: resolve_issue_stage(i, project_status) for i in issues}
        for issue in issues:
            card = card_for(issue)
            if not card or not card.get("id"):
                continue
            reason = blocked_reason(blocked_by.get(issue["number"], []), stage_by_number)
            want = reason is not None
            if want != agileplace.card_is_blocked(card) or (want and reason != agileplace.card_block_reason(card)):
                queue(card, agileplace.ops_blocked(want, reason), f"{'block' if want else 'unblock'}")
                key = title_key(issue["title"]) or str(issue["number"])
                print(f"{'block  ' if want else 'unblock'} [{key}]{': ' + reason if reason else ''}")

    # 5) flush: ONE versioned PATCH per card (optimistic concurrency)
    for entry in card_ops.values():
        agileplace.patch_card(cfg, apply, entry["card"], entry["ops"], "; ".join(entry["notes"]))

    if apply:
        save_state(state)
    else:
        print("--- dry run complete. Re-run with --apply (full .env) to write.")


if __name__ == "__main__":
    main()
