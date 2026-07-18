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


def _mock_io(card, items_and_raw_return, field_meta_return):
    """ExitStack of patches covering every I/O boundary main() touches for one run. Returns the stack
    plus the ghkit.run and agileplace.patch_card mocks (for call-site assertions)."""
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
    stack.enter_context(patch("agileplace.list_cards", return_value=[card]))
    patch_card_mock = stack.enter_context(patch("agileplace.patch_card"))
    return stack, run_mock, patch_card_mock


def _run_main_once(tmp_path, items_and_raw_return, field_meta_return=None):
    cfg = _cfg(tmp_path)
    state_file = tmp_path / ".sync-state.json"
    card = _card()
    stack, run_mock, patch_card_mock = _mock_io(card, items_and_raw_return, field_meta_return)
    with stack, patch("sync.env_config", return_value=cfg), patch("sync.STATE_FILE", state_file), \
         patch("sys.argv", ["sync.py", "--apply"]):
        sync.main()
    return json.loads(state_file.read_text(encoding="utf-8")), run_mock, patch_card_mock


# --- merge-base advance invariant, end-to-end through two real main() runs -----------------------

def test_merge_base_advances_only_after_confirmed_write_across_two_main_runs(tmp_path, capsys):
    raw_items = [{"id": "PVTI_1", "content": {"url": ISSUE_URL}, "Start": "2026-01-01", "Target": None}]
    field_meta = _field_meta()

    # Run 1: item_id missing -> ghproject.set_project_date's own guard skips the write.
    parsed_no_item_id = {ISSUE_URL: {"item_id": None, "number": 1, "status": "In progress",
                                     "start": "2026-01-01", "target": None}}
    state_after_1, _, _ = _run_main_once(tmp_path, (parsed_no_item_id, raw_items), field_meta)
    assert "start" not in state_after_1["issues"][ISSUE_URL], (
        "merge base must NOT advance when the GH-side write was skipped (item_id missing)")

    # Run 2: same GitHub-side date, item_id now present -> the write is confirmed.
    parsed_with_item_id = {ISSUE_URL: {"item_id": "PVTI_1", "number": 1, "status": "In progress",
                                       "start": "2026-01-01", "target": None}}
    state_after_2, run_mock, _ = _run_main_once(tmp_path, (parsed_with_item_id, raw_items), field_meta)
    assert state_after_2["issues"][ISSUE_URL]["start"] == "2026-02-01", (
        "merge base must advance once the GH-side write is confirmed")
    run_mock.assert_called_once()  # the real ghproject.set_project_date issued exactly one gh write


# --- unmatched_kinds: computed in main(), WARNs, and gates sync_dates end-to-end -------------------

def test_unmatched_kinds_warns_and_skips_both_date_kinds(tmp_path, capsys):
    # No raw row exposes ANY candidate key for "Start" or "Target" -> both kinds are unmatched.
    raw_items = [{"id": "PVTI_1", "content": {"url": ISSUE_URL}, "unrelated": "x"}]
    parsed = {ISSUE_URL: {"item_id": "PVTI_1", "number": 1, "status": "In progress",
                          "start": None, "target": None}}
    state, run_mock, patch_card_mock = _run_main_once(tmp_path, (parsed, raw_items), _field_meta())

    out = capsys.readouterr().out
    assert "WARN  Projects v2 'start' field resolved but no item ever exposed a matching key" in out
    assert "WARN  Projects v2 'target' field resolved but no item ever exposed a matching key" in out
    run_mock.assert_not_called()               # no GH date write attempted for either skipped kind
    patch_card_mock.assert_not_called()        # no AgilePlace date write queued either
    assert "start" not in state["issues"][ISSUE_URL]
    assert "target" not in state["issues"][ISSUE_URL]


# --- guarded call site: frozenset() when field_meta is falsy, no crash, no WARN --------------------

def test_no_warn_and_no_crash_when_field_meta_is_none(tmp_path, capsys):
    parsed = {ISSUE_URL: {"item_id": "PVTI_1", "number": 1, "status": "In progress",
                          "start": "2026-01-01", "target": None}}
    raw_items = [{"id": "PVTI_1", "content": {"url": ISSUE_URL}}]  # would flag both kinds if checked
    state, run_mock, patch_card_mock = _run_main_once(tmp_path, (parsed, raw_items), field_meta_return=None)

    out = capsys.readouterr().out
    assert "WARN  Projects v2" not in out
    run_mock.assert_not_called()
    patch_card_mock.assert_not_called()
    assert "start" not in state["issues"][ISSUE_URL]
