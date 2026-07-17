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


def _field(item: dict, name: str, *alts: str):
    """A Projects v2 field value off a `gh project item-list` row. gh flattens field values as top-level
    keys; the exact casing varies, so try the configured name, its lower-case form, and any aliases."""
    for key in (name, name.lower(), *alts):
        if key in item and item[key] not in (None, ""):
            return item[key]
    return None


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


def items(cfg: dict) -> dict[str, dict] | None:
    """All project items keyed by issue URL, or None on failure/not-configured (caller falls back)."""
    if not configured(cfg):
        return None
    p = cfg["gh_project"]
    try:
        out = ghkit.run(cfg, ["project", "item-list", str(p["number"]), "--owner", p["owner"],
                              "--format", "json", "--limit", "1000"])
        data = json.loads(out.stdout or "{}")
        return parse_items(data.get("items", []), p["status_field"], p["start_field"], p["target_field"])
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError, KeyError, SystemExit):
        return None


def issue_status_map(cfg: dict) -> dict[str, str]:
    """issue URL -> Status option name (raw). Empty when unavailable."""
    parsed = items(cfg)
    if parsed is None:
        return {}
    return {url: v["status"] for url, v in parsed.items() if v.get("status")}
