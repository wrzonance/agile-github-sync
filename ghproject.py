"""GitHub Projects v2 — the Status source of truth (Phase 1), later also dates (Phase 4).

Uses the `gh project` CLI (owner-scoped; runs through ghkit.run so it inherits the target cwd). Reads
are one `item-list` call; writes (Phase 1b/4) go through the dry-run gate. Parsing is a pure function so
it's unit-tested against a fixture; the live `gh project` shape is validated at first real run (no
Project is reachable offline). Requires the `project` token scope: `gh auth refresh -s project`.
"""
from __future__ import annotations

import json
import subprocess

import ghkit


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


def _field_key_seen(item: dict, name: str, *alts: str) -> bool:
    """True if ANY candidate key for this field is present in `item`, regardless of value -- including
    present-but-empty. Presence-only; never used for value reads. Distinguishes a genuinely-unset field
    (key present, value empty) from a field gh never exposed under any known key (key truly absent)."""
    return any(key in item for key in _field_candidates(name, *alts))


def parse_items(items: list, status_field: str = "Status", start_field: str = "Start",
                target_field: str = "Target") -> dict[str, dict]:
    """Map issue URL -> {item_id, number, status, start, target} from `gh project item-list` JSON.
    Skips non-issue content (draft items / PRs without a URL are simply absent)."""
    result = {}
    for it in items:
        content = it.get("content") or {}
        url = content.get("url")
        if not url:
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
    p = cfg["gh_project"]
    try:
        out = ghkit.run(cfg, ["project", "item-list", str(p["number"]), "--owner", p["owner"],
                              "--format", "json", "--limit", "1000"])
        data = json.loads(out.stdout or "{}")
        return data.get("items", [])
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError, KeyError, SystemExit):
        return None


def items_and_raw(cfg: dict) -> tuple[dict[str, dict] | None, list | None]:
    """(parse_items(...) keyed by issue URL, raw item-list rows), or (None, None) on failure/not-
    configured. The raw rows let callers (e.g. unmatched_date_kinds) inspect field keys gh actually
    exposed, beyond what parse_items already extracted."""
    raw = _fetch_raw_items(cfg)
    if raw is None:
        return None, None
    p = cfg["gh_project"]
    try:
        parsed = parse_items(raw, p["status_field"], p["start_field"], p["target_field"])
    except KeyError:
        return None, None
    return parsed, raw


def items(cfg: dict) -> dict[str, dict] | None:
    """All project items keyed by issue URL, or None on failure/not-configured (caller falls back)."""
    return items_and_raw(cfg)[0]


def unmatched_date_kinds(raw_items: list | None, field_meta: dict | None, start_field: str,
                          target_field: str, known_kinds: frozenset[str] = frozenset()) -> frozenset[str]:
    """Kinds ("start"/"target") that USED TO read real values (kind in `known_kinds` -- some issue's
    merge-base previously held a non-empty value for it, see sync.known_date_kinds) but NOW no raw item
    row exposes a matching key for that field's name at all -- a regression signal worth warning about
    before dates silently stop syncing (issue #6's two-run misread-as-deletion scenario).

    A kind with NO known history is never flagged on zero-match alone: `gh project item-list` only
    flattens a custom field's key onto an item that actually carries a value, so a field that has never
    been set on any item -- the common case on a project's first rollout, even with the field
    correctly configured -- is indistinguishable from a genuine name mismatch by key-presence alone.
    `known_kinds` is what tells "used to work, now doesn't" apart from "never used".

    A row that HAS the key with an empty/null value does NOT count as a mismatch either way (that's a
    normal unset field, the common case). Pure: no I/O, no printing. frozenset() when raw_items or
    field_meta is falsy/empty."""
    if not raw_items or not field_meta:
        return frozenset()
    checks = (("start", start_field, field_meta.get("start_field_id")),
              ("target", target_field, field_meta.get("target_field_id")))
    return frozenset(kind for kind, name, field_id in checks
                      if field_id and kind in known_kinds
                      and not any(_field_key_seen(row, name) for row in raw_items))


def issue_status_map(cfg: dict) -> dict[str, str]:
    """issue URL -> Status option name (raw). Empty when unavailable."""
    parsed = items(cfg)
    if parsed is None:
        return {}
    return {url: v["status"] for url, v in parsed.items() if v.get("status")}


def issue_dates_map(cfg: dict) -> dict[str, dict]:
    """issue URL -> {start, target, item_id} from the Project (Phase 4). Empty when unavailable."""
    parsed = items(cfg)
    if parsed is None:
        return {}
    return {url: {"start": v["start"], "target": v["target"], "item_id": v["item_id"]}
            for url, v in parsed.items()}


def field_meta(cfg: dict) -> dict | None:
    """{project_id, status_field_id, status_options{name_lower:id}, start_field_id, target_field_id} for
    writes (Status in Phase 1b, dates in Phase 4). None on failure. VALIDATE LIVE: gh project shapes."""
    if not configured(cfg):
        return None
    p = cfg["gh_project"]
    try:
        proj = ghkit.run(cfg, ["project", "view", str(p["number"]), "--owner", p["owner"], "--format", "json"])
        fl = ghkit.run(cfg, ["project", "field-list", str(p["number"]), "--owner", p["owner"],
                             "--limit", "200", "--format", "json"])  # default is only 30 fields
        meta = {"project_id": json.loads(proj.stdout)["id"], "status_field_id": None,
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


def set_project_date(cfg: dict, apply: bool, project_id: str, item_id: str, field_id: str, date: str | None) -> bool:
    """Set (date=YYYY-MM-DD) or clear (date=None) a Projects v2 date field, through the dry-run gate.
    Returns True iff a write was actually issued (live PATCH or dry-run print); False when skipped
    because item_id or field_id is falsy -- callers use this to avoid advancing their merge-base
    when the GitHub-side write never happened."""
    if not (item_id and field_id):
        return False
    args = ["project", "item-edit", "--id", item_id, "--project-id", project_id, "--field-id", field_id]
    args += (["--date", date] if date else ["--clear"])
    if apply:
        ghkit.run(cfg, args)
        print(f"gh    project item {item_id} date -> {date or 'cleared'}")
    else:
        print(f"DRY   gh project item-edit {item_id} {'--date ' + date if date else '--clear'}")
    return True
