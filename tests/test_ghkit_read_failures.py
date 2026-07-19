"""Unit tests for issue #14: repo_name/open_pr_issue_numbers must fail closed and never fake a
successful-but-empty read.

Before this change, open_pr_issue_numbers() returned `set()` on BOTH a genuine "no open PR closes
any issue" result AND every read failure (repo-context resolution failing, or the graphql call
raising CalledProcessError/TimeoutExpired/KeyError/TypeError/JSONDecodeError). Callers could not
tell "no open PRs" from "we don't know" -- so a transient read failure looked identical to a real
signal that no issue should be in 'In review', and callers acting on that fabricated negative
would demote/leave-behind cards whose PR was in fact still open.

These tests pin the tri-state contract:

  (a) open_pr_issue_numbers() returns `set[int]` (possibly empty) on a successful read, and
      `None` -- never a fabricated empty set -- on ANY failure (no repo context, CalledProcessError,
      TimeoutExpired, KeyError, TypeError, json.JSONDecodeError all masked to None, none propagate).
  (b) repo_name() never propagates subprocess.TimeoutExpired (or CalledProcessError/
      FileNotFoundError/SystemExit) to the caller -- always returns str | None.

Run: pytest -q tests/test_ghkit_read_failures.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ghkit  # noqa: E402


# --- repo_name(): never propagates, always str | None -----------------------

def test_repo_name_returns_none_on_timeout(monkeypatch):
    def fail(cfg, args, **k):
        raise subprocess.TimeoutExpired(cmd=args, timeout=ghkit.GH_TIMEOUT)

    monkeypatch.setattr(ghkit, "run", fail)
    assert ghkit.repo_name({}) is None


def test_repo_name_returns_none_on_called_process_error(monkeypatch):
    def fail(cfg, args, **k):
        raise subprocess.CalledProcessError(returncode=1, cmd=args)

    monkeypatch.setattr(ghkit, "run", fail)
    assert ghkit.repo_name({}) is None


def test_repo_name_returns_str_on_success(monkeypatch):
    monkeypatch.setattr(ghkit, "run", lambda cfg, args, **k: Mock(stdout="acme/widgets\n"))
    assert ghkit.repo_name({}) == "acme/widgets"


# --- open_pr_issue_numbers(): tri-state, never a fabricated empty set -------

def test_open_pr_issue_numbers_returns_none_on_no_repo_context(monkeypatch):
    monkeypatch.setattr(ghkit, "_repo_context", lambda cfg: None)
    called = []
    monkeypatch.setattr(ghkit, "run", lambda cfg, args, **k: called.append(args))

    result = ghkit.open_pr_issue_numbers({})

    assert result is None
    assert called == []  # no gh api call attempted


@pytest.mark.parametrize("exc", [
    subprocess.CalledProcessError(returncode=1, cmd=["gh"]),
    subprocess.TimeoutExpired(cmd=["gh"], timeout=60),
    KeyError("data"),
    TypeError("not subscriptable"),
    json.JSONDecodeError("bad json", "", 0),
])
def test_open_pr_issue_numbers_returns_none_on_any_read_failure(monkeypatch, exc):
    monkeypatch.setattr(ghkit, "_repo_context",
                        lambda cfg: ghkit.RepoContext(owner="acme", name="widgets", host="github.com"))

    def fake_run(cfg, args, **k):
        raise exc

    monkeypatch.setattr(ghkit, "run", fake_run)
    assert ghkit.open_pr_issue_numbers({}) is None


def test_open_pr_issue_numbers_returns_empty_set_on_genuine_no_open_prs(monkeypatch):
    """A successful read that finds zero open PRs closing any issue must stay a real, distinguishable
    empty set -- not collapse to the same None a failed read now returns."""
    monkeypatch.setattr(ghkit, "_repo_context",
                        lambda cfg: ghkit.RepoContext(owner="acme", name="widgets", host="github.com"))
    monkeypatch.setattr(ghkit, "run", lambda cfg, args, **k: Mock(
        stdout=json.dumps({"data": {"repository": {"pullRequests": {"nodes": []}}}})))

    result = ghkit.open_pr_issue_numbers({})

    assert result == set()
    assert result is not None


def test_open_pr_issue_numbers_returns_populated_set_on_success(monkeypatch):
    monkeypatch.setattr(ghkit, "_repo_context",
                        lambda cfg: ghkit.RepoContext(owner="acme", name="widgets", host="github.com"))
    monkeypatch.setattr(ghkit, "run", lambda cfg, args, **k: Mock(stdout=json.dumps({
        "data": {"repository": {"pullRequests": {"nodes": [
            {"closingIssuesReferences": {"nodes": [{"number": 7}, {"number": 9}]}},
        ]}}}
    })))

    assert ghkit.open_pr_issue_numbers({}) == {7, 9}
