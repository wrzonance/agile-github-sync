"""Unit tests for issue #15: pinning gh's execution environment and raw-string GraphQL variables.

A stale GH_REPO/GH_HOST in the calling shell or a scheduled task's environment silently retargets
every gh call -- reads AND writes -- onto a repo the user never configured, with every internal
consistency check (repo_name() included) agreeing on the wrong answer. These tests pin:

  (a) run() always scrubs GH_REPO/GH_HOST from the subprocess env, regardless of what's in
      os.environ, while leaving unrelated vars (GH_TOKEN included) untouched and never mutating
      the real os.environ;
  (b) _repo_context() resolves owner/name/host from a single `gh repo view` call and fails closed
      (returns None) on any malformed/missing data;
  (c) open_pr_issue_numbers/sub_issue_numbers/blocked_by_map build their argv with -f (raw string)
      for owner/name, -F (typed) retained for the genuinely Int! `num`, and --hostname threaded
      through from the resolved RepoContext -- short-circuiting to their empty/None contract with
      no gh api call attempted when _repo_context() fails.

Run: pytest -q
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


# --- run(): env scrub -------------------------------------------------------

def test_run_strips_gh_repo_and_gh_host_from_subprocess_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_REPO", "someone/else")
    monkeypatch.setenv("GH_HOST", "stale.example.com")
    monkeypatch.setenv("GH_TOKEN", "tok-123")  # must survive -- it's how auth flows

    captured = {}

    def fake_run(argv, **kwargs):
        captured.update(kwargs)
        return Mock(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    ghkit.run({"target_repo_path": tmp_path}, ["repo", "view"])

    env = captured["env"]
    assert "GH_REPO" not in env
    assert "GH_HOST" not in env
    assert env.get("GH_TOKEN") == "tok-123"       # unrelated var preserved
    # the real process environment itself is never mutated by building the scrubbed copy
    import os
    assert os.environ.get("GH_REPO") == "someone/else"
    assert os.environ.get("GH_HOST") == "stale.example.com"


def test_run_scrubs_even_when_gh_repo_gh_host_absent(tmp_path, monkeypatch):
    """No GH_REPO/GH_HOST set at all -> env dict simply lacks them too; scrub is unconditional,
    not an if-present branch."""
    monkeypatch.delenv("GH_REPO", raising=False)
    monkeypatch.delenv("GH_HOST", raising=False)
    monkeypatch.setenv("GH_TOKEN", "tok-456")

    captured = {}
    monkeypatch.setattr(subprocess, "run",
                        lambda argv, **kwargs: (captured.update(kwargs), Mock(stdout=""))[1])

    ghkit.run({"target_repo_path": tmp_path}, ["repo", "view"])

    assert "GH_REPO" not in captured["env"]
    assert "GH_HOST" not in captured["env"]
    assert captured["env"].get("GH_TOKEN") == "tok-456"


# --- _repo_context(): success + fail-closed paths ---------------------------

def _mock_repo_view(monkeypatch, stdout_obj):
    monkeypatch.setattr(ghkit, "run",
                        lambda cfg, args, **k: Mock(stdout=json.dumps(stdout_obj)))


def test_repo_context_resolves_owner_name_host(monkeypatch):
    _mock_repo_view(monkeypatch, {
        "nameWithOwner": "wrzonance/agile-github-sync",
        "url": "https://github.com/wrzonance/agile-github-sync",
    })
    ctx = ghkit._repo_context({})
    assert ctx == ghkit.RepoContext(owner="wrzonance", name="agile-github-sync", host="github.com")


def test_repo_context_resolves_ghes_host(monkeypatch):
    _mock_repo_view(monkeypatch, {
        "nameWithOwner": "acme/widgets",
        "url": "https://ghes.acme.internal/acme/widgets",
    })
    ctx = ghkit._repo_context({})
    assert ctx == ghkit.RepoContext(owner="acme", name="widgets", host="ghes.acme.internal")


def test_repo_context_none_on_malformed_json(monkeypatch):
    monkeypatch.setattr(ghkit, "run", lambda cfg, args, **k: Mock(stdout="not json"))
    assert ghkit._repo_context({}) is None


def test_repo_context_none_on_missing_url(monkeypatch):
    _mock_repo_view(monkeypatch, {"nameWithOwner": "acme/widgets"})
    assert ghkit._repo_context({}) is None


def test_repo_context_none_on_missing_name_with_owner(monkeypatch):
    _mock_repo_view(monkeypatch, {"url": "https://github.com/acme/widgets"})
    assert ghkit._repo_context({}) is None


def test_repo_context_none_on_unparseable_host(monkeypatch):
    _mock_repo_view(monkeypatch, {"nameWithOwner": "acme/widgets", "url": "not-a-url"})
    assert ghkit._repo_context({}) is None


def test_repo_context_none_on_name_with_owner_missing_slash(monkeypatch):
    _mock_repo_view(monkeypatch, {"nameWithOwner": "acmewidgets",
                                  "url": "https://github.com/acme/widgets"})
    assert ghkit._repo_context({}) is None


def test_repo_context_none_on_name_with_owner_extra_slash(monkeypatch):
    _mock_repo_view(monkeypatch, {"nameWithOwner": "acme/widgets/extra",
                                  "url": "https://github.com/acme/widgets"})
    assert ghkit._repo_context({}) is None


def test_repo_context_none_on_empty_owner(monkeypatch):
    """nameWithOwner="/widgets" has exactly one '/' (passes the count check) but an empty owner --
    must still fail closed rather than yielding RepoContext(owner="", ...)."""
    _mock_repo_view(monkeypatch, {"nameWithOwner": "/widgets",
                                  "url": "https://github.com/acme/widgets"})
    assert ghkit._repo_context({}) is None


def test_repo_context_none_on_empty_name(monkeypatch):
    """nameWithOwner="acme/" has exactly one '/' (passes the count check) but an empty name --
    must still fail closed rather than yielding RepoContext(name="", ...)."""
    _mock_repo_view(monkeypatch, {"nameWithOwner": "acme/",
                                  "url": "https://github.com/acme/widgets"})
    assert ghkit._repo_context({}) is None


def test_repo_context_none_on_gh_call_failure(monkeypatch):
    def fail(cfg, args, **k):
        raise subprocess.CalledProcessError(1, args, stderr="not a git repo")
    monkeypatch.setattr(ghkit, "run", fail)
    assert ghkit._repo_context({}) is None


def test_repo_context_none_on_timeout(monkeypatch):
    def fail(cfg, args, **k):
        raise subprocess.TimeoutExpired(cmd=args, timeout=60)
    monkeypatch.setattr(ghkit, "run", fail)
    assert ghkit._repo_context({}) is None


# --- open_pr_issue_numbers: argv + short-circuit ----------------------------

def test_open_pr_issue_numbers_uses_raw_string_vars_and_hostname(monkeypatch):
    monkeypatch.setattr(ghkit, "_repo_context",
                        lambda cfg: ghkit.RepoContext(owner="acme", name="widgets", host="github.com"))
    captured_args = {}

    def fake_run(cfg, args, **k):
        captured_args["args"] = args
        return Mock(stdout=json.dumps({"data": {"repository": {"pullRequests": {"nodes": []}}}}))

    monkeypatch.setattr(ghkit, "run", fake_run)
    result = ghkit.open_pr_issue_numbers({})

    assert result == set()
    args = captured_args["args"]
    assert "--hostname" in args and args[args.index("--hostname") + 1] == "github.com"
    assert "-f" in args and "owner=acme" in args
    assert "-f" in args and "name=widgets" in args
    assert "-F" not in args  # no Int! variables in this query


def test_open_pr_issue_numbers_short_circuits_on_no_repo_context(monkeypatch):
    monkeypatch.setattr(ghkit, "_repo_context", lambda cfg: None)
    called = []
    monkeypatch.setattr(ghkit, "run", lambda cfg, args, **k: called.append(args))
    assert ghkit.open_pr_issue_numbers({}) == set()
    assert called == []  # no gh api call attempted


# --- sub_issue_numbers: argv (num stays -F) + short-circuit -----------------

def test_sub_issue_numbers_uses_raw_string_for_owner_name_typed_for_num(monkeypatch):
    monkeypatch.setattr(ghkit, "_repo_context",
                        lambda cfg: ghkit.RepoContext(owner="acme", name="widgets", host="github.com"))
    captured_args = {}

    def fake_run(cfg, args, **k):
        captured_args["args"] = args
        return Mock(stdout=json.dumps(
            {"data": {"repository": {"issue": {"subIssues": {"nodes": [{"number": 7}]}}}}}))

    monkeypatch.setattr(ghkit, "run", fake_run)
    result = ghkit.sub_issue_numbers({}, 42)

    assert result == [7]
    args = captured_args["args"]
    assert "--hostname" in args and args[args.index("--hostname") + 1] == "github.com"
    assert "owner=acme" in args and args[args.index("owner=acme") - 1] == "-f"
    assert "name=widgets" in args and args[args.index("name=widgets") - 1] == "-f"
    assert "num=42" in args and args[args.index("num=42") - 1] == "-F"  # Int! stays typed


def test_sub_issue_numbers_short_circuits_on_no_repo_context(monkeypatch):
    monkeypatch.setattr(ghkit, "_repo_context", lambda cfg: None)
    called = []
    monkeypatch.setattr(ghkit, "run", lambda cfg, args, **k: called.append(args))
    assert ghkit.sub_issue_numbers({}, 42) is None
    assert called == []


# --- blocked_by_map: argv + short-circuit -----------------------------------

def test_blocked_by_map_uses_hostname_and_resolved_owner_name(monkeypatch):
    monkeypatch.setattr(ghkit, "_repo_context",
                        lambda cfg: ghkit.RepoContext(owner="acme", name="widgets", host="ghes.acme.internal"))
    captured_args = {}

    def fake_run(cfg, args, **k):
        captured_args["args"] = args
        return Mock(stdout="3\n")

    monkeypatch.setattr(ghkit, "run", fake_run)
    result = ghkit.blocked_by_map({}, [10])

    assert result == {10: [3]}
    args = captured_args["args"]
    assert "--hostname" in args and args[args.index("--hostname") + 1] == "ghes.acme.internal"
    assert "repos/acme/widgets/issues/10/dependencies/blocked_by" in args


def test_blocked_by_map_short_circuits_on_no_repo_context(monkeypatch):
    monkeypatch.setattr(ghkit, "_repo_context", lambda cfg: None)
    called = []
    monkeypatch.setattr(ghkit, "run", lambda cfg, args, **k: called.append(args))
    assert ghkit.blocked_by_map({}, [1, 2]) is None
    assert called == []
