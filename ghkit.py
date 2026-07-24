"""GitHub side of the sync via the `gh` CLI. Every call runs with cwd = TARGET_REPO_PATH so gh resolves
the repo from that clone's remote. List args, never shell=True. Commands use structured JSON; native
sub-issues use GraphQL (with a title-key fallback in sync.py), while dependency reads use REST. Label
and milestone writes flow through the dry-run gate.

NOTE: the GitHub calls here are validated at first live run; unit tests mock the remote boundary.
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
    """The repo + host every gh api/graphql call must agree on, resolved fresh once per run (never
    cached across runs, never env-sourced) from one `gh repo view` call so name and host can never
    disagree with each other."""
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


def run(cfg: dict, args: list[str], *, check: bool = True, host: str | None = None,
        input: str | None = None) -> subprocess.CompletedProcess:
    """Run one `gh` invocation. `input`, when given, is piped to the subprocess's stdin -- the
    mechanism create_issue() uses for `--body-file -` so an issue body never has to survive a shell
    quoting pass. Every existing call site omits it, so the default (None) reproduces exactly the
    subprocess.run(input=None) behavior those call sites already relied on."""
    target = cfg.get("target_repo_path")
    if target is None:
        raise SystemExit("TARGET_REPO_PATH is not set (.env or environment) -- cannot target the repo")
    if not target.is_dir():
        raise SystemExit(f"TARGET_REPO_PATH does not exist or is not a directory: {target}")
    try:
        return subprocess.run(["gh", *args], cwd=str(target), check=check, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=GH_TIMEOUT,
                              env=_gh_subprocess_env(host), input=input)
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


def resolve_repo_context(cfg: dict) -> RepoContext | None:
    """Resolve owner/name/host fresh from one `gh repo view` call so all three can never disagree.
    None on any failure (gh error, timeout, malformed/missing JSON fields, unparseable host) --
    callers already treat repo_name()-returning-None as "don't proceed"; this just extends that same
    fail-closed contract to also cover host resolution.

    sync.main calls this once per run and threads the result through cfg['repo_context'] (issue
    #97), so hot-loop readers stop re-spawning `gh repo view` -- per-RUN scope keeps the
    no-stale-cache guarantee (a fresh process still resolves fresh)."""
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


def _repo_context(cfg: dict) -> RepoContext | None:
    """The run's RepoContext: cfg['repo_context'] when main() already resolved it this run,
    else a fresh resolve (standalone callers -- smoke, tests -- keep working unchanged)."""
    cached = cfg.get("repo_context")
    if isinstance(cached, RepoContext):
        return cached
    return resolve_repo_context(cfg)


def list_issues(cfg: dict) -> list[dict]:
    """Every issue with the facts the sync needs, normalized, in one call.

    Closed NOT_PLANNED/DUPLICATE issues remain in the snapshot so callers can treat them as known
    Done dependencies and retire any card that predates their closure. ``sync`` keeps them out of
    the card-creation path.
    """
    out = run(cfg, ["issue", "list", "--state", "all", "--limit", "1000", "--json",
                    "number,title,state,stateReason,labels,milestone,assignees,url,body,issueType"])
    issues = json.loads(out.stdout or "[]")
    normalized = []
    for i in issues:
        ms = i.get("milestone") or {}
        normalized.append({
            "number": i["number"],
            "title": i.get("title", ""),
            "state": i.get("state", ""),
            "state_reason": str(i.get("stateReason") or "").upper(),
            "labels": [l["name"] for l in i.get("labels", [])],
            "milestone": ms.get("title") or None,
            "assignees": [a.get("login") for a in i.get("assignees", [])],
            "url": i.get("url", ""),
            "body": i.get("body") or "",  # description_sync's GitHub-side canonicalization input
            "has_open_pr": False,  # populated by open_pr_issue_numbers()
            # gh's own `issueType` field is an object ({"id","name","description","color"}) or null,
            # never a bare string -- (i.get("issueType") or {}).get("name") is exactly that shape,
            # confirmed live (issue #82 spike). None means "native Task or no type set".
            "issue_type": (i.get("issueType") or {}).get("name"),
        })
    return normalized


def list_issue_bodies(cfg: dict) -> list[dict] | None:
    """Every issue's number/url/state/body, for the Intake feature's disqualification and marker-
    resume reads. Tri-state, mirroring open_pr_issue_numbers exactly: a list on success (possibly
    empty when the repo genuinely has zero issues -- a real, distinguishable result), and **None**
    on ANY failure (gh error, timeout, or a malformed/non-list response), so callers can tell "no
    issues" from "we don't know" instead of treating a failed read as an empty snapshot and
    double-creating issues a resume should have found. `body` is normalized to "" -- gh's JSON
    output omits the field entirely for a bodyless issue rather than emitting null."""
    try:
        out = run(cfg, ["issue", "list", "--state", "all", "--limit", "1000", "--json",
                        "number,url,state,body"])
        issues = json.loads(out.stdout or "[]")
        if not isinstance(issues, list):
            raise TypeError("gh issue list must return a JSON array")
        return [{
            "number": i["number"],
            "url": i.get("url", ""),
            "state": i.get("state", ""),
            "body": i.get("body") or "",
        } for i in issues]
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError,
            KeyError, TypeError):
        return None


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
                TypeError, ValueError) as exc:
            print(f"WARN  blocked-by snapshot incomplete for issue #{n}: {exc}", file=sys.stderr)
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


def create_label(cfg: dict, apply: bool, name: str) -> None:
    """Create one repo label so a reconciled add can land -- `gh issue edit --add-label` does NOT
    create labels missing from the repo (live failure, issue #91). Fixed neutral color; the name is
    a positional argument to gh (no CSV-splitting flag), so is_gh_label_safe does not apply here.
    gh errors (including already-exists) propagate; the metadata_sync caller treats the create as
    best-effort ahead of its single add retry."""
    if apply:
        # options first, then `--` so a dash-prefixed name stays positional instead of being
        # parsed as a flag
        run(cfg, ["label", "create", "--color", "ededed",
                  "--description", "created by agile-github-sync", "--", name])
        print(f"gh    label create {name}")
    else:
        print(f"DRY   gh label create '{name}'")


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


def edit_issue_body(cfg: dict, apply: bool, number: int, body: str) -> bool:
    """Set an issue's body via `gh issue edit --body-file -`, through the dry-run gate. The body is
    piped through run()'s `input=` stdin passthrough (same idiom as create_issue's --body-file -),
    never interpolated into argv, so a description containing shell metacharacters or gh-flag-like
    text can't be misparsed.

    Returns True only when the write actually happened (apply=True and gh succeeded) and False for a
    dry run -- description_sync.sync_description gates its base-advance on this exact boolean
    (gh_write_ok), so a dry run must never report success. Any CalledProcessError/TimeoutExpired from
    run() propagates uncaught, matching create_issue/edit_label's own apply=True behavior -- a failed
    write must not be swallowed into a false "it worked".

    Validates `number`/`body` at this boundary, before either the dry-run print or a live run() call
    -- an invalid number would otherwise reach `gh issue edit <number>` and fail opaquely, and a
    non-string body would crash run()'s stdin pipe with a confusing TypeError."""
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise ValueError(f"edit_issue_body: number must be a positive int, got {number!r}")
    if not isinstance(body, str):
        raise ValueError(f"edit_issue_body: body must be a str, got {type(body).__name__}")
    if not apply:
        print(f"DRY   gh issue edit {number} --body-file -")
        return False
    run(cfg, ["issue", "edit", str(number), "--body-file", "-"], input=body)
    print(f"gh    issue {number} body updated")
    return True


# GitHub's three DEFAULT native issue types -- NOT the full universe of `--type` values: an org can
# rename or disable these and define custom types of its own. This repo's reverse-seed table
# (card_types.REVERSE_SEED_BY_CARD_TYPE) only ever emits "Bug" today, so this literal set is a
# deliberate scope limiter for create_issue's schema/boundary check; it does NOT re-probe org
# enablement, which is the caller's responsibility (see intake.promote()). Extending the reverse-
# seed table to a custom org type requires widening this set too, or create_issue will reject a
# type the org has actually enabled.
_GH_ISSUE_TYPES = frozenset({"Task", "Bug", "Feature"})


def _issue_number_from_url(url: str) -> int:
    """The trailing /issues/{n} segment of a GitHub issue URL, as an int. `gh issue create`'s
    stdout contract is exactly one bare URL line; a malformed one is a genuine unrecovered failure,
    so this raises (ValueError) rather than guessing -- callers let it propagate uncaught, same as
    every other create_issue() failure mode."""
    return int(url.rsplit("/", 1)[-1])


def create_issue(cfg: dict, apply: bool, title: str, body: str,
                  issue_type: str | None = None) -> dict | None:
    """Create one issue via `gh issue create --body-file -`, through the dry-run gate. The body is
    never interpolated into argv -- it is piped through run()'s `input=` stdin passthrough (Task
    2), so a body containing shell metacharacters or gh-flag-like text can't be misparsed.

    `issue_type`, when given, is threaded onto the command as `--type <value>` (and into the dry-run
    print line) -- but ONLY after passing the boundary check below. API-VALIDATION.md records
    `gh issue create --type <TYPE>` as non-atomic (an org missing that type still gets the issue
    created before the command fails), so this function never re-probes org enablement itself and
    never retries -- that landmine is exactly why callers must resolve `issue_type` through
    card_types.validate_reverse_issue_type (gated on ghkit.org_issue_types) BEFORE calling here,
    passing None whenever the type isn't confirmed enabled.

    apply=False prints the planned title (and type, if any) and returns None, with zero calls to
    run() -- identical dry-run shape to edit_label/set_milestone. apply=True runs the create, parses
    the issue number out of gh's own stdout (a bare created-issue URL), and returns {"number", "url"}.
    Any CalledProcessError/TimeoutExpired from run() propagates uncaught -- no swallowed sentinel.

    Validates `title` at this boundary, before either the dry-run print or a live run() call: a
    blank or non-string title would otherwise reach subprocess.Popen unvalidated -- None raises an
    opaque TypeError ("argv must be str"), and "" produces a gh CalledProcessError -- either way an
    exception nothing upstream catches, crashing the entire sync run for one bad title. Raises
    ValueError with the offending value for context, matching edit_label's own boundary-validation
    convention (unsafe label names). `issue_type`, when not None, is validated the same way against
    the _GH_ISSUE_TYPES allowlist (GitHub's default types -- see its comment for the custom-org-
    type caveat) -- a schema check, not an org-enablement check (that's the caller's job, via
    validate_reverse_issue_type)."""
    if not isinstance(title, str) or not title.strip():
        raise ValueError(f"create_issue: title must be a non-empty string, got {title!r}")
    if issue_type is not None and issue_type not in _GH_ISSUE_TYPES:
        raise ValueError(f"create_issue: issue_type must be one of {sorted(_GH_ISSUE_TYPES)}, "
                          f"got {issue_type!r}")
    if not apply:
        type_suffix = f" --type '{issue_type}'" if issue_type else ""
        print(f"DRY   gh issue create --title '{title}'{type_suffix}")
        return None
    args = ["issue", "create", "--title", title, "--body-file", "-"]
    if issue_type:
        args += ["--type", issue_type]
    out = run(cfg, args, input=body)
    url = out.stdout.strip()
    print(f"gh    issue create -> {url}")
    return {"number": _issue_number_from_url(url), "url": url}


def org_issue_types(cfg: dict) -> frozenset[str] | None:
    """The set of native issue TYPE names enabled for the repo's owning organization, via one
    `gh api orgs/{owner}/issue-types` call over the repo context resolved by `_repo_context`.

    Tri-state, mirroring open_pr_issue_numbers/sub_issue_numbers exactly: a frozenset[str] on
    success (possibly empty when the org has no issue types enabled -- a real, distinguishable
    result), and **None** on ANY failure (no repo context, 404/non-org repo, subprocess error or
    timeout, or a malformed/non-list response) -- so callers get one uniform fail-closed signal
    rather than a fabricated empty set standing in for "we don't know". This is the exact probe
    API-VALIDATION.md records as mandatory before any `gh issue create --type` call, since that flag
    is non-atomic (see create_issue's own docstring). One WARN is printed on failure so a silent
    outage here isn't invisible; callers (card_types.validate_reverse_issue_type) already treat
    `None` and "type not enabled" as the same fail-closed signal, so no caller needs to special-case
    the warning itself."""
    ctx = _repo_context(cfg)
    if ctx is None:
        print("WARN  org issue-types probe skipped -- repo context unavailable", file=sys.stderr)
        return None
    try:
        out = run(cfg, ["api", "--hostname", ctx.host, f"orgs/{ctx.owner}/issue-types"])
        data = json.loads(out.stdout or "[]")
        if not isinstance(data, list):
            raise TypeError("orgs/{owner}/issue-types must return a JSON array")
        return frozenset(
            item["name"] for item in data
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError,
            TypeError, KeyError) as exc:
        print(f"WARN  org issue-types probe failed: {exc}", file=sys.stderr)
        return None


# --- Issue #66: GitHub-side issue-comment I/O --------------------------------
#
# All four functions below go through `gh api ... --input -` for writes -- never `gh issue
# comment` and never an argv-interpolated `-f body={body}` flag (the design doc's findings #5/#6):
# a JSON body built with json.dumps is piped through run()'s existing `input=` stdin passthrough,
# the same mechanism create_issue/edit_issue_body already use for issue bodies, so a comment body
# containing shell metacharacters or gh-flag-like text can never be misparsed.


def _normalize_gh_comment(raw: dict) -> dict:
    """One GitHub REST issue-comment payload normalized into the GhComment shape
    ({"id", "author", "body", "created", "edited"}). Raises TypeError/ValueError on a non-object
    payload or a missing/non-numeric id -- a comment the sync can't identify is a genuine
    unrecovered failure that must abort the whole list_issue_comments() read (mirroring
    _local_blocker_numbers' own per-item raise-and-abort convention), not be silently skipped.
    Every other field degrades to a safe default instead of raising."""
    if not isinstance(raw, dict):
        raise TypeError(f"GitHub issue-comment payload is {type(raw).__name__}, expected an object")
    comment_id = raw.get("id")
    if not isinstance(comment_id, int) or isinstance(comment_id, bool):
        raise ValueError(f"GitHub issue-comment has a missing/non-numeric id ({comment_id!r})")
    user = raw.get("user")
    author = user.get("login") if isinstance(user, dict) else None
    created = raw.get("created_at")
    edited = raw.get("updated_at")
    return {
        "id": comment_id,
        "author": author if isinstance(author, str) else None,
        "body": raw.get("body") if isinstance(raw.get("body"), str) else "",
        "created": created if isinstance(created, str) else "",
        "edited": edited if isinstance(edited, str) else "",
    }


def list_issue_comments(cfg: dict, number: int) -> list[dict] | None:
    """Every comment on one GitHub issue, normalized into GhComment dicts, via one paginated
    `gh api repos/{owner}/{repo}/issues/{number}/comments --paginate --slurp` read.

    Tri-state, mirroring blocked_by_map/org_issue_types exactly: a list on success (possibly
    empty -- a genuinely commentless issue is a real, distinguishable result), and **None** on ANY
    failure (no repo context, gh error, timeout, or a malformed response/item), so comment_sync
    can tell "no comments" from "we don't know" instead of treating a failed read as an empty
    snapshot and re-mirroring every comment as new.

    Boundary-validates `number` (a positive, non-bool int) before any I/O, raising ValueError --
    matching edit_issue_body's own boundary-validation convention."""
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise ValueError(f"list_issue_comments: number must be a positive int, got {number!r}")
    ctx = _repo_context(cfg)
    if ctx is None:
        return None
    try:
        out = run(cfg, ["api", "--hostname", ctx.host,
                        f"repos/{ctx.owner}/{ctx.name}/issues/{number}/comments",
                        "--paginate", "--slurp"])
        pages = json.loads(out.stdout or "[]")
        if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
            raise TypeError("issue-comments response must be a list of pages")
        return [_normalize_gh_comment(item) for page in pages for item in page]
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError,
            TypeError, ValueError) as exc:
        print(f"WARN  issue #{number} comment read failed: {exc}", file=sys.stderr)
        return None


