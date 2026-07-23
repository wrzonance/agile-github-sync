#!/usr/bin/env python3
"""Ongoing GitHub -> AgilePlace sync (Model 2). Agnostic: derives everything from live GitHub + the
board + the GitHub Projects v2 Status, with no manifest/issue-map.

Per run: ensure a card per active issue (matched by URL, then customId); retire existing cards for
NOT_PLANNED/DUPLICATE issues; move each active card to the lane for its stage (Projects v2 Status =
source of truth, label/PR fallback); mirror sub-issues as parent/child connections; mirror blocked-by
as native card dependencies (the Blocked flag is human-owned -- never written by the sync);
bidirectionally reconcile labels/milestone <-> tags and planned dates <-> Project date fields. Every mutation to one card is batched into a single versioned PATCH (optimistic
concurrency).

DRY RUN by default. State is target-scoped, issue-URL-keyed, records each issue's card id (so a
re-created card resets its merge base instead of wiping GitHub), atomic, and fail-closed.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections.abc import Mapping

import agileplace
import card_types
import ghkit
import ghproject
import intake
import vetting_latch
from card_coherence import (contested_cards, fence_run_indices, filter_poisoned_edges,
                            laneid_op_value, lane_conflict, poisoned_card_ids, same_card)
from config import STATE_FILE, env_config
from description_sync import sync_description
from metadata_sync import sync_dates, sync_metadata
from stages import (epic_key_for_task, is_retired_issue, issue_custom_id,
                    issue_stage, normalize_status, title_key)

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
    authoritative native GitHub and AgilePlace snapshots and remain limited to cards managed by
    this sync.
    """
    adds = sorted(desired - existing)
    removes = sorted((existing & managed) - desired) if authoritative else []
    return adds, removes


def sync_child_connections(cfg: dict, apply: bool, epics: list[dict], card_for, by_key: dict,
                           by_number: dict, poisoned: frozenset[str],
                           managed_card_ids: set[str]) -> None:
    """Mirror GitHub sub-issues as native AgilePlace parent/child card connections (step 3):
    authoritative native reads reconcile exactly (additions and removals); the [KEY] title-key
    fallback is add-only, because a heuristic must never authorize destructive reconciliation.

    Issue #75: a parent card poisoned by Layer 2's lane-conflict check (queue()) already has its
    flush PATCH skipped -- child connections must stay consistent with that, never writing native
    connect/disconnect calls against a card whose own state this run refused to persist. A poisoned
    CHILD card must never be connected/disconnected either -- filter the already-computed
    adds/removes (see filter_poisoned_edges) rather than pre-filtering `desired`, so a
    still-linked-but-poisoned child never gets misread as a stale edge and queued for removal."""
    for epic in epics:
        parent = card_for(epic)
        if not parent or not parent.get("id"):
            continue
        key = issue_custom_id(epic)
        if str(parent["id"]) in poisoned:
            print(f"WARN  [{key}] skipping child connections -- parent card {parent['id']} is poisoned")
            continue
        task_numbers, authoritative = _epic_task_resolution(cfg, epic, by_key)
        task_issues = [by_number[number] for number in task_numbers if number in by_number]
        desired = {str(card["id"])
                   for issue in task_issues
                   if (card := card_for(issue)) and card.get("id")}
        # A plan-only parent has not reached AgilePlace yet, so its authoritative server-side child
        # set is empty. Never send its synthetic identity across a real read boundary.
        existing_snapshot = (
            frozenset()
            if parent.get("_planOnly")
            else agileplace.card_child_ids(cfg, str(parent["id"]))
        )
        existing = set(existing_snapshot or ())
        adds, removes = _child_connection_changes(
            desired, existing, managed_card_ids,
            authoritative=authoritative and existing_snapshot is not None)
        adds, removes, dropped = filter_poisoned_edges(adds, removes, poisoned)
        if dropped:
            print(f"WARN  [{key}] dropping poisoned child card id(s) from connect/disconnect")
        if adds:
            agileplace.connect_children(cfg, apply, str(parent["id"]), adds)
            print(f"{'link ' if apply else 'DRY  '} [{key}] +{len(adds)} child card(s)")
        if removes:
            agileplace.disconnect_children(cfg, apply, str(parent["id"]), removes)
            print(f"{'unlink' if apply else 'DRY  '} [{key}] -{len(removes)} child card(s)")


