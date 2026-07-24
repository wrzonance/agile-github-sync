"""Run-scoped RepoContext caching (issue #97): one `gh repo view` per run, not per call site."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ghkit  # noqa: E402


def test_repo_context_prefers_run_scoped_value(monkeypatch):
    """cfg['repo_context'] short-circuits _repo_context with zero subprocess spawns."""
    ctx = ghkit.RepoContext(owner="acme", name="repo", host="github.com")
    monkeypatch.setattr(ghkit, "run",
                        Mock(side_effect=AssertionError("gh must not be spawned")))
    assert ghkit._repo_context({"repo_context": ctx}) is ctx


def test_repo_context_resolves_fresh_without_run_scoped_value(monkeypatch):
    payload = json.dumps({"nameWithOwner": "acme/repo", "url": "https://github.com/acme/repo"})
    monkeypatch.setattr(ghkit, "run", Mock(return_value=SimpleNamespace(stdout=payload)))
    ctx = ghkit._repo_context({})
    assert (ctx.owner, ctx.name, ctx.host) == ("acme", "repo", "github.com")


def test_repo_context_ignores_non_context_cache_value(monkeypatch):
    """A malformed cfg['repo_context'] (not a RepoContext) never short-circuits -- the fresh
    resolve path still runs, keeping the fail-closed contract."""
    payload = json.dumps({"nameWithOwner": "acme/repo", "url": "https://github.com/acme/repo"})
    monkeypatch.setattr(ghkit, "run", Mock(return_value=SimpleNamespace(stdout=payload)))
    ctx = ghkit._repo_context({"repo_context": "acme/repo"})
    assert isinstance(ctx, ghkit.RepoContext)


def test_resolve_repo_context_is_the_fresh_resolver(monkeypatch):
    """resolve_repo_context never reads the run-scoped cache -- it IS the resolver main() uses
    to populate it."""
    stale = ghkit.RepoContext(owner="stale", name="stale", host="stale.example")
    payload = json.dumps({"nameWithOwner": "acme/repo", "url": "https://github.com/acme/repo"})
    monkeypatch.setattr(ghkit, "run", Mock(return_value=SimpleNamespace(stdout=payload)))
    ctx = ghkit.resolve_repo_context({"repo_context": stale})
    assert (ctx.owner, ctx.name, ctx.host) == ("acme", "repo", "github.com")
