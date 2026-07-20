#!/usr/bin/env python3
"""Ongoing GitHub -> AgilePlace sync (Model 2). Agnostic: derives everything from live GitHub + the
board + the GitHub Projects v2 Status, with no manifest/issue-map.

Per run: ensure a card per active issue (matched by URL, then customId); retire existing cards for
NOT_PLANNED/DUPLICATE issues; move each active card to the lane for its stage (Projects v2 Status =
source of truth, label/PR fallback); mirror sub-issues as parent/child connections; mirror blocked-by
as the card Blocked state; bidirectionally reconcile labels/milestone <-> tags and planned dates <->
Project date fields. Every mutation to one card is batched into a single versioned PATCH (optimistic
concurrency).

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
from stages import (blocked_reason, epic_key_for_task, is_retired_issue, issue_stage,
                    normalize_status, title_key)

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
    if state["schema"] != STATE_SCHEMA:
        raise SystemExit(f"ERROR: {STATE_FILE} uses state schema {state['schema']!r}, but this sync "
                         f"requires schema {STATE_SCHEMA}. Inspect or delete it, then re-run.")
    issues = state.setdefault("issues", {})
    # An entry without a card identity has no trustworthy merge base. Reset it before callers use
    # even its date-history signals; main() binds the live card id before reconciliation.
    state["issues"] = {url: entry if entry.get("card_id") else {}
                       for url, entry in issues.items()}
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


def _epic_task_resolution(cfg: dict, epic: dict, by_key: dict) -> tuple[list[int], bool]:
    """Return ``(numbers, authoritative)`` for an epic's tasks.

    A successful native sub-issue read is authoritative, including an empty result. The title-key
    fallback is only a heuristic, so callers may use it to add connections but must not use it to
    authorize removals.
    """
    nums = ghkit.sub_issue_numbers(cfg, epic["number"])
    if nums is not None:
        return nums, True
    print(f"WARN  [{title_key(epic['title']) or epic['number']}] native sub-issues unavailable -- "
          f"falling back to the [KEY] title convention")
    epic_key = title_key(epic["title"])
    if epic_key is None:
        print(f"WARN  [{epic['number']}] epic has no [KEY] prefix -- fallback matches nothing")
        return [], False
    return ([i["number"] for i in by_key.values()
             if epic_key_for_task(title_key(i["title"]) or "") == epic_key], False)


def epic_task_numbers(cfg: dict, epic: dict, by_key: dict) -> list[int]:
    """Native sub-issues, or [KEY] convention matches when the native read fails."""
    numbers, _ = _epic_task_resolution(cfg, epic, by_key)
    return numbers


def _child_connection_changes(desired: set[str], existing: set[str], managed: set[str],
                              *, authoritative: bool) -> tuple[list[str], list[str]]:
    """Return child-card additions and safe removals for one epic.

    Additions are safe from either native or title-fallback discovery. Removals require an
    authoritative native snapshot and remain limited to cards managed by this sync.
    """
    adds = sorted(desired - existing)
    removes = sorted((existing & managed) - desired) if authoritative else []
    return adds, removes


def issue_card_title(issue: dict) -> str:
    t = issue["title"]
    k = title_key(t)
    if k and t.startswith(f"[{k}]"):
        return t[len(f"[{k}]"):].strip() or t
    return t


def issue_custom_id(issue: dict) -> str:
    """The customId written to and read from AgilePlace for one GitHub issue."""
    return title_key(issue["title"]) or str(issue["number"])


def _same_card(left: dict | None, right: dict | None) -> bool:
    if not left or not right:
        return False
    if left is right:
        return True
    left_id = str(left.get("id") or "")
    right_id = str(right.get("id") or "")
    return bool(left_id) and left_id == right_id


def _matching_card(issue: dict, card_by_url: dict, card_by_cid: dict) -> dict | None:
    """Match by URL first, then customId, refusing an ambiguous cross-card match."""
    custom_id = issue_custom_id(issue)
    url_match = card_by_url.get(issue["url"])
    custom_id_match = card_by_cid.get(custom_id)
    if url_match and custom_id_match:
        url_card_id = str(url_match.get("id") or "")
        custom_id_card_id = str(custom_id_match.get("id") or "")
        if not _same_card(url_match, custom_id_match):
            raise SystemExit(
                f"ERROR: GitHub issue {issue['url']} matches AgilePlace card {url_card_id or '<unknown>'} "
                f"by URL but card {custom_id_card_id or '<unknown>'} by customId {custom_id!r}. "
                "Refusing to reconcile an ambiguous card match."
            )
    return url_match or custom_id_match


def _reconciled_custom_id_index(issues: list[dict], card_by_url: dict,
                                card_by_cid: dict) -> tuple[dict, frozenset[str]]:
    """Return the URL-corrected customId index and IDs released by pending rename repairs."""
    reconciled = dict(card_by_cid)
    released = set()
    # Validate the immutable board snapshot first: issue order must never erase a disagreement.
    for issue in issues:
        if card_by_url.get(issue["url"]):
            _matching_card(issue, card_by_url, card_by_cid)
    for issue in issues:
        url_match = card_by_url.get(issue["url"])
        if not url_match:
            continue
        # Catch two URL-owned issues planning the same previously-unclaimed customId.
        _matching_card(issue, card_by_url, reconciled)
        desired_custom_id = issue_custom_id(issue)
        current_custom_id = agileplace.custom_id_value(url_match)
        if current_custom_id and _same_card(reconciled.get(current_custom_id), url_match):
            del reconciled[current_custom_id]
            if current_custom_id != desired_custom_id:
                released.add(current_custom_id)
        reconciled[desired_custom_id] = url_match
    return reconciled, frozenset(released)


def explicit_stage_status(issue: dict, project_status: dict) -> str | None:
    """The canonical stage this issue's Projects v2 Status maps to, or None when there's no Status set
    OR it's a custom option name that doesn't match one of our five stages -- i.e. exactly the case
    where resolve_issue_stage() has to fall back to label/PR derivation instead of a human's explicit
    call. Callers must use this (not raw truthiness of project_status[url]) to decide whether an
    issue's stage actually came from an explicit Status -- a truthy-but-unrecognized raw value (e.g.
    a custom 'Triage' option) is NOT an explicit canonical call."""
    return normalize_status(project_status.get(issue["url"]))


def resolve_issue_stage(issue: dict, project_status: dict) -> str:
    if is_retired_issue(issue):
        return "Done"
    return explicit_stage_status(issue, project_status) or issue_stage(issue)


def _protect_open_pr_stage(stage: str, current_lane_id: str, lanes: list, milestone: str,
                            stage_map: dict | None, *, open_pr_read_failed: bool,
                            has_explicit_status: bool, issue_closed: bool = False) -> str:
    """Guard against demoting a card OUT of 'In review' purely because this run's open-PR read
    failed (ghkit.open_pr_issue_numbers returned None): a transient GitHub API hiccup must never
    silently walk a card backward on a vanished signal. Pure, no I/O, never mutates its arguments.

    Freezes the stage at 'In review' only when ALL of: the read failed, there's no explicit Projects
    v2 Status for this issue (a human's explicit call always wins over the guard), the issue isn't
    closed (a CLOSED issue's 'Done' comes from the authoritative state signal, not the lost open-PR
    signal -- freezing it would strand a finished card in review), the computed stage isn't already
    'In review', and the card's current lane is already one of the acceptable lanes for 'In review'
    (i.e. the card is already sitting in review -- this never PROMOTES a card into review, it only
    freezes one already there). Every other case passes `stage` through unchanged."""
    if not open_pr_read_failed or has_explicit_status or issue_closed or stage == "In review":
        return stage
    _, acceptable = agileplace.resolve_lane_for_stage(lanes, "In review", milestone, stage_map, quiet=True)
    if str(current_lane_id) in {str(i) for i in acceptable}:
        return "In review"
    return stage


def _retire_card(issue: dict, card: dict, lanes: list, stage_map: dict | None,
                 apply: bool, queue) -> None:
    """Move one URL-matched retired issue card to Done and clear its stale blocked state."""
    key = issue_custom_id(issue)
    reason = issue["state_reason"]
    if not card.get("id"):
        print(f"WARN  [{key}] cannot retire card without an id ({reason})")
        return

    current = str(card.get("laneId") or (card.get("lane") or {}).get("id") or "")
    target, acceptable = agileplace.resolve_lane_for_stage(
        lanes, "Done", issue.get("milestone") or "", stage_map)
    ops, actions = [], []
    if target and current not in {str(lane_id) for lane_id in acceptable}:
        ops.append(agileplace.op_lane(target["id"]))
        actions.append(f"-> '{agileplace.lane_title(target)}'")
    elif target:
        actions.append(f"already '{agileplace.lane_title(target)}'")
    else:
        print(f"WARN  [{key}] cannot retire to Done: no unambiguous Done lane ({reason})")
    if agileplace.card_is_blocked(card) or agileplace.card_block_reason(card):
        ops.extend(agileplace.ops_blocked(False, None))
        actions.append("clear blocked")
    if ops:
        queue(card, ops, f"retire:{reason}")
    action = "; ".join(actions) or "no card changes available"
    print(f"{'retire' if apply else 'DRY   retire'} [{key}] {action} ({reason})")


def _label_set(labels, ignore: frozenset) -> set[str]:
    return {l for l in labels if l not in ignore and not l.startswith(MS_PREFIX)}


def _filter_gh_safe_labels(names: frozenset[str], *, key: str, action: str) -> frozenset[str]:
    """Subset of names safe to pass to gh's --add-label/--remove-label; prints one WARN per rejected
    name (comma, or a '"' anywhere -- gh CSV-splits the flag value) naming the offender and side."""
    safe = frozenset(n for n in names if ghkit.is_gh_label_safe(n))
    for bad in sorted(names - safe):
        print(f"WARN  [{key}] label {bad!r} contains a comma or a double quote -- gh CSV-splits "
              f"--add-label/--remove-label values; skipping {action} on GitHub")
    return safe


def _card_milestones(card: dict, base: str | None, gh: str | None) -> tuple[str | None, set[str]]:
    """(selected current milestone value, all raw milestone: tags incl. empty-suffix ones for cleanup).

    Selection over the card's non-empty milestone: suffixes is by PROVENANCE, not sort order:
      - zero suffixes       -> None
      - `base` among them    -> base (nothing changed AP-side this pass; a coexisting extra tag is
                                cleanup fodder, never a same-pass override -- closes the
                                milestone:0.0.0 downgrade abuse vector from issue #7's 'Why')
      - else `gh` among them -> gh (same rationale, GitHub-side anchor)
      - else                 -> sorted(suffixes)[0] -- tie-break used ONLY among tags matching
                                 NEITHER anchor, i.e. genuinely new/fully-unanchored AP-side values;
                                 never used to arbitrate an anchored tag against an unanchored one.
    Pure function of its three inputs; no I/O. Determinism is a property of the base/gh-anchor rule
    (and, only in the fully-unanchored case, the sort tie-break) -- NOT, as the prior docstring
    claimed, a virtue of sorting itself; sorting alone was the actual bug (issue #7).
    """
    tags = {t for t in agileplace.card_tags(card) if t.startswith(MS_PREFIX)}
    suffixes = {t[len(MS_PREFIX):] for t in tags if t[len(MS_PREFIX):]}
    if not suffixes:
        return None, tags
    if base is not None and base in suffixes:
        return base, tags
    if gh is not None and gh in suffixes:
        return gh, tags
    return sorted(suffixes)[0], tags


def _stale_milestone_tags(ms_tags: set[str], old_base: str | None, new_ms: str | None) -> frozenset[str]:
    """Subset of ms_tags (the 2nd _card_milestones return) safe to remove via ops_tag_remove this
    pass. Postcondition: result <= ms_tags always -- never proposes removing a tag that was never on
    the card. Included:
      - new_ms is None (reconcile resolved the milestone to UNSET this pass -- GitHub cleared it, or
        it was never set): EVERY milestone: tag is stale. With no current milestone there is nothing
        legitimate for any tag to represent, and leaving one behind lets it resurrect the cleared
        value on a later pass -- once the base is persisted as None the leftover looks like a fresh,
        unanchored AgilePlace value and gets pushed straight back onto GitHub, silently undoing the
        user's deletion (the cross-run resurrection Codex flagged). A tag cannot be a genuine pending
        upgrade here: if it were, reconcile_value would have resolved new_ms TO that value, not None.
      - otherwise (new_ms is a real value), the conservative set:
          - every empty-suffix tag ("milestone:" alone) -- always stale, carries no value
          - f"{MS_PREFIX}{old_base}" iff ALL THREE hold: old_base is not None, old_base != new_ms (the
            base has been confirmed superseded THIS pass), AND that literal tag is a member of ms_tags
            (it may legitimately not be, e.g. the base was never re-tagged onto this card)
        and deliberately EXCLUDES any other non-empty-suffix tag (one matching neither the old base
        nor the new value): while a real milestone still stands it cannot be told apart from a pending,
        ambiguous human edit by value alone, so it is preserved rather than destroyed -- risking the
        deletion of a genuine, not-yet-reconciled upgrade (issue #7).
    """
    if new_ms is None:
        return frozenset(ms_tags)
    stale = {t for t in ms_tags if t == MS_PREFIX}
    old_tag = f"{MS_PREFIX}{old_base}" if old_base is not None else None
    if old_tag is not None and old_base != new_ms and old_tag in ms_tags:
        stale.add(old_tag)
    return frozenset(stale)


def sync_metadata(cfg, apply, issue, card, ignore, issues_state, queue) -> None:
    url = issue["url"]
    prev = issues_state[url]

    gh_labels = _label_set(issue["labels"], ignore)
    ap_label_tags = _label_set((t for t in agileplace.card_tags(card) if not t.startswith(MS_PREFIX)), ignore)
    base_labels = _label_set(prev.get("labels", []), ignore)
    r = reconcile(base_labels, gh_labels, ap_label_tags)
    key = issue_custom_id(issue)

    gh_add_safe = _filter_gh_safe_labels(r.gh_add, key=key, action="add")
    gh_remove_safe = _filter_gh_safe_labels(r.gh_remove, key=key, action="remove")
    for item in sorted(gh_add_safe):
        ghkit.edit_label(cfg, apply, issue["number"], item, add=True)
    for item in sorted(gh_remove_safe):
        ghkit.edit_label(cfg, apply, issue["number"], item, add=False)
    # A name skipped from an add was never actually written to GitHub -> pull it back out of the new
    # base; a name skipped from a remove is still actually on GitHub -> keep it in the new base. The
    # two terms never overlap: gh_add/gh_remove are disjoint set-differences of the same final/gh_now
    # pair (reconcile.py), so a name can't be skipped from both an add and a remove in the same run.
    new_base = (r.new_base - (r.gh_add - gh_add_safe)) | (r.gh_remove - gh_remove_safe)
    tags_to_remove: set[str] = set(r.ap_remove)
    tag_ops = [agileplace.op_tag(t) for t in sorted(r.ap_add)]

    gh_ms = issue.get("milestone")
    ap_ms, ms_tags = _card_milestones(card, prev.get("milestone"), gh_ms)
    new_ms = reconcile_value(prev.get("milestone"), gh_ms, ap_ms)
    if new_ms != gh_ms:
        ghkit.set_milestone(cfg, apply, issue["number"], new_ms)
    desired_ms_tag = f"{MS_PREFIX}{new_ms}" if new_ms else None
    stale = _stale_milestone_tags(ms_tags, prev.get("milestone"), new_ms) - ({desired_ms_tag} - {None})
    if stale or (desired_ms_tag and desired_ms_tag not in ms_tags):
        tags_to_remove |= stale
        if desired_ms_tag and desired_ms_tag not in ms_tags:
            tag_ops.append(agileplace.op_tag(desired_ms_tag))

    tag_ops += agileplace.ops_tag_remove(card.get("tags") or [], tags_to_remove)
    if tag_ops:
        queue(card, tag_ops, "tags/milestone")
    if gh_add_safe or gh_remove_safe or r.ap_add or r.ap_remove or new_ms != gh_ms or tag_ops:
        print(f"meta  [{key}] labels gh+{len(gh_add_safe)}/-{len(gh_remove_safe)}"
              f" ap+{len(r.ap_add)}/-{len(r.ap_remove)} milestone={new_ms}")
    if apply:
        prev.update({"labels": sorted(new_base), "milestone": new_ms})


def sync_dates(cfg, apply, issue, card, pitem, field_meta, issues_state, queue) -> None:
    """Bidirectional planned dates (AgilePlace-wins) from an authoritative field-ID snapshot.

    Only a date whose Project field id is known is synced. main() skips this function entirely when
    the GraphQL date snapshot failed, so a read failure cannot be mistaken for a project-wide clear.

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
    key = issue_custom_id(issue)
    item_id = pitem.get("item_id")
    for kind, field_id, ap_field in (("start", field_meta.get("start_field_id"), "plannedStart"),
                                     ("target", field_meta.get("target_field_id"), "plannedFinish")):
        if not field_id:
            continue
        gh_date = pitem.get(kind)
        ap_date = card.get(ap_field)
        new = reconcile_value(prev.get(kind), gh_date, ap_date, prefer="ap")
        gh_write_ok = True
        if new != gh_date:
            gh_write_ok = ghproject.set_project_date(cfg, apply, field_meta["project_id"], item_id,
                                                     field_id, new, field_meta.get("host"))
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
    active_issues = [issue for issue in issues if not is_retired_issue(issue)]
    retired_issues = [issue for issue in issues if is_retired_issue(issue)]
    open_pr = ghkit.open_pr_issue_numbers(cfg)
    open_pr_read_failed = open_pr is None
    if open_pr_read_failed:
        print("WARN  open-PR read FAILED -- leaving PR-derived 'In review' stages untouched this run")
    else:
        for i in active_issues:
            i["has_open_pr"] = i["number"] in open_pr
    by_number = {i["number"]: i for i in active_issues}
    by_key = {issue_custom_id(i): i for i in active_issues}

    state = load_state(target, str(cfg["board_id"])) if online else {"issues": {}}
    issues_state = state.setdefault("issues", {})

    # Projects v2: tri-state. A configured-but-FAILED read must not silently fall back and mass-move
    # lanes -- and neither may a technically-successful read that yields zero recognized statuses
    # despite the Project actually having issue-linked items (misspelled GH_PROJECT_STATUS_FIELD, or a
    # gh output-shape change): that is the same mass-move reached through a different door (issue #5).
    if ghproject.configured(cfg):
        pit = ghproject.items(cfg)
        call_failed = pit is None
        project_items = pit or {}
    else:
        call_failed, project_items = False, {}
    project_status = {u: v["status"] for u, v in project_items.items() if v.get("status")}
    zero_status_despite_items = (ghproject.configured(cfg) and not call_failed
                                  and bool(project_items) and not project_status)
    project_read_failed = call_failed or zero_status_despite_items
    field_meta = ghproject.field_meta(cfg) if (ghproject.configured(cfg) and not project_read_failed) else None
    if field_meta and not (field_meta.get("start_field_id") or field_meta.get("target_field_id")):
        field_meta = None
    date_read_failed = False
    if field_meta:
        dated_items = ghproject.hydrate_item_dates(cfg, project_items, field_meta)
        if dated_items is None:
            date_read_failed = True
            field_meta = None
        else:
            project_items = dated_items
    move_lanes = not project_read_failed
    if zero_status_despite_items:
        print(f"WARN  Projects v2 has {len(project_items)} issue item(s) but none carry a recognized "
              f"'{cfg['gh_project']['status_field']}' Status -- check GH_PROJECT_STATUS_FIELD; "
              f"leaving active-issue lanes untouched this run")
    elif project_read_failed:
        print("WARN  Projects v2 read FAILED -- leaving active-issue lanes untouched this run "
              "(Status is the source of truth)")
    elif ghproject.configured(cfg):
        print(f"projects v2: {len(project_status)} items carry Status{'; dates enabled' if field_meta else ''}")
    if date_read_failed:
        print("WARN  Projects v2 date field-value read FAILED -- skipping all date sync this run")

    lanes = agileplace.board_layout(cfg) if online else []
    cards = agileplace.list_cards(cfg) if online else []
    smap = cfg.get("stage_lane_map")

    all_card_by_url, all_card_by_cid = {}, {}
    for card in cards:
        for u in agileplace.card_external_urls(card):
            all_card_by_url[u] = card
        cid = agileplace.custom_id_value(card)
        if cid:
            all_card_by_cid[cid] = card

    retired_card_by_url = {
        issue["url"]: all_card_by_url[issue["url"]]
        for issue in retired_issues if issue["url"] in all_card_by_url
    }
    retired_cards = tuple(retired_card_by_url.values())

    def reserved_for_retirement(card):
        return any(card is retired or _same_card(card, retired) for retired in retired_cards)

    retired_card_by_cid = {
        agileplace.custom_id_value(card): card
        for card in retired_cards if agileplace.custom_id_value(card)
    }
    card_by_url = {
        url: card for url, card in all_card_by_url.items() if not reserved_for_retirement(card)
    }
    card_by_cid = {
        cid: card for cid, card in all_card_by_cid.items() if not reserved_for_retirement(card)
    }

    def retirement_reservation(issue):
        url_card = all_card_by_url.get(issue["url"])
        if url_card and reserved_for_retirement(url_card):
            return "external-link URL", url_card
        custom_id_card = retired_card_by_cid.get(issue_custom_id(issue))
        if custom_id_card:
            return "customId", custom_id_card
        return None

    active_reservations = {
        issue["url"]: reservation
        for issue in active_issues if (reservation := retirement_reservation(issue))
    }
    syncable_issues = [issue for issue in active_issues if issue["url"] not in active_reservations]
    for issue in active_issues:
        reservation = active_reservations.get(issue["url"])
        if reservation:
            kind, card = reservation
            print(f"WARN  deferring active card [{issue_custom_id(issue)}]: {kind} is held by "
                  f"retired card {card.get('id') or '<unknown>'}")

    card_by_cid, pending_custom_id_releases = _reconciled_custom_id_index(
        syncable_issues, card_by_url, card_by_cid)
    epics = [i for i in syncable_issues if "type:epic" in i["labels"]]

    def card_for(issue):
        return _matching_card(issue, card_by_url, card_by_cid)

    card_ops: dict = {}

    def queue(card, ops, note):
        entry = card_ops.setdefault(str(card["id"]), {"card": card, "ops": [], "notes": []})
        entry["ops"].extend(ops)
        entry["notes"].append(note)

    # Retired issues are dependency facts, not active work. Existing cards are matched by their
    # authoritative GitHub URL only: a customId may have been reused and must never make us retire
    # another issue's card. Retirement is independent of Projects/open-PR read health because the
    # CLOSED reason itself is the authoritative signal.
    for issue in retired_issues:
        card = retired_card_by_url.get(issue["url"])
        if card:
            _retire_card(issue, card, lanes, smap, apply, queue)
        elif all_card_by_cid.get(issue_custom_id(issue)):
            print(f"WARN  [{issue_custom_id(issue)}] retired issue has only a customId card match; "
                  "refusing to retire without the GitHub external-link URL")

    # 1) ensure a card per active issue
    for issue in syncable_issues:
        if card_for(issue):
            continue
        key = issue_custom_id(issue)
        if key in pending_custom_id_releases:
            print(f"WARN  deferring card [{key}] until the renamed customId is released by a prior run")
            continue
        stage = resolve_issue_stage(issue, project_status)  # informational only when the read failed
        lane = None
        if not project_read_failed:
            lane, _ = agileplace.resolve_lane_for_stage(lanes, stage, issue.get("milestone") or "", smap)
        created = agileplace.create_card(cfg, apply, issue_card_title(issue), key, issue["url"],
                                         lane["id"] if lane else None)
        if apply and created.get("id"):
            card_by_url[issue["url"]] = created
            if key:
                card_by_cid[key] = created
        lane_note = (f" lane={agileplace.lane_title(lane)}" if lane
                     else " lane=deferred (Projects v2 read failed)" if project_read_failed else "")
        print(f"{'made ' if apply else 'DRY  '} card [{key}] stage={stage}{lane_note}")

    # 2) per active issue: base reset if card changed; lane; metadata; dates
    for issue in syncable_issues:
        key = issue_custom_id(issue)
        card = card_for(issue)
        if not card or not card.get("id"):
            continue  # freshly dry-run-created (no id yet), or unresolved
        cid = str(card["id"])
        st = issues_state.setdefault(issue["url"], {})
        if st.get("card_id") != cid:
            issues_state[issue["url"]] = {"card_id": cid}  # fresh/migrated/replaced -> reset merge base
        if agileplace.custom_id_value(card) != key:
            queue(card, [agileplace.op_custom_id(key)], f"customId->{key}")
            print(f"{'sync ' if apply else 'DRY  '} [{key}] customId")

        stage = resolve_issue_stage(issue, project_status)
        if move_lanes:
            current = str(card.get("laneId") or (card.get("lane") or {}).get("id") or "")
            has_explicit_status = explicit_stage_status(issue, project_status) is not None
            lane_stage = _protect_open_pr_stage(stage, current, lanes, issue.get("milestone") or "", smap,
                                                 open_pr_read_failed=open_pr_read_failed,
                                                 has_explicit_status=has_explicit_status,
                                                 issue_closed=str(issue.get("state", "")).upper() == "CLOSED")
            target_lane, acceptable = agileplace.resolve_lane_for_stage(lanes, lane_stage, issue.get("milestone") or "", smap)
            if target_lane:
                if current not in {str(i) for i in acceptable}:
                    queue(card, [agileplace.op_lane(target_lane["id"])], f"lane->{agileplace.lane_title(target_lane)}")
                    print(f"{'move ' if apply else 'DRY  '} [{key}] -> '{agileplace.lane_title(target_lane)}' (stage {lane_stage})")
        sync_metadata(cfg, apply, issue, card, cfg["label_sync_ignore"], issues_state, queue)
        if field_meta:
            sync_dates(cfg, apply, issue, card, project_items.get(issue["url"]), field_meta, issues_state, queue)

    # 3) parent/child connections: authoritative native reads reconcile exactly; title-key fallback
    # is add-only because a heuristic must never authorize destructive reconciliation.
    our_card_ids = {str(c["id"]) for c in all_card_by_url.values() if c.get("id")}
    for epic in epics:
        parent = card_for(epic)
        if not parent or not parent.get("id"):
            continue
        key = issue_custom_id(epic)
        task_numbers, authoritative = _epic_task_resolution(cfg, epic, by_key)
        task_urls = [by_number[n]["url"] for n in task_numbers if n in by_number]
        desired = {str(card_by_url[u]["id"]) for u in task_urls if u in card_by_url and card_by_url[u].get("id")}
        existing = agileplace.card_child_ids(parent)
        adds, removes = _child_connection_changes(
            desired, existing, our_card_ids, authoritative=authoritative)
        if adds:
            agileplace.connect_children(cfg, apply, str(parent["id"]), adds)
            print(f"{'link ' if apply else 'DRY  '} [{key}] +{len(adds)} child card(s)")
        if removes:
            agileplace.disconnect_children(cfg, apply, str(parent["id"]), removes)
            print(f"{'unlink' if apply else 'DRY  '} [{key}] -{len(removes)} child card(s)")

    # 4) dependencies -> card Blocked state (skip entirely unless the whole blocked-by snapshot is complete)
    blocked_by = (ghkit.blocked_by_map(cfg, [i["number"] for i in syncable_issues])
                  if online and syncable_issues else {} if online else None)
    if online and blocked_by is None:
        print("WARN  blocked-by snapshot incomplete -- leaving ALL card Blocked states untouched this run")
    if blocked_by is not None:
        stage_by_number = {i["number"]: resolve_issue_stage(i, project_status) for i in issues}
        for issue in syncable_issues:
            card = card_for(issue)
            if not card or not card.get("id"):
                continue
            reason = blocked_reason(blocked_by.get(issue["number"], []), stage_by_number)
            want = reason is not None
            if want != agileplace.card_is_blocked(card) or (want and reason != agileplace.card_block_reason(card)):
                queue(card, agileplace.ops_blocked(want, reason), f"{'block' if want else 'unblock'}")
                key = issue_custom_id(issue)
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