def _dependency_changes(desired: set[str], current: set[str],
                        managed: set[str]) -> tuple[list[str], list[str]]:
    """Dependency additions and safe removals for one card. Removals stay limited to
    dependencies on cards managed by this sync -- a human-made dependency involving any
    other card is invisible to reconciliation, in both directions."""
    return sorted(desired - current), sorted((current & managed) - desired)


def _blocker_cards(by_number: dict, card_for, retired_issues: list,
                   retired_card_by_url: dict) -> dict:
    """Issue number -> card for every issue that can act as a blocker -- retired
    (NOT_PLANNED/DUPLICATE) issues included, via their URL-owned cards. A retired Done
    blocker's edge is structural: resolving blockers through active issues only would drop
    it from the desired set while its card stayed in the managed set, deleting the valid
    native dependency as stale on every run."""
    cards = {number: card for number, issue in by_number.items()
             if (card := card_for(issue)) and card.get("id")}
    for issue in retired_issues:
        card = retired_card_by_url.get(issue["url"])
        if card and card.get("id"):
            cards.setdefault(issue["number"], card)
    return cards


def _managed_card_ids(syncable_issues: list[dict], card_for, retired_card_by_url: dict[str, dict]) -> set[str]:
    """Every card id this sync manages: active-issue cards resolved by card_for (URL match OR
    customId fallback), plus every URL-matched retired card. Broader than
    _removal_authority_card_ids -- a customId-only match still confers full authority here, so
    this set drives additions and the child-connection removal path. Pure, read-only, never
    raises. This is the single source of truth for 'managed'; main() calls it directly so tests
    and production never drift onto separate copies of the formula."""
    return (
        {str(card["id"]) for issue in syncable_issues
         if (card := card_for(issue)) and card.get("id")}
        | {str(card["id"]) for card in retired_card_by_url.values() if card.get("id")}
    )


def _removal_authority_card_ids(syncable_issues: list[dict], card_by_url: dict[str, dict],
                                retired_card_by_url: dict[str, dict]) -> set[str]:
    """Strong-identity card ids only: cards an active issue matched via its own external-link
    URL, plus URL-matched retired cards. A card an active issue reached only through
    _matching_card's customId fallback -- including a retired card whose external link was
    manually removed and got silently adopted through a customId collision (issue #60) --
    confers no removal authority over that card's dependencies. Additions are unaffected;
    only REMOVAL decisions (sync_dependencies -> _dependency_changes) consume this. Pure,
    read-only, never raises. Always a subset of _managed_card_ids's result."""
    return (
        {str(card["id"]) for issue in syncable_issues
         if (card := card_by_url.get(issue["url"])) and card.get("id")}
        | {str(card["id"]) for card in retired_card_by_url.values() if card.get("id")}
    )


def sync_dependencies(cfg: dict, apply: bool, syncable_issues: list, blocked_by: dict,
                      blocker_card_by_number: dict, card_for,
                      removal_authority_card_ids: set[str],
                      poisoned: frozenset[str]) -> None:
    """Mirror GitHub blocked-by edges as native AgilePlace dependencies (issue #57).

    EVERY edge is mirrored, including edges whose blocker is Done -- the edge is structural, and
    AgilePlace's own dependencyStats display satisfaction. (The Blocked flag deliberately differs:
    it reflects incomplete blockers only.) GitHub is authoritative only between two sync-managed
    cards. A failed or unrecognized dependency read skips the card entirely: duplicate-create
    behavior is unconfirmed live, so nothing is ever re-created against unknown state.

    Dependency REMOVALS additionally require the target card to carry strong (URL-matched)
    identity -- a customId-only match never confers removal authority (issue #60).

    Issue #75: a card poisoned by Layer 2's lane-conflict check (queue()) never gets its own
    dependency edges touched, and a poisoned BLOCKER card is filtered out of the already-computed
    adds/removes (never pre-filtered out of `desired` -- doing so would make a still-linked-but-
    poisoned blocker look like a stale edge and get queued into `removes`)."""
    for issue in syncable_issues:
        card = card_for(issue)
        if not card or not card.get("id"):
            continue
        cid = str(card["id"])
        key = issue_custom_id(issue)
        if cid in poisoned:
            print(f"WARN  [{key}] skipping dependency sync -- card {cid} is poisoned")
            continue
        desired = {str(blocker_card_by_number[number]["id"])
                   for number in blocked_by.get(issue["number"], [])
                   if number in blocker_card_by_number}
        if card.get("_planOnly"):
            current = set()  # a fresh card has no server-side dependencies; never read a plan-only id
        else:
            entries = agileplace.card_dependencies(cfg, cid)
            if entries is None:
                print(f"WARN  [{key}] dependency state unknown -- leaving this card's dependencies untouched")
                continue
            current = agileplace.incoming_dependency_ids(entries)
        adds, removes = _dependency_changes(desired, current, removal_authority_card_ids)
        adds, removes, dropped = filter_poisoned_edges(adds, removes, poisoned)
        if dropped:
            print(f"WARN  [{key}] dropping poisoned card id(s) from dependency writes")
        if adds:
            agileplace.create_dependencies(cfg, apply, cid, adds)
            print(f"{'dep   ' if apply else 'DRY  '} [{key}] +{len(adds)} dependency(ies)")
        if removes:
            agileplace.delete_dependencies(cfg, apply, cid, removes)
            print(f"{'undep ' if apply else 'DRY  '} [{key}] -{len(removes)} dependency(ies)")


