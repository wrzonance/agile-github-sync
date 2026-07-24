# Sync I/O Performance Overhaul Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse the sync's ~317 `gh` subprocess spawns + ~261 sequential AgilePlace HTTPS
requests per run (10+ minutes on the reference 83-card/120-issue board) to single-digit `gh`
spawns + ~22 bounded-concurrency AgilePlace latency waves (target: 5–20 s), changing reads only.

**Architecture:** Three stacked slices, one issue/branch/PR each. (1) #97: run-scoped caches —
resolve `RepoContext` once, keep Project field metadata alive, reuse the fetched issue bodies.
(2) #98: one cursor-paginated GraphQL query (`ghkit_snapshot.fetch_issue_graph`) replaces the
per-issue comment reads, per-issue blocked-by reads, and per-epic sub-issue reads; existing
per-item readers remain as fallbacks. (3) #99: `board_reads.gather_board_reads` prefetches card
descriptions, dependencies, AgilePlace comments, and epic children through a bounded
`ThreadPoolExecutor`; reconciliation stays serial and ordered. Writes are untouched everywhere.

**Tech Stack:** Python stdlib only (`concurrent.futures`, `json`); `gh` CLI for GitHub GraphQL;
existing urllib AgilePlace client. No new third-party dependencies.

## Global Constraints

- Repo is **stdlib-only** — do not add httpx/requests or any third-party package.
- Never break a tri-state read contract: `None` = "we don't know" must keep skipping the exact
  writes it skips today (blocked-by → all dependency writes; comments → that issue only;
  project read → lane moves).
- Writes stay serial: intake's customId → refetch → external-link order, per-issue comment
  action order, one versioned PATCH per card.
