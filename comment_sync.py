"""Two-way GitHub issue <-> AgilePlace card comment sync (issue #66).

This module carries the shared timestamp-normalization helper (Task 1) plus the pure planning core
(Task 4): provenance-prefix build/parse, sync-identity check, and `resolve_comment_sync` -- the
planner that turns (identity, ledger, gh_comments, ap_comments) into a `CommentSyncPlan` of
`CommentAction`s. Everything in this module is pure -- no network/subprocess/filesystem I/O, either
at import time or at plan time. I/O modules (`agileplace_comments`, `ghkit`) and the wiring
entrypoint (`sync_comments`, a later task) execute the plan this module produces.

Ledger rows (see sync.py's issues_state[url]["comments"]) are the CommentLedgerEntry shape:
``{"gh_id": int|None, "ap_id": int|None, "origin": "gh"|"ap", "gh_created": str|None,
"gh_edited": str|None, "ap_created": str|None, "ap_edited": str|None, "deleted": bool}``. A
persisted row either has both ids non-None (a live pair) or ``deleted`` is True (a tombstone --
ids kept forever, content never stored, so a stale re-read can never resurrect it as new).
"""
from __future__ import annotations

from datetime import datetime, timezone
from re import compile as _re_compile
from typing import Literal, NamedTuple

import richtext


