"""Config for the ongoing GitHub->AgilePlace sync. Stdlib only. No instance-specific defaults in code
(security rule): everything comes from .env / environment, with .env.example carrying the values.
"""
from __future__ import annotations

import os
from pathlib import Path

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
    }
