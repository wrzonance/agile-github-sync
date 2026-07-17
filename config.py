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


def load_env_file() -> None:
    """Load KEY=VALUE lines from ./.env; real environment variables win over it."""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key.strip(), value)


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
        "gh_project": {
            "owner": os.environ.get("GH_PROJECT_OWNER") or None,
            "number": os.environ.get("GH_PROJECT_NUMBER") or None,
            "status_field": os.environ.get("GH_PROJECT_STATUS_FIELD", "Status"),
            "start_field": os.environ.get("GH_PROJECT_START_FIELD", "Start"),
            "target_field": os.environ.get("GH_PROJECT_TARGET_FIELD", "Target"),
        },
    }
