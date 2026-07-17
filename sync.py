#!/usr/bin/env python3
"""Ongoing GitHub -> AgilePlace sync (Model 2). Agnostic: derives everything from live GitHub + the
board + the GitHub Projects v2 Status, with no manifest/issue-map.

Per run:
  1. Ensure a card per GitHub issue (epics AND tasks), matched by external-link URL.
  2. Move each card to the lane for its stage -- stage = the issue's Projects v2 Status (source of
     truth) or, as a fallback, the label/PR-derived stage.
  3. Mirror sub-issues as AgilePlace parent/child connections (epic card -> task cards); LeanKit then
     rolls child progress/dates up to the parent natively.
  4. Bidirectional metadata: labels/milestone on each issue <-> its card's tags (3-way merge).

DRY RUN by default. State is target-scoped, issue-URL-keyed, atomic, fail-closed. Connections and card
creation are validated at first live run (not reachable offline). Local-only safe.
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
from stages import (blocked_reason, epic_key_for_task, epic_rollup, issue_stage,
                    normalize_status, title_key)

MS_PREFIX = "milestone:"


def load_state(target: str, board: str) -> dict:
    if not STATE_FILE.exists():
        return {"target": target, "board": board, "issues": {}}
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as err:
        raise SystemExit(f"ERROR: {STATE_FILE} is unreadable/corrupt ({err}). Refusing to run so removals "
                         f"aren't resurrected. Inspect or delete it, then re-run.")
    if state.get("target") != target or str(state.get("board")) != str(board):
        raise SystemExit(f"ERROR: {STATE_FILE} is for target {state.get('target')}/board {state.get('board')}, "
                         f"but configured for {target}/board {board}. Move or delete it, then re-run.")
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
    """Native sub-issues first; fall back to the [KEY] title convention (warn loudly on GraphQL failure)."""
    nums = ghkit.sub_issue_numbers(cfg, epic["number"])
    if nums is None:
        print(f"WARN  [{title_key(epic['title']) or epic['number']}] native sub-issues unavailable -- "
              f"falling back to the [KEY] title convention")
    if nums:
        return nums
    epic_key = title_key(epic["title"])
    return [i["number"] for i in by_key.values()
            if epic_key_for_task(title_key(i["title"]) or "") == epic_key]


def issue_card_title(issue: dict) -> str:
    """The card title: the GitHub issue title without its redundant '[KEY] ' prefix (customId carries it)."""
    t = issue["title"]
    k = title_key(t)
    if k and t.startswith(f"[{k}]"):
        return t[len(f"[{k}]"):].strip() or t
    return t


def resolve_issue_stage(issue: dict, project_status: dict) -> str:
    """Per issue: Projects v2 Status is the source of truth; else label/PR derivation."""
    raw = project_status.get(issue["url"])
    return (normalize_status(raw) if raw else None) or issue_stage(issue)


def _label_set(labels, ignore: frozenset) -> set[str]:
    return {l for l in labels if l not in ignore and not l.startswith(MS_PREFIX)}


def _card_milestone(card: dict) -> str | None:
    for tag in agileplace.card_tags(card):
        if tag.startswith(MS_PREFIX):
            return tag[len(MS_PREFIX):]
    return None


def sync_metadata(cfg: dict, apply: bool, issue: dict, card: dict, ignore: frozenset, issues_state: dict) -> None:
    url = issue["url"]
    prev = issues_state.get(url, {})

    gh_labels = _label_set(issue["labels"], ignore)
    ap_label_tags = _label_set((t for t in agileplace.card_tags(card) if not t.startswith(MS_PREFIX)), ignore)
    base_labels = _label_set(prev.get("labels", []), ignore)
    r = reconcile(base_labels, gh_labels, ap_label_tags)
    for item in sorted(r.gh_add):
        ghkit.edit_label(cfg, apply, issue["number"], item, add=True)
    for item in sorted(r.gh_remove):
        ghkit.edit_label(cfg, apply, issue["number"], item, add=False)
    for tag in sorted(r.ap_add):
        agileplace.edit_tag(cfg, apply, card, tag, add=True)
    for tag in sorted(r.ap_remove):
        agileplace.edit_tag(cfg, apply, card, tag, add=False)

    gh_ms = issue.get("milestone")
    ap_ms = _card_milestone(card)
    new_ms = reconcile_value(prev.get("milestone"), gh_ms, ap_ms)
    if new_ms != gh_ms:
        ghkit.set_milestone(cfg, apply, issue["number"], new_ms)
    if new_ms != ap_ms:
        if ap_ms:
            agileplace.edit_tag(cfg, apply, card, f"{MS_PREFIX}{ap_ms}", add=False)
        if new_ms:
            agileplace.edit_tag(cfg, apply, card, f"{MS_PREFIX}{new_ms}", add=True)

    if r.gh_add or r.gh_remove or r.ap_add or r.ap_remove or new_ms != gh_ms or new_ms != ap_ms:
        key = title_key(issue["title"]) or str(issue["number"])
        print(f"meta  [{key}] labels gh+{len(r.gh_add)}/-{len(r.gh_remove)} ap+{len(r.ap_add)}/-{len(r.ap_remove)}"
              f" milestone={new_ms}")
    if apply:
        issues_state.setdefault(url, {}).update({"labels": sorted(r.new_base), "milestone": new_ms})


def sync_dates(cfg: dict, apply: bool, issue: dict, card: dict, pitem: dict | None,
               field_meta: dict, issues_state: dict) -> None:
    """Bidirectional planned dates (AgilePlace-wins): Project Start/Target date fields <-> card
    plannedStart/plannedFinish, via the 3-way merge against the per-issue base. Skips issues not on the
    Project."""
    if not pitem:
        return
    url = issue["url"]
    prev = issues_state.get(url, {})
    key = title_key(issue["title"]) or str(issue["number"])
    for kind, gh_field_id, ap_field in (("start", field_meta.get("start_field_id"), "plannedStart"),
                                        ("target", field_meta.get("target_field_id"), "plannedFinish")):
        gh_date = pitem.get(kind)
        ap_date = card.get(ap_field)
        new = reconcile_value(prev.get(kind), gh_date, ap_date, prefer="ap")  # dates are AgilePlace-owned
        if new != gh_date:
            ghproject.set_project_date(cfg, apply, field_meta["project_id"], pitem["item_id"], gh_field_id, new)
        if new != ap_date:
            agileplace.set_planned_date(cfg, apply, card, ap_field, new)
        if (new != gh_date or new != ap_date):
            print(f"date  [{key}] {kind} -> {new or 'unset'}")
        if apply:
            issues_state.setdefault(url, {})[kind] = new


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

    project_status = ghproject.issue_status_map(cfg)
    field_meta = ghproject.field_meta(cfg) if ghproject.configured(cfg) else None
    project_dates = ghproject.issue_dates_map(cfg) if (field_meta and online) else {}
    if ghproject.configured(cfg):
        print(f"projects v2: {len(project_status)} items carry Status (source of truth for stage)"
              f"{'; dates enabled' if field_meta else ''}")

    lanes = agileplace.board_layout(cfg) if online else []
    cards = agileplace.list_cards(cfg) if online else []
    state = load_state(target, str(cfg["board_id"])) if online else {"issues": {}}
    issues_state = state.setdefault("issues", {})
    smap = cfg.get("stage_lane_map")

    card_by_url = {}
    for card in cards:
        for u in agileplace.card_external_urls(card):
            card_by_url[u] = card

    # 1) ensure a card per issue
    for issue in issues:
        if issue["url"] in card_by_url:
            continue
        key = title_key(issue["title"]) or str(issue["number"])
        stage = resolve_issue_stage(issue, project_status)
        lane, _ = agileplace.resolve_lane_for_stage(lanes, stage, issue.get("milestone") or "", smap)
        created = agileplace.create_card(cfg, apply, issue_card_title(issue), key, issue["url"],
                                         lane["id"] if lane else None)
        if apply and created.get("id"):
            card_by_url[issue["url"]] = created
        print(f"{'made ' if apply else 'DRY  '} card [{key}] stage={stage}"
              f"{' lane=' + agileplace.lane_title(lane) if lane else ''}")

    # 2) per issue: move to the stage's lane + reconcile metadata
    for issue in issues:
        key = title_key(issue["title"]) or str(issue["number"])
        card = card_by_url.get(issue["url"])
        if not card:
            continue  # freshly dry-run-created (no id yet), or unresolved
        stage = resolve_issue_stage(issue, project_status)
        target_lane, acceptable = agileplace.resolve_lane_for_stage(lanes, stage, issue.get("milestone") or "", smap)
        if target_lane:
            current = str(card.get("laneId") or (card.get("lane") or {}).get("id") or "")
            if current not in {str(i) for i in acceptable}:
                agileplace.move_card(cfg, apply, card, target_lane["id"])
                print(f"{'moved' if apply else 'DRY  '} [{key}] -> '{agileplace.lane_title(target_lane)}' (stage {stage})")
        sync_metadata(cfg, apply, issue, card, cfg["label_sync_ignore"], issues_state)
        if field_meta:
            sync_dates(cfg, apply, issue, card, project_dates.get(issue["url"]), field_meta, issues_state)

    # 3) parent/child connections: each epic card -> its task cards (mirrors sub-issues)
    for epic in epics:
        parent = card_by_url.get(epic["url"])
        if not parent or not parent.get("id"):
            continue
        task_urls = [by_number[n]["url"] for n in epic_task_numbers(cfg, epic, by_key) if n in by_number]
        child_ids = [str(card_by_url[u]["id"]) for u in task_urls
                     if u in card_by_url and card_by_url[u].get("id")]
        missing = [cid for cid in child_ids if cid not in agileplace.card_child_ids(parent)]
        if missing:
            key = title_key(epic["title"]) or str(epic["number"])
            agileplace.connect_children(cfg, apply, str(parent["id"]), missing)
            print(f"{'linked' if apply else 'DRY  '} [{key}] parent -> {len(missing)} child card(s)")

    # 4) dependencies -> card Blocked state (a card is blocked while any GitHub blocker isn't Done)
    blocked_by = ghkit.blocked_by_map(cfg, [i["number"] for i in issues]) if online else None
    if blocked_by is not None:
        stage_by_number = {i["number"]: resolve_issue_stage(i, project_status) for i in issues}
        for issue in issues:
            card = card_by_url.get(issue["url"])
            if not card or not card.get("id"):
                continue
            reason = blocked_reason(blocked_by.get(issue["number"], []), stage_by_number)
            want = reason is not None
            if want != agileplace.card_is_blocked(card) or (want and reason != agileplace.card_block_reason(card)):
                agileplace.set_blocked(cfg, apply, card, want, reason)
                key = title_key(issue["title"]) or str(issue["number"])
                print(f"{'block  ' if want else 'unblock'} [{key}]{': ' + reason if reason else ''}")

    if apply:
        save_state(state)
    else:
        print("--- dry run complete. Re-run with --apply (full .env) to write.")


if __name__ == "__main__":
    main()
