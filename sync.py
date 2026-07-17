#!/usr/bin/env python3
"""Ongoing GitHub -> AgilePlace sync. Agnostic: derives everything from live GitHub + the board, no
manifest/issue-map.

Two jobs each run:
  1. Card movement -- roll each epic up from its tasks' stages and move its card lane-for-lane
     (Backlog -> Ready -> In progress -> In review -> Done).
  2. Bidirectional metadata -- on the EPIC issue: labels (a set) and milestone (a single value) <-> its
     card's tags, each via a 3-way merge against .sync-state.json (removals propagate; milestone is a
     single set-operation, never clear-then-set). Tasks have no card, so metadata sync is epic-level.

DRY RUN by default. State is target-scoped and keyed by the immutable issue URL; a dry run never
advances the merge base; state I/O is atomic and fails closed on corruption. Local-only safe: if the
target repo has no reachable remote, it prints a notice and does nothing.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile

import agileplace
import ghkit
from config import STATE_FILE, env_config
from reconcile import reconcile, reconcile_value
from stages import epic_key_for_task, epic_rollup, issue_stage, title_key

MS_PREFIX = "milestone:"  # reserved card-tag namespace projecting the GitHub milestone


def load_state(target: str, board: str) -> dict:
    """Fail closed: corrupt JSON or a state file for a different target/board refuses to proceed rather
    than silently discarding the merge base (which would resurrect removals)."""
    if not STATE_FILE.exists():
        return {"target": target, "board": board, "epics": {}}
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as err:
        raise SystemExit(f"ERROR: {STATE_FILE} is unreadable/corrupt ({err}). Refusing to run so removals "
                         f"aren't resurrected. Inspect or delete it, then re-run.")
    if state.get("target") != target or str(state.get("board")) != str(board):
        raise SystemExit(f"ERROR: {STATE_FILE} is for target {state.get('target')}/board {state.get('board')}, "
                         f"but configured for {target}/board {board}. Move or delete it, then re-run.")
    state.setdefault("epics", {})
    return state


def save_state(state: dict) -> None:
    """Atomic: write a sibling temp file and os.replace, so an interrupted run can't corrupt state."""
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
    """Native sub-issues first; fall back to the [KEY] title convention. A GraphQL FAILURE (None) warns
    loudly before falling back; a genuine empty result falls back quietly."""
    nums = ghkit.sub_issue_numbers(cfg, epic["number"])
    if nums is None:
        print(f"WARN  [{title_key(epic['title']) or epic['number']}] native sub-issues unavailable -- "
              f"falling back to the [KEY] title convention")
    if nums:
        return nums
    epic_key = title_key(epic["title"])
    return [i["number"] for i in by_key.values()
            if epic_key_for_task(title_key(i["title"]) or "") == epic_key]


def _label_set(labels, ignore: frozenset) -> set[str]:
    """Labels eligible for the tag mirror: minus lifecycle/ignored labels and the reserved milestone
    namespace (which the milestone projection owns)."""
    return {l for l in labels if l not in ignore and not l.startswith(MS_PREFIX)}


def _card_milestone(card: dict) -> str | None:
    for tag in agileplace.card_tags(card):
        if tag.startswith(MS_PREFIX):
            return tag[len(MS_PREFIX):]
    return None


