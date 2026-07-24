"""Unit tests for issue #66 Task 3/8: ghkit.py gains GitHub-side issue-comment I/O --
list_issue_comments/create_issue_comment/edit_issue_comment/delete_issue_comment.

Findings #5/#6 (design doc 2026-07-23-issue-66-comment-sync-design.md) fixed a spike divergence:
every write here goes through `gh api ... --input -` with a json.dumps-built body piped through
run()'s existing `input=` stdin passthrough -- never `gh issue comment` and never an
argv-interpolated `-f body={body}` flag, which would both mis-parse shell-meaningful bodies and
diverge from create_issue/edit_issue_body's own stdin idiom.

Contract pinned here:

  list_issue_comments(cfg, number) -> list[dict] | None
    Tri-state, mirroring blocked_by_map/org_issue_types exactly: a list (possibly empty) on
    success, None on ANY failure (no repo context, gh error, timeout, malformed response/item).
    Boundary-validates `number` (positive, non-bool int) before any I/O.

  create_issue_comment(cfg, apply, number, body) -> int | None
    apply=False -> DRY print, zero run() calls, None. apply=True -> POST via `gh api --input -`,
    returns the new comment's id (int); a response that can't be parsed raises.

  edit_issue_comment(cfg, apply, comment_id, body) -> bool
    apply=False -> DRY print, zero run() calls, False. apply=True -> PATCH via the same
    `--input -`/json.dumps/stdin mechanism, returns True only on an actual write.

  delete_issue_comment(cfg, apply, comment_id) -> bool
    apply=False -> DRY print, zero run() calls, False. apply=True -> DELETE (no body), returns
    True only on an actual write.

Run: pytest -q tests/test_ghkit_comments.py
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


def _context() -> ghkit.RepoContext:
    return ghkit.RepoContext(owner="acme", name="widgets", host="github.com")


def _stub_repo_context(monkeypatch, ctx: ghkit.RepoContext | None = None) -> None:
    monkeypatch.setattr(ghkit, "_repo_context", lambda cfg: ctx if ctx is not None else _context())


def _raw_comment(**overrides) -> dict:
    base = {
        "id": 501,
        "user": {"login": "octocat"},
        "body": "hello",
        "created_at": "2026-07-01T00:00:00Z",
        "updated_at": "2026-07-02T00:00:00Z",
    }
    base.update(overrides)
    return base


# --- list_issue_comments(): boundary validation ------------------------------

@pytest.mark.parametrize("number", [None, "42", 0, -1, 4.2, True])
def test_list_issue_comments_rejects_an_unusable_number_before_reaching_run(monkeypatch, number):
    monkeypatch.setattr(ghkit, "_repo_context", lambda cfg: (_ for _ in ()).throw(
        AssertionError("must not resolve repo context for an unusable number")))
    monkeypatch.setattr(ghkit, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not call run for an unusable number")))

    with pytest.raises(ValueError, match="number"):
        ghkit.list_issue_comments({}, number)


# --- list_issue_comments(): tri-state, mirrors blocked_by_map/org_issue_types ----

def test_list_issue_comments_returns_none_when_repo_context_unavailable(monkeypatch):
    monkeypatch.setattr(ghkit, "_repo_context", lambda cfg: None)
    monkeypatch.setattr(ghkit, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not call run without repo context")))

    assert ghkit.list_issue_comments({}, 42) is None


def test_list_issue_comments_returns_normalized_comments_on_success(monkeypatch):
    _stub_repo_context(monkeypatch)
    pages = [[_raw_comment(id=1, body="first"), _raw_comment(id=2, body="second")]]
    monkeypatch.setattr(ghkit, "run", lambda cfg, args, **k: Mock(stdout=json.dumps(pages)))

    result = ghkit.list_issue_comments({}, 42)

    assert result == [
        {"id": 1, "author": "octocat", "body": "first",
         "created": "2026-07-01T00:00:00Z", "edited": "2026-07-02T00:00:00Z"},
        {"id": 2, "author": "octocat", "body": "second",
         "created": "2026-07-01T00:00:00Z", "edited": "2026-07-02T00:00:00Z"},
    ]


def test_list_issue_comments_returns_empty_list_for_genuinely_commentless_issue(monkeypatch):
    _stub_repo_context(monkeypatch)
    monkeypatch.setattr(ghkit, "run", lambda cfg, args, **k: Mock(stdout=json.dumps([[]])))

    result = ghkit.list_issue_comments({}, 42)

    assert result == []
    assert result is not None


def test_list_issue_comments_normalizes_missing_author_to_none(monkeypatch):
    _stub_repo_context(monkeypatch)
    raw = _raw_comment()
    del raw["user"]
    monkeypatch.setattr(ghkit, "run", lambda cfg, args, **k: Mock(stdout=json.dumps([[raw]])))

    result = ghkit.list_issue_comments({}, 42)

    assert result[0]["author"] is None


def test_list_issue_comments_requests_expected_paginated_endpoint(monkeypatch):
    _stub_repo_context(monkeypatch)
    captured = {}

    def fake_run(cfg, args, **k):
        captured["args"] = args
        return Mock(stdout=json.dumps([[]]))

    monkeypatch.setattr(ghkit, "run", fake_run)
    ghkit.list_issue_comments({}, 42)

    args = captured["args"]
    assert args[0] == "api"
    assert "repos/acme/widgets/issues/42/comments" in args
    assert "--paginate" in args
    assert "--slurp" in args


@pytest.mark.parametrize("exc", [
    subprocess.CalledProcessError(returncode=1, cmd=["gh"]),
    subprocess.TimeoutExpired(cmd=["gh"], timeout=60),
    json.JSONDecodeError("bad json", "", 0),
])
def test_list_issue_comments_returns_none_and_warns_on_read_failure(monkeypatch, capsys, exc):
    _stub_repo_context(monkeypatch)

    def fake_run(cfg, args, **k):
        raise exc

    monkeypatch.setattr(ghkit, "run", fake_run)

    assert ghkit.list_issue_comments({}, 42) is None
    assert "WARN" in capsys.readouterr().err


def test_list_issue_comments_returns_none_on_non_list_of_lists_response(monkeypatch, capsys):
    _stub_repo_context(monkeypatch)
    monkeypatch.setattr(ghkit, "run", lambda cfg, args, **k: Mock(stdout=json.dumps({"not": "pages"})))

    assert ghkit.list_issue_comments({}, 42) is None
    assert "WARN" in capsys.readouterr().err


def test_list_issue_comments_returns_none_on_item_with_missing_id(monkeypatch, capsys):
    _stub_repo_context(monkeypatch)
    raw = _raw_comment()
    del raw["id"]
    monkeypatch.setattr(ghkit, "run", lambda cfg, args, **k: Mock(stdout=json.dumps([[raw]])))

    assert ghkit.list_issue_comments({}, 42) is None
    assert "WARN" in capsys.readouterr().err


# --- create_issue_comment(): dry-run: zero calls to the transport boundary ------

def test_create_issue_comment_dry_run_makes_zero_run_calls_and_returns_none(monkeypatch, capsys):
    monkeypatch.setattr(ghkit, "_repo_context", lambda cfg: (_ for _ in ()).throw(
        AssertionError("must not resolve repo context on a dry run")))
    monkeypatch.setattr(ghkit, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not call run")))

    result = ghkit.create_issue_comment({}, False, 42, "new comment")

    assert result is None
    assert capsys.readouterr().out.startswith("DRY")


# --- create_issue_comment(): apply=True: exact argv + stdin json body -----------

def test_create_issue_comment_apply_posts_via_gh_api_input_stdin(monkeypatch):
    _stub_repo_context(monkeypatch)
    captured = {}

    def fake_run(cfg, args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return Mock(stdout=json.dumps({"id": 999}))

    monkeypatch.setattr(ghkit, "run", fake_run)

    result = ghkit.create_issue_comment({}, True, 42, "the comment body")

    args = captured["args"]
    assert args[0] == "api"
    assert "repos/acme/widgets/issues/42/comments" in args
    assert "--method" in args and args[args.index("--method") + 1] == "POST"
    assert "--input" in args and args[args.index("--input") + 1] == "-"
    assert json.loads(captured["kwargs"]["input"]) == {"body": "the comment body"}
    assert result == 999


def test_create_issue_comment_never_interpolates_body_into_argv(monkeypatch):
    _stub_repo_context(monkeypatch)
    captured = {}

    def fake_run(cfg, args, **kwargs):
        captured["args"] = args
        return Mock(stdout=json.dumps({"id": 1}))

    monkeypatch.setattr(ghkit, "run", fake_run)

    ghkit.create_issue_comment({}, True, 42, "-f body=malicious --evil-flag")

    assert not any("malicious" in a for a in captured["args"])


def test_create_issue_comment_raises_on_unparseable_response(monkeypatch):
    _stub_repo_context(monkeypatch)
    monkeypatch.setattr(ghkit, "run", lambda cfg, args, **k: Mock(stdout=json.dumps({"no": "id"})))

    with pytest.raises(ValueError):
        ghkit.create_issue_comment({}, True, 42, "body")


@pytest.mark.parametrize("exc", [
    subprocess.CalledProcessError(returncode=1, cmd=["gh"]),
    subprocess.TimeoutExpired(cmd=["gh"], timeout=60),
])
def test_create_issue_comment_propagates_run_failures_uncaught(monkeypatch, exc):
    _stub_repo_context(monkeypatch)

    def fake_run(cfg, args, **k):
        raise exc

    monkeypatch.setattr(ghkit, "run", fake_run)

    with pytest.raises(type(exc)):
        ghkit.create_issue_comment({}, True, 42, "body")


def test_create_issue_comment_raises_systemexit_when_repo_context_unavailable(monkeypatch):
    monkeypatch.setattr(ghkit, "_repo_context", lambda cfg: None)
    monkeypatch.setattr(ghkit, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not call run without repo context")))

    with pytest.raises(SystemExit):
        ghkit.create_issue_comment({}, True, 42, "body")


# --- create_issue_comment(): boundary validation --------------------------------

@pytest.mark.parametrize("number", [None, "42", 0, -1, 4.2, True])
@pytest.mark.parametrize("apply", [True, False])
def test_create_issue_comment_rejects_an_unusable_number_before_reaching_run(monkeypatch, number, apply):
    monkeypatch.setattr(ghkit, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not call run for an unusable number")))

    with pytest.raises(ValueError, match="number"):
        ghkit.create_issue_comment({}, apply, number, "body")


@pytest.mark.parametrize("body", [None, 42, ["not", "a", "string"]])
@pytest.mark.parametrize("apply", [True, False])
def test_create_issue_comment_rejects_a_non_string_body_before_reaching_run(monkeypatch, body, apply):
    monkeypatch.setattr(ghkit, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not call run for a non-string body")))

    with pytest.raises(ValueError, match="body"):
        ghkit.create_issue_comment({}, apply, 42, body)


# --- edit_issue_comment(): dry-run ----------------------------------------------

def test_edit_issue_comment_dry_run_makes_zero_run_calls_and_returns_false(monkeypatch, capsys):
    monkeypatch.setattr(ghkit, "_repo_context", lambda cfg: (_ for _ in ()).throw(
        AssertionError("must not resolve repo context on a dry run")))
    monkeypatch.setattr(ghkit, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not call run")))

    result = ghkit.edit_issue_comment({}, False, 501, "updated body")

    assert result is False
    assert capsys.readouterr().out.startswith("DRY")


# --- edit_issue_comment(): apply=True -------------------------------------------

def test_edit_issue_comment_apply_patches_via_gh_api_input_stdin(monkeypatch):
    _stub_repo_context(monkeypatch)
    captured = {}

    def fake_run(cfg, args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return Mock(stdout="")

    monkeypatch.setattr(ghkit, "run", fake_run)

    result = ghkit.edit_issue_comment({}, True, 501, "updated body")

    args = captured["args"]
    assert args[0] == "api"
    assert "repos/acme/widgets/issues/comments/501" in args
    assert "--method" in args and args[args.index("--method") + 1] == "PATCH"
    assert "--input" in args and args[args.index("--input") + 1] == "-"
    assert json.loads(captured["kwargs"]["input"]) == {"body": "updated body"}
    assert result is True


def test_edit_issue_comment_never_uses_dash_f_body_flag(monkeypatch):
    """Fixes the spike's divergent argv-embedded `-f body={body}` (finding #5/#6)."""
    _stub_repo_context(monkeypatch)
    captured = {}

    def fake_run(cfg, args, **kwargs):
        captured["args"] = args
        return Mock(stdout="")

    monkeypatch.setattr(ghkit, "run", fake_run)

    ghkit.edit_issue_comment({}, True, 501, "body")

    assert "-f" not in captured["args"]


