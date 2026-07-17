"""Pure kanban-stage logic: derive an issue's stage from GitHub facts, roll an epic up from its tasks,
and match board lanes to stages. No I/O -- exhaustively unit-tested.

Stage vocabulary mirrors the board columns: Backlog -> Ready -> In progress -> In review -> Done. As
issues advance, an epic's card advances lane-for-lane.
"""
from __future__ import annotations

STAGES = ("Backlog", "Ready", "In progress", "In review", "Done")

_STAGE_BY_LOWER = {s.lower(): s for s in STAGES}


def normalize_status(name: str) -> str | None:
    """Map a GitHub Projects v2 Status option name to a canonical stage (case-insensitive), or None if
    it isn't one of ours (caller then falls back to label/PR derivation)."""
    return _STAGE_BY_LOWER.get((name or "").strip().lower())

# LeanKit lane.cardStatus has only three values; In progress and In review both live under "started",
# so lanes are disambiguated by title (see lane_matches_stage).
STAGE_CARD_STATUS = {
    "Backlog": "notStarted",
    "Ready": "notStarted",
    "In progress": "started",
    "In review": "started",
    "Done": "finished",
}

STAGE_TITLE_HINTS = {
    "Backlog": ("backlog", "not started", "todo"),
    "Ready": ("ready", "planned"),
    "In progress": ("in progress", "doing", "in flight"),
    "In review": ("in review", "review"),
    "Done": ("done", "finished", "complete"),
}


def issue_stage(issue: dict) -> str:
    """One issue's stage from its GitHub facts.

    issue = {"state": "OPEN"|"CLOSED", "labels": [str], "has_open_pr": bool, "assignees": [str]}
    """
    if str(issue.get("state", "")).upper() == "CLOSED":
        return "Done"
    labels = set(issue.get("labels", []))
    if "agent:in-review" in labels or issue.get("has_open_pr"):
        return "In review"
    if "agent:in-progress" in labels or issue.get("assignees"):
        return "In progress"
    if "agent:ready" in labels:
        return "Ready"
    return "Backlog"


def epic_rollup(task_stages: list[str]) -> str:
    """An epic's card stage rolled up from its tasks' stages.

    Policy (intentional, NOT a monotonic high-water mark): the least-advanced *active* work drives the
    epic. If a task starts fresh work, the epic legitimately shows 'In progress' even if another task
    was 'In review' -- the card reflects current reality, not the furthest any task ever reached.
    """
    if not task_stages:
        return "Backlog"
    stages = set(task_stages)
    if stages == {"Done"}:
        return "Done"
    if "In progress" in stages:
        return "In progress"
    if "In review" in stages:
        return "In review"
    if "Done" in stages:  # some done, rest not yet active -> work has started
        return "In progress"
    if "Ready" in stages:
        return "Ready"
    return "Backlog"


def lane_matches_stage(lane_title: str, stage: str) -> bool:
    """True if a lane's title denotes the given stage. Word-boundary substring match so real-world lane
    names resolve ('Ready to Start' -> Ready, 'Code Review' -> In review) without false hits inside
    other words ('Already Done' does NOT match Ready). The hint sets stay disjoint, so In progress and
    In review remain distinct."""
    t = (lane_title or "").strip().lower()
    if not t:
        return False
    padded = f" {t}"
    return any(t == h or f" {h}" in padded for h in STAGE_TITLE_HINTS[stage])


def title_key(title: str) -> str | None:
    """The [KEY] prefix of an issue title (e.g. '[EP-0C] ...' -> 'EP-0C'), or None."""
    title = title or ""
    if title.startswith("[") and "]" in title:
        return title[1:title.index("]")].strip() or None
    return None


def blocked_reason(blockers: list[int], stage_by_number: dict) -> str | None:
    """A card is Blocked while any of its GitHub blocked-by issues isn't Done. Returns the reason string
    (naming the incomplete blockers) or None when nothing incomplete blocks it. Pure -- unit-tested."""
    incomplete = sorted(b for b in blockers if stage_by_number.get(b) != "Done")
    if not incomplete:
        return None
    return "Blocked by " + ", ".join(f"#{b}" for b in incomplete)


def epic_key_for_task(task_key: str) -> str | None:
    """Convention fallback used only when native sub-issues are unavailable: task key '0C2' -> epic
    key 'EP-0C' (strip the trailing task number)."""
    if not task_key:
        return None
    core = task_key.rstrip("0123456789")
    return f"EP-{core}" if core else None