def issue_card_title(issue: dict) -> str:
    t = issue["title"]
    k = title_key(t)
    if k and t.startswith(f"[{k}]"):
        return t[len(f"[{k}]"):].strip() or t
    return t


def _matching_card(issue: dict, card_by_url: dict, card_by_cid: dict) -> dict | None:
    """Match by URL first, then customId, refusing an ambiguous cross-card match."""
    custom_id = issue_custom_id(issue)
    url_match = card_by_url.get(issue["url"])
    custom_id_match = card_by_cid.get(custom_id)
    if url_match and custom_id_match:
        url_card_id = str(url_match.get("id") or "")
        custom_id_card_id = str(custom_id_match.get("id") or "")
        if not same_card(url_match, custom_id_match):
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
        if current_custom_id and same_card(reconciled.get(current_custom_id), url_match):
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


def resolve_issue_stage(issue: dict, project_status: dict, project_items: dict,
                        stage_map: dict | None) -> str:
    """CLOSED always wins as "Done". Else an explicit Project Status wins. Else issue_stage()'s
    fallback -- UNLESS that fallback is the bare-else "Backlog" AND the board declares an "Intake"
    lane mapping AND this issue isn't already a Project member, in which case it holds at "Intake"
    instead: a freshly-discovered issue with no work signal waits for a human to vet it onto the
    board rather than landing straight in Backlog. Board membership (project_items) and any work
    signal (a fallback other than bare "Backlog") both veto "Intake" unconditionally."""
    if str(issue.get("state", "")).upper() == "CLOSED":
        return "Done"
    explicit = explicit_stage_status(issue, project_status)
    if explicit:
        return explicit
    fallback = issue_stage(issue)
    if (fallback == "Backlog" and "Intake" in (stage_map or {})
            and issue["url"] not in project_items):
        return "Intake"
    return fallback


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


def _apply_lane_move(cfg: dict, apply: bool, issue: dict, card: dict, key: str, stage: str,
                     current: str, lanes: list, stage_map: dict | None, project_status: dict,
                     queue, *, open_pr_read_failed: bool) -> None:
    """Pure lift of the ordinary lane-move body for one active issue's existing card -- unchanged
    behavior, just named and callable so the loop-2 wiring can gate it behind the Intake vetting
    latch (issue #63). `cfg` is accepted for call-site symmetry with vetting_latch.apply_latch even
    though the move itself needs nothing beyond what its own helpers already take."""
    has_explicit_status = explicit_stage_status(issue, project_status) is not None
    lane_stage = _protect_open_pr_stage(stage, current, lanes, issue.get("milestone") or "", stage_map,
                                        open_pr_read_failed=open_pr_read_failed,
                                        has_explicit_status=has_explicit_status,
                                        issue_closed=str(issue.get("state", "")).upper() == "CLOSED")
    target_lane, acceptable = agileplace.resolve_lane_for_stage(
        lanes, lane_stage, issue.get("milestone") or "", stage_map)
    if target_lane and current not in {str(i) for i in acceptable}:
        queue(card, [agileplace.op_lane(target_lane["id"])], f"lane->{agileplace.lane_title(target_lane)}")
        print(f"{'move ' if apply else 'DRY  '} [{key}] -> '{agileplace.lane_title(target_lane)}' (stage {lane_stage})")


