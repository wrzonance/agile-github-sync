"""Regression coverage for retired GitHub issues (NOT_PLANNED/DUPLICATE)."""
from __future__ import annotations

import json
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ghkit  # noqa: E402
import sync  # noqa: E402
from stages import blocked_reason  # noqa: E402


def _github_issue(number: int, state_reason: str) -> dict:
    return {
        "number": number,
        "title": f"issue {number}",
        "state": "CLOSED",
        "stateReason": state_reason,
        "labels": [],
        "milestone": None,
        "assignees": [],
        "url": f"https://github.com/acme/repo/issues/{number}",
    }


def _config(tmp_path) -> dict:
    return {
        "token": "token",
        "host": "example.leankit.com",
        "board_id": "42",
        "target_repo_path": tmp_path,
        "label_sync_ignore": frozenset(),
        "stage_lane_map": {},
        "gh_project": {},
    }


def _run_main(tmp_path, monkeypatch, raw_issues, cards, blocked_by=None):
    monkeypatch.setattr(
        ghkit,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=json.dumps(raw_issues)),
    )
    stack = ExitStack()
    stack.enter_context(patch("ghkit.repo_name", return_value="acme/repo"))
    stack.enter_context(patch("ghkit.open_pr_issue_numbers", return_value=set()))
    stack.enter_context(patch("ghkit.blocked_by_map", return_value=blocked_by or {}))
    stack.enter_context(patch("ghproject.configured", return_value=False))
    stack.enter_context(patch("agileplace.board_layout", return_value=[]))
    stack.enter_context(patch("agileplace.list_cards", return_value=cards))
    create_card = stack.enter_context(patch("agileplace.create_card", return_value={}))
    patch_card = stack.enter_context(patch("agileplace.patch_card"))
    with stack, patch("sync.env_config", return_value=_config(tmp_path)), \
         patch("sync.STATE_FILE", tmp_path / ".sync-state.json"), \
         patch("sys.argv", ["sync.py"]):
        sync.main()
    return create_card, patch_card


def test_retired_issues_remain_known_done_blockers(monkeypatch):
    raw_issues = [
        _github_issue(10, "not_planned"),
        _github_issue(11, "DUPLICATE"),
    ]
    monkeypatch.setattr(
        ghkit,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=json.dumps(raw_issues)),
    )

    issues = ghkit.list_issues({})
    project_status = {issue["url"]: "Backlog" for issue in issues}
    stage_by_number = {
        issue["number"]: sync.resolve_issue_stage(issue, project_status)
        for issue in issues
    }

    assert [issue["state_reason"] for issue in issues] == ["NOT_PLANNED", "DUPLICATE"]
    assert stage_by_number == {10: "Done", 11: "Done"}
    assert blocked_reason([10, 11], stage_by_number) is None


def test_retired_issue_without_card_is_not_created(tmp_path, monkeypatch):
    create_card, patch_card = _run_main(
        tmp_path,
        monkeypatch,
        [_github_issue(10, "NOT_PLANNED")],
        cards=[],
    )

    create_card.assert_not_called()
    patch_card.assert_not_called()
