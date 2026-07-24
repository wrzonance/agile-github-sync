"""ghkit_snapshot.fetch_issue_graph (issue #98): one paginated GraphQL query replaces the
per-issue comment reads, per-issue blocked-by reads, and per-epic sub-issue reads.

Contracts pinned here:
  - comments normalize to ghkit._normalize_gh_comment's exact shape with id = databaseId;
  - an overflowing (>100) or unnormalizable comment collection leaves that ISSUE absent
    (per-issue fallback), never poisons the rest of the snapshot;
  - blocked-by failures are per-issue (blocked_by_failed); resolve_blocked_by applies the
    all-or-nothing rule over REQUESTED numbers only, and cross-repo blockers are WARN-skipped;
  - sub-issue overflow (>100) leaves that epic absent (per-epic fallback);
  - any page/transport failure returns None (callers keep today's per-item behavior).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ghkit  # noqa: E402
import ghkit_snapshot  # noqa: E402

CTX = ghkit.RepoContext(owner="acme", name="repo", host="github.com")
CFG = {"repo_context": CTX}


def _page(nodes, has_next=False, cursor=None):
    return {"data": {"repository": {"issues": {
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
        "nodes": nodes}}}}


def _node(number, comments=(), c_total=None, blocked=(), b_total=None, subs=(), s_total=None):
    return {
        "number": number,
        "comments": {"totalCount": c_total if c_total is not None else len(comments),
                     "nodes": list(comments)},
        "blockedBy": {"totalCount": b_total if b_total is not None else len(blocked),
                      "nodes": list(blocked)},
        "subIssues": {"totalCount": s_total if s_total is not None else len(subs),
                      "nodes": list(subs)},
    }


def test_fetch_normalizes_comments_to_rest_shape(monkeypatch):
    node = _node(5, comments=[{"databaseId": 42, "author": {"login": "alice"},
                               "body": "hi", "createdAt": "2026-01-01T00:00:00Z",
                               "updatedAt": "2026-01-02T00:00:00Z"}])
    monkeypatch.setattr(ghkit_snapshot, "_run_page", Mock(return_value=_page([node])))
    graph = ghkit_snapshot.fetch_issue_graph(CFG)
    assert graph.comments[5] == [{"id": 42, "author": "alice", "body": "hi",
                                  "created": "2026-01-01T00:00:00Z",
                                  "edited": "2026-01-02T00:00:00Z"}]


def test_comment_overflow_leaves_issue_absent(monkeypatch):
    node = _node(5, comments=[], c_total=101)
    monkeypatch.setattr(ghkit_snapshot, "_run_page", Mock(return_value=_page([node])))
    graph = ghkit_snapshot.fetch_issue_graph(CFG)
    assert 5 not in graph.comments  # per-issue fallback territory
    assert graph.blocked_by == {}   # the rest of the snapshot survives


def test_unnormalizable_comment_isolates_only_that_issue(monkeypatch):
    bad = _node(5, comments=[{"databaseId": None, "body": "x"}])
    good = _node(6, comments=[{"databaseId": 9, "author": None, "body": "y",
                               "createdAt": "c", "updatedAt": "e"}])
    monkeypatch.setattr(ghkit_snapshot, "_run_page", Mock(return_value=_page([bad, good])))
    graph = ghkit_snapshot.fetch_issue_graph(CFG)
    assert 5 not in graph.comments
    assert graph.comments[6] == [{"id": 9, "author": None, "body": "y",
                                  "created": "c", "edited": "e"}]


def test_blocked_by_filters_foreign_repo_and_keeps_local(monkeypatch, capsys):
    node = _node(7, blocked=[{"number": 3, "repository": {"nameWithOwner": "acme/repo"}},
                             {"number": 9, "repository": {"nameWithOwner": "other/repo"}}])
    monkeypatch.setattr(ghkit_snapshot, "_run_page", Mock(return_value=_page([node])))
    graph = ghkit_snapshot.fetch_issue_graph(CFG)
    assert graph.blocked_by == {7: [3]}
    assert "skipping cross-repo blocker other/repo#9" in capsys.readouterr().out


def test_blocked_by_repo_match_is_casefolded(monkeypatch):
    node = _node(7, blocked=[{"number": 3, "repository": {"nameWithOwner": "Acme/Repo"}}])
    monkeypatch.setattr(ghkit_snapshot, "_run_page", Mock(return_value=_page([node])))
    assert ghkit_snapshot.fetch_issue_graph(CFG).blocked_by == {7: [3]}


def test_blocked_by_malformed_entry_marks_only_that_issue_failed(monkeypatch):
    node = _node(7, blocked=[{"number": 3}])  # no repository -> not repository-qualified
    monkeypatch.setattr(ghkit_snapshot, "_run_page", Mock(return_value=_page([node])))
    graph = ghkit_snapshot.fetch_issue_graph(CFG)
    assert graph.blocked_by_failed == frozenset({7})
    assert graph.comments == {7: []}  # other portions survive


def test_sub_issues_collected_and_overflow_leaves_epic_absent(monkeypatch):
    ok = _node(8, subs=[{"number": 81}, {"number": 82}])
    over = _node(9, subs=[], s_total=101)
    monkeypatch.setattr(ghkit_snapshot, "_run_page", Mock(return_value=_page([ok, over])))
    graph = ghkit_snapshot.fetch_issue_graph(CFG)
    assert graph.sub_issues[8] == [81, 82]
    assert 9 not in graph.sub_issues


def test_pagination_walks_all_pages(monkeypatch):
    pages = [_page([_node(1)], has_next=True, cursor="C1"), _page([_node(2)])]
    run_page = Mock(side_effect=pages)
    monkeypatch.setattr(ghkit_snapshot, "_run_page", run_page)
    graph = ghkit_snapshot.fetch_issue_graph(CFG)
    assert set(graph.comments) == {1, 2}
    assert run_page.call_args_list[1].args[-1] == "C1"  # cursor threaded into page 2


def test_query_failure_returns_none(monkeypatch):
    monkeypatch.setattr(ghkit_snapshot, "_run_page", Mock(return_value=None))
    assert ghkit_snapshot.fetch_issue_graph(CFG) is None


def test_missing_repo_context_returns_none(monkeypatch):
    monkeypatch.setattr(ghkit, "resolve_repo_context", lambda _cfg: None)
    monkeypatch.setattr(ghkit_snapshot, "_run_page",
                        Mock(side_effect=AssertionError("must not query without a context")))
    assert ghkit_snapshot.fetch_issue_graph({}) is None


def test_run_page_parses_gh_stdout(monkeypatch):
    payload = _page([_node(1)])
    from types import SimpleNamespace
    captured = {}

    def fake_run(cfg, args, **kwargs):
        captured["args"] = args
        return SimpleNamespace(stdout=json.dumps(payload))

    monkeypatch.setattr(ghkit, "run", fake_run)
    assert ghkit_snapshot._run_page(CFG, CTX, ghkit_snapshot._query(True), None) == payload
    assert captured["args"][:2] == ["api", "graphql"]
    assert "--hostname" in captured["args"] and "github.com" in captured["args"]


# --- draft-phase Codex review fixes (stack review, 2026-07-24) --------------------------------

def test_malformed_sub_issue_node_leaves_epic_absent(monkeypatch):
    """A null/malformed element in subIssues.nodes must NOT be silently dropped with the rest
    recorded as authoritative -- an incomplete 'authoritative' set could authorize disconnecting
    the omitted managed child. The epic stays absent so the non-destructive per-epic fallback
    runs instead."""
    node = _node(9, subs=[{"number": 91}, None, {"number": 92}])
    monkeypatch.setattr(ghkit_snapshot, "_run_page", Mock(return_value=_page([node])))
    graph = ghkit_snapshot.fetch_issue_graph(CFG)
    assert 9 not in graph.sub_issues


def test_blocked_by_failure_is_scoped_to_the_failing_issue(monkeypatch):
    """An unrelated issue with overflowing/malformed blockedBy must not disable dependency
    reconciliation for every syncable issue -- the old per-issue reader only failed over the
    REQUESTED numbers, and resolve_blocked_by preserves that scope."""
    over = _node(7, blocked=[], b_total=51)
    ok = _node(8, blocked=[{"number": 3, "repository": {"nameWithOwner": "acme/repo"}}])
    monkeypatch.setattr(ghkit_snapshot, "_run_page", Mock(return_value=_page([over, ok])))
    graph = ghkit_snapshot.fetch_issue_graph(CFG)

    assert graph.blocked_by_failed == frozenset({7})
    assert graph.blocked_by == {8: [3]}
    # requested set touches the failed issue -> all-or-nothing None, same as blocked_by_map
    assert ghkit_snapshot.resolve_blocked_by(CFG, graph, True, [7, 8]) is None
    # requested set avoids it -> the snapshot stays usable
    assert ghkit_snapshot.resolve_blocked_by(CFG, graph, True, [8]) == {8: [3]}


def test_comments_clause_is_omitted_without_comment_sync_identity(monkeypatch):
    """With comment sync disabled (no identity -- the default), the batch must not download
    comment bodies for every issue: the clause is omitted from the query and the comments map
    stays empty (sync_comments self-disables before ever reading it)."""
    captured = {}

    def fake_run_page(cfg, ctx, query, cursor):
        captured["query"] = query
        return _page([{"number": 5,
                       "blockedBy": {"totalCount": 0, "nodes": []},
                       "subIssues": {"totalCount": 0, "nodes": []}}])

    monkeypatch.setattr(ghkit_snapshot, "_run_page", fake_run_page)
    graph = ghkit_snapshot.fetch_issue_graph(CFG)  # CFG has no comment_sync_identity

    assert "comments(" not in captured["query"]
    assert graph.comments == {}
    assert graph.sub_issues == {5: []}


def test_comments_clause_is_included_with_identity_or_when_forced(monkeypatch):
    captured = {}

    def fake_run_page(cfg, ctx, query, cursor):
        captured["query"] = query
        return _page([_node(5)])

    monkeypatch.setattr(ghkit_snapshot, "_run_page", fake_run_page)
    ghkit_snapshot.fetch_issue_graph({**CFG, "comment_sync_identity": {"gh_login": "b"}})
    assert "comments(" in captured["query"]

    captured.clear()
    ghkit_snapshot.fetch_issue_graph(CFG, include_comments=True)  # smoke's explicit override
    assert "comments(" in captured["query"]
