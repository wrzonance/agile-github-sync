"""GitHub Projects v2 — the Status source of truth (Phase 1) and date fields (Phase 4).

Uses the `gh` CLI through ghkit.run. Status/item identity comes from owner-scoped `project item-list`;
field discovery uses `project view` and `field-list`; date values use a paginated GraphQL snapshot
matched by field id so a successful all-cleared field is distinguishable from a failed read. Writes
(date fields, item add, and Status) all go through the dry-run gate. Requires the `project` token
scope: `gh auth refresh -s project`.
"""
from __future__ import annotations

import hashlib
import json
import subprocess

import ghkit

# Dry-run placeholder prefix for add_item's return value, so a caller can tell a not-yet-real
# planned item id apart from a genuine GitHub-issued one at a glance (e.g. in logs).
PLANNED_ITEM_ID_PREFIX = "planned-item:"


def configured(cfg: dict) -> bool:
    p = cfg.get("gh_project") or {}
    return bool(p.get("owner") and p.get("number"))


def _camel(name: str) -> str:
    """gh's camelCase flatten transform for multi-word field names (cli/cli v2.96.0 queries.go):
    lowercase only the first rune, e.g. 'Start Date' -> 'start Date'. Falsy name returned unchanged."""
    if not name:
        return name
    return name[0].lower() + name[1:]


def _field_candidates(name: str, *alts: str) -> tuple[str, ...]:
    """Shared probe order for a field's possible flattened keys: the configured name, its full
    lower-case form, gh's camelCase flatten, then any caller-supplied aliases verbatim (no de-dup)."""
    return (name, name.lower(), _camel(name), *alts)


def _field(item: dict, name: str, *alts: str):
    """A Projects v2 field value off a `gh project item-list` row. gh flattens field values as top-level
    keys; the exact casing varies, so try the configured name, its lower-case form, gh's camelCase
    flatten, and any aliases."""
    for key in _field_candidates(name, *alts):
        if key in item and item[key] not in (None, ""):
            return item[key]
    return None


_NON_ISSUE_CONTENT_TYPES = frozenset({"PullRequest", "DraftIssue"})


def parse_items(items: list, status_field: str = "Status", start_field: str = "Start",
                target_field: str = "Target") -> dict[str, dict]:
    """Map issue URL -> {item_id, number, status, start, target} from `gh project item-list` JSON.
    Skips non-issue content: draft items carry no URL at all, but Pull Requests DO -- `gh project
    item-list` populates content.url for a linked PR just like it does for an Issue, so a PR row
    must be excluded explicitly by content.type rather than assumed absent by URL alone. A row
    whose content.type key is missing entirely is treated as an Issue (some fixtures/gh output
    paths never populate it), so only a content.type explicitly naming a non-issue kind is
    excluded."""
    result = {}
    for it in items:
        content = it.get("content") or {}
        url = content.get("url")
        if not url or content.get("type") in _NON_ISSUE_CONTENT_TYPES:
            continue
        result[url] = {
            "item_id": it.get("id"),
            "number": content.get("number"),
            "status": _field(it, status_field, "status"),
            "start": _field(it, start_field),
            "target": _field(it, target_field),
        }
    return result


def _fetch_raw_items(cfg: dict) -> list | None:
    """Raw `gh project item-list` rows, or None on failure/not-configured."""
    if not configured(cfg):
        return None
    ctx = ghkit._repo_context(cfg)
    if ctx is None:  # can't resolve the target host -> fail closed rather than hit the default host
        return None
    p = cfg["gh_project"]
    try:
        out = ghkit.run(cfg, ["project", "item-list", str(p["number"]), "--owner", p["owner"],
                              "--format", "json", "--limit", "1000"], host=ctx.host)
        data = json.loads(out.stdout or "{}")
        return data.get("items", [])
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError, KeyError, SystemExit):
        return None


def items(cfg: dict) -> dict[str, dict] | None:
    """All project items keyed by issue URL, or None on failure/not-configured (caller falls back)."""
    raw = _fetch_raw_items(cfg)
    if raw is None:
        return None
    p = cfg["gh_project"]
    try:
        return parse_items(raw, p["status_field"], p["start_field"], p["target_field"])
    except KeyError:
        return None