- Per-run caching only — no cross-run or module-global caches (a fresh process resolves fresh).
- Immutability: extend `cfg` via `cfg = {**cfg, ...}`, never mutate in place.
- All work branches from `feat/issue-93` (PR #94 base); branches stack:
  `perf/issue-97-run-context` → `perf/issue-98-graphql-batch` → `perf/issue-99-ap-read-phase`.
- Conventional Commits; every commit ends with
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- After each phase: full suite `python -m pytest -q` green, then draft PR
  (`gh pr create --draft`), body links its issue (`Closes #9x`) and notes the stacking.
- `tests/test_regression_budget.py` pins per-file budgets/hashes — update its constants in the
  same commit as the file it guards (the test's own docstring says how).

---

## Phase 1 — issue #97, branch `perf/issue-97-run-context` (base: `feat/issue-93`)

### Task 1: Run-scoped RepoContext in ghkit

**Files:**
- Modify: `ghkit.py:84-108` (`_repo_context`), add `resolve_repo_context`
- Modify: `sync.py:570` (main entry wiring)
- Test: `tests/test_ghkit.py` (add; file exists)

**Interfaces:**
- Produces: `ghkit.resolve_repo_context(cfg: dict) -> RepoContext | None` — resolves fresh via
  one `gh repo view` (the current `_repo_context` body, renamed).
- Produces: `ghkit._repo_context(cfg)` now returns `cfg["repo_context"]` when it is a
  `RepoContext`, else resolves fresh (unchanged behavior for callers without the key).

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/test_ghkit.py -q`
  Expected: FAIL (`repo_context` key ignored today → AssertionError from the Mock).

- [ ] **Step 3: Implement** — split `_repo_context`: move its whole current body into
  `resolve_repo_context(cfg)` (public, same docstring plus: "sync.main calls this once per run
  and threads the result through cfg['repo_context'] -- per-RUN scope keeps the no-stale-cache
  guarantee"); `_repo_context` becomes:

```python
def _repo_context(cfg: dict) -> RepoContext | None:
    cached = cfg.get("repo_context")
    if isinstance(cached, RepoContext):
        return cached
    return resolve_repo_context(cfg)
```

Update the `RepoContext` class docstring: the "never cached" sentence becomes "resolved fresh
once per run (never cached across runs, never env-sourced)".

- [ ] **Step 4: Wire sync.main** — at `sync.py:570` replace

```python
    target = ghkit.repo_name(cfg)
```

with

```python
    repo_context = ghkit.resolve_repo_context(cfg)
    if repo_context:
        cfg = {**cfg, "repo_context": repo_context}
    target = f"{repo_context.owner}/{repo_context.name}" if repo_context else None
```

(`repo_name` returned `nameWithOwner` from the same `gh repo view`; identical value, one spawn
instead of two-plus-hot-loop.)

- [ ] **Step 5: Full suite green** — `python -m pytest -q`. Tests that monkeypatch
  `ghkit.repo_name` for main() runs must be switched to monkeypatch
  `ghkit.resolve_repo_context` returning
  `ghkit.RepoContext(owner="acme", name="repo", host="github.com")`
  (grep: `patch("ghkit.repo_name"` and `monkeypatch.setattr(ghkit, "repo_name"` in tests/).

- [ ] **Step 6: Commit** — `perf(ghkit): resolve repo context once per run (#97)`

### Task 2: Keep Project status-write metadata alive; run-scoped field_meta

**Files:**
- Modify: `ghproject.py:207-260` (`ProjectV2Status`, `resolve_project_v2_status`),
  `ghproject.py:269` (`field_meta`)
- Modify: `sync.py:591` (pv2 wiring)
- Test: `tests/test_ghproject.py` (exists; extend)

**Interfaces:**
- `ProjectV2Status` gains field `status_meta: dict | None` — the un-gated `field_meta` dict
  whenever the fetch succeeded, even when the Project has no date fields. Existing
  `field_meta` member keeps its exact date-gating semantics (None when dates are off) so
  `sync.py:711`'s `if field_meta:` gate is untouched.
- `ghproject.field_meta(cfg)` returns `cfg["project_field_meta"]` when present (dict), else
  fetches as today.

- [ ] **Step 1: Failing tests**

```python
def test_resolve_keeps_status_meta_without_date_fields(monkeypatch):
    """A Project with Status but no Start/Target fields still exposes status_meta, so
    downstream status writes (vetting latch) need no re-fetch."""
    meta = {"project_id": "P1", "host": "github.com", "status_field_id": "F1",
            "status_options": {"backlog": "O1"}, "start_field_id": None, "target_field_id": None}
    monkeypatch.setattr(ghproject, "configured", lambda _cfg: True)
    monkeypatch.setattr(ghproject, "items", lambda _cfg: {"u": {"status": "Backlog"}})
    monkeypatch.setattr(ghproject, "field_meta", lambda _cfg: meta)
    pv2 = ghproject.resolve_project_v2_status({"gh_project": {"status_field": "Status"}})
    assert pv2.field_meta is None          # date sync stays off
    assert pv2.status_meta == meta         # status writes stay armed


def test_field_meta_prefers_run_scoped_value(monkeypatch):
    meta = {"project_id": "P1", "host": "h", "status_field_id": "F1",
            "status_options": {}, "start_field_id": None, "target_field_id": None}
    monkeypatch.setattr(ghkit, "run", Mock(side_effect=AssertionError("no spawn")))
    monkeypatch.setattr(ghproject, "configured", lambda _cfg: True)
    assert ghproject.field_meta({"project_field_meta": meta}) is meta
```

- [ ] **Step 2: Verify failure** — `python -m pytest tests/test_ghproject.py -q`

- [ ] **Step 3: Implement** — in `resolve_project_v2_status` replace the None-ing block:

```python
    field_meta_ = field_meta(cfg) if (configured(cfg) and not project_read_failed) else None
    status_meta = field_meta_ if isinstance(field_meta_, dict) else None
    if field_meta_ and not (field_meta_.get("start_field_id") or field_meta_.get("target_field_id")):
        field_meta_ = None
```

(keep the rest of the date-hydration flow exactly as is — on date-read failure `field_meta_`
still goes None but `status_meta` survives). Return `status_meta=status_meta` in the NamedTuple.
In `field_meta()` add at the top (after the `configured` guard):

```python
    cached = cfg.get("project_field_meta")
    if isinstance(cached, dict):
        return cached
```

In `sync.main` after `pv2 = ghproject.resolve_project_v2_status(cfg)` (line 591):

```python
    if pv2.status_meta:
        cfg = {**cfg, "project_field_meta": pv2.status_meta}
```

- [ ] **Step 4: Full suite green**; fix any NamedTuple-arity constructions in tests
  (grep `ProjectV2Status(` in tests/).

- [ ] **Step 5: Commit** — `perf(ghproject): run-scoped field_meta; status writes stop re-fetching project metadata (#97)`

### Task 3: Intake reuses the already-fetched issue bodies

**Files:**
- Modify: `intake.py:362` (`promote` prescan)
- Test: `tests/test_intake.py` (transport-call-count tests exist — update expectations)

**Interfaces:**
- `intake.promote` signature unchanged; it derives the marker snapshot from the `issues` param
  (which `ghkit.list_issues` populated with `body`) instead of a second `gh issue list`.
- `IntakeSummary.prescan_failed` stays in the struct (callers read it) but is now always False.
  `ghkit.list_issue_bodies` is NOT deleted — smoke.py uses it.

- [ ] **Step 1: Failing test**

```python
def test_promote_does_not_relist_issue_bodies(monkeypatch):
    """The marker-resume prescan reads the body-bearing `issues` snapshot promote() already
    receives -- zero extra gh spawns."""
    monkeypatch.setattr(ghkit, "list_issue_bodies",
                        Mock(side_effect=AssertionError("must not re-list issue bodies")))
    # reuse this file's existing promote() fixture wiring for one candidate card whose marked
    # issue is present in `issues` with a marker body; assert summary.resumed == 1
```

(Adapt to the file's existing fixture helpers — the assertion that matters is the Mock
side_effect never firing while a marker-resume still succeeds from `issues[n]["body"]`.)

- [ ] **Step 2: Verify failure**, **Step 3: Implement** — in `promote()` replace

```python
    issues_with_bodies = ghkit.list_issue_bodies(cfg)
    if issues_with_bodies is None:
        print("WARN  intake: could not read issue bodies for marker-resume scan -- skipping "
              f"all {len(candidates)} candidate(s) this run")
        return IntakeSummary(candidates=len(candidates), prescan_failed=True, resumed=0, created=0)
```

with

```python
    issues_with_bodies = [{"number": i["number"], "url": i["url"], "state": i["state"],
                           "body": i.get("body") or ""} for i in issues]
```

and update the docstring paragraph about `list_issue_bodies` (the tri-state prescan read is
gone: `list_issues` raising is main()'s own failure mode, so a promote() call always holds a
complete body snapshot). Delete the now-dead `prescan_failed=True` branch tests; keep the
field.

- [ ] **Step 4: Full suite green** (transport-call-count tests drop by one gh call).
- [ ] **Step 5: Commit** — `perf(intake): marker prescan reuses the run's issue snapshot (#97)`

### Task 4: Phase 1 wrap

- [ ] `python -m pytest -q` green; update `tests/test_regression_budget.py` constants if tripped.
- [ ] Push branch; open **draft** PR: title `perf: run-scoped repo/project context and intake body reuse`,
  base `feat/issue-93`, body: why (epic #96 cost model: ~100 spawns saved), what, Testing
  checkboxes, `Closes #97`, note "stacked on #94; retarget to main after it merges",
  `🤖 Co-authored by Claude Fable 5`.

---

## Phase 2 — issue #98, branch `perf/issue-98-graphql-batch` (base: phase 1 branch)

### Task 5: `ghkit_snapshot.py` — batched issue graph

**Files:**
- Create: `ghkit_snapshot.py`
- Test: `tests/test_ghkit_snapshot.py` (create)

**Interfaces:**
- Produces: `fetch_issue_graph(cfg: dict) -> IssueGraph | None` — None on ANY query/parse
  failure (callers then use the existing per-item readers).
- Produces: `IssueGraph(NamedTuple)`:
  - `comments: dict[int, list[dict]]` — normalized to the exact `_normalize_gh_comment` shape
    `{"id","author","body","created","edited"}` with `id = databaseId`. An issue whose
    `comments.totalCount > 100` or whose nodes fail normalization is **absent** from the map
    (= "fetch this one per-issue").
  - `blocked_by: dict[int, list[int]] | None` — target-repo blocker numbers per issue; `None`
    when any issue's blockedBy is malformed or `totalCount > 50` (all-or-nothing, mirroring
    `blocked_by_map`). Cross-repo blockers are WARN-skipped exactly like
    `_local_blocker_numbers` (same message text, same casefold compare).
  - `sub_issues: dict[int, list[int]]` — absent when `totalCount > 100` (= per-epic fallback).

- [ ] **Step 1: Failing tests** (representative set — write all of these):

```python
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ghkit  # noqa: E402
import ghkit_snapshot  # noqa: E402

CTX = ghkit.RepoContext(owner="acme", name="repo", host="github.com")


def _page(nodes, has_next=False, cursor=None):
    return json.dumps({"data": {"repository": {"issues": {
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
        "nodes": nodes}}}})


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
    monkeypatch.setattr(ghkit_snapshot, "_run_page",
                        Mock(return_value=json.loads(_page([node]))))
    graph = ghkit_snapshot.fetch_issue_graph({"repo_context": CTX})
    assert graph.comments[5] == [{"id": 42, "author": "alice", "body": "hi",
                                  "created": "2026-01-01T00:00:00Z",
                                  "edited": "2026-01-02T00:00:00Z"}]


def test_comment_overflow_leaves_issue_absent(monkeypatch):
    node = _node(5, comments=[], c_total=101)
    monkeypatch.setattr(ghkit_snapshot, "_run_page",
                        Mock(return_value=json.loads(_page([node]))))
    graph = ghkit_snapshot.fetch_issue_graph({"repo_context": CTX})
    assert 5 not in graph.comments  # per-issue fallback territory


def test_blocked_by_filters_foreign_repo_and_keeps_local(monkeypatch, capsys):
    node = _node(7, blocked=[{"number": 3, "repository": {"nameWithOwner": "acme/repo"}},
                             {"number": 9, "repository": {"nameWithOwner": "other/repo"}}])
    monkeypatch.setattr(ghkit_snapshot, "_run_page",
                        Mock(return_value=json.loads(_page([node]))))
    graph = ghkit_snapshot.fetch_issue_graph({"repo_context": CTX})
    assert graph.blocked_by == {7: [3]}
    assert "skipping cross-repo blocker other/repo#9" in capsys.readouterr().out


def test_blocked_by_overflow_fails_closed_to_none(monkeypatch):
    node = _node(7, blocked=[], b_total=51)
    monkeypatch.setattr(ghkit_snapshot, "_run_page",
                        Mock(return_value=json.loads(_page([node]))))
    graph = ghkit_snapshot.fetch_issue_graph({"repo_context": CTX})
    assert graph.blocked_by is None
    assert graph.comments == {7: []}  # other portions survive


def test_pagination_walks_all_pages(monkeypatch):
    pages = [json.loads(_page([_node(1)], has_next=True, cursor="C1")),
             json.loads(_page([_node(2)]))]
    run_page = Mock(side_effect=pages)
    monkeypatch.setattr(ghkit_snapshot, "_run_page", run_page)
    graph = ghkit_snapshot.fetch_issue_graph({"repo_context": CTX})
    assert set(graph.comments) == {1, 2}
    assert run_page.call_args_list[1].args[-1] == "C1"  # cursor threaded


def test_query_failure_returns_none(monkeypatch):
    monkeypatch.setattr(ghkit_snapshot, "_run_page", Mock(return_value=None))
    assert ghkit_snapshot.fetch_issue_graph({"repo_context": CTX}) is None


def test_sub_issue_overflow_leaves_epic_absent(monkeypatch):
    node = _node(9, subs=[], s_total=101)
    monkeypatch.setattr(ghkit_snapshot, "_run_page",
                        Mock(return_value=json.loads(_page([node]))))
    graph = ghkit_snapshot.fetch_issue_graph({"repo_context": CTX})
    assert 9 not in graph.sub_issues
```

- [ ] **Step 2: Verify failures** — `python -m pytest tests/test_ghkit_snapshot.py -q`

- [ ] **Step 3: Implement `ghkit_snapshot.py`** (complete file):

```python
"""Run-scoped batched GitHub reads (issue #98).

One cursor-paginated GraphQL query replaces the sync's three GitHub hot loops -- per-issue
comment reads (ghkit.list_issue_comments), per-issue blocked-by reads (ghkit.blocked_by_map),
and per-epic sub-issue reads (ghkit.sub_issue_numbers). Those per-item readers all remain the
fallback paths: an issue absent from a map (comment overflow >100, sub-issue overflow >100, or
a normalization failure isolated to that issue) is fetched individually by the existing code,
and a whole-query failure returns None so callers keep today's behavior end to end.

Contracts preserved exactly:
- comments normalize to ghkit._normalize_gh_comment's shape with id = databaseId (the REST id),
  so existing comment-sync ledgers keep matching;
- blocked_by is all-or-nothing (None on any malformed/overflowing entry) and WARN-skips
  cross-repo blockers with the same message _local_blocker_numbers prints;
- field shapes verified live against github.com GraphQL, 2026-07-24.
"""
from __future__ import annotations

import json
import subprocess
from typing import NamedTuple

import ghkit

_PAGE_SIZE = 50
_MAX_PAGES = 40  # 2000 issues -- defensive, mirrors agileplace pagination guards

_QUERY = """query($owner:String!,$name:String!,$cursor:String){
repository(owner:$owner,name:$name){
  issues(first:%d, states:[OPEN,CLOSED], after:$cursor){
    pageInfo{hasNextPage endCursor}
    nodes{
      number
      comments(first:100){totalCount nodes{databaseId author{login} body createdAt updatedAt}}
      blockedBy(first:50){totalCount nodes{number repository{nameWithOwner}}}
      subIssues(first:100){totalCount nodes{number}}
    }
  }
}}""" % _PAGE_SIZE


class IssueGraph(NamedTuple):
    """Per-issue GitHub read snapshot. Absence from `comments`/`sub_issues` means "fetch that
    item through the existing per-item reader"; `blocked_by is None` means the whole blocked-by
    snapshot is unusable (skip all dependency writes, same as blocked_by_map returning None)."""
    comments: dict[int, list[dict]]
    blocked_by: dict[int, list[int]] | None
    sub_issues: dict[int, list[int]]


def _run_page(cfg: dict, ctx: ghkit.RepoContext, cursor: str | None) -> dict | None:
    """One GraphQL page as parsed JSON, or None on any transport/parse failure."""
    args = ["api", "graphql", "--hostname", ctx.host, "-f", f"query={_QUERY}",
            "-f", f"owner={ctx.owner}", "-f", f"name={ctx.name}"]
    if cursor:
        args += ["-f", f"cursor={cursor}"]
    try:
        return json.loads(ghkit.run(cfg, args).stdout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def _normalize_comment(node) -> dict:
    """GraphQL comment node -> ghkit._normalize_gh_comment's exact output shape. Raises on a
    missing/non-numeric databaseId -- same abort convention as the REST normalizer."""
    if not isinstance(node, dict):
        raise TypeError("comment node must be an object")
    comment_id = node.get("databaseId")
    if not isinstance(comment_id, int) or isinstance(comment_id, bool):
        raise ValueError(f"comment node has a missing/non-numeric databaseId ({comment_id!r})")
    author = (node.get("author") or {}).get("login")
    return {
        "id": comment_id,
        "author": author if isinstance(author, str) else None,
        "body": node.get("body") if isinstance(node.get("body"), str) else "",
        "created": node.get("createdAt") if isinstance(node.get("createdAt"), str) else "",
        "edited": node.get("updatedAt") if isinstance(node.get("updatedAt"), str) else "",
    }


def _local_blockers(node: dict, number: int, target_repo: str) -> list[int]:
    """Target-repo blocker numbers for one issue node. Raises on malformed/overflowing input --
    the caller folds that into the all-or-nothing None, mirroring blocked_by_map."""
    blocked = node.get("blockedBy")
    if not isinstance(blocked, dict) or not isinstance(blocked.get("nodes"), list):
        raise TypeError("blockedBy must be a connection object")
    if blocked.get("totalCount", 0) > 50:
        raise ValueError("blockedBy overflow")
    numbers = []
    for b in blocked["nodes"]:
        if not isinstance(b, dict):
            raise TypeError("blockedBy node must be an object")
        n = b.get("number")
        repo = ((b.get("repository") or {}).get("nameWithOwner"))
        if not isinstance(n, int) or isinstance(n, bool) or n < 1 or not isinstance(repo, str):
            raise ValueError("blockedBy node lacks a valid repository-qualified issue")
        if repo.casefold() != target_repo.casefold():
            print(f"WARN  issue #{number}: skipping cross-repo blocker {repo}#{n} "
                  f"(target {target_repo})")
            continue
        numbers.append(n)
    return numbers


def fetch_issue_graph(cfg: dict) -> IssueGraph | None:
    """The whole repo's issue graph in ceil(N/50) gh spawns. None on any page failure."""
    ctx = ghkit._repo_context(cfg)
    if ctx is None:
        return None
    target_repo = f"{ctx.owner}/{ctx.name}"
    comments: dict[int, list[dict]] = {}
    blocked_by: dict[int, list[int]] | None = {}
    sub_issues: dict[int, list[int]] = {}
    cursor = None
    for _ in range(_MAX_PAGES):
        payload = _run_page(cfg, ctx, cursor)
        try:
            conn = payload["data"]["repository"]["issues"]
            nodes, page_info = conn["nodes"], conn["pageInfo"]
        except (KeyError, TypeError):
            return None
        for node in nodes:
            if not isinstance(node, dict) or not isinstance(node.get("number"), int):
                return None
            number = node["number"]
            c = node.get("comments") or {}
            if isinstance(c.get("nodes"), list) and c.get("totalCount", 0) <= 100:
                try:
                    comments[number] = [_normalize_comment(n) for n in c["nodes"]]
                except (TypeError, ValueError):
                    pass  # absent -> per-issue fallback for this one issue
            if blocked_by is not None:
                try:
                    blockers = _local_blockers(node, number, target_repo)
                    if blockers:
                        blocked_by[number] = blockers
                except (TypeError, ValueError):
                    blocked_by = None  # all-or-nothing, same as blocked_by_map
            s = node.get("subIssues") or {}
            if isinstance(s.get("nodes"), list) and s.get("totalCount", 0) <= 100:
                nums = [n.get("number") for n in s["nodes"] if isinstance(n, dict)]
                if all(isinstance(n, int) and not isinstance(n, bool) for n in nums):
                    sub_issues[number] = nums
        if not page_info.get("hasNextPage"):
            return IssueGraph(comments=comments, blocked_by=blocked_by, sub_issues=sub_issues)
        cursor = page_info.get("endCursor")
    return None  # pagination guard tripped -- treat as failure
```

- [ ] **Step 4: Tests pass** — `python -m pytest tests/test_ghkit_snapshot.py -q`
- [ ] **Step 5: Commit** — `perf(ghkit): batched issue-graph GraphQL snapshot (#98)`

### Task 6: Wire the snapshot into sync.main

**Files:**
- Modify: `sync.py` — fetch once; thread into blocked-by, epics, comment sync
- Modify: `comment_sync.py:sync_comments` + `_fetch_both_sides` — optional prefetched gh side
- Test: `tests/test_sync_main.py`, `tests/test_comment_sync.py` (extend)

**Interfaces:**
- `sync_comments(cfg, apply, issue, card, issues_state, gh_comments=None)` — `gh_comments`
  is a prefetched normalized list or None (= fetch per-issue as today).
- `_epic_task_resolution(cfg, epic, by_key, sub_issues=None)` and the chain up through
  `sync_child_connections(..., sub_issues=None)`: `sub_issues` is `graph.sub_issues` or None.

- [ ] **Step 1: Failing tests**

```python
def test_main_fetches_issue_graph_once_and_skips_per_issue_readers(...):
    """With a healthy IssueGraph, main() never calls ghkit.list_issue_comments,
    ghkit.blocked_by_map, or ghkit.sub_issue_numbers."""
    # monkeypatch ghkit_snapshot.fetch_issue_graph -> IssueGraph(comments={...}, blocked_by={},
    #   sub_issues={}); monkeypatch the three per-item readers with
    #   Mock(side_effect=AssertionError); run main() with one issue+card; assert clean run.

def test_main_falls_back_to_per_item_readers_when_graph_is_none(...):
    # fetch_issue_graph -> None; assert blocked_by_map/sub_issue_numbers/list_issue_comments
    # are called exactly as before this change (reuse existing call-count fixtures).

def test_sync_comments_uses_prefetched_gh_side(monkeypatch):
    # sync_comments(..., gh_comments=[...]) must not call ghkit.list_issue_comments.
```

- [ ] **Step 2: Verify failures.**

- [ ] **Step 3: Implement.** In `sync.main` after the `open_pr` read (`sync.py:578`):

```python
    graph = ghkit_snapshot.fetch_issue_graph(cfg) if online else None
```

Blocked-by (`sync.py:729`) becomes:

```python
    if not online:
        blocked_by = None
    elif not syncable_issues:
        blocked_by = {}
    elif graph is not None:
        blocked_by = graph.blocked_by  # None here means: skip writes, same contract
    else:
        blocked_by = ghkit.blocked_by_map(cfg, [i["number"] for i in syncable_issues])
```

`_epic_task_resolution` (`sync.py:86`): before the per-epic call:

```python
    if sub_issues is not None and epic["number"] in sub_issues:
        return sub_issues[epic["number"]], True
    nums = ghkit.sub_issue_numbers(cfg, epic["number"])
```

Thread `sub_issues=graph.sub_issues if graph else None` through
`sync_child_connections(...)` → `epic_task_numbers(...)` → `_epic_task_resolution(...)`
(keyword-only params with `None` defaults; call-site at `sync.py:719` passes it).

Comment wiring (`sync.py:759`):

```python
                gh_pre = graph.comments.get(issue["number"]) if graph else None
                sync_comments(cfg, apply, issue, card, issues_state, gh_comments=gh_pre)
```

`comment_sync._fetch_both_sides(cfg, number, card_id, gh_comments=None)`: use the prefetched
list when it is not None, else `ghkit.list_issue_comments(cfg, number)` — the rest identical.

- [ ] **Step 4: Full suite green.**
- [ ] **Step 5: Smoke step (project rule: every feature adds one):** add step 24 to `smoke.py` —
  call `ghkit_snapshot.fetch_issue_graph(cfg)` live, cross-check `graph.comments` for the
  repo's issue #1 against `ghkit.list_issue_comments(cfg, 1)` (ids and bodies equal), and
  `record()` PASS/INFO accordingly. Read-only; no cards involved; place before CLEANUP.
- [ ] **Step 6: Commit** — `perf(sync): one issue-graph fetch replaces per-issue GitHub hot loops (#98)`
  then push; draft PR base = phase 1 branch, `Closes #98`.

---

## Phase 3 — issue #99, branch `perf/issue-99-ap-read-phase` (base: phase 2 branch)

### Task 7: `board_reads.py` — bounded-concurrency AgilePlace prefetch

**Files:**
- Create: `board_reads.py`
- Test: `tests/test_board_reads.py` (create)

**Interfaces:**
- Produces:

```python
class BoardReads(NamedTuple):
    descriptions: dict[str, str]                 # card_id -> description ("" normalized)
    dependencies: dict[str, list | None]         # card_id -> raw entries, None = read failed
    ap_comments: dict[str, list[dict] | None]    # card_id -> comments, None = read failed
    children: dict[str, frozenset[str] | None]   # parent_id -> child ids, None = read failed

def gather_board_reads(cfg: dict, *, description_card_ids, dependency_card_ids,
                       comment_card_ids, child_parent_ids, max_workers: int = 8) -> BoardReads
```

- Workers call the EXISTING readers: `agileplace.get_card` (description), `agileplace.card_dependencies`,
  `agileplace_comments.list_comments` (its `SystemExit` caught → None), `agileplace.card_child_ids`.
  Any other exception in a worker → that key maps to None/absent (fail toward "unknown", the
  same value the serial reader produces on failure). Consumers never see a raised thread error.

- [ ] **Step 1: Failing tests**

```python
def test_gather_collects_all_four_families(monkeypatch):
    monkeypatch.setattr(agileplace, "get_card",
                        lambda _cfg, cid: {"id": cid, "description": f"D{cid}"})
    monkeypatch.setattr(agileplace, "card_dependencies", lambda _cfg, cid: [{"cardId": cid}])
    monkeypatch.setattr(agileplace_comments, "list_comments", lambda _cfg, cid: [{"id": 1}])
    monkeypatch.setattr(agileplace, "card_child_ids", lambda _cfg, cid: frozenset({"K"}))
    reads = board_reads.gather_board_reads({}, description_card_ids=["A"],
                                           dependency_card_ids=["A", "B"],
                                           comment_card_ids=["A"], child_parent_ids=["E"])
    assert reads.descriptions == {"A": "DA"}
    assert reads.dependencies == {"A": [{"cardId": "A"}], "B": [{"cardId": "B"}]}
    assert reads.ap_comments == {"A": [{"id": 1}]}
    assert reads.children == {"E": frozenset({"K"})}


def test_worker_failures_map_to_unknown_not_raise(monkeypatch):
    monkeypatch.setattr(agileplace, "get_card", Mock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(agileplace_comments, "list_comments",
                        Mock(side_effect=SystemExit("AP read failed")))
    reads = board_reads.gather_board_reads({}, description_card_ids=["A"],
                                           dependency_card_ids=[], comment_card_ids=["A"],
                                           child_parent_ids=[])
    assert "A" not in reads.descriptions      # absent -> serial lazy fallback path
    assert reads.ap_comments == {"A": None}   # None -> "skip this issue", today's contract
```

- [ ] **Step 2: Verify failures.**

- [ ] **Step 3: Implement `board_reads.py`** (complete file):

```python
"""Bounded-concurrency AgilePlace read phase (issue #99).

Gather-then-reconcile: every per-card AgilePlace read the run needs (description hydration,
dependency snapshots, comment lists, epic children) is issued through one bounded
ThreadPoolExecutor, and reconciliation then proceeds strictly serially in stable issue order
against the returned maps. Workers only READ and only via the existing agileplace/
agileplace_comments functions -- every write path in the run stays serial and ordered.

Failure maps to the same 'unknown' value each serial reader produces today (absent/None), so
consumers keep their exact skip semantics; a worker exception never propagates. max_workers=8
keeps well under AgilePlace rate limits; the client's own per-request 429 retry still applies
per thread."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import NamedTuple

import agileplace
import agileplace_comments


class BoardReads(NamedTuple):
    descriptions: dict[str, str]
    dependencies: dict[str, list | None]
    ap_comments: dict[str, list | None]
    children: dict[str, frozenset | None]


def _description(cfg: dict, card_id: str):
    fresh = agileplace.get_card(cfg, card_id)
    return fresh.get("description") or ""


def _comments(cfg: dict, card_id: str):
    try:
        return agileplace_comments.list_comments(cfg, card_id)
    except SystemExit:
        return None


def gather_board_reads(cfg: dict, *, description_card_ids, dependency_card_ids,
                       comment_card_ids, child_parent_ids, max_workers: int = 8) -> BoardReads:
    jobs = (
        [("desc", cid, _description) for cid in description_card_ids]
        + [("deps", cid, agileplace.card_dependencies) for cid in dependency_card_ids]
        + [("comm", cid, _comments) for cid in comment_card_ids]
        + [("kids", cid, agileplace.card_child_ids) for cid in child_parent_ids]
    )
    out = {"desc": {}, "deps": {}, "comm": {}, "kids": {}}
    if jobs:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(fn, cfg, cid): (family, cid) for family, cid, fn in jobs}
            for future, (family, cid) in futures.items():
                try:
                    out[family][cid] = future.result()
                except Exception:  # noqa: BLE001 -- fail toward "unknown", never crash the run
                    if family in ("deps", "comm", "kids"):
                        out[family][cid] = None
    return BoardReads(descriptions={k: v for k, v in out["desc"].items() if v is not None},
                      dependencies=out["deps"], ap_comments=out["comm"], children=out["kids"])
```

- [ ] **Step 4: Tests pass.** **Step 5: Commit** — `perf(board_reads): bounded AgilePlace read phase (#99)`

### Task 8: Wire the prefetch into the run

**Files:**
- Modify: `sync.py` (gather call after `_run_intake_promotion`, ~line 631; thread maps into
  the four consumers), `description_sync.py:187` (`sync_description` param),
  `comment_sync.py` (`sync_comments`/`_fetch_both_sides` ap side param)
- Test: `tests/test_sync_main.py`, `tests/test_description_sync.py`, `tests/test_comment_sync.py`

**Interfaces:**
- `sync_description(cfg, apply, issue, card, issues_state, queue, ap_description=None)` —
  when `ap_description` is not None it is used verbatim (skipping `card_description`); the
  `_planOnly` → `""` convention stays first.
- `sync_comments(..., gh_comments=None, ap_comments=_UNSET)` — `_UNSET` sentinel = fetch as
  today; `None` = the prefetch failed = skip this issue with today's WARN; a list = use it.
- Dependency loop: `entries = reads.dependencies.get(cid, _MISS)`; `_MISS` → serial
  `agileplace.card_dependencies(cfg, cid)` (covers cards created mid-run).
- Hierarchy: `existing_snapshot = reads.children.get(parent_id, _MISS)`; `_MISS` → serial
  `agileplace.card_child_ids(...)`.

- [ ] **Step 1: Failing tests** — for each consumer, a main()-level test that (a) prefetched
  values are used with the direct readers mocked to `AssertionError`, and (b) a card absent
  from the maps still resolves via the direct reader (lazy fallback). Reuse the existing
  main() fixtures in `tests/test_sync_main.py`.

- [ ] **Step 2: Verify failures.**

- [ ] **Step 3: Implement.** Gather site in `sync.main` (after `_run_intake_promotion`, before
  the per-issue loop):

```python
    matched_cards = {}
    for issue in syncable_issues:
        c = card_for(issue)
        if c and c.get("id") and not c.get("_planOnly"):
            matched_cards[issue["url"]] = c
    epic_parent_ids = [str(c["id"]) for e in epics
                       if (c := card_for(e)) and c.get("id") and not c.get("_planOnly")]
    reads = board_reads.gather_board_reads(
        cfg,
        description_card_ids=[str(c["id"]) for c in matched_cards.values()
                              if "description" not in c],
        dependency_card_ids=[str(c["id"]) for c in matched_cards.values()],
        comment_card_ids=([str(c["id"]) for c in matched_cards.values()]
                          if cfg.get("comment_sync_identity") else []),
        child_parent_ids=epic_parent_ids,
    ) if online else board_reads.BoardReads({}, {}, {}, {})
```

Then thread `reads` into the four consumers exactly per the Interfaces block above (the
per-issue loop passes `ap_description=reads.descriptions.get(cid)`; the comment loop passes
`ap_comments=reads.ap_comments.get(cid, _UNSET)`; `sync_dependencies` and
`sync_child_connections` take the maps as parameters with `None` default meaning "no prefetch,
read serially" so their unit tests stay unchanged).

- [ ] **Step 4: Full suite green** — including regression-budget constants for `sync.py` growth.
- [ ] **Step 5: Commit** — `perf(sync): reconcile against the gathered board reads (#99)`;
  push; draft PR base = phase 2 branch, `Closes #99`.

### Task 9: Epic wrap

- [ ] Re-run the whole suite from phase 3 head; confirm the three draft PRs form the stack
  #97 ← #98 ← #99 and each body carries its Testing checkboxes and the Windows verification
  step ("re-run `python sync.py` dry run on the reference board; expect seconds, and byte-
  identical planned PATCH lines modulo WARN ordering").
- [ ] Comment on epic #96 with the measured before/after spawn/request counts from the tests.

## Self-Review Notes

- Spec coverage: #97 tasks 1–3 (context, project meta, intake); #98 tasks 5–6 (loader + wiring
  + smoke step); #99 tasks 7–8 (pool + wiring). Codex P3 CPU items (orphan-match indexing,
  lane-index precompute) deliberately excluded — not material to the 10-minute cost (YAGNI).
- Type consistency: `IssueGraph.sub_issues: dict[int, list[int]]` consumed via
  `sub_issues.get(epic["number"])`; `BoardReads` keys are `str(card["id"])` everywhere —
  matching the run's existing `cid = str(card["id"])` convention.
- All fallback readers (`blocked_by_map`, `sub_issue_numbers`, `list_issue_comments`,
  `card_description`, `card_dependencies`, `card_child_ids`) are kept, not deleted.