def _retire_card(issue: dict, card: dict, lanes: list, stage_map: dict | None,
                 apply: bool, queue) -> None:
    """Move one URL-matched retired issue card to Done (lane only -- flags are human-owned)."""
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
    if ops:
        queue(card, ops, f"retire:{reason}")
    action = "; ".join(actions) or "no card changes available"
    print(f"{'retire' if apply else 'DRY   retire'} [{key}] {action} ({reason})")


def _retire_matched_issues(retired_issues: list[dict], retired_card_by_url: dict[str, dict],
                           all_card_by_cid: dict[str, dict], contested: dict[str, set[str]],
                           contested_urls: frozenset[str], lanes: list, stage_map: dict | None,
                           apply: bool, queue) -> None:
    """Retired issues are dependency facts, not active work. Existing cards are matched by their
    authoritative GitHub URL only: a customId may have been reused and must never make us retire
    another issue's card. Retirement is independent of Projects/open-PR read health because the
    CLOSED reason itself is the authoritative signal.

    Issue #70/#75 Layer 1: a contested card is deferred wholesale here too, never partially
    retired -- and a customId match against an already-contested card is not a distinct finding
    (Layer 1 already printed the "card N claimed by K issue URLs" WARN for that card id), so this
    never warns twice for the same card."""
    for issue in retired_issues:
        if issue["url"] in contested_urls:
            continue
        card = retired_card_by_url.get(issue["url"])
        cid_card = all_card_by_cid.get(issue_custom_id(issue))
        if card:
            _retire_card(issue, card, lanes, stage_map, apply, queue)
        elif cid_card and str(cid_card.get("id") or "") not in contested:
            print(f"WARN  [{issue_custom_id(issue)}] retired issue has only a customId card match; "
                  "refusing to retire without the GitHub external-link URL")


def _created_card_snapshot(cfg: dict, created: Mapping) -> Mapping:
    """Refetch a just-created card once so its authoritative server state becomes the snapshot.

    The live create response is sparse -- the new id, but no version and no customId/laneId echo
    (validated live 2026-07-21, issue #55). Indexed as-is it queues redundant /customId and /laneId
    ops that the issue-#8 stale-ops guard then reads as a concurrent edit, aborting every fresh
    create+sync apply. The refetched card carries a usable version, so the later PATCH skips its own
    refetch: net API calls are unchanged. A failed or mismatched refetch falls back to the sparse
    response (the pre-fix behavior) rather than failing a run whose create already succeeded.
    """
    try:
        fresh = agileplace.get_card(cfg, created["id"])
    except SystemExit as err:
        print(f"WARN  refetch of created card {created['id']} failed ({err}) -- "
              "keeping the sparse create response as its snapshot")
        return created
    if str(fresh.get("id") or "") != str(created["id"]):
        print(f"WARN  refetch of created card {created['id']} returned card {fresh.get('id')!r} -- "
              "keeping the sparse create response as its snapshot")
        return created
    return fresh