_DATE_VALUES_QUERY = """query($project:ID!,$endCursor:String){node(id:$project){... on ProjectV2{
  items(first:100,after:$endCursor){pageInfo{hasNextPage endCursor} nodes{id
    fieldValues(first:100){pageInfo{hasNextPage} nodes{
      ... on ProjectV2ItemFieldDateValue{date field{... on ProjectV2Field{id}}}
    }}
  }}
}}}"""


def _date_values_from_pages(stdout: str,
                            field_ids: dict[str, str]) -> dict[str, dict[str, str | None]]:
    """Parse a complete `gh api graphql --paginate --slurp` date snapshot by Project item id."""
    pages = json.loads(stdout or "[]")
    if not isinstance(pages, list) or not pages:
        raise TypeError("date snapshot must contain at least one page")
    result = {}
    for page_index, page in enumerate(pages):
        if not isinstance(page, dict) or page.get("errors"):
            raise ValueError("date snapshot contains GraphQL errors")
        connection = page["data"]["node"]["items"]
        page_info = connection["pageInfo"]
        has_next = page_info["hasNextPage"]
        if not isinstance(has_next, bool) or has_next != (page_index < len(pages) - 1):
            raise ValueError("date snapshot outer pagination is incomplete")
        nodes = connection["nodes"]
        if not isinstance(nodes, list):
            raise TypeError("date snapshot items must be a list")
        for item in nodes:
            item_id = item["id"]
            values = item["fieldValues"]
            if not isinstance(item_id, str) or not item_id or item_id in result:
                raise ValueError("date snapshot contains an invalid or duplicate item id")
            if values["pageInfo"].get("hasNextPage") is not False:
                raise ValueError("date snapshot field-value pagination is incomplete")
            result[item_id] = _date_values_for_item(values["nodes"], field_ids)
    return result


def _date_values_for_item(nodes: list, field_ids: dict[str, str]) -> dict[str, str | None]:
    """Extract configured date kinds from one item's GraphQL field-value union nodes."""
    if not isinstance(nodes, list):
        raise TypeError("date snapshot field values must be a list")
    by_id = {field_id: kind for kind, field_id in field_ids.items()}
    result = {}
    for node in nodes:
        if node == {}:  # expected for non-date union members excluded by the inline fragment
            continue
        if not isinstance(node, dict):
            raise TypeError("date snapshot contains a malformed date value")
        field = node.get("field")
        field_id = field.get("id") if isinstance(field, dict) else None
        if not isinstance(field_id, str):
            raise TypeError("date snapshot contains a malformed date value")
        kind = by_id.get(field_id)
        if not kind:
            continue
        if "date" not in node or (node["date"] is not None and not isinstance(node["date"], str)):
            raise TypeError("date snapshot contains a malformed date value")
        if kind in result:
            raise ValueError("date snapshot contains duplicate values for one field")
        result[kind] = node["date"]
    return result


def hydrate_item_dates(cfg: dict, project_items: dict[str, dict],
                       field_meta: dict) -> dict[str, dict] | None:
    """Return copied parsed items with authoritative GraphQL dates mapped by field id.

    A successful snapshot represents cleared fields as None. None means the snapshot failed or was
    incomplete, so callers must skip all date reconciliation for the run rather than infer clears.
    """
    project_id = field_meta.get("project_id")
    host = field_meta.get("host")
    candidates = {kind: field_meta.get(f"{kind}_field_id") for kind in ("start", "target")}
    if (not isinstance(project_id, str) or not project_id
            or not isinstance(host, str) or not host
            or any(field_id is not None and not isinstance(field_id, str)
                   for field_id in candidates.values())):
        return None
    field_ids = {kind: field_id for kind, field_id in candidates.items() if field_id}
    if len(set(field_ids.values())) != len(field_ids):
        return None
    try:
        out = ghkit.run(cfg, ["api", "graphql", "--hostname", host, "--paginate", "--slurp",
                              "-f", f"query={_DATE_VALUES_QUERY}", "-f", f"project={project_id}"])
        snapshot = _date_values_from_pages(out.stdout, field_ids)
        hydrated = {}
        for url, item in project_items.items():
            item_id = item.get("item_id")
            if not isinstance(item_id, str) or item_id not in snapshot:
                raise ValueError("item-list and GraphQL date snapshots disagree")
            dates = snapshot[item_id]
            hydrated[url] = {**item, "start": dates.get("start"), "target": dates.get("target")}
        return hydrated
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError,
            KeyError, TypeError, ValueError, AttributeError, IndexError, FileNotFoundError):
        return None


