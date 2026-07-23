"""Board topology for AgilePlace (LeanKit): lanes, card types, and stage<->lane resolution.

Split out of agileplace.py (issue #84) to separate *board topology* (this module) from the
*API transport / card read-write* concerns agileplace.py keeps. This module composes stages.py's
stage vocabulary (STAGES, STAGE_CARD_STATUS, lane_matches_stage, title_contains_phrase) rather than
duplicating it, and calls back into agileplace.api for its one live read (board_layout). The
dependency runs one way only: this module imports agileplace, agileplace.py must never import this
module back.

Lanes resolve to a stage by TITLE among LEAF lanes, failing closed when ambiguous -- see
resolve_lane_for_stage.
"""
from __future__ import annotations

from typing import NamedTuple

import agileplace
from stages import STAGES, STAGE_CARD_STATUS, lane_matches_stage, title_contains_phrase


def lane_title(lane: dict) -> str:
    return (lane.get("title") or lane.get("name") or "").strip()


def _lanes_with_ids(lanes: list) -> list[dict]:
    valid = []
    for lane in lanes:
        if not isinstance(lane, dict):
            print(f"WARN  lane <{type(lane).__name__}> is not an object -- skipping malformed lane")
            continue
        malformed_text = next(
            (field for field in ("title", "name")
             if lane.get(field) and not isinstance(lane[field], str)),
            None,
        )
        if malformed_text:
            value = lane[malformed_text]
            print(f"WARN  lane id {lane.get('id', '<unknown>')!r} has non-string {malformed_text} "
                  f"({type(value).__name__}) -- skipping malformed lane")
            continue
        if "id" not in lane or lane["id"] is None:
            print(f"WARN  lane '{lane_title(lane) or '<untitled>'}' has no id -- skipping malformed lane")
            continue
        try:
            hash(lane["id"])
        except TypeError:
            print(f"WARN  lane '{lane_title(lane) or '<untitled>'}' has unhashable id "
                  f"({type(lane['id']).__name__}) -- skipping malformed lane")
            continue
        valid.append(lane)
    return valid


class BoardLayout(NamedTuple):
    """Return-shape of one board GET: leaf-and-parent lanes plus the board's configured card types,
    each already structurally validated (malformed entries filtered, one WARN apiece)."""
    lanes: list
    card_types: list


def _card_types_with_ids(card_types: list) -> list[dict]:
    """Structural validation mirroring _lanes_with_ids: malformed card-type entries are skipped with
    one WARN each, never raised. Eligibility (isCardType) and name resolution are card_types.py's
    semantic job -- this only guarantees every entry handed onward is a dict with a usable id."""
    valid = []
    for card_type in card_types:
        if not isinstance(card_type, dict):
            print(f"WARN  card type <{type(card_type).__name__}> is not an object -- "
                  f"skipping malformed card type")
            continue
        title = card_type.get("title")
        if title is not None and not isinstance(title, str):
            print(f"WARN  card type id {card_type.get('id', '<unknown>')!r} has non-string title "
                  f"({type(title).__name__}) -- skipping malformed card type")
            continue
        display_title = (title or "").strip() or "<untitled>"
        if "id" not in card_type or card_type["id"] is None:
            print(f"WARN  card type '{display_title}' has no id -- skipping malformed card type")
            continue
        try:
            hash(card_type["id"])
        except TypeError:
            print(f"WARN  card type '{display_title}' has unhashable id "
                  f"({type(card_type['id']).__name__}) -- skipping malformed card type")
            continue
        valid.append(card_type)
    return valid


def board_layout(cfg: dict) -> BoardLayout:
    response = agileplace.api(cfg, "GET", f"board/{cfg['board_id']}")
    return BoardLayout(
        lanes=_lanes_with_ids(response.get("lanes", [])),
        card_types=_card_types_with_ids(response.get("cardTypes", [])),
    )


def _ancestor_titles(lane: dict, by_id: dict) -> list[str]:
    titles, parent = [], lane.get("parentLaneId")
    while parent and parent in by_id:
        titles.append(lane_title(by_id[parent]))
        parent = by_id[parent].get("parentLaneId")
    return titles


def _leaf_lanes(lanes: list) -> list:
    """Only leaf lanes hold cards; parent/container lanes must never be a move target."""
    lanes = _lanes_with_ids(lanes)
    parent_ids = {l.get("parentLaneId") for l in lanes if l.get("parentLaneId")}
    return [l for l in lanes if l["id"] not in parent_ids]


