"""Unit tests for sync._removal_authority_card_ids (issue #60).

The contract: a card an active issue reached ONLY through _matching_card's customId
fallback -- e.g. a retired card whose external link was manually removed and got
silently adopted through a customId collision -- confers no removal authority over
that card's dependencies. Only sync_dependencies' removal decision consumes this
narrower set; additions and every other managed-card consumer (child-connection
removals via managed_card_ids) are unaffected.

Run: pytest -q
"""
import copy
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sync import (  # noqa: E402
    _managed_card_ids,
    _removal_authority_card_ids,
    issue_custom_id,
    sync_dependencies,
)


def _issue(number, key, url=None):
    return {"number": number, "title": f"[{key}] t", "url": url or f"https://github.com/o/r/issues/{number}"}


# --- purity ----------------------------------------------------------------

def test_pure_deterministic_and_never_mutates_inputs():
    syncable_issues = [_issue(1, "A", "u1")]
    card_by_url = {"u1": {"id": "C1"}}
    retired_card_by_url = {"u9": {"id": "R9"}}
    issues_before = copy.deepcopy(syncable_issues)
    card_by_url_before = copy.deepcopy(card_by_url)
    retired_before = copy.deepcopy(retired_card_by_url)

    first = _removal_authority_card_ids(syncable_issues, card_by_url, retired_card_by_url)
    second = _removal_authority_card_ids(syncable_issues, card_by_url, retired_card_by_url)

    assert first == second == {"C1", "R9"}
    assert syncable_issues == issues_before
    assert card_by_url == card_by_url_before
    assert retired_card_by_url == retired_before


def test_never_raises_on_empty_or_non_matching_inputs():
    assert _removal_authority_card_ids([], {}, {}) == set()
    assert _removal_authority_card_ids([_issue(1, "A", "u1")], {}, {}) == set()


# --- issue #60: customId-only matches confer no removal authority ----------

def test_customid_only_match_confers_no_removal_authority():
    """The active issue has NO url match -- in main() this is exactly the shape produced
    when card_for()/_matching_card() only resolved the card through the customId fallback
    (e.g. a retired card's external link was removed and a customId collision silently
    adopted it). Such a card must not appear in the result on this issue's account."""
    issue = _issue(1, "A", "u1")
    result = _removal_authority_card_ids([issue], {}, {})
    assert result == set()


def test_url_matched_active_card_is_included():
    issue = _issue(1, "A", "u1")
    card_by_url = {"u1": {"id": "C1"}}
    result = _removal_authority_card_ids([issue], card_by_url, {})
    assert result == {"C1"}


def test_every_retired_card_remains_included():
    retired_card_by_url = {"u9": {"id": "R9"}, "u8": {"id": "R8"}}
    result = _removal_authority_card_ids([], {}, retired_card_by_url)
    assert result == {"R9", "R8"}


def test_card_without_id_is_excluded():
    """Fixtures use a truthy-but-id-less card ({"title": ...}, not {}) on both sides: an
    empty dict is itself falsy, so it would short-circuit the `card and card.get("id")`
    guard before ever reaching the `.get("id")` check it's meant to pin, letting a mutant
    that drops that check pass unnoticed."""
    issue = _issue(1, "A", "u1")
    card_by_url = {"u1": {"title": "x"}}  # matched but no id yet (plan-only shape without id key)
    retired_card_by_url = {"u9": {"title": "y"}}
    result = _removal_authority_card_ids([issue], card_by_url, retired_card_by_url)
    assert result == set()


# --- strictly subtractive: always a subset of managed_card_ids -------------

def test_result_is_always_a_subset_of_managed_card_ids():
    """Checks the invariant against the REAL managed_card_ids formula (sync._managed_card_ids,
    the same function main() calls) rather than a copy pasted into the test -- a formula drift
    in sync.py (e.g. dropping the `.get('id')` guard, or forgetting to union in
    retired_card_by_url) shows up here instead of being silently missed. card_for resolves
    issue 2 ONLY via customId (simulating _matching_card's fallback) -- exactly the shape
    managed_card_ids itself does not distinguish, but removal authority must."""
    issues = [_issue(1, "A", "u1"), _issue(2, "B", "u2")]
    card_by_url = {"u1": {"id": "C1"}}          # issue 2 has no URL match
    card_by_cid = {"B": {"id": "C2"}}           # issue 2 only resolves via customId
    retired_card_by_url = {"u9": {"id": "R9"}}

    def card_for(issue):
        return card_by_url.get(issue["url"]) or card_by_cid.get(issue_custom_id(issue))

    managed_card_ids = _managed_card_ids(issues, card_for, retired_card_by_url)
    removal_authority = _removal_authority_card_ids(issues, card_by_url, retired_card_by_url)

    assert removal_authority <= managed_card_ids
    assert removal_authority == {"C1", "R9"}
    assert managed_card_ids == {"C1", "C2", "R9"}  # C2 still managed (additions still see it)


# --- sync_dependencies: additions unaffected by narrowing -------------------

def test_additions_are_unaffected_by_a_narrowed_removal_authority_set():
    """Additions never consult the removal-authority set at all (_dependency_changes only
    intersects it against removals) -- narrowing it for issue #60 must not block a
    legitimate add whose blocker card is outside the narrower set."""
    issues = [_issue(1, "A"), _issue(2, "B")]
    cards = {1: {"id": "C1"}, 2: {"id": "C2"}}
    calls = {"create": [], "delete": [], "reads": []}

    def fake_read(cfg, cid):
        calls["reads"].append(cid)
        return {"C1": [], "C2": []}.get(cid)

    with patch("sync.agileplace.card_dependencies", side_effect=fake_read), \
         patch("sync.agileplace.incoming_dependency_ids",
               side_effect=lambda entries: {e["cardId"] for e in entries
                                            if e.get("direction") == "incoming"}), \
         patch("sync.agileplace.create_dependencies",
               side_effect=lambda cfg, apply, cid, ids: calls["create"].append((cid, sorted(ids)))), \
         patch("sync.agileplace.delete_dependencies",
               side_effect=lambda cfg, apply, cid, ids: calls["delete"].append((cid, sorted(ids)))):
        # removal_authority_card_ids excludes C2 entirely -- the add must still happen.
        sync_dependencies({}, True, issues, {1: [2]}, cards, lambda i: cards.get(i["number"]),
                          removal_authority_card_ids={"C1"}, poisoned=frozenset())

    assert calls["create"] == [("C1", ["C2"])]
    assert calls["delete"] == []
