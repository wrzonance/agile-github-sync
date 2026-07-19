"""End-to-end wiring tests for sync.main() (issue #6, task 5): items_and_raw swap, unmatched_kinds
computation + WARN print, and threading unmatched_kinds into the sync_dates call site.

Unlike test_sync_dates.py (which calls sync_dates directly), these mock every I/O boundary (ghkit,
ghproject's gh-touching functions, agileplace's HTTP client) but exercise the REAL main(),
load_state/save_state, and sync_dates -- so they pin that main() actually plumbs raw_items and
unmatched_kinds through, and that the merge-base advance invariant holds across real state persisted
to disk between two separate main() runs (not just within one sync_dates call).

TEST-CONSTRUCTION NOTE (final design decision #1): the two-run merge-base test holds the GitHub-side
date (pitem["start"]) CONSTANT across both runs and toggles ONLY item_id (None -> present) to simulate
"write skipped" -> "write confirmed". Do not fake this by changing the GitHub-side value itself -- that
exercises a different (legitimate) reconcile_value path ("GitHub genuinely changed it").

Run: pytest -q
"""
from __future__ import annotations

import json
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sync  # noqa: E402

ISSUE_URL = "https://github.com/acme/repo/issues/1"


def _issue():
    return {"number": 1, "title": "widget", "state": "OPEN", "labels": [],
            "milestone": None, "assignees": [], "url": ISSUE_URL}


def _card():
    return {"id": "C1", "version": 1, "externalLink": {"url": ISSUE_URL}, "tags": [],
            "plannedStart": "2026-02-01", "plannedFinish": None, "laneId": None}


def _field_meta():
    return {"project_id": "PVT_1", "status_field_id": "STF", "status_options": {},
            "start_field_id": "SF_1", "target_field_id": "TF_1"}


def _cfg(tmp_path):
    return {
        "token": "tok", "host": "example.leankit.com", "board_id": "42",
        "target_repo_path": tmp_path,
        "label_sync_ignore": frozenset(),
        "stage_lane_map": {},
        "gh_project": {"owner": "acme", "number": "7", "status_field": "Status",
                       "start_field": "Start", "target_field": "Target"},
    }


def _mock_io(card, items_and_raw_return, field_meta_return, existing_cards=None):
    """ExitStack of patches covering every I/O boundary main() touches for one run. Returns the stack
    plus the ghkit.run, agileplace.patch_card, and agileplace.create_card mocks (for call-site
    assertions). `existing_cards` defaults to [card] (the common "card already exists" scenario); pass
    [] to exercise the new-card-creation loop instead."""
    stack = ExitStack()
    stack.enter_context(patch("ghkit.repo_name", return_value="acme/repo"))
    stack.enter_context(patch("ghkit.list_issues", return_value=[_issue()]))
    stack.enter_context(patch("ghkit.open_pr_issue_numbers", return_value=set()))
    stack.enter_context(patch("ghkit.blocked_by_map", return_value={}))
    run_mock = stack.enter_context(patch("ghkit.run", return_value=Mock(stdout="")))
    stack.enter_context(patch("ghproject.configured", return_value=True))
    stack.enter_context(patch("ghproject.items_and_raw", return_value=items_and_raw_return))
    stack.enter_context(patch("ghproject.field_meta", return_value=field_meta_return))
    stack.enter_context(patch("agileplace.board_layout", return_value=[]))
    stack.enter_context(patch("agileplace.list_cards",
                              return_value=[card] if existing_cards is None else existing_cards))
    patch_card_mock = stack.enter_context(patch("agileplace.patch_card"))
    create_card_mock = stack.enter_context(patch("agileplace.create_card", return_value={}))
    return stack, run_mock, patch_card_mock, create_card_mock


def _run_main_once(tmp_path, items_and_raw_return, field_meta_return=None, seed_issues_state=None,
                   existing_cards=None):
    """seed_issues_state pre-populates the on-disk state file's issues[ISSUE_URL] before main() runs --
    used to simulate a kind that previously read real values (see ghproject.unmatched_date_kinds's
    known_kinds gate). existing_cards is forwarded to _mock_io (see there)."""
    cfg = _cfg(tmp_path)
    state_file = tmp_path / ".sync-state.json"
    if seed_issues_state is not None:
        state_file.write_text(json.dumps({"schema": 2, "target": "acme/repo", "board": "42",
                                          "issues": {ISSUE_URL: seed_issues_state}}), encoding="utf-8")
    card = _card()
    stack, run_mock, patch_card_mock, create_card_mock = _mock_io(
        card, items_and_raw_return, field_meta_return, existing_cards=existing_cards)
    with stack, patch("sync.env_config", return_value=cfg), patch("sync.STATE_FILE", state_file), \
         patch("sys.argv", ["sync.py", "--apply"]):
        sync.main()
    return json.loads(state_file.read_text(encoding="utf-8")), run_mock, patch_card_mock, create_card_mock


