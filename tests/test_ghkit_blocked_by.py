"""Repository-qualification tests for GitHub issue dependencies."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ghkit  # noqa: E402


def _context() -> ghkit.RepoContext:
    return ghkit.RepoContext(owner="acme", name="widgets", host="github.com")


def test_blocked_by_map_skips_cross_repo_blocker_with_identifying_warning(monkeypatch, capsys):
    """A foreign #7 must not be mistaken for acme/widgets#7."""
    blockers = [[
        {
            "number": 3,
            "repository_url": "https://api.github.com/repos/acme/widgets",
        },
        {
            "number": 7,
            "repository_url": "https://api.github.com/repos/other/roadmap",
        },
    ]]
    monkeypatch.setattr(ghkit, "_repo_context", lambda cfg: _context())
    monkeypatch.setattr(ghkit, "run", lambda cfg, args, **kwargs: Mock(
        stdout=json.dumps(blockers)))

    assert ghkit.blocked_by_map({}, [10]) == {10: [3]}

    warning = capsys.readouterr().out
    assert "WARN" in warning
    assert "other/roadmap#7" in warning
    assert "acme/widgets" in warning


def test_blocked_by_map_accepts_embedded_repository_identity(monkeypatch, capsys):
    blockers = [[{
        "number": 4,
        "repository": {"nameWithOwner": "Acme/Widgets"},
    }]]
    monkeypatch.setattr(ghkit, "_repo_context", lambda cfg: _context())
    monkeypatch.setattr(ghkit, "run", lambda cfg, args, **kwargs: Mock(
        stdout=json.dumps(blockers)))

    assert ghkit.blocked_by_map({}, [10]) == {10: [4]}
    assert capsys.readouterr().out == ""


def test_blocked_by_map_fails_closed_when_blocker_has_no_repository_identity(monkeypatch):
    monkeypatch.setattr(ghkit, "_repo_context", lambda cfg: _context())
    monkeypatch.setattr(ghkit, "run", lambda cfg, args, **kwargs: Mock(
        stdout=json.dumps([[{"number": 4}]])))

    assert ghkit.blocked_by_map({}, [10]) is None
