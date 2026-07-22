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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import intake  # noqa: E402
import sync  # noqa: E402

ISSUE_URL = "https://github.com/acme/repo/issues/1"


def _issue():
    return {"number": 1, "title": "widget", "state": "OPEN", "labels": [],
            "milestone": None, "assignees": [], "url": ISSUE_URL}


def _card():
    return {"id": "C1", "version": 1, "customId": "1",
            "externalLink": {"url": ISSUE_URL}, "tags": [],
            "plannedStart": None, "plannedFinish": None, "laneId": "LANE1"}


def _cfg(tmp_path):
    return {
        "token": "tok", "host": "example.leankit.com", "board_id": "42",
        "target_repo_path": tmp_path,
        "label_sync_ignore": frozenset(),
        "stage_lane_map": {"Intake": ["New Requests"]},
        "gh_project": {"owner": "acme", "number": "7", "status_field": "Status",
                       "start_field": "Start", "target_field": "Target"},
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
    call_cfg, call_apply, call_cards, call_lanes, call_stage_map, call_issues = promote_mock.call_args.args
    assert call_apply is True
    assert call_cards == cards
    assert call_lanes == lanes
    assert call_stage_map == {"Intake": ["New Requests"]}
    assert call_issues == issues


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
