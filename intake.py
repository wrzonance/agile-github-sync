"""Reverse intake (issue #62): promote unmanaged AgilePlace cards -- ones sitting in the board's
designated Intake lane with no GitHub link and no known customId -- into new GitHub issues, then
write the link/customId back so the ordinary one-way sync adopts them.

This module owns candidate selection and the AgilePlace-shaped read helpers (card_created_by_name,
op_external_link, card_web_url) that support it. They live HERE rather than in agileplace.py: this
module is their sole consumer, and agileplace.py has no file-budget headroom left (805/800 lines
before this feature existed) to absorb them without breaking the 800-line hard cap.

Candidate selection scope, from the issue: a card qualifies only when it sits in a lane the board's
STAGE_LANE_MAP maps to the "Intake" stage, carries no external link matching a known target-repo
issue URL, and carries no customId matching an existing issue's issue_custom_id. Matching is strict
and existence-based throughout -- never a format guess -- so a foreign link (to Jira, a doc, another
repo entirely) or a stale/unrecognized customId never disqualifies a card by itself.
"""
from __future__ import annotations

import agileplace
from stages import issue_custom_id

# Embedded verbatim in every promoted issue's body so a create-then-writeback crash can be resumed
# by search: `str.format` with a single `card_id` placeholder, coerced to str before formatting.
MARKER_TEMPLATE = "<!-- agile-github-sync:agileplace-card={card_id} -->"


def marker_for_card(card_id) -> str:
    """The resume marker for one AgilePlace card, embedded in its promoted issue's body."""
    return MARKER_TEMPLATE.format(card_id=str(card_id))


def card_created_by_name(card: dict) -> str | None:
    """Best-effort human name for a card's creator, from AgilePlace's `createdBy` field.

    VALIDATE LIVE (see API-VALIDATION.md): the #57 probe confirmed `createdBy` is present on card
    reads; whether it carries a full io v2 user object (fullName/emailAddress) or a bare id string
    is unconfirmed -- no live probe was possible in this worktree (no .env). Defensive against
    either shape (and anything malformed) so this never raises: a dict yields fullName, falling
    back to emailAddress; any other shape (bare id string, None, list, empty dict) yields None
    rather than surfacing something that isn't actually a human-readable name."""
    created_by = card.get("createdBy")
    if not isinstance(created_by, dict):
        return None
    name = created_by.get("fullName") or created_by.get("emailAddress")
    return name.strip() if isinstance(name, str) and name.strip() else None


def provenance_line(name: str | None) -> str:
    """One line of issue-body provenance text. Never raises and always returns a non-empty string,
    for any input -- including malformed values that aren't actually `str | None`, since this sits
    directly ahead of issue-body text a human will read."""
    if isinstance(name, str) and name.strip():
        return f"Requested by {name.strip()} via AgilePlace."
    return "Requested via AgilePlace (creator name unavailable)."


def op_external_link(label: str, url: str) -> dict:
    """RFC-6902 op linking a card to its promoted GitHub issue. `add` on the singular `/externalLink`
    path: AgilePlace applies `add` as replace when the path is already occupied (confirmed live, see
    API-VALIDATION.md), so this covers both a bare card and one that already carries a link. Does
    NOT attempt the plural `/externalLinks` array shape -- unconfirmed, out of scope here."""
    return {"op": "add", "path": "/externalLink", "value": {"label": label, "url": url}}


def card_web_url(cfg: dict, card_id) -> str:
    """Best-effort human-facing (web app) URL for one card, embedded in its promoted issue's body.

    UNCONFIRMED (see API-VALIDATION.md): no separate web-app host config key exists, and no live
    probe was possible in this worktree. Falls back to `cfg.get("host")` (AGILEPLACE_HOST) as an
    explicitly-flagged best guess -- the same host every other AgilePlace URL in this codebase is
    built from (see agileplace.api()). A missing host still returns a usable (if host-less) string
    rather than raising."""
    host = cfg.get("host") or ""
    return f"https://{host}/card/{card_id}"