def create_issue_comment(cfg: dict, apply: bool, number: int, body: str) -> int | None:
    """Post one comment on a GitHub issue via `gh api --input -` (POST), through the dry-run gate.

    apply=False prints a DRY line and returns None, with zero calls to run() -- identical dry-run
    shape to create_issue/edit_issue_body. apply=True posts the JSON-encoded body and returns the
    new comment's id (int); a response whose `id` can't be parsed as an int raises ValueError
    rather than letting a created-but-unparsed comment masquerade as a swallowed None to
    comment_sync's ledger writeback. Any CalledProcessError/TimeoutExpired from run() propagates
    uncaught, matching create_issue/edit_issue_body's own apply=True behavior.

    Validates `number`/`body` at this boundary, before either the dry-run print or repo-context
    resolution -- an invalid number/body must never reach a live `gh api` call."""
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise ValueError(f"create_issue_comment: number must be a positive int, got {number!r}")
    if not isinstance(body, str):
        raise ValueError(f"create_issue_comment: body must be a str, got {type(body).__name__}")
    if not apply:
        print(f"DRY   gh api issue {number} comment -- POST")
        return None
    ctx = _repo_context(cfg)
    if ctx is None:
        raise SystemExit(f"create_issue_comment: repo context unavailable for issue #{number}")
    out = run(cfg, ["api", "--hostname", ctx.host,
                    f"repos/{ctx.owner}/{ctx.name}/issues/{number}/comments",
                    "--method", "POST", "--input", "-"], input=json.dumps({"body": body}))
    data = json.loads(out.stdout)
    comment_id = data.get("id") if isinstance(data, dict) else None
    if not isinstance(comment_id, int) or isinstance(comment_id, bool):
        raise ValueError(f"create_issue_comment: response id is not an int ({comment_id!r})")
    print(f"gh    issue {number} comment created -> {comment_id}")
    return comment_id


