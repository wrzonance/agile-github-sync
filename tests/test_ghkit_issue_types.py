"""Unit tests for issue #82 Task 4/9: ghkit.py gains native-issue-type plumbing --

  (a) list_issues() requests `issueType` and normalizes it to `issue_type` (the derived NAME, or
      None for a native Task / no type set) -- spike-confirmed shape: gh's `issueType` field is an
      object ({"id","name","description","color"}) or null, never a bare string.
  (b) create_issue() gains a trailing optional `issue_type` param, boundary-validated against
      GitHub's fixed three-value set (Task/Bug/Feature) BEFORE any subprocess call or dry-run print,
      threaded into the real argv as `--type <value>` and into the dry-run print line. It does NOT
      re-probe org enablement -- that's the caller's job via org_issue_types/
      card_types.validate_reverse_issue_type.
  (c) org_issue_types(cfg) is a new tri-state read (frozenset[str] on success, None on ANY failure)
      mirroring open_pr_issue_numbers/sub_issue_numbers exactly.

Run: pytest -q tests/test_ghkit_issue_types.py
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


# --- list_issues(): issueType -> issue_type -------------------------------

def _raw_issue(number: int, issue_type: dict | None) -> dict:
    return {
        "number": number,
        "title": f"issue {number}",
        "state": "OPEN",
        "stateReason": "",
        "labels": [],
        "milestone": None,
        "assignees": [],
        "url": f"https://github.com/o/r/issues/{number}",
        "issueType": issue_type,
    }


def test_list_issues_normalizes_issue_type_name(monkeypatch):
    raw = [_raw_issue(1, {"id": "IT_1", "name": "Bug", "description": "", "color": "red"})]
    monkeypatch.setattr(ghkit, "run", lambda *a, **k: Mock(stdout=json.dumps(raw)))

    issues = ghkit.list_issues({})

    assert issues[0]["issue_type"] == "Bug"


def test_list_issues_null_issue_type_normalizes_to_none(monkeypatch):
    raw = [_raw_issue(2, None)]
    monkeypatch.setattr(ghkit, "run", lambda *a, **k: Mock(stdout=json.dumps(raw)))

    issues = ghkit.list_issues({})

    assert issues[0]["issue_type"] is None


def test_list_issues_missing_issue_type_key_normalizes_to_none(monkeypatch):
    raw = [_raw_issue(3, None)]
    del raw[0]["issueType"]
    monkeypatch.setattr(ghkit, "run", lambda *a, **k: Mock(stdout=json.dumps(raw)))

    issues = ghkit.list_issues({})

    assert issues[0]["issue_type"] is None


def test_list_issues_requests_issue_type_field(monkeypatch):
    captured = {}

    def fake_run(cfg, args, **k):
        captured["args"] = args
        return Mock(stdout="[]")

    monkeypatch.setattr(ghkit, "run", fake_run)
    ghkit.list_issues({})

    fields = captured["args"][captured["args"].index("--json") + 1]
    assert "issueType" in fields.split(",")


# --- create_issue(): issue_type boundary validation ------------------------

@pytest.mark.parametrize("apply", [True, False])
def test_create_issue_rejects_an_unrecognized_issue_type_before_reaching_run(monkeypatch, apply):
    monkeypatch.setattr(ghkit, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not call run for an invalid issue_type")))

    with pytest.raises(ValueError, match="issue_type"):
        ghkit.create_issue({}, apply, "Title", "body", issue_type="Epic")


@pytest.mark.parametrize("issue_type", ["Task", "Bug", "Feature"])
def test_create_issue_accepts_each_recognized_issue_type(monkeypatch, issue_type):
    monkeypatch.setattr(ghkit, "run", lambda cfg, args, **k: Mock(
        stdout="https://github.com/o/r/issues/1\n"))

    result = ghkit.create_issue({}, True, "Title", "body", issue_type=issue_type)

    assert result == {"number": 1, "url": "https://github.com/o/r/issues/1"}


def test_create_issue_threads_type_flag_into_argv_when_apply(monkeypatch):
    captured = {}

    def fake_run(cfg, args, **k):
        captured["args"] = args
        return Mock(stdout="https://github.com/o/r/issues/1\n")

    monkeypatch.setattr(ghkit, "run", fake_run)

    ghkit.create_issue({}, True, "Title", "body", issue_type="Bug")

    args = captured["args"]
    assert "--type" in args and args[args.index("--type") + 1] == "Bug"


def test_create_issue_omits_type_flag_when_issue_type_is_none(monkeypatch):
    captured = {}

    def fake_run(cfg, args, **k):
        captured["args"] = args
        return Mock(stdout="https://github.com/o/r/issues/1\n")

    monkeypatch.setattr(ghkit, "run", fake_run)

    ghkit.create_issue({}, True, "Title", "body")

    assert "--type" not in captured["args"]


def test_create_issue_dry_run_prints_type_when_given(monkeypatch, capsys):
    monkeypatch.setattr(ghkit, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not call run")))

    result = ghkit.create_issue({}, False, "Title", "body", issue_type="Feature")

    assert result is None
    out = capsys.readouterr().out
    assert "--type" in out and "Feature" in out


def test_create_issue_dry_run_omits_type_when_not_given(monkeypatch, capsys):
    monkeypatch.setattr(ghkit, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not call run")))

    ghkit.create_issue({}, False, "Title", "body")

    assert "--type" not in capsys.readouterr().out


def test_create_issue_never_reprobes_org_enablement(monkeypatch):
    """create_issue's own boundary check is a fixed-literal schema check, not an org-enablement
    probe -- org_issue_types is never called from inside create_issue."""
    monkeypatch.setattr(ghkit, "org_issue_types", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("create_issue must not call org_issue_types itself")))
    monkeypatch.setattr(ghkit, "run", lambda cfg, args, **k: Mock(
        stdout="https://github.com/o/r/issues/1\n"))

    ghkit.create_issue({}, True, "Title", "body", issue_type="Bug")


# --- org_issue_types(): tri-state, mirrors open_pr_issue_numbers -----------

def _stub_repo_context(monkeypatch):
    monkeypatch.setattr(ghkit, "_repo_context", lambda cfg: ghkit.RepoContext(
        owner="acme", name="repo", host="github.com"))


def test_org_issue_types_returns_frozenset_of_names_on_success(monkeypatch):
    _stub_repo_context(monkeypatch)
    monkeypatch.setattr(ghkit, "run", lambda cfg, args, **k: Mock(stdout=json.dumps([
        {"id": 1, "name": "Bug", "description": "", "color": "red"},
        {"id": 2, "name": "Feature", "description": "", "color": "blue"},
    ])))

    result = ghkit.org_issue_types({})

    assert result == frozenset({"Bug", "Feature"})


def test_org_issue_types_returns_empty_frozenset_on_genuine_zero_types(monkeypatch):
    _stub_repo_context(monkeypatch)
    monkeypatch.setattr(ghkit, "run", lambda cfg, args, **k: Mock(stdout="[]"))

    result = ghkit.org_issue_types({})

    assert result == frozenset()
    assert result is not None


def test_org_issue_types_returns_none_when_repo_context_unavailable(monkeypatch):
    monkeypatch.setattr(ghkit, "_repo_context", lambda cfg: None)
    monkeypatch.setattr(ghkit, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not call run without repo context")))

    assert ghkit.org_issue_types({}) is None


@pytest.mark.parametrize("exc", [
    subprocess.CalledProcessError(returncode=1, cmd=["gh"]),
    subprocess.TimeoutExpired(cmd=["gh"], timeout=60),
    json.JSONDecodeError("bad json", "", 0),
])
def test_org_issue_types_returns_none_on_any_read_failure(monkeypatch, exc):
    _stub_repo_context(monkeypatch)

    def fake_run(cfg, args, **k):
        raise exc

    monkeypatch.setattr(ghkit, "run", fake_run)
    assert ghkit.org_issue_types({}) is None


def test_org_issue_types_returns_none_on_non_list_response(monkeypatch):
    _stub_repo_context(monkeypatch)
    monkeypatch.setattr(ghkit, "run", lambda cfg, args, **k: Mock(
        stdout=json.dumps({"not": "a list"})))

    assert ghkit.org_issue_types({}) is None


def test_org_issue_types_skips_malformed_entries_without_raising(monkeypatch):
    _stub_repo_context(monkeypatch)
    monkeypatch.setattr(ghkit, "run", lambda cfg, args, **k: Mock(stdout=json.dumps([
        {"id": 1, "name": "Bug"},
        "not-a-dict",
        {"id": 2, "name": 123},
        {"id": 3},
    ])))

    result = ghkit.org_issue_types({})

    assert result == frozenset({"Bug"})


def test_org_issue_types_requests_expected_endpoint(monkeypatch):
    _stub_repo_context(monkeypatch)
    captured = {}

    def fake_run(cfg, args, **k):
        captured["args"] = args
        return Mock(stdout="[]")

    monkeypatch.setattr(ghkit, "run", fake_run)
    ghkit.org_issue_types({})

    assert captured["args"][0] == "api"
    assert "orgs/acme/issue-types" in captured["args"]
