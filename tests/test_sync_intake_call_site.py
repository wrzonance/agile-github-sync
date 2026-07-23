"""sync.main()'s reverse-intake call site (Task 8/8, issue #62).

Thin, isolated from the large test_sync_main.py suite: this file's only concern is that main()
actually calls intake.promote() with the right positional arguments (cfg, apply, cards, lanes,
stage_map, issues) at the right point in the pipeline, and that a non-empty IntakeSummary produces
the expected one-line console summary. It does not re-verify intake.promote()'s own behavior --
that is tests/test_intake.py's job -- so intake.promote itself is monkeypatched here, matching the
low-level-transport-boundary convention this repo's other main()-level tests use (patching the
collaborator, not its internals).

Run: pytest -q
"""
from __future__ import annotations

import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import intake  # noqa: E402
import sync  # noqa: E402

# _mock_io is test_sync_main.py's richer I/O-boundary-mocking helper (lane/existing-card control,
# a real patch_card mock to inspect) -- reused here rather than duplicated, for the one test below
# that needs intake.promote() to run for REAL end-to-end (every other test in this file monkeypatches
# intake.promote() itself, which _run_main's lighter stack below is built for).
from test_sync_main import _card as _sync_main_card, _cfg as _sync_main_cfg, _mock_io  # noqa: E402

ISSUE_URL = "https://github.com/acme/repo/issues/1"


def _issue():
    return {"number": 1, "title": "widget", "state": "OPEN", "labels": [],
            "milestone": None, "assignees": [], "url": ISSUE_URL}


def _card():
    # "description": "" (issue #65) keeps agileplace_description.card_description() on its zero-I/O path.
    return {"id": "C1", "version": 1, "customId": "1",
            "externalLink": {"url": ISSUE_URL}, "tags": [],
            "plannedStart": None, "plannedFinish": None, "laneId": "LANE1",
            "description": ""}


def _cfg(tmp_path):
    return {
        "token": "tok", "host": "example.leankit.com", "board_id": "42",
        "target_repo_path": tmp_path,
        "label_sync_ignore": frozenset(),
        "stage_lane_map": {"Intake": ["New Requests"]},
        "gh_project": {"owner": "acme", "number": "7", "status_field": "Status",
                       "start_field": "Start", "target_field": "Target"},
        "ap_description_max_length": 20000,  # issue #65: sync_description reads this unconditionally
    }


def _run_main(tmp_path, promote_return, lanes=(), cards=None):
    """ExitStack covering every I/O boundary main() touches before/around the intake call site.
    Returns the intake.promote mock for call-site assertions."""
    state_file = tmp_path / ".sync-state.json"
    cfg = _cfg(tmp_path)
    stack = ExitStack()
    stack.enter_context(patch("ghkit.repo_name", return_value="acme/repo"))
    stack.enter_context(patch("ghkit.list_issues", return_value=[_issue()]))
    stack.enter_context(patch("ghkit.open_pr_issue_numbers", return_value=set()))
    stack.enter_context(patch("ghkit.blocked_by_map", return_value={}))
    stack.enter_context(patch("ghkit.run", return_value=Mock(stdout="")))
    stack.enter_context(patch("ghproject.configured", return_value=False))
    stack.enter_context(patch("agileplace.board_layout", return_value=list(lanes)))
    stack.enter_context(patch("agileplace.list_cards", return_value=cards if cards is not None else [_card()]))
    stack.enter_context(patch("agileplace.card_dependencies", return_value=[]))
    stack.enter_context(patch("agileplace.patch_card"))
    stack.enter_context(patch("agileplace.create_card", return_value={}))
    promote_mock = stack.enter_context(patch("intake.promote", return_value=promote_return))

    with stack, patch("sync.env_config", return_value=cfg), patch("sync.STATE_FILE", state_file), \
         patch("sys.argv", ["sync.py", "--apply"]):
        sync.main()
    return promote_mock


def test_main_calls_intake_promote_with_full_unfiltered_cards_and_issues(tmp_path, capsys):
    # has_open_pr is annotated onto active issues by main() (an existing, pre-#62 step) before the
    # intake call site is reached, so the issue promote() receives carries that field too.
    lanes = [{"id": "LANE1", "title": "New Requests"}]
    cards = [_card()]
    issues = [{**_issue(), "has_open_pr": False}]
    summary = intake.IntakeSummary(candidates=0, prescan_failed=False, resumed=0, created=0)

    promote_mock = _run_main(tmp_path, summary, lanes=lanes, cards=cards)

    promote_mock.assert_called_once()
    _call_cfg, call_apply, call_cards, call_lanes, call_stage_map, call_issues = promote_mock.call_args.args
    assert call_apply is True
    assert call_cards == cards
    assert call_lanes == lanes
    assert call_stage_map == {"Intake": ["New Requests"]}
    assert call_issues == issues