def issue_status_map(cfg: dict) -> dict[str, str]:
    """issue URL -> Status option name (raw). Empty when unavailable."""
    parsed = items(cfg)
    if parsed is None:
        return {}
    return {url: v["status"] for url, v in parsed.items() if v.get("status")}


def field_meta(cfg: dict) -> dict | None:
    """{project_id, host, status_field_id, status_options{name_lower:id}, start_field_id, target_field_id}
    for Project field discovery and date writes. Status metadata feeds set_item_status's Status write
    path (Status option id lookup by lower-cased name) as well as the read side. None on failure.
    VALIDATE LIVE: gh project shapes."""
    if not configured(cfg):
        return None
    ctx = ghkit._repo_context(cfg)
    if ctx is None:  # can't resolve the target host -> fail closed rather than hit the default host
        return None
    p = cfg["gh_project"]
    try:
        proj = ghkit.run(cfg, ["project", "view", str(p["number"]), "--owner", p["owner"], "--format", "json"],
                         host=ctx.host)
        fl = ghkit.run(cfg, ["project", "field-list", str(p["number"]), "--owner", p["owner"],
                             "--limit", "200", "--format", "json"], host=ctx.host)  # default is only 30 fields
        # host is carried in the meta so the date-write path (set_project_date) pins the same
        # target host without re-resolving it per write.
        meta = {"project_id": json.loads(proj.stdout)["id"], "host": ctx.host, "status_field_id": None,
                "status_options": {}, "start_field_id": None, "target_field_id": None}
        for f in json.loads(fl.stdout).get("fields", []):
            name = (f.get("name") or "").strip().lower()
            if name == p["status_field"].strip().lower():
                meta["status_field_id"] = f.get("id")
                meta["status_options"] = {(o.get("name") or "").strip().lower(): o.get("id")
                                          for o in f.get("options", [])}
            elif name == p["start_field"].strip().lower():
                meta["start_field_id"] = f.get("id")
            elif name == p["target_field"].strip().lower():
                meta["target_field_id"] = f.get("id")
        return meta
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError, KeyError, SystemExit):
        return None


def set_project_date(cfg: dict, apply: bool, project_id: str, item_id: str, field_id: str,
                     date: str | None, host: str | None = None) -> bool:
    """Set (date=YYYY-MM-DD) or clear (date=None) a Projects v2 date field, through the dry-run gate.
    Returns True iff a write was actually issued (live PATCH or dry-run print); False when skipped
    because item_id or field_id is falsy -- callers use this to avoid advancing their merge-base
    when the GitHub-side write never happened. `host` pins the write to the resolved target host
    (carried in field_meta); `gh project item-edit` has no --hostname flag, so GH_HOST is its only
    host selector and a write must never fall back to the default host."""
    if not (item_id and field_id):
        return False
    args = ["project", "item-edit", "--id", item_id, "--project-id", project_id, "--field-id", field_id]
    args += (["--date", date] if date else ["--clear"])
    if apply:
        ghkit.run(cfg, args, host=host)
        print(f"gh    project item {item_id} date -> {date or 'cleared'}")
    else:
        print(f"DRY   gh project item-edit {item_id} {'--date ' + date if date else '--clear'}")
    return True


