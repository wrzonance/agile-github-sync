"""AgilePlace card-description read/write helpers (issue #65 Task 1/7).

Split out of agileplace.py -- rather than growing that module further -- because it was already at
the project's 800-line file cap on this branch's base (code.md: "never add to a file already over
budget -- extract first"). Depends on agileplace.get_card for the lazy-refetch fallback in
card_description(); agileplace.py has no dependency back on this module, so there is no cycle.
"""
from __future__ import annotations

import agileplace


def card_description(cfg: dict, card: dict) -> str:
    """The card's description text, normalized to "" for None/absent. list_cards() never returns
    `description` (no field-selection params sent, confirmed against the live board), so a card
    reaching this function via the board snapshot is almost always missing the key entirely -- that
    (and ONLY that) triggers one lazy get_card refetch. A card that DOES carry the key -- including
    description="" -- takes the zero-I/O path: an explicit empty string is a real, current
    description, not "unknown", and must never be treated as a reason to hit the network."""
    if "description" in card:
        return card["description"] or ""
    fresh = agileplace.get_card(cfg, card["id"])
    return fresh.get("description") or ""


def op_description(html: str) -> dict:
    """A single unconditional replace of the card's description with `html` (already rendered by
    richtext.markdown_to_leankit_html and, when oversized, truncated by
    description_sync._truncate_for_agileplace). No validation here -- description_sync owns the
    decision of *whether* to write; this is purely the op-builder, matching agileplace.op_custom_id's
    shape."""
    return {"op": "replace", "path": "/description", "value": html}