def _ensure_cards_for_syncable_issues(cfg: dict, apply: bool, syncable_issues: list[dict], card_for,
                                      pending_custom_id_releases: frozenset[str], project_status: dict,
                                      project_items: dict, project_read_failed: bool, lanes: list,
                                      stage_map: dict | None, card_by_url: dict[str, dict],
                                      card_by_cid: dict[str, dict],
                                      type_by_name: Mapping[str, str]) -> None:
    """Step 1: ensure a card exists for every syncable active issue that doesn't have one yet.

    Mutates `card_by_url`/`card_by_cid` IN PLACE to register each freshly created card under its
    issue's url/customId, the same run-scoped accumulator convention `queue()` uses for `card_ops` --
    steps 2-4 close over these SAME dict objects via `card_for()`, so a card created here is visible
    to them immediately, with no second index-rebuild pass. Dry-run creates carry an obvious
    plan-only id that is indexed the same way; dry-run state is never saved, so that identity can't
    escape this run.

    `type_by_name` is card_types.resolve_card_type_ids(...).by_name (issue #82): a new card's typeId
    is set at create time from the issue's derived card type, when that name resolved to a board
    type. create_card's type_title mirrors the derived name back into the dry-run snapshot's nested
    type.title, so a same-pass card_types.sync_card_type reads current==derived and never double-
    queues the same typeId patch."""
    for issue in syncable_issues:
        if card_for(issue):
            continue
        key = issue_custom_id(issue)
        if key in pending_custom_id_releases:
            print(f"WARN  deferring card [{key}] until the renamed customId is released by a prior run")
            continue
        # informational only when the read failed
        stage = resolve_issue_stage(issue, project_status, project_items, stage_map)
        lane = None
        if not project_read_failed:
            lane, _ = agileplace.resolve_lane_for_stage(lanes, stage, issue.get("milestone") or "", stage_map)
        derived_type = card_types.derive_card_type_name(issue)
        type_id = type_by_name.get(derived_type) if derived_type else None
        created = agileplace.create_card(cfg, apply, issue_card_title(issue), key, issue["url"],
                                         lane["id"] if lane else None,
                                         type_id=type_id, type_title=derived_type if type_id else None)
        if apply and created.get("id"):
            created = _created_card_snapshot(cfg, created)
        if created.get("id"):
            card_by_url[issue["url"]] = created
            if key:
                card_by_cid[key] = created
        lane_note = (f" lane={agileplace.lane_title(lane)}" if lane
                     else " lane=deferred (Projects v2 read failed)" if project_read_failed else "")
        print(f"{'made ' if apply else 'DRY  '} card [{key}] stage={stage}{lane_note}")