def test_main_does_not_create_a_duplicate_card_for_a_resumed_active_issue(tmp_path):
    """Data-integrity regression (issue #62 follow-up): when marker-resume reattaches an existing
    Intake card to an ALREADY-ACTIVE issue, the run must NOT then create a duplicate card for that
    same issue. The card's writeback lands after main()'s local `cards` snapshot was taken, so
    _run_intake_promotion must refresh card_by_url/card_by_cid from the promotion's adoptions before
    the per-issue card-creation loop -- otherwise card_for(issue) misses and agileplace.create_card
    is called for an issue that already has a card. Dry run: create_card is still mock-observed
    whether or not --apply is set, and the marker-resume writeback needs no live get_card refetch."""
    active_url = "https://github.com/acme/repo/issues/7"
    active_issue = {"number": 7, "title": "Raw idea", "state": "OPEN", "labels": [],
                    "milestone": None, "assignees": [], "url": active_url}
    intake_card = {"id": "C-intake", "version": 1, "laneId": "lane-intake", "title": "Raw idea",
                  "description": ""}  # issue #65: this card reaches the per-issue loop below
    intake_lane = {"id": "lane-intake", "title": "New Requests"}
    cfg = {**_sync_main_cfg(tmp_path), "stage_lane_map": {"Intake": ["New Requests"]}}
    state_file = tmp_path / ".sync-state.json"

    stack, _run, _patch_card, create_card_mock = _mock_io(
        intake_card, ({}, []), field_meta_return=None,
        existing_cards=[intake_card], lanes_return=[intake_lane], issue_return=active_issue)

    with stack, patch("sync.env_config", return_value=cfg), \
         patch("sync.STATE_FILE", state_file), patch("sys.argv", ["sync.py"]), \
         patch("ghkit.list_issue_bodies",
               return_value=[{"number": 7, "url": active_url, "state": "OPEN",
                              "body": intake.marker_for_card("C-intake")}]):
        sync.main()

    create_card_mock.assert_not_called()


def test_main_runs_intake_only_after_the_fail_closed_identity_check(tmp_path):
    """P2 regression (issue #62 follow-up): intake.promote() must run only AFTER
    _reconciled_custom_id_index's ambiguous-identity guard. On a board where one issue matches one
    card by URL but a DIFFERENT card by customId, the run must SystemExit before any intake write --
    so promote() (and the writes it would make) is never reached. Before the fix, intake ran at the
    top of the pipeline and mutated the board before this fail-closed check aborted the run."""
    cfg = _cfg(tmp_path)
    state_file = tmp_path / ".sync-state.json"
    url_card = {"id": "C-url", "externalLink": {"url": ISSUE_URL}, "laneId": "LANE1"}
    cid_card = {"id": "C-cid", "customId": "1", "laneId": "LANE1"}  # issue #1's fallback customId

    stack = ExitStack()
    stack.enter_context(patch("ghkit.repo_name", return_value="acme/repo"))
    stack.enter_context(patch("ghkit.list_issues", return_value=[_issue()]))
    stack.enter_context(patch("ghkit.open_pr_issue_numbers", return_value=set()))
    stack.enter_context(patch("ghkit.blocked_by_map", return_value={}))
    stack.enter_context(patch("ghkit.run", return_value=Mock(stdout="")))
    stack.enter_context(patch("ghproject.configured", return_value=False))
    stack.enter_context(patch("agileplace.board_layout", return_value=[]))
    stack.enter_context(patch("agileplace.list_cards", return_value=[url_card, cid_card]))
    stack.enter_context(patch("agileplace.card_dependencies", return_value=[]))
    stack.enter_context(patch("agileplace.patch_card"))
    stack.enter_context(patch("agileplace.create_card", return_value={}))
    promote_mock = stack.enter_context(patch("intake.promote"))

    with stack, patch("sync.env_config", return_value=cfg), patch("sync.STATE_FILE", state_file), \
         patch("sys.argv", ["sync.py", "--apply"]), pytest.raises(SystemExit):
        sync.main()

    promote_mock.assert_not_called()


def test_main_prints_intake_summary_line_only_when_candidates_nonzero(tmp_path, capsys):
    summary = intake.IntakeSummary(candidates=2, prescan_failed=False, resumed=1, created=1)

    _run_main(tmp_path, summary)

    out = capsys.readouterr().out
    assert "intake: 2 candidate(s) -- 1 resumed, 1 created" in out


