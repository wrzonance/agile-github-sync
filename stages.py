"""Pure kanban-stage logic: derive each issue's stage from its own GitHub facts and match board lanes
to stages. No I/O -- exhaustively unit-tested.

Stage vocabulary mirrors the board columns: Backlog -> Ready -> In progress -> In review -> Done. As
issues advance, each card follows its issue. Epic helpers identify child connections only; an epic's
lane comes from the epic issue's own Status or fallback signals, not from task rollup.
"""
from __future__ import annotations

import re

STAGES = ("Intake", "Backlog", "Ready", "In progress", "In review", "Done")
RETIRED_STATE_REASONS = frozenset({"NOT_PLANNED", "DUPLICATE"})

# "Intake" is deliberately absent from the Project-Status vocabulary: board membership itself means
# vetted, so no explicit Status -- even one literally named "Intake" -- may ever resolve to the
# pre-board holding stage, flag on or off. Such a Status normalizes to None and the caller falls
# back to signal derivation, exactly the classic behavior (PR #68 review).
_STAGE_BY_LOWER = {s.lower(): s for s in STAGES if s != "Intake"}


def normalize_status(name: str) -> str | None:
    """Map a GitHub Projects v2 Status option name to a canonical stage (case-insensitive), or None if
    it isn't one of ours (caller then falls back to label/assignee/PR derivation)."""
    return _STAGE_BY_LOWER.get((name or "").strip().lower())

# LeanKit lane.cardStatus has only three values; In progress and In review both live under "started",
# so lanes are disambiguated by title (see lane_matches_stage).
#
# "Intake" has no board lane of its own -- it is a pre-board holding stage (see resolve_issue_stage in
# sync.py), never a lane-move target. Its entries here are deliberate no-ops (falsy status, empty hint
# tuple) so resolve_lane_for_stage's STAGES-wide ambiguity walk, which indexes both dicts for every
# stage in STAGES, stays byte-identical for every OTHER stage now that STAGES includes "Intake".
STAGE_CARD_STATUS = {
    "Intake": "notStarted",
    "Backlog": "notStarted",
    "Ready": "notStarted",
    "In progress": "started",
    "In review": "started",
    "Done": "finished",
}

STAGE_TITLE_HINTS = {
    "Intake": (),
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

    sync.resolve_issue_stage's "Intake" branch fires only on this function's bare-else fallback, so
    that fallback must keep returning exactly "Backlog" -- never a different sentinel -- or that
    branch silently stops matching.
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


def epic_key_for_task(task_key: str) -> str | None:
    """Convention fallback used only when native sub-issues are unavailable: task key '0C2' -> epic
    key 'EP-0C' (strip the trailing task number)."""
    if not task_key:
        return None
    core = task_key.rstrip("0123456789")
    return f"EP-{core}" if core else None


def issue_custom_id(issue: dict) -> str:
    """The customId written to and read from AgilePlace for one GitHub issue."""
    return title_key(issue["title"]) or str(issue["number"])


# issue #93: the header format written to a card's customId. Only the FINAL ' (GitHub Issue #N)'
# suffix is meaningful; header_match_key's greedy group leaves any earlier lookalike text intact.
_KEYED_HEADER_RE = re.compile(r"(?s)(.+) \(GitHub Issue #\d+\)")
_BARE_HEADER_RE = re.compile(r"GitHub Issue #(\d+)")


def issue_card_header(issue: dict) -> str:
    """The customId header WRITTEN to a card: the sync key plus a visible GitHub issue reference
    ('0C1 (GitHub Issue #5)'), or bare 'GitHub Issue #5' when the title carries no [KEY] (the
    keyed form would redundantly read '5 (GitHub Issue #5)'). issue_custom_id() stays the MATCH
    key; header_match_key() is this format's exact inverse."""
    key = title_key(issue["title"])
    number = issue["number"]
    return f"{key} (GitHub Issue #{number})" if key else f"GitHub Issue #{number}"


def header_match_key(value: str | None) -> str:
    """The MATCH key encoded in a card's customId header -- the exact inverse of
    issue_card_header(), applied to every card-side read so old-format ('0C1') and header-format
    ('0C1 (GitHub Issue #5)') cards resolve to the same key during the transition. A bare
    'GitHub Issue #5' folds to '5' (the unkeyed fallback key). Any other value (old-format,
    human-authored, smoke) passes through unchanged; None/empty normalizes to ''."""
    value = value or ""
    keyed = _KEYED_HEADER_RE.fullmatch(value)
    if keyed:
        return keyed.group(1)
    bare = _BARE_HEADER_RE.fullmatch(value)
    if bare:
        return bare.group(1)
    return value
