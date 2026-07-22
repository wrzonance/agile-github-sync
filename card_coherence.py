"""Pure card-coherence logic for issue #70. No I/O -- exhaustively unit-tested.

Two genuinely-new decisions this run's sync needs, extracted out of sync.py rather than added
inline: sync.py was already 828 lines (over the 800-line hard cap) before this change, so growing
it further would compound a pre-existing budget violation instead of fixing it.

Layer 1 (contested_cards): before any card is touched, detect when this run's issue URLs
(active + retired) don't resolve 1:1 onto AgilePlace cards -- i.e. two or more GitHub issues
both claim the same card id. Those cards are excluded from every match/queue path for the run
(sync.py filters them out via the card ids -> URLs this function returns) rather than risk one
issue's sync clobbering another's.

Layer 2 (lane_conflict): even for uncontested cards, multiple queued ops for the same card can
carry conflicting `/laneId` values (e.g. duplicate `[KEY]`-prefixed issue titles matching the same
card through different match paths within one run). Detects that and reports it so the caller can
mark the card poisoned and skip its flush.
"""
from __future__ import annotations


def contested_cards(issues: list[dict], all_card_by_url: dict[str, dict]) -> dict[str, set[str]]:
    """Group this run's issue URLs by the card id they resolve to via all_card_by_url. Returns
    ONLY card ids (str(card['id'])) claimed by >= 2 distinct issue URLs -> the set of every
    claiming URL. Cards claimed by 0 or 1 URL are omitted entirely.

    Pure: never mutates `issues` or `all_card_by_url`; never raises; no I/O. An issue whose URL
    has no card match is silently excluded (nothing to contest). A matched but id-less card (a
    partial AgilePlace payload) is likewise skipped: with no id it cannot be fenced downstream, so
    it is deferred rather than indexed -- mirrors the run's other `card.get("id")` guards."""
    urls_by_cid: dict[str, set[str]] = {}
    for issue in issues:
        url = issue.get("url")
        card = all_card_by_url.get(url)
        if card is None:
            continue
        card_id = card.get("id")
        if not card_id:
            continue
        urls_by_cid.setdefault(str(card_id), set()).add(url)
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