def edit_issue_comment(cfg: dict, apply: bool, comment_id: int, body: str) -> bool:
    """Edit an existing GitHub issue comment's body via `gh api --input -` (PATCH), through the
    dry-run gate -- the same json.dumps-built-body/stdin-`input=` mechanism as
    create_issue_comment, fixing the spike's divergent argv-embedded `-f body={body}` flag (design
    findings #5/#6), which would both mis-parse shell-meaningful bodies and diverge from create's
    own stdin idiom.

    Returns True only when the write actually happened (apply=True and gh succeeded) -- a dry run
    must never report success, matching edit_issue_body's own apply-gated boolean contract. Any
    CalledProcessError/TimeoutExpired from run() propagates uncaught.

    Validates `comment_id`/`body` at this boundary, before either the dry-run print or repo-context
    resolution."""
    if not isinstance(comment_id, int) or isinstance(comment_id, bool) or comment_id < 1:
        raise ValueError(
            f"edit_issue_comment: comment_id must be a positive int, got {comment_id!r}")
    if not isinstance(body, str):
        raise ValueError(f"edit_issue_comment: body must be a str, got {type(body).__name__}")
    if not apply:
        print(f"DRY   gh api issue comment {comment_id} -- PATCH")
        return False
    ctx = _repo_context(cfg)
    if ctx is None:
        raise SystemExit(f"edit_issue_comment: repo context unavailable for comment {comment_id}")
    run(cfg, ["api", "--hostname", ctx.host,
              f"repos/{ctx.owner}/{ctx.name}/issues/comments/{comment_id}",
              "--method", "PATCH", "--input", "-"], input=json.dumps({"body": body}))
    print(f"gh    issue comment {comment_id} updated")
    return True


def delete_issue_comment(cfg: dict, apply: bool, comment_id: int) -> bool:
    """Delete a GitHub issue comment via `gh api` (DELETE, no request body), through the dry-run
    gate. Returns True only when the write actually happened (apply=True and gh succeeded) -- same
    apply-gated boolean contract as edit_issue_comment/edit_issue_body. Any
    CalledProcessError/TimeoutExpired from run() propagates uncaught.

    Validates `comment_id` at this boundary, before either the dry-run print or repo-context
    resolution."""
    if not isinstance(comment_id, int) or isinstance(comment_id, bool) or comment_id < 1:
        raise ValueError(
            f"delete_issue_comment: comment_id must be a positive int, got {comment_id!r}")
    if not apply:
        print(f"DRY   gh api issue comment {comment_id} -- DELETE")
        return False
    ctx = _repo_context(cfg)
    if ctx is None:
        raise SystemExit(f"delete_issue_comment: repo context unavailable for comment {comment_id}")
    run(cfg, ["api", "--hostname", ctx.host,
              f"repos/{ctx.owner}/{ctx.name}/issues/comments/{comment_id}",
              "--method", "DELETE"])
    print(f"gh    issue comment {comment_id} deleted")
    return True
