"""GitHub side of the sync via the `gh` CLI. Every call runs with cwd = TARGET_REPO_PATH so gh resolves
the repo from that clone's remote. List args, never shell=True. Reads are one cheap JSON call; native
sub-issues use GraphQL (with a title-key fallback in sync.py); the two writes (add/remove label) flow
through the dry-run gate.

NOTE: the GitHub calls here are validated at first live run (no remote is reachable offline).
"""
from __future__ import annotations

import json
import subprocess


GH_TIMEOUT = 60  # seconds; bounds every gh call so a stall can't hang the sync


def run(cfg: dict, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    target = cfg.get("target_repo_path")
    if target is None:
        raise SystemExit("TARGET_REPO_PATH is not set (.env or environment) -- cannot target the repo")
    if not target.is_dir():
        raise SystemExit(f"TARGET_REPO_PATH does not exist or is not a directory: {target}")
    return subprocess.run(["gh", *args], cwd=str(target), check=check, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=GH_TIMEOUT)


def gh_available() -> bool:
    try:
        subprocess.run(["gh", "--version"], capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def repo_name(cfg: dict) -> str | None:
    try:
        return run(cfg, ["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"]).stdout.strip() or None
    except (subprocess.CalledProcessError, FileNotFoundError, SystemExit):
        return None


def list_issues(cfg: dict) -> list[dict]:
    """Every issue with the facts the sync needs, normalized, in one call."""
    out = run(cfg, ["issue", "list", "--state", "all", "--limit", "1000", "--json",
                    "number,title,state,labels,milestone,assignees,url"])
    issues = json.loads(out.stdout or "[]")
    normalized = []
    for i in issues:
        ms = i.get("milestone") or {}
        normalized.append({
            "number": i["number"],
            "title": i.get("title", ""),
            "state": i.get("state", ""),
            "labels": [l["name"] for l in i.get("labels", [])],
            "milestone": ms.get("title") or None,
            "assignees": [a.get("login") for a in i.get("assignees", [])],
            "url": i.get("url", ""),
            "has_open_pr": False,  # populated by open_pr_issue_numbers()
        })
    return normalized


def open_pr_issue_numbers(cfg: dict) -> set[int]:
    """Issue numbers that an OPEN PR declares it will close (an 'in review' signal). Best-effort:
    returns empty on any error so the label-based signal still drives 'In review'."""
    q = """query($owner:String!,$name:String!){repository(owner:$owner,name:$name){
      pullRequests(states:OPEN,first:100){nodes{closingIssuesReferences(first:20){nodes{number}}}}}}"""
    repo = repo_name(cfg)
    if not repo or "/" not in repo:
        return set()
    owner, name = repo.split("/", 1)
    try:
        out = run(cfg, ["api", "graphql", "-f", f"query={q}", "-F", f"owner={owner}", "-F", f"name={name}"])
        prs = json.loads(out.stdout)["data"]["repository"]["pullRequests"]["nodes"]
        return {n["number"] for pr in prs for n in pr["closingIssuesReferences"]["nodes"]}
    except (subprocess.CalledProcessError, KeyError, TypeError, json.JSONDecodeError):
        return set()


def sub_issue_numbers(cfg: dict, epic_number: int) -> list[int] | None:
    """Native sub-issue numbers of an epic (GraphQL). Tri-state: a list on success (possibly empty when
    the epic genuinely has no native children); **None on query failure** (GHES/permission/schema), so
    the caller can warn before falling back to the title convention rather than silently mis-associating.
    """
    repo = repo_name(cfg)
    if not repo or "/" not in repo:
        return None
    owner, name = repo.split("/", 1)
    q = """query($owner:String!,$name:String!,$num:Int!){repository(owner:$owner,name:$name){
      issue(number:$num){subIssues(first:100){nodes{number}}}}}"""
    try:
        out = run(cfg, ["api", "graphql", "-f", f"query={q}", "-F", f"owner={owner}",
                        "-F", f"name={name}", "-F", f"num={epic_number}"])
        nodes = json.loads(out.stdout)["data"]["repository"]["issue"]["subIssues"]["nodes"]
        return [n["number"] for n in nodes]
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, KeyError, TypeError, json.JSONDecodeError):
        return None


def blocked_by_map(cfg: dict, issue_numbers: list[int]) -> dict[int, list[int]] | None:
    """{issue_number: [blocker_issue_numbers]} from GitHub's issue-dependencies REST API. **None** when
    the endpoint isn't available (dependencies then skipped entirely). VALIDATE LIVE: the exact
    endpoint/shape (issue dependencies are a newer GitHub feature)."""
    repo = repo_name(cfg)
    if not repo:
        return None
    result: dict[int, list[int]] = {}
    endpoint_ok = None
    for n in issue_numbers:
        try:
            out = run(cfg, ["api", f"repos/{repo}/issues/{n}/dependencies/blocked_by",
                            "--jq", ".[].number"], check=True)
            endpoint_ok = True
            nums = [int(x) for x in out.stdout.split() if x.strip().isdigit()]
            if nums:
                result[n] = nums
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            if endpoint_ok is None:
                return None  # not available on the first probe -> skip dependencies
    return result


def edit_label(cfg: dict, apply: bool, number: int, label: str, *, add: bool) -> None:
    """Add or remove one label on an issue, through the dry-run gate."""
    flag = "--add-label" if add else "--remove-label"
    if apply:
        run(cfg, ["issue", "edit", str(number), flag, label])
        print(f"gh    issue {number} {flag[2:]} {label}")
    else:
        print(f"DRY   gh issue edit {number} {flag} '{label}'")


def set_milestone(cfg: dict, apply: bool, number: int, title: str | None) -> None:
    """Set (title) or clear (title=None) an issue's milestone, through the dry-run gate. A single
    set-operation -- never clear-then-set -- so replacing one milestone with another cannot lose it."""
    if title:
        if apply:
            run(cfg, ["issue", "edit", str(number), "--milestone", title])
            print(f"gh    issue {number} milestone -> {title}")
        else:
            print(f"DRY   gh issue edit {number} --milestone '{title}'")
    else:
        if apply:
            run(cfg, ["issue", "edit", str(number), "--remove-milestone"])
            print(f"gh    issue {number} milestone cleared")
        else:
            print(f"DRY   gh issue edit {number} --remove-milestone")