def _card_lane_id(card: dict) -> str:
    """Same coercion convention sync.py already uses for a card's current lane id."""
    return str(card.get("laneId") or (card.get("lane") or {}).get("id") or "")


def _intake_lane_ids(lanes: list, stage_map: dict | None) -> set[str]:
    """Acceptable lane ids for the "Intake" stage, or the empty set if the feature is unconfigured.

    Guards on `stage_map` and `"Intake" in stage_map` BEFORE delegating to
    agileplace.resolve_lane_for_stage: STAGE_CARD_STATUS["Intake"] == "notStarted" is shared with
    Backlog/Ready, and STAGE_TITLE_HINTS["Intake"] is intentionally empty, so resolve_lane_for_stage's
    unmapped-stage inference fallback could otherwise latch onto an unrelated notStarted leaf lane.
    Missing/empty stage_map or no "Intake" key means the feature no-ops silently -- no candidates,
    zero AgilePlace calls."""
    if not stage_map or "Intake" not in stage_map:
        return set()
    _, acceptable = agileplace.resolve_lane_for_stage(lanes, "Intake", "", stage_map, quiet=True)
    return acceptable


def _disqualifying_custom_ids(issues: list[dict]) -> set[str]:
    """The set of customId values already claimed by an existing GitHub issue, over the FULL issues
    list. Strict existence-based matching -- a card's customId disqualifies it only if it actually
    matches one of these, never by looking like a plausible id format."""
    return {issue_custom_id(issue) for issue in issues}


def _is_candidate(card: dict, intake_lane_ids: set[str], target_urls: set[str],
                   managed_custom_ids: set[str]) -> bool:
    """Pure predicate: does this card need a new GitHub issue?

    A card qualifies only when its current lane is one of `intake_lane_ids`, it carries no external
    link matching `target_urls` (the known target-repo issue URLs), and its customId doesn't match
    any of `managed_custom_ids`. A foreign external link (anything not in `target_urls`) or an
    unrecognized customId never disqualifies a card by itself -- only an actual match does."""
    if _card_lane_id(card) not in intake_lane_ids:
        return False
    if set(agileplace.card_external_urls(card)) & target_urls:
        return False
    return agileplace.custom_id_value(card) not in managed_custom_ids


def intake_candidates(cards: list[dict], lanes: list, stage_map: dict | None,
                       issues: list[dict]) -> list[dict]:
    """Cards eligible for promotion into a new GitHub issue, in `cards`' own order. Pure -- reads
    `cards`, `lanes`, `stage_map`, and `issues` without mutating any of them. Returns [] outright
    (zero AgilePlace/GitHub calls from any caller) whenever the Intake stage isn't configured.

    `issues` is the already-fetched, title-bearing ghkit.list_issues() snapshot (same one sync.py's
    main loop uses) -- not ghkit.list_issue_bodies()'s separate, tri-state, body-bearing read that
    the marker-resume path uses further down the promotion flow."""
    intake_lane_ids = _intake_lane_ids(lanes, stage_map)
    if not intake_lane_ids:
        return []
    target_urls = {issue["url"] for issue in issues}
    managed_custom_ids = _disqualifying_custom_ids(issues)
    return [card for card in cards
            if _is_candidate(card, intake_lane_ids, target_urls, managed_custom_ids)]


def _issue_body(card: dict, cfg: dict) -> str:
    """The body text for a card's promoted issue: provenance line, a link back to the card, and the
    resume marker. No description translation -- the card's own description is out of scope."""
    lines = [
        provenance_line(card_created_by_name(card)),
        "",
        f"AgilePlace card: {card_web_url(cfg, card.get('id'))}",
        "",
        marker_for_card(card.get("id")),
    ]
    return "\n".join(lines)