def sync_metadata(cfg: dict, apply: bool, epic: dict, card: dict, ignore: frozenset, epics: dict) -> None:
    url = epic["url"]
    prev = epics.get(url, {})

    # Labels (set-valued) -- filter ignore from BOTH sides and the base before reconciling.
    gh_labels = _label_set(epic["labels"], ignore)
    ap_label_tags = _label_set((t for t in agileplace.card_tags(card) if not t.startswith(MS_PREFIX)), ignore)
    base_labels = _label_set(prev.get("labels", []), ignore)
    r = reconcile(base_labels, gh_labels, ap_label_tags)
    for item in sorted(r.gh_add):
        ghkit.edit_label(cfg, apply, epic["number"], item, add=True)
    for item in sorted(r.gh_remove):
        ghkit.edit_label(cfg, apply, epic["number"], item, add=False)
    for tag in sorted(r.ap_add):
        agileplace.edit_tag(cfg, apply, card, tag, add=True)
    for tag in sorted(r.ap_remove):
        agileplace.edit_tag(cfg, apply, card, tag, add=False)

    # Milestone (single value) -- 3-way merge, applied as one set-operation per side (no clear-then-set).
    gh_ms = epic.get("milestone")
    ap_ms = _card_milestone(card)
    new_ms = reconcile_value(prev.get("milestone"), gh_ms, ap_ms)
    if new_ms != gh_ms:
        ghkit.set_milestone(cfg, apply, epic["number"], new_ms)
    if new_ms != ap_ms:
        if ap_ms:
            agileplace.edit_tag(cfg, apply, card, f"{MS_PREFIX}{ap_ms}", add=False)
        if new_ms:
            agileplace.edit_tag(cfg, apply, card, f"{MS_PREFIX}{new_ms}", add=True)

    changed = r.gh_add or r.gh_remove or r.ap_add or r.ap_remove or new_ms != gh_ms or new_ms != ap_ms
    if changed:
        key = title_key(epic["title"]) or str(epic["number"])
        print(f"meta  [{key}] labels gh+{len(r.gh_add)}/-{len(r.gh_remove)} ap+{len(r.ap_add)}/-{len(r.ap_remove)}"
              f"  milestone={new_ms}")
    if apply:  # advance the merge base only on a real write, or a dry run would swallow changes
        epics[url] = {"labels": sorted(r.new_base), "milestone": new_ms}


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync GitHub issue state -> AgilePlace cards (lanes + bidirectional metadata)")
    parser.add_argument("--apply", action="store_true", help="actually write (default: verbose dry run)")
    args = parser.parse_args()

    cfg = env_config()
    online = bool(cfg["token"] and cfg["host"] and cfg["board_id"])
    apply = args.apply and online
    if args.apply and not online:
        print("NOTE: --apply given but AgilePlace is not fully configured (.env) -- forcing dry run")
    elif not online:
        print("DRY RUN: AgilePlace not fully configured -> no writes; printing planned moves")
    elif not apply:
        print("DRY RUN (read-only): pass --apply to move cards / sync metadata")

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

    lanes = agileplace.board_layout(cfg) if online else []
    cards = agileplace.list_cards(cfg) if online else []
    state = load_state(target, str(cfg["board_id"])) if online else {"epics": {}}

    for epic in epics:
        key = title_key(epic["title"]) or str(epic["number"])
        task_nums = epic_task_numbers(cfg, epic, by_key)
        task_stages = [issue_stage(by_number[n]) for n in task_nums if n in by_number]
        stage = epic_rollup(task_stages) if task_stages else issue_stage(epic)

        card = agileplace.find_card(cards, epic["url"], key) if online else None
        if not card:
            print(f"noop  [{key}] stage={stage} -- {'no card (run init 04 first)' if online else 'board unknown (dry)'}")
            continue

        target_lane = agileplace.resolve_lane_for_stage(lanes, stage, epic.get("milestone") or "")
        if not target_lane:
            print(f"noop  [{key}] no unambiguous lane for stage '{stage}' -- not moving")
        else:
            current = str(card.get("laneId") or (card.get("lane") or {}).get("id") or "")
            if current == str(target_lane["id"]):
                print(f"ok    [{key}] already in '{agileplace.lane_title(target_lane)}' (stage {stage})")
            else:
                agileplace.move_card(cfg, apply, card, target_lane["id"])
                print(f"{'moved' if apply else 'DRY  '} [{key}] -> '{agileplace.lane_title(target_lane)}' (stage {stage})")

        sync_metadata(cfg, apply, epic, card, cfg["label_sync_ignore"], state["epics"])

    if apply:
        save_state(state)
    else:
        print("--- dry run complete. Re-run with --apply (full .env) to write.")


if __name__ == "__main__":
    main()