def add_item(cfg: dict, apply: bool, issue_url: str) -> str | None:
    """Add an issue to the configured Project (the "vet onto the board" write for the Intake latch),
    through the dry-run gate. Returns the new item id, or None when not configured or the write
    failed -- never raises.

    Dry run returns a deterministic placeholder (PLANNED_ITEM_ID_PREFIX + a truncated sha256 of the
    url) instead of calling gh at all, so a caller exercises the exact same str-shaped contract on
    both branches. Idempotency of a live re-add against an item already on the board is unverified
    here (the spike used mocks only) -- flagged for a live probe against a real Project.
    """
    if not configured(cfg):
        return None
    p = cfg["gh_project"]
    if not apply:
        placeholder = PLANNED_ITEM_ID_PREFIX + hashlib.sha256(issue_url.encode()).hexdigest()[:16]
        print(f"DRY   gh project item-add {p['number']} --owner {p['owner']} --url {issue_url} "
              "--format json")
        return placeholder
    ctx = ghkit._repo_context(cfg)
    if ctx is None:  # can't resolve the target host -> fail closed rather than hit the default host
        return None
    try:
        out = ghkit.run(cfg, ["project", "item-add", str(p["number"]), "--owner", p["owner"],
                              "--url", issue_url, "--format", "json"], host=ctx.host)
        payload = json.loads(out.stdout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError, SystemExit):
        return None
    # Valid-but-non-object JSON (null, an array) must also become a structured failure -- never
    # a TypeError mid-flow (never-raises contract, PR #71 review).
    item_id = payload.get("id") if isinstance(payload, dict) else None
    # A malformed id must become a structured failure here, never a TypeError later in argv
    # (issue #69) -- the never-raises contract covers gh misbehaving too.
    return item_id if isinstance(item_id, str) and item_id else None


def _status_write_meta(cfg: dict, stage: str) -> tuple[str, str, str, str | None] | None:
    """(project_id, field_id, option_id, host) for a Status write, or None when any part is
    missing or malformed. Every id must be a non-empty string BEFORE it can reach subprocess argv
    (issue #69): malformed gh output becomes a structured failure, never a mid-write TypeError."""
    meta = field_meta(cfg)
    if not isinstance(meta, dict):
        return None
    options = meta.get("status_options")
    option_id = options.get((stage or "").strip().lower()) if isinstance(options, dict) else None
    ids = (meta.get("project_id"), meta.get("status_field_id"), option_id)
    if not all(isinstance(value, str) and value for value in ids):
        return None
    return (*ids, meta.get("host"))


def can_set_status(cfg: dict, stage: str) -> bool:
    """Preflight for the vetting latch: True iff a Status write for `stage` is fully resolvable
    (field metadata present, every id well-formed, the option exists). Checked BEFORE add_item so
    a doomed Status write can never strand a status-less member on the board -- the half-state
    behind issue #69's delayed-demotion path. Never raises."""
    return _status_write_meta(cfg, stage) is not None


def set_item_status(cfg: dict, apply: bool, item_id: str, stage: str) -> bool:
    """Set a Project item's Status field to `stage` (matched case-insensitively against the
    configured Status field's options), through the dry-run gate. Returns True iff a write was
    actually issued (live PATCH or dry-run print); False when field_meta is unavailable, the Status
    field itself isn't configured on the board, `stage` matches none of its options, or any id is
    malformed -- never raises.

    Resolves field_meta(cfg) fresh on every call rather than accepting it as a parameter: main()'s own
    local is unconditionally nulled on boards with no Start/Target date fields configured, and reusing
    that would silently disable this write on a Status-only board.
    """
    if not (isinstance(item_id, str) and item_id):
        return False
    resolved = _status_write_meta(cfg, stage)
    if resolved is None:
        return False
    project_id, status_field_id, option_id, host = resolved
    args = ["project", "item-edit", "--id", item_id, "--project-id", project_id,
            "--field-id", status_field_id, "--single-select-option-id", option_id]
    if not apply:
        print(f"DRY   gh {' '.join(args)}")
        return True
    try:
        ghkit.run(cfg, args, host=host)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, SystemExit):
        return False
    print(f"gh    project item {item_id} status -> {stage}")
    return True
