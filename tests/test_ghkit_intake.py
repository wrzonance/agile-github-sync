"""Unit tests for issue #62 Task 2/8: ghkit.run() gains an `input` passthrough kwarg (the stdin
plumbing create_issue needs for `--body-file -`), and ghkit.list_issue_bodies() -- the read the
Intake feature's disqualification/resume logic depends on.

These tests pin two boundary contracts:

  (a) run()'s new `input` kwarg threads straight through to subprocess.run(..., input=...); the
      default (None) leaves every existing call site's behavior unchanged -- no call site passes
      `input` today, and subprocess.run(input=None) is exactly what happens now without the kwarg.
  (b) list_issue_bodies() is tri-state, mirroring open_pr_issue_numbers's own None-on-any-failure
      shape exactly: a list[dict] (possibly empty) on a successful read, and **None** on ANY
      failure (CalledProcessError, TimeoutExpired, JSONDecodeError, or a non-list response) so
      callers can tell "repo genuinely has zero issues" from "we don't know" -- this is the flag
      intake.promote() uses to set prescan_failed=True and skip every write for the run rather than
      act on an incomplete snapshot. `body` is normalized to "" (never None) since gh's JSON field
      omits `body` entirely for a bodyless issue.

Run: pytest -q tests/test_ghkit_intake.py
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


# --- run(): input passthrough ------------------------------------------------

def test_run_passes_input_through_to_subprocess(tmp_path, monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured.update(kwargs)
        return Mock(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    ghkit.run({"target_repo_path": tmp_path}, ["issue", "create"], input="body text")

    assert captured["input"] == "body text"


def test_run_default_input_is_none(tmp_path, monkeypatch):
    """No call site passes `input` today -- the default must stay None so subprocess.run(...) behaves
    exactly as it did before the kwarg existed."""
    captured = {}

    def fake_run(argv, **kwargs):
        captured.update(kwargs)
        return Mock(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    ghkit.run({"target_repo_path": tmp_path}, ["repo", "view"])

    assert captured["input"] is None


# --- list_issue_bodies(): tri-state, mirrors open_pr_issue_numbers ----------

def test_list_issue_bodies_returns_list_on_success(monkeypatch):
    monkeypatch.setattr(ghkit, "run", lambda cfg, args, **k: Mock(stdout=json.dumps([
        {"number": 1, "url": "https://github.com/o/r/issues/1", "state": "OPEN", "body": "hello"},
        {"number": 2, "url": "https://github.com/o/r/issues/2", "state": "CLOSED", "body": None},
    ])))

    result = ghkit.list_issue_bodies({})

    assert result == [
        {"number": 1, "url": "https://github.com/o/r/issues/1", "state": "OPEN", "body": "hello"},
        {"number": 2, "url": "https://github.com/o/r/issues/2", "state": "CLOSED", "body": ""},
    ]


def test_list_issue_bodies_missing_body_key_normalizes_to_empty_string(monkeypatch):
    monkeypatch.setattr(ghkit, "run", lambda cfg, args, **k: Mock(stdout=json.dumps([
        {"number": 3, "url": "https://github.com/o/r/issues/3", "state": "OPEN"},
    ])))

    result = ghkit.list_issue_bodies({})

    assert result == [{"number": 3, "url": "https://github.com/o/r/issues/3", "state": "OPEN", "body": ""}]


def test_list_issue_bodies_returns_empty_list_on_genuine_zero_issues(monkeypatch):
    """A successful read that finds zero issues must stay a real, distinguishable empty list -- not
    collapse to the same None a failed read returns."""
    monkeypatch.setattr(ghkit, "run", lambda cfg, args, **k: Mock(stdout="[]"))

    result = ghkit.list_issue_bodies({})

    assert result == []
    assert result is not None


@pytest.mark.parametrize("exc", [
    subprocess.CalledProcessError(returncode=1, cmd=["gh"]),
    subprocess.TimeoutExpired(cmd=["gh"], timeout=60),
    json.JSONDecodeError("bad json", "", 0),
])
def test_list_issue_bodies_returns_none_on_any_read_failure(monkeypatch, exc):
    def fake_run(cfg, args, **k):
        raise exc

    monkeypatch.setattr(ghkit, "run", fake_run)
    assert ghkit.list_issue_bodies({}) is None


def test_list_issue_bodies_returns_none_on_non_list_response(monkeypatch):
    """gh's own contract is a JSON array for `issue list`; a malformed/unexpected shape (e.g. an
    object) must fail closed rather than raise deep inside a caller that assumes a list."""
    monkeypatch.setattr(ghkit, "run", lambda cfg, args, **k: Mock(stdout=json.dumps({"not": "a list"})))
    assert ghkit.list_issue_bodies({}) is None


def test_list_issue_bodies_requests_expected_fields_and_state_all(monkeypatch):
    captured_args = {}

    def fake_run(cfg, args, **k):
        captured_args["args"] = args
        return Mock(stdout="[]")

    monkeypatch.setattr(ghkit, "run", fake_run)
    ghkit.list_issue_bodies({})

    args = captured_args["args"]
    assert args[:2] == ["issue", "list"]
    assert "--state" in args and args[args.index("--state") + 1] == "all"
    assert "--json" in args
    fields = args[args.index("--json") + 1]
    assert set(fields.split(",")) == {"number", "url", "state", "body"}
