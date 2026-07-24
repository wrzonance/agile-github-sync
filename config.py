"""Config for the ongoing GitHub->AgilePlace sync. Stdlib only. No instance-specific defaults in code
(security rule): everything comes from .env / environment, with .env.example carrying the values.
"""
from __future__ import annotations

import os
from pathlib import Path

from stages import STAGES

REPO_DIR = Path(__file__).resolve().parent
ENV_FILE = REPO_DIR / ".env"
STATE_FILE = REPO_DIR / ".sync-state.json"

# Lifecycle labels that drive lane movement (see stages.py) rather than being mirrored as tags. These
# are filtered from BOTH sides and the base before reconciling. Extend via .env LABEL_SYNC_IGNORE.
DEFAULT_IGNORE = ("agent:in-progress", "agent:in-review", "agent:ready")

# Never let .env inject a gh repo/host override (issue #15): ghkit.py strips these from every gh
# subprocess's env regardless of source, and this blocklist keeps them from ever reaching
# os.environ.setdefault in the first place. Declared independently of ghkit.py's own
# _GH_ENV_OVERRIDE_KEYS -- only two call sites total, and the two modules have no existing import
# relationship in either direction; sharing one constant for two string literals would be
# over-abstraction for no benefit.
_ENV_LOADER_BLOCKLIST = frozenset({"GH_REPO", "GH_HOST"})

# AgilePlace's `description` field length limit is undocumented (VALIDATE LIVE -- see
# API-VALIDATION.md); this is a conservative ceiling picked to stay well under every publicly
# documented LeanKit/AgilePlace card-field limit we could find. description_sync truncates (with
# TRUNCATION_MARKER appended) rather than risking a write-time 4xx from an oversized description.
DEFAULT_AP_DESCRIPTION_MAX_LENGTH = 20000


def load_env_file() -> None:
    """Load KEY=VALUE lines from ./.env; real environment variables win over it. GH_REPO/GH_HOST are
    never set this way (see _ENV_LOADER_BLOCKLIST) -- they must never silently retarget which repo/host
    every gh call operates against."""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if value and key not in _ENV_LOADER_BLOCKLIST:
            os.environ.setdefault(key, value)


def parse_stage_lane_map(raw: str) -> dict[str, list[str]]:
    """Parse STAGE_LANE_MAP: ';'-separated 'Stage=Lane' entries, '|' for multiple lanes per stage.

    Stage names match the canonical STAGES case-insensitively. For a stage's lane list the FIRST lane
    is the move-to target; ALL listed lanes count as 'already in that stage' (so the sync won't shuffle
    a card between equivalent lanes -- e.g. New Requests|Approved both meaning Backlog). Unmapped stages
    fall back to title/cardStatus inference.
    """
    canonical = {s.lower(): s for s in STAGES}
    mapping: dict[str, list[str]] = {}
    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        stage_raw, _, lanes_raw = entry.partition("=")
        stage = canonical.get(stage_raw.strip().lower())
        lane_list = [l.strip() for l in lanes_raw.split("|") if l.strip()]
        if stage and lane_list:
            mapping[stage] = lane_list
    return mapping


def _parse_ap_description_max_length(raw: str | None) -> int:
    """AP_DESCRIPTION_MAX_LENGTH from .env/environment, falling back to
    DEFAULT_AP_DESCRIPTION_MAX_LENGTH (with one WARN) on anything that isn't a positive int -- a
    malformed override must never silently disable truncation (e.g. becoming 0/negative) or crash
    env_config() outright."""
    if raw is None or not raw.strip():
        return DEFAULT_AP_DESCRIPTION_MAX_LENGTH
    try:
        value = int(raw.strip())
    except ValueError:
        print(f"WARN  AP_DESCRIPTION_MAX_LENGTH={raw!r} is not an integer -- using default "
              f"{DEFAULT_AP_DESCRIPTION_MAX_LENGTH}")
        return DEFAULT_AP_DESCRIPTION_MAX_LENGTH
    if value <= 0:
        print(f"WARN  AP_DESCRIPTION_MAX_LENGTH={raw!r} must be a positive integer -- using default "
              f"{DEFAULT_AP_DESCRIPTION_MAX_LENGTH}")
        return DEFAULT_AP_DESCRIPTION_MAX_LENGTH
    return value


def _parse_comment_sync_identity(gh_login: str | None, ap_author: str | None) -> dict | None:
    """PURE -- no I/O, no print/WARN (issue #66 Task 1, finding #1): the self-disable WARN for
    comment sync belongs to comment_sync.sync_comments' first real invocation, not to config parsing,
    because two live suites assert env_config() never prints as a side effect of unrelated config.
    Both COMMENT_SYNC_GH_LOGIN and COMMENT_SYNC_AP_AUTHOR present and non-blank -> the identity dict;
    either missing or blank -> None, silently -- comment sync self-disables without a word here."""
    if gh_login is None or ap_author is None:
        return None
    gh_login = gh_login.strip()
    ap_author = ap_author.strip()
    if not gh_login or not ap_author:
        return None
    return {"gh_login": gh_login, "ap_author": ap_author}


def env_config() -> dict:
    """token/host/board_id are None when absent (offline dry run). target_repo_path is the local clone
    every `gh` call runs against."""
    load_env_file()
    target = os.environ.get("TARGET_REPO_PATH") or None
    ignore = os.environ.get("LABEL_SYNC_IGNORE", "")
    extra = {p.strip() for p in ignore.split(",") if p.strip()}
    return {
        "token": os.environ.get("AGILEPLACE_TOKEN") or None,
        "host": os.environ.get("AGILEPLACE_HOST") or None,
        "board_id": os.environ.get("AGILEPLACE_BOARD_ID") or None,
        "target_repo_path": Path(target).expanduser() if target else None,
        "label_sync_ignore": frozenset(DEFAULT_IGNORE) | frozenset(extra),
        "stage_lane_map": parse_stage_lane_map(os.environ.get("STAGE_LANE_MAP", "")),
        "ap_description_max_length": _parse_ap_description_max_length(
            os.environ.get("AP_DESCRIPTION_MAX_LENGTH")),
        "comment_sync_identity": _parse_comment_sync_identity(
            os.environ.get("COMMENT_SYNC_GH_LOGIN"), os.environ.get("COMMENT_SYNC_AP_AUTHOR")),
        "gh_project": {
            "owner": os.environ.get("GH_PROJECT_OWNER") or None,
            "number": os.environ.get("GH_PROJECT_NUMBER") or None,
            "status_field": os.environ.get("GH_PROJECT_STATUS_FIELD", "Status"),
            "start_field": os.environ.get("GH_PROJECT_START_FIELD", "Start"),
            "target_field": os.environ.get("GH_PROJECT_TARGET_FIELD", "Target"),
        },
    }
