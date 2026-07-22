"""Unit tests for issue #62 Task 3/8: ghkit.create_issue() -- the Intake feature's issue-creation
call, gated by the same dry-run idiom as edit_label/set_milestone and piping the body through the
new run(..., input=...) stdin passthrough (Task 2) rather than interpolating it into argv.

These tests pin the boundary contract:

  (a) apply=False performs ZERO calls to ghkit.run (mirroring
      test_edit_label_dry_run_still_works_for_safe_labels's own low-level-boundary style), prints
      a DRY line, and returns None -- no plan is silently half-executed.
  (b) apply=True calls ghkit.run exactly once with `gh issue create --title <title> --body-file -`
      and the body passed via the `input=` kwarg (never interpolated into argv, so a body
      containing shell metacharacters or gh flag-like text can never be misparsed).
  (c) create_issue never passes --type -- API-VALIDATION.md records `gh issue create --type` as
      non-atomic (creates the issue, THEN fails, so a blind retry double-creates), so the Intake
      feature must never touch that flag at all.
  (d) apply=True parses the created issue's number and url out of gh's own stdout (a bare URL) and
      returns {"number": int, "url": str}.
  (e) Any failure from run() (CalledProcessError, TimeoutExpired) propagates UNCAUGHT -- no
      swallowed sentinel -- matching edit_label/set_milestone's own apply=True behavior.

Run: pytest -q tests/test_ghkit_create_issue.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ghkit  # noqa: E402


# --- dry-run: zero calls to the transport boundary --------------------------

def test_create_issue_dry_run_makes_zero_run_calls(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(ghkit, "run", lambda *a, **k: calls.append((a, k)))

    result = ghkit.create_issue({}, False, "New card title", "body text")

    assert result is None
    assert calls == []
    assert capsys.readouterr().out.startswith("DRY")


def test_create_issue_dry_run_prints_title(monkeypatch, capsys):
    monkeypatch.setattr(ghkit, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call run")))

    ghkit.create_issue({}, False, "Some Title Here", "body")

    out = capsys.readouterr().out
    assert "Some Title Here" in out


# --- apply=True: exact argv shape + stdin body passthrough -------------------

def test_create_issue_apply_calls_run_with_body_file_stdin_and_input_kwarg(monkeypatch):
    captured = {}

    def fake_run(cfg, args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return Mock(stdout="https://github.com/owner/repo/issues/42\n")

    monkeypatch.setattr(ghkit, "run", fake_run)

    ghkit.create_issue({}, True, "Card Title", "the body")

    args = captured["args"]
    assert args[:2] == ["issue", "create"]
    assert "--title" in args and args[args.index("--title") + 1] == "Card Title"
    assert "--body-file" in args and args[args.index("--body-file") + 1] == "-"
    assert captured["kwargs"].get("input") == "the body"


def test_create_issue_never_passes_type_flag(monkeypatch):
    captured = {}

    def fake_run(cfg, args, **kwargs):
        captured["args"] = args
        return Mock(stdout="https://github.com/owner/repo/issues/1\n")

    monkeypatch.setattr(ghkit, "run", fake_run)

    ghkit.create_issue({}, True, "Title", "body")

    assert "--type" not in captured["args"]


def test_create_issue_calls_run_exactly_once(monkeypatch):
    calls = []

    def fake_run(cfg, args, **kwargs):
        calls.append((args, kwargs))
        return Mock(stdout="https://github.com/owner/repo/issues/7\n")

    monkeypatch.setattr(ghkit, "run", fake_run)

    ghkit.create_issue({}, True, "Title", "body")

    assert len(calls) == 1


# --- apply=True: number/url parsed from gh's stdout URL ---------------------

def test_create_issue_returns_number_and_url_parsed_from_stdout(monkeypatch):
    monkeypatch.setattr(ghkit, "run", lambda cfg, args, **k: Mock(
        stdout="https://github.com/owner/repo/issues/123\n"))

    result = ghkit.create_issue({}, True, "Title", "body")

    assert result == {"number": 123, "url": "https://github.com/owner/repo/issues/123"}


def test_create_issue_strips_whitespace_from_stdout_url(monkeypatch):
    monkeypatch.setattr(ghkit, "run", lambda cfg, args, **k: Mock(
        stdout="  https://github.com/owner/repo/issues/99  \n"))

    result = ghkit.create_issue({}, True, "Title", "body")

    assert result == {"number": 99, "url": "https://github.com/owner/repo/issues/99"}


# --- apply=True: transport failures propagate uncaught -----------------------

@pytest.mark.parametrize("exc", [
    subprocess.CalledProcessError(returncode=1, cmd=["gh"]),
    subprocess.TimeoutExpired(cmd=["gh"], timeout=60),
])
def test_create_issue_propagates_run_failures_uncaught(monkeypatch, exc):
    def fake_run(cfg, args, **k):
        raise exc

    monkeypatch.setattr(ghkit, "run", fake_run)

    with pytest.raises(type(exc)):
        ghkit.create_issue({}, True, "Title", "body")


# --- title validated at the boundary, before ever reaching subprocess --------
#
# A blank or non-string title would otherwise reach subprocess.Popen unvalidated: `None` raises an
# opaque TypeError ("argv must be str"), and "" produces a CalledProcessError from gh itself -- both
# uncaught by anything upstream (intake.promote's per-candidate loop, sync.main()'s call site), so a
# single bad title would crash the entire sync run. create_issue validates its own input regardless
# of caller behavior (intake._is_candidate now filters blank titles too, but this is this function's
# own boundary contract, not something it may assume its caller already enforced).

@pytest.mark.parametrize("title", [None, "", "   ", 42])
@pytest.mark.parametrize("apply", [True, False])
def test_create_issue_rejects_an_unusable_title_before_reaching_run(monkeypatch, title, apply):
    monkeypatch.setattr(ghkit, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not call run for an unusable title")))

    with pytest.raises(ValueError, match="title"):
        ghkit.create_issue({}, apply, title, "body")