# --- merge-base advance invariant, end-to-end through two real main() runs -----------------------

def test_merge_base_advances_only_after_confirmed_write_across_two_main_runs(tmp_path, capsys):
    raw_items = [{"id": "PVTI_1", "content": {"url": ISSUE_URL}, "Start": "2026-01-01", "Target": None}]
    field_meta = _field_meta()

    # Run 1: item_id missing -> ghproject.set_project_date's own guard skips the write.
    parsed_no_item_id = {ISSUE_URL: {"item_id": None, "number": 1, "status": "In progress",
                                     "start": "2026-01-01", "target": None}}
    state_after_1, _, _, _ = _run_main_once(tmp_path, (parsed_no_item_id, raw_items), field_meta)
    assert "start" not in state_after_1["issues"][ISSUE_URL], (
        "merge base must NOT advance when the GH-side write was skipped (item_id missing)")

    # Run 2: same GitHub-side date, item_id now present -> the write is confirmed.
    parsed_with_item_id = {ISSUE_URL: {"item_id": "PVTI_1", "number": 1, "status": "In progress",
                                       "start": "2026-01-01", "target": None}}
    state_after_2, run_mock, _, _ = _run_main_once(tmp_path, (parsed_with_item_id, raw_items), field_meta)
    assert state_after_2["issues"][ISSUE_URL]["start"] == "2026-02-01", (
        "merge base must advance once the GH-side write is confirmed")
    run_mock.assert_called_once()  # the real ghproject.set_project_date issued exactly one gh write


# --- unmatched_kinds: computed in main(), WARNs, and gates sync_dates end-to-end -------------------

def test_unmatched_kinds_warns_and_skips_both_date_kinds(tmp_path, capsys):
    # Both kinds previously read real values (known_kinds), but NOW no raw row exposes ANY candidate
    # key for "Start" or "Target" -> a genuine regression, both kinds are unmatched.
    raw_items = [{"id": "PVTI_1", "content": {"url": ISSUE_URL}, "unrelated": "x"}]
    parsed = {ISSUE_URL: {"item_id": "PVTI_1", "number": 1, "status": "In progress",
                          "start": None, "target": None}}
    state, run_mock, patch_card_mock, _ = _run_main_once(
        tmp_path, (parsed, raw_items), _field_meta(),
        seed_issues_state={"start": "2026-01-01", "target": "2026-01-09"})

    out = capsys.readouterr().out
    assert "WARN  Projects v2 'start' field resolved but no item ever exposed a matching key" in out
    assert "WARN  Projects v2 'target' field resolved but no item ever exposed a matching key" in out
    run_mock.assert_not_called()               # no GH date write attempted for either skipped kind
    patch_card_mock.assert_not_called()        # no AgilePlace date write queued either
    assert state["issues"][ISSUE_URL]["start"] == "2026-01-01"    # base untouched, not wiped/advanced
    assert state["issues"][ISSUE_URL]["target"] == "2026-01-09"


def test_no_warn_and_no_skip_on_first_rollout_with_no_known_date_history(tmp_path, capsys):
    # Neither kind has ANY prior recorded value (fresh state, first run) and no raw row exposes a
    # matching key for either -- the common case when a Project's date fields are configured correctly
    # but nobody has set one on any item yet. This must sync normally, not be mistaken for a name
    # mismatch and permanently blocked (issue #6 follow-up).
    raw_items = [{"id": "PVTI_1", "content": {"url": ISSUE_URL}, "unrelated": "x"}]
    parsed = {ISSUE_URL: {"item_id": "PVTI_1", "number": 1, "status": "In progress",
                          "start": None, "target": None}}
    state, run_mock, patch_card_mock, _ = _run_main_once(tmp_path, (parsed, raw_items), _field_meta())

    out = capsys.readouterr().out
    assert "WARN  Projects v2" not in out
    run_mock.assert_called_once()               # start: AgilePlace's plannedStart is written to GitHub
    assert state["issues"][ISSUE_URL]["start"] == "2026-02-01"


# --- guarded call site: frozenset() when field_meta is falsy, no crash, no WARN --------------------