def _release_lane(candidates: list[dict], release: str, by_id: dict) -> dict | None:
    """Resolve duplicate candidates to exactly one lane under the requested release ancestor."""
    if len(candidates) == 1:
        return candidates[0]
    if not release:
        return None
    matches = [lane for lane in candidates
               if any(title_contains_phrase(title, release)
                      for title in _ancestor_titles(lane, by_id))]
    return matches[0] if len(matches) == 1 else None


def _mapped_lanes(leaves: list[dict], stage_titles: list[str], release: str,
                  by_id: dict) -> list[dict] | None:
    """Resolve configured titles in order; None means a duplicate title stayed ambiguous."""
    by_title = {}
    for lane in leaves:
        by_title.setdefault(lane_title(lane).lower(), []).append(lane)
    ordered, seen = [], set()
    for wanted in stage_titles:
        matches = by_title.get(wanted.strip().lower(), [])
        selected = _release_lane(matches, release, by_id) if matches else None
        if len(matches) > 1 and selected is None:
            return None
        if selected and selected["id"] not in seen:
            seen.add(selected["id"])
            ordered.append(selected)
    return ordered


def resolve_lane_for_stage(lanes: list, stage: str, release: str, stage_map: dict | None = None, *,
                            quiet: bool = False):
    """(target_lane_or_None, acceptable_lane_ids). STAGE_LANE_MAP wins (first = target, all = in-stage),
    with duplicate titles resolved by release ancestor; else infer by lane title then non-conflicting
    cardStatus, failing CLOSED on ambiguity. Leaf lanes only.

    quiet=True suppresses the STAGE_LANE_MAP-misconfiguration WARN -- for callers that evaluate this
    purely as an internal membership check (not the actual, decisive lane-move call), so one
    misconfiguration doesn't print a duplicate WARN per such check."""
    lanes = _lanes_with_ids(lanes)
    leaves = _leaf_lanes(lanes)
    by_id = {l["id"]: l for l in lanes}

    if stage_map and stage in stage_map:
        ordered = _mapped_lanes(leaves, stage_map[stage], release, by_id)
        if ordered is None:
            return None, set()
        if ordered:
            return ordered[0], {lane["id"] for lane in ordered}
        if not quiet:
            print(f"WARN  STAGE_LANE_MAP lists {stage_map[stage]} for '{stage}' but none match a leaf lane -- inferring")

    cands = [lane for lane in leaves if lane_matches_stage(lane_title(lane), stage)]
    if not cands:
        cands = [
            lane for lane in leaves
            if lane.get("cardStatus") == STAGE_CARD_STATUS[stage]
            and not any(other != stage and lane_matches_stage(lane_title(lane), other)
                        for other in STAGES)
        ]
    if len(cands) == 1:
        return cands[0], {cands[0]["id"]}
    selected = _release_lane(cands, release, by_id)
    if selected:
        return selected, {selected["id"]}
    return None, set()  # none, or still ambiguous -> don't move


def stage_for_lane(lane_id: str, stage_map: dict[str, list[str]] | None, lanes: list) -> str | None:
    """Reverse of the STAGE_LANE_MAP lookup above: which single stage claims a card's *current*
    lane, or None. Pure, no I/O; never raises.

    Coerces both `lane_id` and every lane's `id` to str before comparison -- lane ids fetched from
    AgilePlace can be int or str depending on source, while call sites always pass lane_id as
    str(card.get("laneId") or ...) (existing sync.py convention). A naive dict-keyed lookup would
    silently return None for an int-typed lane id and get mistaken for a genuinely unmapped lane,
    same idiom as resolve_lane_for_stage/_protect_open_pr_stage's str(...) coercion.

    Resolves lane_id -> lane dict (fail closed -> None if unknown after str-coercion), then the
    single stage in stage_map whose title list contains that lane's title (case-insensitive exact
    match, matching _mapped_lanes's semantics -- not lane_matches_stage's substring semantics).
    Returns None on: unknown lane_id, falsy stage_map, zero matches, or 2+ matches -- spec collapses
    both ambiguous cases to the same WARN+skip outcome the caller applies, not a reopened error."""
    if not stage_map:
        return None
    by_id = {str(lane["id"]): lane for lane in _lanes_with_ids(lanes)}
    lane = by_id.get(str(lane_id))
    if lane is None:
        return None
    title = lane_title(lane).lower()
    matches = [stage for stage, titles in stage_map.items()
               if any((t or "").strip().lower() == title for t in titles)]
    return matches[0] if len(matches) == 1 else None
