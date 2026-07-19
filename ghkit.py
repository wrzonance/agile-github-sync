"""GitHub side of the sync via the `gh` CLI. Every call runs with cwd = TARGET_REPO_PATH so gh resolves
the repo from that clone's remote. List args, never shell=True. Reads are one cheap JSON call; native
sub-issues use GraphQL (with a title-key fallback in sync.py); the two writes (add/remove label) flow
through the dry-run gate.

NOTE: the GitHub calls here are validated at first live run (no remote is reachable offline).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import NamedTuple
from urllib.parse import urlparse


GH_TIMEOUT = 60  # seconds; bounds every gh call so a stall can't hang the sync

# Per `gh help environment`: "GH_REPO: specify the GitHub repository ... for commands that otherwise
# operate on a local repository" -- exactly `repo view`/`issue list`/`issue edit`, the commands this
# module relies on to resolve from TARGET_REPO_PATH's own remote. GH_HOST similarly overrides which
# host every gh call targets. A stale value in the calling shell/scheduler environment (or injected by
# config.load_env_file()) would silently retarget every read AND write onto the wrong repo/host while
# every internal consistency check still passes -- so both are stripped from the subprocess env on
# every call, regardless of source. GH_TOKEN is deliberately NOT stripped: it is how auth flows and
# scrubbing it would break every call outright.
_GH_ENV_OVERRIDE_KEYS = frozenset({"GH_REPO", "GH_HOST"})


class RepoContext(NamedTuple):
    """The repo + host every gh api/graphql call must agree on, resolved fresh (never cached, never
    env-sourced) from one `gh repo view` call so name and host can never disagree with each other."""
    owner: str
    name: str
    host: str


def _gh_subprocess_env(host: str | None = None) -> dict[str, str]:
    """A full copy of the current environment with GH_REPO/GH_HOST removed -- never mutates
    os.environ itself. Everything else (PATH, HOME, GH_TOKEN, GH_CONFIG_DIR, locale vars) passes
    through unchanged; a deny-list copy keeps the blast radius small and auditable versus an
    allow-list's risk of silently dropping something `gh` actually needs.

    `host`, when given, re-adds GH_HOST as the *freshly resolved* target host (never the ambient
    value we just scrubbed). This is for host-selectorless commands like `gh project`, which -- unlike
    `gh api`/`repo`/`issue` -- have no --hostname flag and don't infer the host from the target clone's
    cwd, so GH_HOST is their only host selector."""
    env = {k: v for k, v in os.environ.items() if k not in _GH_ENV_OVERRIDE_KEYS}
    if host:
        env["GH_HOST"] = host
    return env


def run(cfg: dict, args: list[str], *, check: bool = True,
        host: str | None = None) -> subprocess.CompletedProcess:
    target = cfg.get("target_repo_path")
    if target is None:
        raise SystemExit("TARGET_REPO_PATH is not set (.env or environment) -- cannot target the repo")
    if not target.is_dir():
        raise SystemExit(f"TARGET_REPO_PATH does not exist or is not a directory: {target}")
    try:
        return subprocess.run(["gh", *args], cwd=str(target), check=check, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=GH_TIMEOUT,
                              env=_gh_subprocess_env(host))
    except subprocess.CalledProcessError as exc:
        # Surface gh's own message; captured-and-discarded stderr makes every failure opaque.
        if exc.stderr:
            print(f"gh {' '.join(args[:2])} failed: {exc.stderr.strip()}", file=sys.stderr)
        raise


def repo_name(cfg: dict) -> str | None:
    try:
        return run(cfg, ["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"]).stdout.strip() or None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, SystemExit):
        return None


def _repo_context(cfg: dict) -> RepoContext | None:
    """Resolve owner/name/host from one `gh repo view` call so all three can never disagree.
    None on any failure (gh error, timeout, malformed/missing JSON fields, unparseable host) --
    callers already treat repo_name()-returning-None as "don't proceed"; this just extends that same
    fail-closed contract to also cover host resolution."""
    try:
        out = run(cfg, ["repo", "view", "--json", "nameWithOwner,url"])
        data = json.loads(out.stdout)
        name_with_owner = data["nameWithOwner"]
        url = data["url"]
        # urlparse() raises ValueError on malformed URLs (e.g. an unmatched IPv6
        # bracket) -- keep it inside the try so that too follows the fail-closed path.
        host = urlparse(url).hostname if isinstance(url, str) else None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError,
            SystemExit, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    if not isinstance(name_with_owner, str) or name_with_owner.count("/") != 1:
        return None
    owner, name = name_with_owner.split("/", 1)
    if not owner or not name:
        return None
    if not host:
        return None
    return RepoContext(owner=owner, name=name, host=host)


def list_issues(cfg: dict) -> list[dict]:
    """Every issue with the facts the sync needs, normalized, in one call. Issues closed as
    not-planned or duplicate are excluded entirely: they are not work, and a card for one would sit
    in the board's Done lane as if it were (the target repo carries 16 such neutralized husks)."""
    out = run(cfg, ["issue", "list", "--state", "all", "--limit", "1000", "--json",
                    "number,title,state,stateReason,labels,milestone,assignees,url"])
    issues = json.loads(out.stdout or "[]")
    normalized = []
    for i in issues:
        if str(i.get("stateReason") or "").upper() in ("NOT_PLANNED", "DUPLICATE"):
            continue
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