def test_main_prints_no_intake_line_when_candidates_zero(tmp_path, capsys):
    summary = intake.IntakeSummary(candidates=0, prescan_failed=False, resumed=0, created=0)

    _run_main(tmp_path, summary)

    out = capsys.readouterr().out
    assert "intake:" not in out


# --- end-to-end: intake.promote() runs for REAL through main() ---------------
#
# Every test above monkeypatches intake.promote() itself to pin the call-site contract in
# isolation. This one lets it run for real (only its own collaborators -- ghkit.create_issue,
# agileplace.patch_card, etc. -- are mocked), pinning two behavioral invariants together:
# promoting an Intake candidate never queues a `/laneId` op, and the promoted card actually
# receives both writeback PATCH calls (issue #62 critical-bug regression: before the version-
# staleness fix in intake._writeback, the second of those two calls 409/428'd against a real
# versioned card -- see tests/test_intake_writeback_version_conflict.py for the isolated version
# of that regression).

def _intake_card():
    """A card sitting in the board's Intake lane with no external link and no customId -- the
    shape intake.intake_candidates() must select."""
    return {"id": "C-intake", "version": 1, "laneId": "lane-intake", "title": "Raw idea"}


def test_main_wires_intake_promote_and_never_moves_the_promoted_cards_lane(tmp_path):
    """main() must actually call intake.promote() with the full, unfiltered cards/lanes/stage_map/
    issues it already loaded (not some later-filtered subset) -- and, end-to-end, promoting a
    candidate must never queue a `/laneId` patch op for it. The newly created issue is also absent
    from THIS run's active_issues (`issues` was fetched before promote() runs), so the ordinary
    per-issue lane-sync loop cannot reach the new card this run either -- next run's ordinary sync
    adopts it via the written-back link/customId like any other card."""
    parsed = {ISSUE_URL: {
        "item_id": "PVTI_1", "number": 1, "status": "Backlog", "start": None, "target": None,
    }}
    intake_lane = {"id": "lane-intake", "title": "New Requests"}
    matched_card = _sync_main_card()
    intake_card = _intake_card()
    cfg = {**_sync_main_cfg(tmp_path), "stage_lane_map": {"Intake": ["New Requests"]}}
    created_issue = {"number": 99, "url": "https://github.com/acme/repo/issues/99"}

    stack, _, patch_card_mock, _ = _mock_io(
        matched_card, (parsed, []), field_meta_return=None,
        existing_cards=[matched_card, intake_card], lanes_return=[intake_lane])
    state_file = tmp_path / ".sync-state.json"

    # intake._card_for_link_write's real explicit refetch (issue #62 critical-bug fix) calls
    # agileplace.get_card directly, bypassing the patch_card mock above -- stub it with a fresh,
    # usable-version snapshot so the writeback's second PATCH proceeds normally.
    with stack, patch("sync.env_config", return_value=cfg), \
         patch("sync.STATE_FILE", state_file), patch("sys.argv", ["sync.py", "--apply"]), \
         patch("ghkit.list_issue_bodies", return_value=[]), \
         patch("ghkit.create_issue", return_value=created_issue) as create_issue_mock, \
         patch("agileplace.get_card",
               return_value={"id": intake_card["id"], "version": 2}):
        sync.main()

    # Wiring: main() actually invoked create_issue for the Intake-lane candidate, with the cfg
    # main() itself loaded and the card's own title -- proof intake.promote() ran for real rather
    # than being skipped or fed a stale/filtered argument.
    create_issue_mock.assert_called_once()
    called_cfg, called_apply, called_title, _called_body = create_issue_mock.call_args.args
    assert called_cfg is cfg
    assert called_apply is True
    assert called_title == "Raw idea"

    # Invariant: promotion never moves a card's lane -- neither the intake card's own writeback
    # PATCH nor anything else in this run's full patch_card call list ever carries a /laneId op.
    assert patch_card_mock.call_args_list, "expected at least the intake writeback PATCH calls"
    for call in patch_card_mock.call_args_list:
        ops = call.args[3]
        assert all(op.get("path") != "/laneId" for op in ops)

    # The intake card itself received its link + customId writeback PATCH calls. Matched by id,
    # not object identity: the version-staleness fix (issue #62 critical bug) makes intake._writeback
    # build a NEW, version-stripped dict for the second (link) write rather than reusing `card`, so
    # only the first (customId) PATCH call still carries `intake_card` itself.
    intake_calls = [call for call in patch_card_mock.call_args_list
                    if call.args[2].get("id") == intake_card["id"]]
    assert len(intake_calls) == 2
    assert [call.args[3][0]["path"] for call in intake_calls] == ["/customId", "/externalLink"]
