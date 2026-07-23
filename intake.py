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
import card_types
import ghkit
from stages import issue_custom_id, title_key

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
    # resolve_lane_for_stage returns lane ids in whatever type the board gave them (raw, often
    # ints) -- stringified here to match _card_lane_id()'s always-str return, the same coercion
    # convention sync.py uses for a card's current lane id.
    return {str(lane_id) for lane_id in acceptable}


def _disqualifying_custom_ids(issues: list[dict]) -> set[str]:
    """The set of customId values already claimed by an existing GitHub issue, over the FULL issues
    list. Strict existence-based matching -- a card's customId disqualifies it only if it actually
    matches one of these, never by looking like a plausible id format."""
    return {issue_custom_id(issue) for issue in issues}


def _has_usable_title(card: dict) -> bool:
    """A card's title is usable when it's a non-blank string. Defensive against any other shape
    (missing, None, whitespace-only, or a malformed non-string value) -- never raises."""
    title = card.get("title")
    return isinstance(title, str) and title.strip() != ""


def _is_candidate(card: dict, intake_lane_ids: set[str], target_urls: set[str],
                   managed_custom_ids: set[str]) -> bool:
    """Pure predicate: does this card need a new GitHub issue?

    A card qualifies only when it has a usable id AND a usable title, its current lane is one of
    `intake_lane_ids`, it carries no external link matching `target_urls` (the known target-repo
    issue URLs), and its customId doesn't match any of `managed_custom_ids`. A foreign external
    link (anything not in `target_urls`) or an unrecognized customId never disqualifies a card by
    itself -- only an actual match does.

    A card lacking a usable id (missing, None, or empty -- the same truthy check sync.py's own
    card-matching loop uses) is never a candidate: promoting it would build marker_for_card(None),
    card_web_url(cfg, None), and eventually a live PATCH against agileplace's "/card/None".

    A card lacking a usable title (missing, None, blank/whitespace, or a malformed non-string
    value) is never a candidate either: ghkit.create_issue's `gh issue create --title` would either
    reject an empty title outright or, for a None title, crash subprocess with an uncaught
    TypeError -- either way an unrecovered exception that would crash the entire sync run for one
    blank-titled Intake card."""
    if not card.get("id"):
        return False
    if not _has_usable_title(card):
        return False
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

    This is the lane/link/customId predicate only. The title-derived customId collision guard lives
    in promote() instead (not here), because it must run AFTER marker-resume matching: a card whose
    title-derived key matches an existing issue that is ITS OWN resume target must be resumed, not
    dropped -- and marker awareness needs the body-bearing ghkit.list_issue_bodies() snapshot that
    only promote() fetches.

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
    adopted: (card, issue) pairs for every candidate whose card got a writeback this run (resumed
        AND created). The caller (sync._run_intake_promotion) registers each card in its ownership
        indices under the issue's URL/customId, so the per-issue card-creation loop matches a
        just-linked card instead of creating a DUPLICATE for a resumed, already-active issue whose
        writeback landed after the local cards snapshot was taken.
    """
    candidates: int
    prescan_failed: bool
    resumed: int
    created: int
    adopted: tuple = ()


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
    """Write a promoted issue's customId and link back onto its AgilePlace card, as two SEPARATE
    patch_card calls -- never batched into one PATCH.

    customId (the actual sync join key -- both `_is_candidate`'s own disqualification check and the
    ordinary sync's card-matching key on it) is written FIRST. It's also the only one of the two
    writes patch_card retries once on a 409/428 version conflict (see API-VALIDATION.md). Writing it
    first means the state left behind by ANY failure partway through this function is always one of:
    nothing written (card stays a full candidate; the next run's marker-resume scan retries the
    whole writeback), or customId written but the link missing (the card is still fully tracked --
    matched and reconciled by the ordinary sync via its customId -- just missing the informational
    external-link decoration). The reverse order would risk the opposite: a card whose link write
    succeeded but whose customId write then failed would carry a link matching a known target URL,
    permanently disqualifying it from `_is_candidate` (see the external-link check above) while its
    join key was never actually established -- stranding it with no further retry path at all.

    The link write is skipped (with a WARN) whenever `card` already carries ANY external link --
    either the plural, array-shaped `externalLinks` field (agileplace._card_value_for_patch_path has
    no case for the singular `/externalLink` path this feature writes, so a 409/428 conflict on that
    write can never retry -- it unconditionally re-raises; see API-VALIDATION.md and
    _card_for_link_write below) OR a singular, populated `externalLink` (a candidate deliberately
    KEEPS a foreign link -- one not matching a known target-repo issue URL -- per _is_candidate, and
    a singular `/externalLink` `add` REPLACES an occupied property, so writing here would silently
    destroy that foreign Jira/doc link). Only a bare, link-less card gets the intake link written.

    The customId write above may bump the card's server-side resource version. The link write below
    must never reuse `card`'s now-possibly-stale version for its own PATCH -- doing so would send a
    version the server has already superseded and deterministically 409/428 on every real writeback
    against a card with a usable version (the ordinary case from agileplace.list_cards()). See
    _card_for_link_write for how this is avoided."""
    card_id = card.get("id")
    key = _writeback_key(card.get("title", ""), issue["number"])
    agileplace.patch_card(cfg, apply, card, [agileplace.op_custom_id(key)],
                          note=f"intake customId -> {key}")
    if "externalLinks" in card or card.get("externalLink"):
        print(f"WARN  card {card_id}: already carries an external link -- skipping intake link "
              "writeback (a singular /externalLink `add` would REPLACE an existing foreign link, "
              "and the array externalLinks shape is unsupported here); customId writeback already "
              "completed")
    else:
        link_op = op_external_link(f"GitHub #{issue['number']}", issue["url"])
        agileplace.patch_card(cfg, apply, _card_for_link_write(cfg, apply, card), [link_op],
                              note=f"intake link -> {issue['url']}")


def _card_for_link_write(cfg: dict, apply: bool, card: dict) -> dict:
    """The card snapshot used for the SECOND writeback PATCH (the external link). Never mutates
    `card`: returns it unchanged for a dry run (apply=False), or a distinct, freshly-fetched
    snapshot for apply=True (see below) -- either way the caller's own `card` reference is intact.

    The customId write just above may have bumped the card's server-side resource version.
    agileplace._card_value_for_patch_path has no case for `/externalLink`, so agileplace's own
    generic version-conflict recovery (patch_card's refetch-before-PATCH for a version-less card,
    and its one-retry-on-409/428 path) can never validate or retry this path -- it always fails
    closed and re-raises. Reusing `card`'s now-possibly-stale version here would therefore
    deterministically 409/428 on every real apply=True writeback against a card with a usable
    version (the ordinary agileplace.list_cards() case), with no recovery.

    An explicit refetch here -- via the same agileplace.get_card GET the rest of this codebase
    already uses -- sidesteps that gap by never sending a stale version in the first place, rather
    than depending on agileplace to recover from a conflict it structurally can't validate. A
    genuine concurrent edit landing in the narrow window between this refetch and the PATCH itself
    still 409/428s and still propagates uncaught -- unchanged, intentional behavior (see
    API-VALIDATION.md's "Reverse intake" section).

    apply=False (dry run) returns `card` unchanged, performing zero network calls: patch_card's own
    version-less-card path already tolerates a missing version without refetching when apply is
    False, matching the dry-run convention every other codepath in this module follows."""
    if not apply:
        return card
    return agileplace.get_card(cfg, card["id"])


def _reverse_seed_create(cfg: dict, apply: bool, card: dict,
                         org_types: frozenset[str] | None) -> dict | None:
    """Create the GitHub issue for one non-resumed candidate, reverse-seeding its native issue type
    and/or label from the card's own AgilePlace card type (card_types.reverse_seed_for_card_type).
    The native type only ever reaches ghkit.create_issue once card_types.validate_reverse_issue_type
    has confirmed it against `org_types` (None -- probe failed/skipped/dry-run -- always falls back
    to typeless, never raises). The seed label (if any) is applied via ghkit.edit_label, but only
    after a successful create (`issue` is not None) -- a dry-run "would create" plan applies no
    label. Returns whatever ghkit.create_issue returns (None for a dry-run plan, or the created
    {"number", "url"})."""
    seed = card_types.reverse_seed_for_card_type(card_types.card_type_title(card))
    validated_type = card_types.validate_reverse_issue_type(seed.issue_type, org_types)
    issue = ghkit.create_issue(cfg, apply, card.get("title", ""), _issue_body(card, cfg),
                               issue_type=validated_type)
    if issue is not None and seed.label:
        ghkit.edit_label(cfg, apply, issue["number"], seed.label, add=True)
    return issue


def _collides_with_a_different_card(derived_key: str | None, claimed_keys: set[str]) -> bool:
    """True when this card's PROSPECTIVE writeback customId (`derived_key` -- the `title_key` of its
    title, what `_writeback_key` produces from a `[KEY]` prefix) is already owned by a DIFFERENT
    URL-owned card: an existing issue's `issue_custom_id`, or a candidate promoted earlier this run.
    A card whose title has no `[KEY]` prefix derives no key (None -- its writeback falls back to the
    new issue's own unique number, which can never collide) and never collides here."""
    return derived_key is not None and derived_key in claimed_keys


def promote(cfg: dict, apply: bool, cards: list[dict], lanes: list, stage_map: dict | None,
           issues: list[dict]) -> IntakeSummary:
    """Promote every Intake candidate into a GitHub issue (or resume one already created by a prior,
    interrupted run), then write the link/customId back so the ordinary one-way sync adopts it next
    cycle. Never moves a card's lane -- that stays the ordinary sync's job.

    Marker-resume is checked FIRST for every candidate: a card whose own promoted issue already
    exists (found by its resume marker) is resumed -- writeback re-run -- regardless of whether that
    issue's title-derived customId now looks "claimed". Only for a card with NO resumable issue does
    the collision guard apply: if its title-derived writeback customId is already owned by a
    different issue or an earlier candidate this run, it is skipped (creating it would write a
    customId that stalls the next sync's fail-closed identity guard). This ordering is what keeps the
    crash-recovery case from stranding a card whose own issue is now in the `issues` snapshot.

    A card with no resumable issue also gets its AgilePlace card type reverse-seeded onto the new
    GitHub issue, via _reverse_seed_create -- card_types.reverse_seed_for_card_type yields an
    optional native issue TYPE and/or label. The native type only ever reaches ghkit.create_issue
    after card_types.validate_reverse_issue_type confirms it against `org_types`
    (ghkit.org_issue_types(cfg), fetched AT MOST ONCE per promote() call, lazily, on the first
    candidate that actually reaches this branch, and ONLY when `apply` is True). A dry run (or a run
    with no such candidate) never probes org issue types at all: org_types stays None and
    validate_reverse_issue_type's existing fail-closed fallback naturally creates the issue typeless
    -- this is what keeps the two existing dry-run transport-call-count tests intact. The seed label
    (if any) is applied via ghkit.edit_label only after a successful create. A resumed candidate is
    never reseeded -- marker-resume means the issue (and any type/label it should carry) already
    exists from whatever run originally created it.

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
    adopted: list[tuple[dict, dict]] = []
    # customIds already owned by an existing issue, plus any reserved by a card created earlier this
    # run -- the collision set the (marker-unaware) guard below checks a fresh candidate's key against.
    claimed_keys = {issue_custom_id(issue) for issue in issues}
    # Lazily probed at most once, only once apply=True and a not-yet-resumed candidate actually
    # needs it -- see the docstring above and finding #6 in the design.
    org_types: frozenset[str] | None = None
    org_types_probed = False
    for card in candidates:
        marked = _find_marked_issue(card.get("id"), issues_with_bodies)
        derived_key = title_key(card.get("title") or "")
        if marked is not None:
            issue, was_resumed = marked, True
        else:
            if _collides_with_a_different_card(derived_key, claimed_keys):
                print(f"WARN  intake: skipping card {card.get('id')} -- its title-derived customId "
                      f"[{derived_key}] is already claimed by an existing issue or another "
                      "candidate; promoting it would collide and stall the ordinary sync")
                continue
            if derived_key is not None:
                claimed_keys.add(derived_key)
            if apply and not org_types_probed:
                org_types = ghkit.org_issue_types(cfg)
                org_types_probed = True
            issue = _reverse_seed_create(cfg, apply, card, org_types)
            was_resumed = False
        if issue is None:
            print(f"DRY   intake: would create issue for card {card.get('id')} "
                  f"({card.get('title', '')!r})")
            continue
        if was_resumed:
            resumed += 1
        else:
            created += 1
        _writeback(cfg, apply, card, issue)
        adopted.append((card, issue))
    return IntakeSummary(candidates=len(candidates), prescan_failed=False,
                         resumed=resumed, created=created, adopted=tuple(adopted))