def open_pr_issue_numbers(cfg: dict) -> set[int] | None:
    """Issue numbers that an OPEN PR declares it will close (an 'in review' signal). Tri-state: a
    set on success (possibly empty when no open PR closes any issue -- a real, distinguishable
    result); **None on read failure** (no repo context, gh error, timeout, or a malformed/missing
    response), so callers can tell "no open PRs" from "we don't know" instead of silently treating
    a failed read as if every issue's PR had closed."""
    q = """query($owner:String!,$name:String!){repository(owner:$owner,name:$name){
      pullRequests(states:OPEN,first:100){nodes{closingIssuesReferences(first:20){nodes{number}}}}}}"""
    ctx = _repo_context(cfg)
    if ctx is None:
        return None
    try:
        out = run(cfg, ["api", "graphql", "--hostname", ctx.host, "-f", f"query={q}",
                        "-f", f"owner={ctx.owner}", "-f", f"name={ctx.name}"])
        prs = json.loads(out.stdout)["data"]["repository"]["pullRequests"]["nodes"]
        return {n["number"] for pr in prs for n in pr["closingIssuesReferences"]["nodes"]}
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, KeyError, TypeError,
            json.JSONDecodeError):
        return None


def sub_issue_numbers(cfg: dict, epic_number: int) -> list[int] | None:
    """Native sub-issue numbers of an epic (GraphQL). Tri-state: a list on success (possibly empty when
    the epic genuinely has no native children); **None on query failure** (GHES/permission/schema), so
    the caller can warn before falling back to the title convention rather than silently mis-associating.
    """
    ctx = _repo_context(cfg)
    if ctx is None:
        return None
    q = """query($owner:String!,$name:String!,$num:Int!){repository(owner:$owner,name:$name){
      issue(number:$num){subIssues(first:100){nodes{number}}}}}"""
    try:
        out = run(cfg, ["api", "graphql", "--hostname", ctx.host, "-f", f"query={q}",
                        "-f", f"owner={ctx.owner}", "-f", f"name={ctx.name}", "-F", f"num={epic_number}"])
        nodes = json.loads(out.stdout)["data"]["repository"]["issue"]["subIssues"]["nodes"]
        return [n["number"] for n in nodes]
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, KeyError, TypeError, json.JSONDecodeError):
        return None


def _repo_name_from_url(value: object) -> str | None:
    """Return owner/name from a REST repository URL, including GHES's /api/v3 prefix."""
    if not isinstance(value, str):
        return None
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    if not parsed.scheme or not parsed.netloc:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    for index, part in enumerate(parts):
        if part.casefold() == "repos" and len(parts) == index + 3:
            return f"{parts[index + 1]}/{parts[index + 2]}"
    return None


