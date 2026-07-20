"""Regression coverage for retired GitHub issues (NOT_PLANNED/DUPLICATE)."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

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
