"""Pure kanban-stage logic: derive each issue's stage from its own GitHub facts and match board lanes
to stages. No I/O -- exhaustively unit-tested.

Stage vocabulary mirrors the board columns: Backlog -> Ready -> In progress -> In review -> Done. As
issues advance, each card follows its issue. Epic helpers identify child connections only; an epic's
lane comes from the epic issue's own Status or fallback signals, not from task rollup.
"""
from __future__ import annotations

STAGES = ("Backlog", "Ready", "In progress", "In review", "Done")
RETIRED_STATE_REASONS = frozenset({"NOT_PLANNED", "DUPLICATE"})

_STAGE_BY_LOWER = {s.lower(): s for s in STAGES}


def normalize_status(name: str) -> str | None:
    """Map a GitHub Projects v2 Status option name to a canonical stage (case-insensitive), or None if
    it isn't one of ours (caller then falls back to label/assignee/PR derivation)."""
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


def title_contains_phrase(title: str, phrase: str) -> bool:
    """Whether ``phrase`` appears in ``title`` between space-delimited word boundaries."""
    normalized_title = (title or "").strip().lower()
    normalized_phrase = (phrase or "").strip().lower()
    if not normalized_title or not normalized_phrase:
        return False
    return f" {normalized_phrase} " in f" {normalized_title} "


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


def is_retired_issue(issue: dict) -> bool:
    """Whether GitHub closed an issue as work that will not be completed here."""
    return (str(issue.get("state", "")).upper() == "CLOSED"
            and str(issue.get("state_reason", "")).upper() in RETIRED_STATE_REASONS)


def lane_matches_stage(lane_title: str, stage: str) -> bool:
    """True if a lane's title denotes the given stage. Word-boundary substring match so real-world lane
    names resolve ('Ready to Start' -> Ready, 'Code Review' -> In review) without false hits inside
    other words ('Already Done' does NOT match Ready). The hint sets stay disjoint, so In progress and
    In review remain distinct."""
    return any(title_contains_phrase(lane_title, hint) for hint in STAGE_TITLE_HINTS[stage])


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