def _blocker_repo_name(blocker: dict) -> str | None:
    """Extract owner/name from either an embedded repository or repository_url."""
    repository = blocker.get("repository")
    if isinstance(repository, dict):
        for key in ("full_name", "nameWithOwner", "name_with_owner"):
            value = repository.get(key)
            if isinstance(value, str):
                parts = value.split("/")
                if len(parts) == 2 and all(parts):
                    return value
        owner = repository.get("owner")
        owner_name = owner.get("login") if isinstance(owner, dict) else owner
        name = repository.get("name")
        if isinstance(owner_name, str) and isinstance(name, str) and owner_name and name:
            return f"{owner_name}/{name}"
    elif isinstance(repository, str):
        parts = repository.split("/")
        if len(parts) == 2 and all(parts):
            return repository
        from_url = _repo_name_from_url(repository)
        if from_url:
            return from_url
    return _repo_name_from_url(blocker.get("repository_url"))


def _local_blocker_numbers(stdout: str, ctx: RepoContext, blocked_number: int) -> list[int]:
    """Parse gh --slurp output and retain only blockers from the target repository."""
    pages = json.loads(stdout or "[]")
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise TypeError("blocked-by response must be a list of pages")
    target_repo = f"{ctx.owner}/{ctx.name}"
    local_numbers: list[int] = []
    for blocker in (item for page in pages for item in page):
        if not isinstance(blocker, dict):
            raise TypeError("blocked-by response item must be an object")
        number = blocker.get("number")
        blocker_repo = _blocker_repo_name(blocker)
        if not isinstance(number, int) or isinstance(number, bool) or number < 1 or not blocker_repo:
            raise ValueError("blocked-by response item lacks a valid repository-qualified issue")
        if blocker_repo.casefold() != target_repo.casefold():
            print(f"WARN  issue #{blocked_number}: skipping cross-repo blocker "
                  f"{blocker_repo}#{number} (target {target_repo})")
            continue
        local_numbers.append(number)
    return local_numbers


def blocked_by_map(cfg: dict, issue_numbers: list[int]) -> dict[int, list[int]] | None:
    """Target-repo blockers keyed by blocked issue. Foreign-repo blockers are warned and skipped.

    **None** means the repository context, endpoint, or response shape was unavailable, so callers
    must skip every blocked-state write rather than act on an incomplete snapshot.
    """
    ctx = _repo_context(cfg)
    if ctx is None:
        return None
    result: dict[int, list[int]] = {}
    for n in issue_numbers:
        try:
            out = run(cfg, ["api", "--hostname", ctx.host,
                            f"repos/{ctx.owner}/{ctx.name}/issues/{n}/dependencies/blocked_by",
                            "--paginate", "--slurp"], check=True)
            nums = _local_blocker_numbers(out.stdout, ctx, n)
            if nums:
                result[n] = nums
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError,
                TypeError, ValueError):
            return None  # ANY failure -> the snapshot is incomplete; skip ALL blocked-state writes
    return result


def is_gh_label_safe(name: str) -> bool:
    """False iff name would be corrupted (or rejected) by gh's --add-label/--remove-label pflag
    StringSlice flag, which CSV-splits its value via Go's encoding/csv Reader in its default
    (LazyQuotes=false) mode: a comma anywhere splits one name into several, and a '"' ANYWHERE --
    not just a leading one -- is a hard parse error for that reader (a bare quote inside an unquoted
    field is rejected outright, regardless of position), so any embedded quote must be treated as
    unsafe too."""
    return "," not in name and '"' not in name


def edit_label(cfg: dict, apply: bool, number: int, label: str, *, add: bool) -> None:
    """Add or remove one label on an issue, through the dry-run gate."""
    if not is_gh_label_safe(label):
        raise ValueError(f"edit_label: unsafe label name {label!r} would be CSV-split by gh's "
                          f"--add-label/--remove-label flag -- caller must pre-filter via "
                          f"is_gh_label_safe() before calling")
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
