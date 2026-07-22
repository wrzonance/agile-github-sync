"""Pure card-coherence logic for issues #70 and #75. No I/O -- exhaustively unit-tested.

Two genuinely-new decisions this run's sync needs, extracted out of sync.py rather than added
inline: sync.py was already 828 lines (over the 800-line hard cap) before this change, so growing
it further would compound a pre-existing budget violation instead of fixing it.

Layer 1 (contested_cards): before any card is touched, detect when this run's issues (active +
retired) don't resolve 1:1 onto AgilePlace cards -- i.e. two or more GitHub issues both claim the
same card id, via EITHER match path (external-link URL or customId fallback -- mirroring
sync.py's own `_matching_card` precedence: URL first, customId only as a fallback). Those cards
are excluded from every match/queue path for the run (sync.py filters them out via the card ids ->
claimant-URLs this function returns) rather than risk one issue's sync clobbering another's. A
customId-only claim is just as capable of a clobber as a URL claim, so it fences the card exactly
the same way (issue #75).

Layer 2 (lane_conflict): even for uncontested cards, multiple queued ops for the same card can
carry conflicting `/laneId` values (e.g. duplicate `[KEY]`-prefixed issue titles matching the same
card through different match paths within one run). Detects that and reports it so the caller can
mark the card poisoned and skip its flush.
"""
from __future__ import annotations

from stages import issue_custom_id


def _claimed_card_id(issue: dict, all_card_by_url: dict[str, dict],
                     all_card_by_cid: dict[str, dict]) -> str | None:
    """Which card (by id) this issue claims, mirroring sync.py's `_matching_card` precedence: a
    url match is the ONLY claim considered when it resolves -- the customId fallback is never
    even attempted in that case, even if it would separately resolve to a different card. Only
    when the url doesn't resolve is the customId fallback attempted, and only when the issue dict
    actually carries the keys issue_custom_id() needs ('title' and 'number'); a minimal/malformed
    issue dict is treated as having no customId claim, never as a KeyError. Returns None when
    neither path resolves, or the resolved card's id is falsy (an id-less partial payload).

    Pure: never mutates any input; never raises for ANY issue dict shape."""
    card = all_card_by_url.get(issue.get("url"))
    if card is None and "title" in issue and "number" in issue:
        card = all_card_by_cid.get(issue_custom_id(issue))
    if card is None:
        return None
    card_id = card.get("id")
    return str(card_id) if card_id else None


def contested_cards(issues: list[dict], all_card_by_url: dict[str, dict],
                    all_card_by_cid: dict[str, dict]) -> dict[str, set[str]]:
    """Group this run's issues by the card id each claims (via _claimed_card_id -- URL first, else
    a guarded customId fallback). Returns ONLY card ids (str) claimed by >= 2 distinct issues ->
    the set of each claiming issue's OWN url (claimant identity is always the issue's url,
    regardless of which path produced the claim). Cards claimed by 0 or 1 issue are omitted
    entirely.

    Pure: never mutates `issues`, `all_card_by_url`, or `all_card_by_cid`; never raises; no I/O.
    An issue that resolves to no card is silently excluded (nothing to contest). A matched but
    id-less card (a partial AgilePlace payload) is likewise skipped: with no id it cannot be
    fenced downstream, so it is deferred rather than indexed -- mirrors the run's other
    `card.get("id")` guards."""
    urls_by_cid: dict[str, set[str]] = {}
    for issue in issues:
        card_id = _claimed_card_id(issue, all_card_by_url, all_card_by_cid)
        if card_id is None:
            continue
        urls_by_cid.setdefault(card_id, set()).add(issue.get("url"))
    return {cid: urls for cid, urls in urls_by_cid.items() if len(urls) >= 2}


def lane_conflict(ops: list[dict], current_lane_id: str | None) -> tuple[str | None, bool]:
    """Scan `ops` for '/laneId' replace op(s) against `current_lane_id` (the value already
    queued for this card, or None if none queued yet). Returns (updated_lane_id, conflict):

      - no '/laneId' op in `ops` -> (current_lane_id, False), unchanged.
      - '/laneId' op present, current_lane_id is None -> (new_value, False).
      - '/laneId' op present, value == current_lane_id -> (current_lane_id, False).
      - '/laneId' op present, value != current_lane_id -> (current_lane_id, True); the returned
        lane_id is deliberately NOT updated to the conflicting value -- a poisoned entry's stored
        lane_id freezes at the first-seen value, since the entry is discarded at flush regardless.

    Pure: never mutates `ops`; never raises; caller (queue()) owns applying the result to
    `card_ops[cid]` and printing the WARN."""
    lane_id = current_lane_id
    conflict = False
    for op in ops:
        if op.get("path") != "/laneId":
            continue
        value = op.get("value")
        if lane_id is None:
            lane_id = value
        elif value != lane_id:
            conflict = True
    return lane_id, conflict


def laneid_op_value(ops: list[dict]) -> str | None:
    """Return the value of the last '/laneId' replace op in `ops`, or None if `ops` carries no
    '/laneId' op at all. Used by callers (sync.py's queue()) that already know a conflict occurred
    via lane_conflict() and need the raw incoming lane value for a diagnostic message -- lane_conflict
    itself only reports whether a conflict happened, not which value triggered it.

    Pure: never mutates `ops`; never raises."""
    value = None
    for op in ops:
        if op.get("path") == "/laneId":
            value = op.get("value")
    return value