def _run_intake_promotion(cfg: dict, apply: bool, cards: list, lanes: list, stage_map: dict | None,
                          issues: list[dict], card_by_url: dict, card_by_cid: dict
                          ) -> intake.IntakeSummary:
    """Reverse intake (issue #62): promote unmanaged Intake-lane cards into new GitHub issues. Runs
    only AFTER _reconciled_custom_id_index's fail-closed identity check has passed, so an ambiguous
    URL/customId board state still aborts the run BEFORE any intake write -- preserving the
    "ambiguous identity fails before any mutation" guarantee. Uses the FULL, unfiltered cards/issues
    (not the retirement-filtered indices main() builds, which #70 owns) -- intake candidate
    selection has nothing to do with retirement. A card promoted this run is never lane-moved this
    run either: `issues` is the run's already-fetched snapshot, so the newly created issue is absent
    from active_issues and the ordinary per-issue lane-sync loop can't reach it until next run picks
    it up via its written-back link.

    Then registers each adopted card in the caller's ownership indices under its issue's URL and
    written-back customId. Without this, a marker-resumed card (its issue already active this run,
    its writeback landing only now -- AFTER the `cards` snapshot card_by_url/card_by_cid were built
    from) stays invisible to the per-issue creation loop below, which would then create a DUPLICATE
    card for that same issue. Prints the one-line summary only when there was at least one
    candidate."""
    summary = intake.promote(cfg, apply, cards, lanes, stage_map, issues)
    for card, issue in summary.adopted:
        card_by_url[issue["url"]] = card
        key = issue_custom_id({"title": card.get("title", ""), "number": issue["number"]})
        if key:
            card_by_cid[key] = card
    if summary.candidates:
        print(f"intake: {summary.candidates} candidate(s) -- "
              f"{summary.resumed} resumed, {summary.created} created")
    return summary


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

    pv2 = ghproject.resolve_project_v2_status(cfg)
    project_items, project_status = pv2.project_items, pv2.project_status
    field_meta, project_read_failed, move_lanes = pv2.field_meta, pv2.project_read_failed, pv2.move_lanes

    # --- issue #82: card-types wiring (comment-banner delimited -- #65 is expected to touch this
    # same `lanes = ...` line, so this is flagged as a likely merge conflict up front) ---
    layout = agileplace.board_layout(cfg) if online else agileplace.BoardLayout(lanes=[], card_types=[])
    lanes = layout.lanes
    resolved = card_types.resolve_card_type_ids(layout.card_types)
    if online:
        for line in resolved.warnings:
            print(line)
    # --- end issue #82 card-types wiring ---
    cards = agileplace.list_cards(cfg) if online else []
    smap = cfg.get("stage_lane_map")

    all_card_by_url, all_card_by_cid = {}, {}
    for card in cards:
        for u in agileplace.card_external_urls(card):
            all_card_by_url[u] = card
        cid = agileplace.custom_id_value(card)
        if cid:
            all_card_by_cid[cid] = card

    # Issue #70/#75 Layer 1: before any card is touched, detect this run's issues that don't
    # resolve 1:1 onto AgilePlace cards (>= 2 distinct issues claiming the same card id, via
    # either the URL or the customId fallback match path) and exclude those cards from every
    # match/queue path this run, rather than risk one issue's sync clobbering another's. The
    # call-site wiring built on top of `contested` lives in card_coherence.fence_run_indices --
    # see that module's docstring for why (and why `contested_cards` is still called here, not
    # there).
    contested = contested_cards(active_issues + retired_issues, all_card_by_url, all_card_by_cid)
    fenced = fence_run_indices(contested, active_issues, retired_issues, all_card_by_url, all_card_by_cid)
    for line in fenced.warnings:
        print(line)
    contested_urls = fenced.contested_urls
    retired_card_by_url = fenced.retired_card_by_url
    card_by_url = fenced.card_by_url
    syncable_issues = fenced.syncable_issues

    card_by_cid, pending_custom_id_releases = _reconciled_custom_id_index(
        syncable_issues, card_by_url, fenced.card_by_cid)

    _run_intake_promotion(cfg, apply, cards, lanes, smap, issues, card_by_url, card_by_cid)

    epics = [i for i in syncable_issues if "type:epic" in i["labels"]]

    def card_for(issue):
        return _matching_card(issue, card_by_url, card_by_cid)

    card_ops: dict = {}

    def queue(card, ops, note):
        # Issue #70 Layer 2: two queue() calls for the same card can carry conflicting /laneId
        # values (e.g. duplicate [KEY]-prefixed issue titles matching the same card through the
        # customId fallback within one run). Detect and poison the entry rather than risk one
        # issue's lane move clobbering another's -- the poisoned entry is skipped wholesale at
        # flush (below), never partially applied.
        cid = str(card["id"])
        entry = card_ops.setdefault(
            cid, {"card": card, "ops": [], "notes": [], "lane_id": None, "poisoned": False})
        new_lane_id, conflict = lane_conflict(ops, entry["lane_id"])
        if conflict:
            entry["poisoned"] = True
            conflicting_value = laneid_op_value(ops)
            print(f"WARN  card {cid} poisoned: conflicting /laneId ops "
                  f"({entry['lane_id']!r} vs {conflicting_value!r})")
        else:
            entry["lane_id"] = new_lane_id
        entry["ops"].extend(ops)
        entry["notes"].append(note)

    # Retired issues (see _retire_matched_issues for the full contract).
    _retire_matched_issues(retired_issues, retired_card_by_url, all_card_by_cid, contested,
                           contested_urls, lanes, smap, apply, queue)

    # 1) ensure a card per active issue (see _ensure_cards_for_syncable_issues).
    _ensure_cards_for_syncable_issues(cfg, apply, syncable_issues, card_for,
                                      pending_custom_id_releases, project_status, project_items,
                                      project_read_failed, lanes, smap, card_by_url, card_by_cid,
                                      resolved.by_name)

    # 2) per active issue: base reset if card changed; lane; metadata; dates
    for issue in syncable_issues:
        key = issue_custom_id(issue)
        card = card_for(issue)
        if not card or not card.get("id"):
            continue  # unresolved (no matching or newly created card this run)
        cid = str(card["id"])
        st = issues_state.setdefault(issue["url"], {})
        if st.get("card_id") != cid:
            issues_state[issue["url"]] = {"card_id": cid}  # fresh/migrated/replaced -> reset merge base
        if agileplace.custom_id_value(card) != key:
            queue(card, [agileplace.op_custom_id(key)], f"customId->{key}")
            print(f"{'sync ' if apply else 'DRY  '} [{key}] customId")

        stage = resolve_issue_stage(issue, project_status, project_items, smap)
        if move_lanes:
            current = str(card.get("laneId") or (card.get("lane") or {}).get("id") or "")
            # A card whose stage resolves to "Intake" this run may already be sitting somewhere a
            # human deliberately moved it (out of the intake lane, mid-vetting) or nowhere mappable
            # at all -- either way the ordinary lane-move must not run blind. apply_latch() decides:
            # True means it already handled (or safely deferred) this card, so the ordinary move is
            # skipped; False means the card is already parked in the Intake lane itself, where the
            # ordinary move is harmless (it will simply find nothing to change).
            # A Project MEMBER with no recognized Status is the flip side (issue #69): membership
            # vetoes Intake, so it resolves to a signal-derived stage the mover would act on --
            # repair_statusless_member retries the Status write and holds the card instead.
            member_item = project_items.get(issue["url"])
            statusless_member = ("Intake" in (smap or {}) and member_item is not None
                                 and explicit_stage_status(issue, project_status) is None)
            latched = (stage == "Intake" and vetting_latch.apply_latch(
                cfg, apply, issue, key, current, lanes, smap)) or (
                statusless_member and vetting_latch.repair_statusless_member(
                    cfg, apply, issue, key, current, lanes, smap, member_item))
            if not latched:
                _apply_lane_move(cfg, apply, issue, card, key, stage, current, lanes, smap,
                                 project_status, queue, open_pr_read_failed=open_pr_read_failed)
        sync_metadata(cfg, apply, issue, card, cfg["label_sync_ignore"], issues_state, queue)
        if field_meta:
            sync_dates(cfg, apply, issue, card, project_items.get(issue["url"]), field_meta, issues_state, queue)
        sync_description(cfg, apply, issue, card, issues_state, queue)
        card_types.sync_card_type(cfg, apply, issue, card, resolved.by_name, issues_state, queue)

    # 3) parent/child connections (see sync_child_connections for the full contract).
    poisoned = poisoned_card_ids(card_ops)
    managed_card_ids = _managed_card_ids(syncable_issues, card_for, retired_card_by_url)
    sync_child_connections(cfg, apply, epics, card_for, by_key, by_number, poisoned, managed_card_ids)

    # 4) GitHub blocked-by edges -> native card dependencies (issue #57) -- all edges, managed
    # pairs only, retired Done blockers resolving through their URL-owned cards. Skip entirely
    # unless the whole blocked-by snapshot is complete. The card Blocked flag belongs to humans:
    # the sync never writes /isBlocked or /blockReason (the old flag-text mirror was retired in
    # issue #57 Phase 2; clear_legacy_blocked_flags.py cleaned up what it left behind). Removal
    # authority is narrower than managed_card_ids here: a card an active issue reached only
    # through the customId fallback confers no removal authority over its dependencies
    # (issue #60) -- see _removal_authority_card_ids.
    blocked_by = (ghkit.blocked_by_map(cfg, [i["number"] for i in syncable_issues])
                  if online and syncable_issues else {} if online else None)
    if online and blocked_by is None:
        print("WARN  blocked-by snapshot incomplete -- leaving ALL card dependencies untouched this run")
    if blocked_by is not None:
        sync_dependencies(cfg, apply, syncable_issues, blocked_by,
                          _blocker_cards(by_number, card_for, retired_issues, retired_card_by_url),
                          card_for,
                          _removal_authority_card_ids(syncable_issues, card_by_url, retired_card_by_url),
                          poisoned)

    # 5) flush: ONE versioned PATCH per card (optimistic concurrency)
    for entry in card_ops.values():
        if entry["poisoned"]:
            continue  # Issue #70 Layer 2: conflicting /laneId ops -- discard, don't half-apply
        agileplace.patch_card(cfg, apply, entry["card"], entry["ops"], "; ".join(entry["notes"]))

    if apply and not any(entry["poisoned"] for entry in card_ops.values()):
        save_state(state)
    elif apply:
        # Issue #70 Layer 2: skipping a poisoned card's PATCH leaves this run's already-advanced merge
        # bases (sync_metadata/sync_dates) unbacked by a write; persisting them would desync next run
        # into a phantom external-delete revert. Hold state at the last clean run -- skipped writes
        # retry then, and healthy cards re-derive harmlessly from the older base.
        print("WARN  poisoned card(s) this run -- sync state NOT persisted (merge bases held clean)")
    else:
        print("--- dry run complete. Re-run with --apply (full .env) to write.")


if __name__ == "__main__":
    main()
