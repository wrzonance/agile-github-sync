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

from typing import NamedTuple

import agileplace
import ghkit
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


class IntakeSummary(NamedTuple):
    """Outcome of one promote() run -- both the caller's summary print and what tests assert on.

    candidates: how many cards intake_candidates() selected this run.
    prescan_failed: True iff ghkit.list_issue_bodies() returned None -- the marker-resume snapshot
        was unusable, so every write for the run was skipped; the candidates remain candidates
        again next run (nothing here is persisted as "handled").
    resumed: candidates whose promoted issue already existed (found via its resume marker) and
        only needed writeback -- recovery from a prior run that crashed between create and writeback.
    created: candidates for which a brand-new GitHub issue was actually created (apply=True only --
        a dry-run "would create" plan never increments this, since nothing was actually created).
    """
    candidates: int
    prescan_failed: bool
    resumed: int
    created: int


def _find_marked_issue(card_id, issues_with_bodies: list[dict]) -> dict | None:
    """The first issue (in the given order -- no dedicated index; scale here doesn't justify one)
    whose body already carries this card's resume marker, for crash recovery between a prior run's
    create_issue and its writeback. None when no issue carries it."""
    marker = marker_for_card(card_id)
    return next((issue for issue in issues_with_bodies if marker in issue.get("body", "")), None)


def _writeback_key(card_title: str, issue_number: int) -> str:
    """The customId written back onto a promoted card, via the SAME [KEY]-prefix convention every
    other customId in this codebase uses (stages.issue_custom_id) -- computed from the CARD's own
    title, never fetched back from GitHub."""
    return issue_custom_id({"title": card_title, "number": issue_number})


def _writeback(cfg: dict, apply: bool, card: dict, issue: dict) -> None:
    """Write a promoted issue's link and customId back onto its AgilePlace card, as two SEPARATE
    patch_card calls -- never batched into one PATCH -- so a link-write failure can never block the
    customId write.

    The link write is skipped (with a WARN) when `card` already carries the plural, array-shaped
    `externalLinks` field: agileplace._card_value_for_patch_path has no case for the singular
    `/externalLink` path this feature writes, so a 409/428 conflict on that write can never retry --
    it unconditionally re-raises (see API-VALIDATION.md). This is safe/fails-closed by design
    (marker-resume recovers it next run), but is a real, intentional asymmetry against the customId
    write's normal one-retry support. The customId write always proceeds either way."""
    card_id = card.get("id")
    if "externalLinks" in card:
        print(f"WARN  card {card_id}: has array-shaped externalLinks -- skipping intake link "
              "writeback (unsupported shape); customId writeback still proceeds")
    else:
        link_op = op_external_link(f"GitHub #{issue['number']}", issue["url"])
        agileplace.patch_card(cfg, apply, card, [link_op], note=f"intake link -> {issue['url']}")
    key = _writeback_key(card.get("title", ""), issue["number"])
    agileplace.patch_card(cfg, apply, card, [agileplace.op_custom_id(key)],
                          note=f"intake customId -> {key}")


def _resume_or_create(cfg: dict, apply: bool, card: dict,
                      issues_with_bodies: list[dict]) -> tuple[dict | None, bool]:
    """One candidate's issue: (issue_or_None, resumed). `issue` is None only for a dry-run plan with
    no prior marker match -- create_issue's own dry-run gate returns None without writing anything."""
    marked = _find_marked_issue(card.get("id"), issues_with_bodies)
    if marked is not None:
        return marked, True
    created = ghkit.create_issue(cfg, apply, card.get("title", ""), _issue_body(card, cfg))
    return created, False


def promote(cfg: dict, apply: bool, cards: list[dict], lanes: list, stage_map: dict | None,
           issues: list[dict]) -> IntakeSummary:
    """Promote every Intake candidate into a GitHub issue (or resume one already created by a prior,
    interrupted run), then write the link/customId back so the ordinary one-way sync adopts it next
    cycle. Never moves a card's lane -- that stays the ordinary sync's job.

    Zero AgilePlace/GitHub calls when there are no candidates. Any create_issue/patch_card failure
    propagates uncaught -- no in-run retry; recovery is the next cycle's marker-based resume scan."""
    candidates = intake_candidates(cards, lanes, stage_map, issues)
    if not candidates:
        return IntakeSummary(candidates=0, prescan_failed=False, resumed=0, created=0)
    issues_with_bodies = ghkit.list_issue_bodies(cfg)
    if issues_with_bodies is None:
        print("WARN  intake: could not read issue bodies for marker-resume scan -- skipping "
              f"all {len(candidates)} candidate(s) this run")
        return IntakeSummary(candidates=len(candidates), prescan_failed=True, resumed=0, created=0)
    resumed = created = 0
    for card in candidates:
        issue, was_resumed = _resume_or_create(cfg, apply, card, issues_with_bodies)
        if issue is None:
            print(f"DRY   intake: would create issue for card {card.get('id')} "
                  f"({card.get('title', '')!r})")
            continue
        if was_resumed:
            resumed += 1
        else:
            created += 1
        _writeback(cfg, apply, card, issue)
    return IntakeSummary(candidates=len(candidates), prescan_failed=False,
                         resumed=resumed, created=created)