@pytest.mark.parametrize("exc", [
    subprocess.CalledProcessError(returncode=1, cmd=["gh"]),
    subprocess.TimeoutExpired(cmd=["gh"], timeout=60),
])
def test_edit_issue_comment_propagates_run_failures_uncaught(monkeypatch, exc):
    _stub_repo_context(monkeypatch)

    def fake_run(cfg, args, **k):
        raise exc

    monkeypatch.setattr(ghkit, "run", fake_run)

    with pytest.raises(type(exc)):
        ghkit.edit_issue_comment({}, True, 501, "body")


def test_edit_issue_comment_raises_systemexit_when_repo_context_unavailable(monkeypatch):
    monkeypatch.setattr(ghkit, "_repo_context", lambda cfg: None)
    monkeypatch.setattr(ghkit, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not call run without repo context")))

    with pytest.raises(SystemExit):
        ghkit.edit_issue_comment({}, True, 501, "body")


# --- edit_issue_comment(): boundary validation ----------------------------------

@pytest.mark.parametrize("comment_id", [None, "501", 0, -1, 4.2, True])
@pytest.mark.parametrize("apply", [True, False])
def test_edit_issue_comment_rejects_an_unusable_comment_id_before_reaching_run(monkeypatch, comment_id, apply):
    monkeypatch.setattr(ghkit, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not call run for an unusable comment_id")))

    with pytest.raises(ValueError, match="comment_id"):
        ghkit.edit_issue_comment({}, apply, comment_id, "body")


@pytest.mark.parametrize("body", [None, 42, ["not", "a", "string"]])
@pytest.mark.parametrize("apply", [True, False])
def test_edit_issue_comment_rejects_a_non_string_body_before_reaching_run(monkeypatch, body, apply):
    monkeypatch.setattr(ghkit, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not call run for a non-string body")))

    with pytest.raises(ValueError, match="body"):
        ghkit.edit_issue_comment({}, apply, 501, body)


# --- delete_issue_comment(): dry-run --------------------------------------------

def test_delete_issue_comment_dry_run_makes_zero_run_calls_and_returns_false(monkeypatch, capsys):
    monkeypatch.setattr(ghkit, "_repo_context", lambda cfg: (_ for _ in ()).throw(
        AssertionError("must not resolve repo context on a dry run")))
    monkeypatch.setattr(ghkit, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not call run")))

    result = ghkit.delete_issue_comment({}, False, 501)

    assert result is False
    assert capsys.readouterr().out.startswith("DRY")


# --- delete_issue_comment(): apply=True -----------------------------------------

def test_delete_issue_comment_apply_deletes_via_gh_api_no_body(monkeypatch):
    _stub_repo_context(monkeypatch)
    captured = {}

    def fake_run(cfg, args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return Mock(stdout="")

    monkeypatch.setattr(ghkit, "run", fake_run)

    result = ghkit.delete_issue_comment({}, True, 501)

    args = captured["args"]
    assert args[0] == "api"
    assert "repos/acme/widgets/issues/comments/501" in args
    assert "--method" in args and args[args.index("--method") + 1] == "DELETE"
    assert captured["kwargs"].get("input") is None
    assert result is True


@pytest.mark.parametrize("exc", [
    subprocess.CalledProcessError(returncode=1, cmd=["gh"]),
    subprocess.TimeoutExpired(cmd=["gh"], timeout=60),
])
def test_delete_issue_comment_propagates_run_failures_uncaught(monkeypatch, exc):
    _stub_repo_context(monkeypatch)

    def fake_run(cfg, args, **k):
        raise exc

    monkeypatch.setattr(ghkit, "run", fake_run)

    with pytest.raises(type(exc)):
        ghkit.delete_issue_comment({}, True, 501)


def test_delete_issue_comment_raises_systemexit_when_repo_context_unavailable(monkeypatch):
    monkeypatch.setattr(ghkit, "_repo_context", lambda cfg: None)
    monkeypatch.setattr(ghkit, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not call run without repo context")))

    with pytest.raises(SystemExit):
        ghkit.delete_issue_comment({}, True, 501)


# --- delete_issue_comment(): boundary validation --------------------------------

@pytest.mark.parametrize("comment_id", [None, "501", 0, -1, 4.2, True])
@pytest.mark.parametrize("apply", [True, False])
def test_delete_issue_comment_rejects_an_unusable_comment_id_before_reaching_run(monkeypatch, comment_id, apply):
    monkeypatch.setattr(ghkit, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not call run for an unusable comment_id")))

    with pytest.raises(ValueError, match="comment_id"):
        ghkit.delete_issue_comment({}, apply, comment_id)
