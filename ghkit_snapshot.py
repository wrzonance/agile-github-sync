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
_MAX_PAGES = 40  # 2000 issues -- defensive, mirrors agileplace's own pagination guards

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
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError,
            SystemExit):
        return None


def _normalize_comment(node) -> dict:
    """GraphQL comment node -> ghkit._normalize_gh_comment's exact output shape. Raises on a
    missing/non-numeric databaseId -- same abort convention as the REST normalizer, except the
    caller isolates the abort to this one issue instead of the whole read."""
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
        repo = (b.get("repository") or {}).get("nameWithOwner")
        if not isinstance(n, int) or isinstance(n, bool) or n < 1 or not isinstance(repo, str):
            raise ValueError("blockedBy node lacks a valid repository-qualified issue")
        if repo.casefold() != target_repo.casefold():
            print(f"WARN  issue #{number}: skipping cross-repo blocker {repo}#{n} "
                  f"(target {target_repo})")
            continue
        numbers.append(n)
    return numbers


def resolve_blocked_by(cfg: dict, graph: IssueGraph | None, online: bool,
                       issue_numbers: list[int]) -> dict[int, list[int]] | None:
    """The run's blocked-by snapshot, preferring the batched graph (issue #98).

    Offline -> None (skip all dependency writes, AgilePlace unreachable); no syncable issues ->
    {} (nothing to reconcile); a present graph -> its blocked_by verbatim (None inside it is the
    batch's own all-or-nothing failure -- never re-run the per-issue loop, whose result could not
    be more complete); no graph -> the existing per-issue reader."""
    if not online:
        return None
    if not issue_numbers:
        return {}
    if graph is not None:
        return graph.blocked_by
    return ghkit.blocked_by_map(cfg, issue_numbers)


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
                    pass  # absent -> the per-issue reader covers this one issue
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
    return None  # pagination guard tripped -- treat as a whole-read failure
