"""Unit tests for issue #65 Task 1/7: ghkit.edit_issue_body() (the GitHub-side client-boundary
write for description sync) and list_issues()'s null-safe 'body' field addition.

edit_issue_body follows the exact dry-run/apply/boundary-validation idiom already established by
create_issue/edit_label: apply=False makes zero calls to run() and returns False; apply=True pipes
the body through run()'s stdin `input=` passthrough (never argv interpolation) and returns True on
success; a malformed `number`/`body` raises ValueError before any I/O.

Run: pytest -q tests/test_ghkit_edit_issue_body.py
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


# --- dry-run: zero calls to the transport boundary, returns False ------------

def test_edit_issue_body_dry_run_makes_zero_run_calls_and_returns_false(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(ghkit, "run", lambda *a, **k: calls.append((a, k)))

    result = ghkit.edit_issue_body({}, False, 42, "new body")

    assert result is False
    assert calls == []
    assert capsys.readouterr().out.startswith("DRY")


def test_edit_issue_body_dry_run_prints_issue_number(monkeypatch, capsys):
    monkeypatch.setattr(ghkit, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call run")))

    ghkit.edit_issue_body({}, False, 42, "body")

    assert "42" in capsys.readouterr().out


# --- apply=True: exact argv shape + stdin body passthrough, returns True -----

def test_edit_issue_body_apply_calls_run_with_body_file_stdin_and_input_kwarg(monkeypatch):
    captured = {}

    def fake_run(cfg, args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return Mock(stdout="")

    monkeypatch.setattr(ghkit, "run", fake_run)

    result = ghkit.edit_issue_body({}, True, 42, "the new body")

    args = captured["args"]
    assert args[:2] == ["issue", "edit"]
    assert args[2] == "42"
    assert "--body-file" in args and args[args.index("--body-file") + 1] == "-"
    assert captured["kwargs"].get("input") == "the new body"
    assert result is True


def test_edit_issue_body_calls_run_exactly_once(monkeypatch):
    calls = []

    def fake_run(cfg, args, **kwargs):
        calls.append((args, kwargs))
        return Mock(stdout="")

    monkeypatch.setattr(ghkit, "run", fake_run)

    ghkit.edit_issue_body({}, True, 42, "body")

    assert len(calls) == 1


def test_edit_issue_body_accepts_empty_string_body(monkeypatch):
    """Clearing a description entirely is a legitimate write -- an empty-string body must not be
    rejected by the boundary validation."""
    captured = {}
    monkeypatch.setattr(ghkit, "run",
                        lambda cfg, args, **k: (captured.update(k), Mock(stdout=""))[1])

    result = ghkit.edit_issue_body({}, True, 1, "")

    assert result is True
    assert captured.get("input") == ""


# --- apply=True: transport failures propagate uncaught -----------------------

@pytest.mark.parametrize("exc", [
    subprocess.CalledProcessError(returncode=1, cmd=["gh"]),
    subprocess.TimeoutExpired(cmd=["gh"], timeout=60),
])
def test_edit_issue_body_propagates_run_failures_uncaught(monkeypatch, exc):
    def fake_run(cfg, args, **k):
        raise exc

    monkeypatch.setattr(ghkit, "run", fake_run)

    with pytest.raises(type(exc)):
        ghkit.edit_issue_body({}, True, 42, "body")


# --- number/body validated at the boundary, before any I/O -------------------

@pytest.mark.parametrize("number", [None, "42", 0, -1, 4.2, True])
@pytest.mark.parametrize("apply", [True, False])
def test_edit_issue_body_rejects_an_unusable_number_before_reaching_run(monkeypatch, number, apply):
    monkeypatch.setattr(ghkit, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not call run for an unusable number")))

    with pytest.raises(ValueError, match="number"):
        ghkit.edit_issue_body({}, apply, number, "body")


@pytest.mark.parametrize("body", [None, 42, ["not", "a", "string"]])
@pytest.mark.parametrize("apply", [True, False])
def test_edit_issue_body_rejects_a_non_string_body_before_reaching_run(monkeypatch, body, apply):
    monkeypatch.setattr(ghkit, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not call run for a non-string body")))

    with pytest.raises(ValueError, match="body"):
        ghkit.edit_issue_body({}, apply, 1, body)


# --- list_issues(): null-safe 'body' field addition (issue #65 struct #5) ----

def _raw_issue(**overrides) -> dict:
    base = {
        "number": 1, "title": "T", "state": "OPEN", "stateReason": "",
        "labels": [], "milestone": None, "assignees": [],
        "url": "https://github.com/o/r/issues/1",
    }
    base.update(overrides)
    return base


def test_list_issues_includes_body_when_present(monkeypatch):
    monkeypatch.setattr(ghkit, "run",
                        lambda *a, **k: Mock(stdout=json.dumps([_raw_issue(body="the body text")])))

    issues = ghkit.list_issues({})

    assert issues[0]["body"] == "the body text"


def test_list_issues_normalizes_missing_body_to_empty_string(monkeypatch):
    monkeypatch.setattr(ghkit, "run", lambda *a, **k: Mock(stdout=json.dumps([_raw_issue()])))

    issues = ghkit.list_issues({})

    assert issues[0]["body"] == ""


def test_list_issues_normalizes_null_body_to_empty_string(monkeypatch):
    monkeypatch.setattr(ghkit, "run",
                        lambda *a, **k: Mock(stdout=json.dumps([_raw_issue(body=None)])))

    issues = ghkit.list_issues({})

    assert issues[0]["body"] == ""


def test_list_issues_requests_body_field_in_gh_json_flag(monkeypatch):
    captured = {}

    def fake_run(cfg, args, **kwargs):
        captured["args"] = args
        return Mock(stdout="[]")

    monkeypatch.setattr(ghkit, "run", fake_run)

    ghkit.list_issues({})

    json_flag_index = captured["args"].index("--json")
    fields = captured["args"][json_flag_index + 1].split(",")
    assert "body" in fields