def _parse_timestamp(raw: str | None) -> datetime | None:
    """Normalizes a comment timestamp to a UTC-aware datetime so both sides of a sync (GH's
    ISO-8601, AP's not-yet-confirmed format) become comparable through one funnel rather than via
    raw lexical string comparison. Total: any input that isn't a parseable ISO-8601 string --
    ``None``, blank, garbage, or simply the wrong type -- degrades to ``None`` and never raises, so a
    comparison site can exclude the comment (with a WARN) instead of crashing the whole sync.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# =================================================================================================
# Provenance prefixes -- exact wording from the issue/design doc, pure build + tolerant parse
# =================================================================================================

_PROVENANCE_TEMPLATES = {
    "gh": "comment by {author} on GitHub",
    "ap": "comment by {author} on Agile Place",
}
_PROVENANCE_LEAD = "comment by "
# Order matters: "on GitHub" is not a substring of "on Agile Place" or vice versa, so a first-match
# scan is unambiguous regardless of dict order -- pinned by test_comment_sync's parser suite.
_PROVENANCE_SUFFIXES = (
    (" on GitHub", "gh"),
    (" on Agile Place", "ap"),
)
# Strips leading whitespace and HTML wrapper tags (e.g. AP's "<p>") so an anchored match still finds
# the prefix even though AP renders comment bodies as HTML. Only the *leading* wrapper is stripped --
# trailing markup after the matched suffix is irrelevant since parsing stops at the first suffix hit.
_LEADING_WRAPPER_RE = _re_compile(r"^(?:\s|<[^>]+>)+")


class ProvenanceHeader(NamedTuple):
    """A parsed `build_provenance_prefix` header: which side the comment ORIGINATED on, and the
    origin author's display label (name/email/id -- whatever `_author_label` extracted at mirror
    time)."""
    origin_side: Literal["gh", "ap"]
    author_label: str


def build_provenance_prefix(origin_side: str, author_label: str) -> str:
    """The exact leading text every mirrored comment carries, naming the platform the ORIGINAL
    comment was written on -- not the platform this rendered text is about to be posted to. Pure,
    total for any origin_side in {"gh", "ap"}; an unrecognized origin_side is a caller bug and
    raises rather than emitting a header a parser could never round-trip."""
    template = _PROVENANCE_TEMPLATES.get(origin_side)
    if template is None:
        raise ValueError(f"build_provenance_prefix: origin_side must be 'gh' or 'ap', got {origin_side!r}")
    return template.format(author=author_label)


def parse_provenance_prefix(text: str) -> ProvenanceHeader | None:
    """Recovers the ProvenanceHeader from a rendered comment body, or None when `text` doesn't open
    with a recognizable prefix (a genuine human comment, or garbage). Anchored at the start (after
    stripping leading whitespace/HTML wrapper tags) rather than searched anywhere in the body, so a
    human comment that merely *mentions* "comment by X on GitHub" mid-sentence never false-positives
    as a mirror.

    Author-label extraction is a first-occurrence-of-suffix-literal split: everything between the
    "comment by " lead and the first matching " on GitHub"/" on Agile Place" suffix is the author
    label, taken verbatim (not itself parsed further). This assumes an author label never contains
    either suffix literal as a substring -- a near-zero-probability real-world case, accepted as-is
    rather than hardened with a different delimiter scheme (design doc finding #4)."""
    if not isinstance(text, str):
        return None
    stripped = _LEADING_WRAPPER_RE.sub("", text, count=1)
    if not stripped.startswith(_PROVENANCE_LEAD):
        return None
    remainder = stripped[len(_PROVENANCE_LEAD):]
    for suffix, origin_side in _PROVENANCE_SUFFIXES:
        idx = remainder.find(suffix)
        if idx == -1:
            continue
        author_label = remainder[:idx].strip()
        if not author_label:
            return None
        return ProvenanceHeader(origin_side=origin_side, author_label=author_label)
    return None


def is_sync_authored(side: str, author_identifier: str | None, identity: dict | None) -> bool:
    """Whether `author_identifier` (a single already-extracted field -- GH login, or one candidate
    AP identity field) matches the sync's own identity for `side`. Casefold compare (identity
    strings are user-typed .env values, case shouldn't matter). Never raises: any non-string/missing
    input on either side of the comparison simply yields False."""
    if not isinstance(identity, dict) or not isinstance(author_identifier, str):
        return False
    expected_key = "gh_login" if side == "gh" else "ap_author" if side == "ap" else None
    expected = identity.get(expected_key) if expected_key else None
    if not isinstance(expected, str) or not expected.strip() or not author_identifier.strip():
        return False
    return expected.strip().casefold() == author_identifier.strip().casefold()


# =================================================================================================
# Plan types
# =================================================================================================

CommentActionKind = Literal[
    "mirror_new", "edit_mirror", "delete_mirror_and_tombstone", "restore_mirror",
    "tombstone_both_gone", "adopt_orphan", "drop_unpairable_orphan",
]


class CommentAction(NamedTuple):
    """One planned step. `ledger_key` and `origin_ids` are always `(gh_id, ap_id)` -- either half
    may be None when that side's id isn't known yet (a not-yet-posted `mirror_new`, or an
    unpairable orphan). `target_side` is the platform an I/O module must act on -- None for actions
    that only touch the ledger (`tombstone_both_gone`, `adopt_orphan`, `drop_unpairable_orphan`).
    `existing_mirror_id` is the CURRENT id of the comment `target_side` must edit/delete, or (for
    `adopt_orphan`/`drop_unpairable_orphan`) the orphan mirror's own id, informationally; None when
    no such id exists yet."""
    kind: CommentActionKind
    target_side: Literal["gh", "ap"] | None
    ledger_key: tuple[int | None, int | None]
    rendered_body: str | None
    existing_mirror_id: int | None
    origin_ids: tuple[int | None, int | None]


class CommentSyncPlan(NamedTuple):
    """`actions` are ordered: ledger-driven actions first (ledger order), then orphan
    adopt/drop actions, then brand-new mirrors -- the latter chronologically ordered across
    interleaved GH/AP sources (design doc: "new mirrors post in chronological order")."""
    actions: list[CommentAction]


def _other_side(side: str) -> str:
    return "ap" if side == "gh" else "gh"


def _comment_body(comment: dict) -> str:
    body = comment.get("body")
    return body if isinstance(body, str) else ""


def _valid_id(raw) -> bool:
    return isinstance(raw, int) and not isinstance(raw, bool)


def _author_label(side: str, comment: dict) -> str:
    """Best-effort human-readable author for a provenance prefix. GH comments carry a single
    `author` (login); AP comments fall back name -> email -> id (mirroring
    agileplace_comments._comment_author_fields' own priority). Never raises; "unknown" when nothing
    usable is present."""
    if side == "gh":
        login = comment.get("author")
        return login if isinstance(login, str) and login.strip() else "unknown"
    for key in ("author_name", "author_email", "author_id"):
        value = comment.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "unknown"


def _ap_identity_candidates(comment: dict) -> list[str]:
    """Every AP field that could plausibly match COMMENT_SYNC_AP_AUTHOR, in priority order -- email
    is the documented example (`adam.wrzeski@jacobs.com`), name/id are defensive fallbacks for
    boards that configure identity differently."""
    return [v for v in (comment.get("author_email"), comment.get("author_name"), comment.get("author_id"))
            if isinstance(v, str) and v.strip()]


def _is_comment_sync_authored(side: str, comment: dict, identity: dict | None) -> bool:
    if side == "gh":
        return is_sync_authored("gh", comment.get("author"), identity)
    return any(is_sync_authored("ap", candidate, identity) for candidate in _ap_identity_candidates(comment))


def _render_mirror_body(target_side: str, origin_side: str, author_label: str, source_body: str) -> str:
    """Translate `source_body` (living, right now, on the platform opposite `target_side`) into
    `target_side`'s format and prepend the provenance prefix naming `origin_side`/`author_label`.
    Translation direction is fully determined by `target_side` alone -- source and target are always
    opposite platforms at every call site (mirror_new, restore_mirror, and edit_mirror's
    canonical-side content, whichever side that is)."""
    prefix = build_provenance_prefix(origin_side, author_label)
    if target_side == "ap":
        return f"<p>{prefix}</p>{richtext.markdown_to_leankit_html(source_body)}"
    return f"{prefix}\n\n{richtext.leankit_html_to_markdown(source_body)}"


# =================================================================================================
# Ledger-driven actions -- drift / delete / restore / tombstone for already-paired comments
# =================================================================================================

def _plan_gone_or_present(row: dict, gh_id: int, ap_id: int, gh_comment: dict | None,
                          ap_comment: dict | None, origin_side: str) -> CommentAction | None:
    """Handles the three ledger-row shapes that don't require a drift comparison: both sides gone
    (tombstone), the origin gone (delete the orphaned mirror + tombstone), or the mirror gone
    (restore it). Returns None when both sides are present -- the caller falls through to drift."""
    mirror_side = _other_side(origin_side)
    if gh_comment is None and ap_comment is None:
        return CommentAction("tombstone_both_gone", None, (gh_id, ap_id), None, None, (gh_id, ap_id))
    origin_comment = gh_comment if origin_side == "gh" else ap_comment
    mirror_comment = ap_comment if origin_side == "gh" else gh_comment
    if origin_comment is None:
        mirror_id = ap_id if mirror_side == "ap" else gh_id
        return CommentAction("delete_mirror_and_tombstone", mirror_side, (gh_id, ap_id), None,
                             mirror_id, (gh_id, ap_id))
    if mirror_comment is None:
        author_label = _author_label(origin_side, origin_comment)
        rendered = _render_mirror_body(mirror_side, origin_side, author_label, _comment_body(origin_comment))
        return CommentAction("restore_mirror", mirror_side, (gh_id, ap_id), rendered, None, (gh_id, ap_id))
    return None


def _side_drifted(live_edited: datetime | None, ledgered_edited: datetime | None) -> bool:
    """A side has drifted when its current edited-timestamp is parseable AND differs from the
    ledgered value. An unparseable LIVE timestamp is excluded from the decision (we genuinely can't
    tell) rather than treated as drift -- see _parse_timestamp's totality contract."""
    return live_edited is not None and live_edited != ledgered_edited


def _plan_drift(row: dict, gh_id: int, ap_id: int, gh_comment: dict, ap_comment: dict,
                origin_side: str) -> CommentAction | None:
    """Both sides are present -- compare each side's live edited-timestamp against its ledgered
    value. Neither drifted -> steady state, no action. One side drifted -> it's canonical. Both
    drifted -> most recent live edit wins (design doc: "comments are cheap to re-edit"). The
    provenance prefix always names the ORIGIN's own current author -- origin/mirror is permanent
    ledger bookkeeping, independent of which side's content happens to be canonical this round."""
    gh_live, ap_live = _parse_timestamp(gh_comment.get("edited")), _parse_timestamp(ap_comment.get("edited"))
    gh_drifted = _side_drifted(gh_live, _parse_timestamp(row.get("gh_edited")))
    ap_drifted = _side_drifted(ap_live, _parse_timestamp(row.get("ap_edited")))
    if not gh_drifted and not ap_drifted:
        return None
    if gh_drifted and ap_drifted:
        canonical_side = "gh" if (gh_live or datetime.min) >= (ap_live or datetime.min) else "ap"
    else:
        canonical_side = "gh" if gh_drifted else "ap"
    target_side = _other_side(canonical_side)
    canonical_comment = gh_comment if canonical_side == "gh" else ap_comment
    origin_comment = gh_comment if origin_side == "gh" else ap_comment
    author_label = _author_label(origin_side, origin_comment)
    rendered = _render_mirror_body(target_side, origin_side, author_label, _comment_body(canonical_comment))
    target_mirror_id = ap_id if target_side == "ap" else gh_id
    return CommentAction("edit_mirror", target_side, (gh_id, ap_id), rendered, target_mirror_id, (gh_id, ap_id))


def _plan_ledger_row(row: dict, gh_by_id: dict, ap_by_id: dict) -> CommentAction | None:
    """One CommentAction (or None -- steady state) for one live ledger row. Tombstoned rows and
    malformed rows (missing an id, or an unrecognized `origin`) are inert -- returns None rather
    than raising, so one corrupted row never aborts the whole plan."""
    if row.get("deleted") is True:
        return None
    gh_id, ap_id, origin_side = row.get("gh_id"), row.get("ap_id"), row.get("origin")
    if not _valid_id(gh_id) or not _valid_id(ap_id) or origin_side not in ("gh", "ap"):
        return None
    gh_comment, ap_comment = gh_by_id.get(gh_id), ap_by_id.get(ap_id)
    gone_or_present = _plan_gone_or_present(row, gh_id, ap_id, gh_comment, ap_comment, origin_side)
    if gone_or_present is not None:
        return gone_or_present
    # _plan_gone_or_present returns None only when both sides are present -- exhaustive over the
    # (gh present/absent) x (ap present/absent) combinations, so both are guaranteed non-None here.
    return _plan_drift(row, gh_id, ap_id, gh_comment, ap_comment, origin_side)


# =================================================================================================
# New (unledgered) comments -- orphan re-adoption and genuine new-origin mirroring
# =================================================================================================

def _split_new_comments(side: str, comments: list[dict], ledgered_ids: set[int],
                        identity: dict | None) -> tuple[list[tuple[dict, ProvenanceHeader]], list[dict]]:
    """Every comment on `side` whose id isn't already referenced by the ledger (live pair or
    tombstone), split into orphan mirrors (sync-authored, parseable prefix -- candidates for
    `_adopt_orphans`) and genuine new-origin comments (candidates for `_plan_mirror_new`). A
    sync-authored comment WITHOUT a parseable prefix is neither -- a malformed/foreign write under
    the sync's own identity -- and is excluded from both so it's never double-posted and never
    mistaken for a human origin comment."""
    orphans: list[tuple[dict, ProvenanceHeader]] = []
    origin_candidates: list[dict] = []
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        comment_id = comment.get("id")
        if not _valid_id(comment_id) or comment_id in ledgered_ids:
            continue
        if _is_comment_sync_authored(side, comment, identity):
            parsed = parse_provenance_prefix(_comment_body(comment))
            if parsed is not None:
                orphans.append((comment, parsed))
            continue
        origin_candidates.append(comment)
    return orphans, origin_candidates


def _find_origin_candidate(parsed: ProvenanceHeader, mirror_comment: dict, candidate_pool: list[dict],
                           candidate_side: str) -> dict | None:
    """Among `candidate_pool` (unledgered, non-sync-authored comments on the side the orphan's
    prefix names as its origin), the one that most plausibly produced this orphan mirror: an
    author-label match (case-insensitive) is required; among ties, the candidate whose `created`
    timestamp sits closest to the orphan's own -- the "orphan-adjacency gap" computation."""
    matches = [c for c in candidate_pool
              if _author_label(candidate_side, c).strip().casefold() == parsed.author_label.strip().casefold()]
    if not matches:
        return None
    mirror_created = _parse_timestamp(mirror_comment.get("created"))
    if mirror_created is None:
        return matches[0]

    def _gap_seconds(candidate: dict) -> float:
        candidate_created = _parse_timestamp(candidate.get("created"))
        if candidate_created is None:
            return float("inf")
        return abs((candidate_created - mirror_created).total_seconds())

    return min(matches, key=_gap_seconds)


def _mirror_side_ids(mirror_side: str, mirror_id: int | None) -> tuple[int | None, int | None]:
    return (mirror_id, None) if mirror_side == "gh" else (None, mirror_id)


def _adopt_orphans(mirror_side: str, orphans: list[tuple[dict, ProvenanceHeader]],
                   candidate_pool: list[dict]) -> tuple[list[CommentAction], list[dict]]:
    """Pairs each orphan mirror (a sync-authored, prefix-carrying comment with no ledger row -- the
    crash-between-post-and-state-write case) against an unclaimed origin candidate on the side its
    prefix names, in the order given. Matched candidates are removed from the pool (a new list each
    time -- inputs are never mutated) so the same origin comment can't be claimed twice, and so it's
    excluded from the later `_plan_mirror_new` pass -- orphans are always re-adopted into the
    ledger, never double-posted."""
    actions: list[CommentAction] = []
    pool = list(candidate_pool)
    origin_side = _other_side(mirror_side)
    for mirror_comment, parsed in orphans:
        mirror_id = mirror_comment.get("id")
        ids = _mirror_side_ids(mirror_side, mirror_id)
        if parsed.origin_side != origin_side:
            actions.append(CommentAction("drop_unpairable_orphan", None, ids, None, mirror_id, ids))
            continue
        candidate = _find_origin_candidate(parsed, mirror_comment, pool, origin_side)
        if candidate is None:
            actions.append(CommentAction("drop_unpairable_orphan", None, ids, None, mirror_id, ids))
            continue
        pool = [c for c in pool if c is not candidate]
        candidate_id = candidate.get("id")
        paired_ids = (candidate_id, mirror_id) if origin_side == "gh" else (mirror_id, candidate_id)
        actions.append(CommentAction("adopt_orphan", None, paired_ids, None, mirror_id, paired_ids))
    return actions, pool


def _plan_mirror_new(gh_candidates: list[dict], ap_candidates: list[dict]) -> list[CommentAction]:
    """Every remaining genuine new-origin comment becomes a `mirror_new` action, chronologically
    ordered across BOTH sources so interleaved GH/AP conversation posts in the order it actually
    happened. Comments with an unparseable `created` sort last -- stable, so ties (including all-
    unparseable) keep gh_candidates-then-ap_candidates original order."""
    far_future = datetime.max.replace(tzinfo=timezone.utc)
    tagged = [("gh", c) for c in gh_candidates] + [("ap", c) for c in ap_candidates]
    tagged.sort(key=lambda pair: _parse_timestamp(pair[1].get("created")) or far_future)
    actions = []
    for side, comment in tagged:
        target_side = _other_side(side)
        author_label = _author_label(side, comment)
        rendered = _render_mirror_body(target_side, side, author_label, _comment_body(comment))
        ids = _mirror_side_ids(side, comment.get("id"))
        actions.append(CommentAction("mirror_new", target_side, ids, rendered, None, ids))
    return actions


def resolve_comment_sync(identity: dict | None, ledger: list[dict], gh_comments: list[dict],
                         ap_comments: list[dict]) -> CommentSyncPlan:
    """The whole per-issue comment-sync plan, pure and total: no I/O, no exceptions from malformed
    per-comment/ledger data, deterministic for a given input. `identity=None` means comment sync is
    self-disabled for this run -- an empty plan, zero work (the disablement WARN belongs to
    `sync_comments`' wiring, not this pure planner)."""
    if identity is None:
        return CommentSyncPlan(actions=[])
    ledger = ledger if isinstance(ledger, list) else []
    gh_comments = gh_comments if isinstance(gh_comments, list) else []
    ap_comments = ap_comments if isinstance(ap_comments, list) else []
    gh_by_id = {c["id"]: c for c in gh_comments if isinstance(c, dict) and _valid_id(c.get("id"))}
    ap_by_id = {c["id"]: c for c in ap_comments if isinstance(c, dict) and _valid_id(c.get("id"))}
    ledger_actions = [action for row in ledger if isinstance(row, dict)
                      and (action := _plan_ledger_row(row, gh_by_id, ap_by_id)) is not None]

    ledgered_gh_ids = {row.get("gh_id") for row in ledger if isinstance(row, dict) and _valid_id(row.get("gh_id"))}
    ledgered_ap_ids = {row.get("ap_id") for row in ledger if isinstance(row, dict) and _valid_id(row.get("ap_id"))}
    gh_orphans, gh_candidates = _split_new_comments("gh", gh_comments, ledgered_gh_ids, identity)
    ap_orphans, ap_candidates = _split_new_comments("ap", ap_comments, ledgered_ap_ids, identity)

    gh_adopt_actions, ap_candidates = _adopt_orphans("gh", gh_orphans, ap_candidates)
    ap_adopt_actions, gh_candidates = _adopt_orphans("ap", ap_orphans, gh_candidates)

    return CommentSyncPlan(actions=[
        *ledger_actions, *gh_adopt_actions, *ap_adopt_actions,
        *_plan_mirror_new(gh_candidates, ap_candidates),
    ])