def test_no_warn_and_no_crash_when_field_meta_is_none(tmp_path, capsys):
    parsed = {ISSUE_URL: {"item_id": "PVTI_1", "number": 1, "status": "In progress",
                          "start": "2026-01-01", "target": None}}
    raw_items = [{"id": "PVTI_1", "content": {"url": ISSUE_URL}}]  # would flag both kinds if checked
    state, run_mock, patch_card_mock, _ = _run_main_once(tmp_path, (parsed, raw_items), field_meta_return=None)

    out = capsys.readouterr().out
    assert "WARN  Projects v2" not in out
    run_mock.assert_not_called()
    patch_card_mock.assert_not_called()
    assert "start" not in state["issues"][ISSUE_URL]


# --- issue #5: fail-closed gate widened to zero-recognized-statuses-despite-items, and extended to
# gate new-card lane assignment (not just existing-card lane moves) -------------------------------

def _zero_status_inputs():
    """A technically-successful item-list read: the Project has one issue-linked item (item_id/number
    populated) but no candidate key for the configured Status field resolved to a value -- the
    misspelled-GH_PROJECT_STATUS_FIELD / gh output-shape-drift signature this issue targets. project_status
    ends up {} even though project_items is non-empty."""
    parsed = {ISSUE_URL: {"item_id": "PVTI_1", "number": 1, "status": None, "start": None, "target": None}}
    raw_items = [{"id": "PVTI_1", "content": {"url": ISSUE_URL, "number": 1}, "unrelated": "x"}]
    return parsed, raw_items


def test_zero_recognized_statuses_treated_as_failed_read(tmp_path, capsys):
    state, run_mock, patch_card_mock, create_card_mock = _run_main_once(
        tmp_path, _zero_status_inputs(), field_meta_return=None)

    out = capsys.readouterr().out
    assert ("WARN  Projects v2 has 1 issue item(s) but none carry a recognized 'Status' Status -- "
            "check GH_PROJECT_STATUS_FIELD") in out
    patch_card_mock.assert_not_called()         # no lane-move (or any) op reached the existing card
    create_card_mock.assert_not_called()        # card already existed -- nothing created this run


def test_new_card_gets_no_lane_when_statuses_unrecognized(tmp_path, capsys):
    _, _, _, create_card_mock = _run_main_once(
        tmp_path, _zero_status_inputs(), field_meta_return=None, existing_cards=[])

    create_card_mock.assert_called_once()
    assert create_card_mock.call_args.args[-1] is None, (
        "new card must be created laneless when the Project read yields zero recognized statuses")


def test_new_card_gets_no_lane_when_project_read_outright_fails(tmp_path, capsys):
    _, _, _, create_card_mock = _run_main_once(
        tmp_path, (None, None), field_meta_return=None, existing_cards=[])

    out = capsys.readouterr().out
    assert "WARN  Projects v2 read FAILED -- leaving lanes untouched this run (Status is the source of truth)" in out, (
        "the outright-failure WARN string must be printed byte-for-byte on the (None, None) call-failed path")
    create_card_mock.assert_called_once()
    assert create_card_mock.call_args.args[-1] is None, (
        "new card must be created laneless when the Project item-list call fails outright")


def test_zero_issue_linked_items_does_not_trip_zero_status_warn(tmp_path, capsys):
    """False-positive guard: a Project whose configured board legitimately has zero issue-linked items
    (every row is a draft/PR, so project_items == {}) must NOT be mistaken for the
    zero-recognized-statuses failure mode -- that WARN, and the fail-closed lane gating that goes with
    it, exist only for a NON-empty item set with no recognized Status. An empty item set must leave
    move_lanes True (lane resolution still attempted for new cards)."""
    raw_items = [{"id": "PVTI_9", "content": {}}]  # draft item: no linked issue/PR at all
    parsed: dict = {}  # ghproject.items_and_raw resolved zero issue-linked items -- not a failure
    fake_lane = {"id": "L1", "title": "Planning"}

    with patch("agileplace.resolve_lane_for_stage", return_value=(fake_lane, {"L1"})) as resolve_mock:
        _, _, _, create_card_mock = _run_main_once(
            tmp_path, (parsed, raw_items), field_meta_return=None, existing_cards=[])

    out = capsys.readouterr().out
    assert "WARN  Projects v2 has" not in out, "zero issue-linked items must never trip the zero-status WARN"
    assert "WARN  Projects v2 read FAILED" not in out
    resolve_mock.assert_called_once()          # lane resolution was attempted -- move_lanes stayed True
    create_card_mock.assert_called_once()
    assert create_card_mock.call_args.args[-1] == "L1", (
        "new card must get a real lane when the Project legitimately has zero issue-linked items")
