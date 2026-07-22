"""Unit tests for card_coherence.py's pure boundary invariants (issues #70 and #75).

These need no network or gh -- they pin what the rest of the sync run depends on:
contested_cards() and lane_conflict() never mutate their inputs and never raise, and
contested_cards() only reports card ids claimed by >= 2 distinct issue URLs -- via EITHER
match path (URL or customId fallback), with the URL path always taking precedence over the
customId fallback for a given issue. poisoned_card_ids() never mutates card_ops or raises,
and treats a missing 'poisoned' key or a non-dict entry value as not-poisoned rather than
an error. Run: pytest -q
"""
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from card_coherence import (  # noqa: E402
    contested_cards,
    laneid_op_value,
    lane_conflict,
    poisoned_card_ids,
)


# --- contested_cards --------------------------------------------------------

def test_contested_cards_groups_only_multiply_claimed_cards():
    issues = [
        {"url": "https://github.com/o/r/issues/1"},
        {"url": "https://github.com/o/r/issues/2"},
        {"url": "https://github.com/o/r/issues/3"},
    ]
    all_card_by_url = {
        "https://github.com/o/r/issues/1": {"id": 100},
        "https://github.com/o/r/issues/2": {"id": 100},  # same card as issue 1 -> contested
        "https://github.com/o/r/issues/3": {"id": 200},  # sole claimant -> not contested
    }
    result = contested_cards(issues, all_card_by_url, {})
    assert result == {
        "100": {"https://github.com/o/r/issues/1", "https://github.com/o/r/issues/2"},
    }


def test_contested_cards_defers_idless_cards_instead_of_raising():
    """A partial AgilePlace payload can carry a matchable externalLink but no `id` yet. Such a card
    is unresolvable -- it cannot be excluded by id downstream -- so contested_cards must skip it
    (defer, exactly as the rest of the run's `card.get("id")` guards do) rather than KeyError on
    `card["id"]` and abort the whole sync. Two issue URLs both resolving only to an id-less card
    contest nothing, because there is no id to fence."""
    issues = [
        {"url": "https://github.com/o/r/issues/1"},
        {"url": "https://github.com/o/r/issues/2"},
    ]
    all_card_by_url = {
        "https://github.com/o/r/issues/1": {"customId": "KEY"},           # no id
        "https://github.com/o/r/issues/2": {"customId": "KEY", "id": ""},  # falsy id
    }
    assert contested_cards(issues, all_card_by_url, {}) == {}


def test_contested_cards_excludes_unmatched_and_uncontested():
    issues = [
        {"url": "https://github.com/o/r/issues/1"},
        {"url": "https://github.com/o/r/issues/2"},  # no card match at all
    ]
    all_card_by_url = {"https://github.com/o/r/issues/1": {"id": 100}}
    assert contested_cards(issues, all_card_by_url, {}) == {}


def test_contested_cards_never_mutates_inputs_and_never_raises():
    issues = [
        {"url": "https://github.com/o/r/issues/1"},
        {"url": "https://github.com/o/r/issues/2"},
    ]
    all_card_by_url = {
        "https://github.com/o/r/issues/1": {"id": 100},
        "https://github.com/o/r/issues/2": {"id": 100},
    }
    all_card_by_cid = {"OTHER": {"id": 999}}
    issues_before = copy.deepcopy(issues)
    all_card_by_url_before = copy.deepcopy(all_card_by_url)
    all_card_by_cid_before = copy.deepcopy(all_card_by_cid)

    contested_cards(issues, all_card_by_url, all_card_by_cid)

    assert issues == issues_before
    assert all_card_by_url == all_card_by_url_before
    assert all_card_by_cid == all_card_by_cid_before

    # Malformed/empty inputs must never raise.
    assert contested_cards([], {}, {}) == {}
    assert contested_cards([{"url": "missing"}], {}, {}) == {}


# --- contested_cards: customId fallback path (issue #75) --------------------

def test_contested_cards_detects_a_pure_customid_collision():
    """Two issues with ZERO url claims -- both resolve only via the customId fallback onto the
    same card -- must be fenced exactly like a URL collision. Claimant identity is still each
    issue's OWN url, even though the URL match path never fired for either."""
    issues = [
        {"url": "https://github.com/o/r/issues/1", "title": "[KEY] one", "number": 1},
        {"url": "https://github.com/o/r/issues/2", "title": "[KEY] two", "number": 2},
    ]
    all_card_by_cid = {"KEY": {"id": 300}}
    result = contested_cards(issues, {}, all_card_by_cid)
    assert result == {
        "300": {"https://github.com/o/r/issues/1", "https://github.com/o/r/issues/2"},
    }


def test_contested_cards_detects_a_mixed_url_and_customid_collision():
    """One issue claims the card via its own url; a second, distinct issue claims the SAME card id
    only via the customId fallback. Any-path fencing must merge these onto one contested entry."""
    issues = [
        {"url": "https://github.com/o/r/issues/1", "title": "url-matched", "number": 1},
        {"url": "https://github.com/o/r/issues/2", "title": "[KEY] two", "number": 2},
    ]
    all_card_by_url = {"https://github.com/o/r/issues/1": {"id": 400}}
    all_card_by_cid = {"KEY": {"id": 400}}
    result = contested_cards(issues, all_card_by_url, all_card_by_cid)
    assert result == {
        "400": {"https://github.com/o/r/issues/1", "https://github.com/o/r/issues/2"},
    }


def test_contested_cards_url_match_takes_precedence_over_customid_fallback():
    """An issue whose own url resolves to card A must claim ONLY card A, even when its customId
    would separately resolve to a different card B -- the customId fallback must never be
    consulted once the url path already resolved. So card B, claimed only by a second issue's
    customId fallback, stays a single (uncontested) claimant."""
    first = {"url": "https://github.com/o/r/issues/1", "title": "[OTHERKEY] one", "number": 1}
    second = {"url": "https://github.com/o/r/issues/2", "title": "[OTHERKEY] two", "number": 2}
    all_card_by_url = {"https://github.com/o/r/issues/1": {"id": 500}}  # only `first`'s url matches
    all_card_by_cid = {"OTHERKEY": {"id": 600}}  # would collide with `first` if url didn't take precedence

    result = contested_cards([first, second], all_card_by_url, all_card_by_cid)

    assert result == {}, (
        "card 500 (first's URL claim) and card 600 (second's customId claim) each have exactly "
        "one claimant -- they must not be merged just because they share a customId string")


def test_contested_cards_defers_idless_card_reached_via_customid():
    issues = [
        {"url": "https://github.com/o/r/issues/1", "title": "[KEY] one", "number": 1},
        {"url": "https://github.com/o/r/issues/2", "title": "[KEY] two", "number": 2},
    ]
    all_card_by_cid = {"KEY": {"customId": "KEY"}}  # no "id" key at all
    assert contested_cards(issues, {}, all_card_by_cid) == {}


def test_contested_cards_omits_a_single_claimant_reached_via_customid():
    issues = [{"url": "https://github.com/o/r/issues/1", "title": "[KEY] one", "number": 1}]
    all_card_by_cid = {"KEY": {"id": 700}}
    assert contested_cards(issues, {}, all_card_by_cid) == {}


def test_contested_cards_never_raises_on_minimal_issue_dicts_through_customid_path():
    """An issue dict lacking 'title'/'number' (as used by this module's other never-raises tests)
    must never reach issue_custom_id() -- it has no customId claim, treated as no claim at all,
    never a KeyError."""
    issues = [
        {"url": "https://github.com/o/r/issues/1"},
        {"url": "https://github.com/o/r/issues/2"},
    ]
    all_card_by_cid = {"KEY": {"id": 800}}  # unreachable: neither issue carries title/number
    assert contested_cards(issues, {}, all_card_by_cid) == {}


# --- lane_conflict -----------------------------------------------------------

def test_lane_conflict_no_lane_op_is_a_noop():
    ops = [{"op": "replace", "path": "/title", "value": "x"}]
    assert lane_conflict(ops, "lane-a") == ("lane-a", False)
    assert lane_conflict(ops, None) == (None, False)


def test_lane_conflict_first_seen_lane_id_is_adopted():
    ops = [{"op": "replace", "path": "/laneId", "value": "lane-a"}]
    assert lane_conflict(ops, None) == ("lane-a", False)


def test_lane_conflict_matching_value_is_not_a_conflict():
    ops = [{"op": "replace", "path": "/laneId", "value": "lane-a"}]
    assert lane_conflict(ops, "lane-a") == ("lane-a", False)


def test_lane_conflict_diverging_value_freezes_at_first_seen():
    ops = [{"op": "replace", "path": "/laneId", "value": "lane-b"}]
    updated_lane_id, conflict = lane_conflict(ops, "lane-a")
    assert conflict is True
    assert updated_lane_id == "lane-a"  # frozen, not overwritten with the conflicting value


def test_lane_conflict_never_mutates_ops_and_never_raises():
    ops = [{"op": "replace", "path": "/laneId", "value": "lane-b"}]
    ops_before = copy.deepcopy(ops)

    lane_conflict(ops, "lane-a")

    assert ops == ops_before

    # Malformed/empty inputs must never raise.
    assert lane_conflict([], None) == (None, False)
    assert lane_conflict([{"op": "replace", "path": "/other"}], "lane-a") == ("lane-a", False)


# --- laneid_op_value ----------------------------------------------------------

def test_laneid_op_value_returns_none_when_no_laneid_op():
    ops = [{"op": "replace", "path": "/title", "value": "x"}]
    assert laneid_op_value(ops) is None
    assert laneid_op_value([]) is None


def test_laneid_op_value_returns_the_laneid_op_value():
    ops = [{"op": "replace", "path": "/title", "value": "x"},
           {"op": "replace", "path": "/laneId", "value": "lane-b"}]
    assert laneid_op_value(ops) == "lane-b"


def test_laneid_op_value_returns_last_laneid_op_value_when_multiple():
    ops = [{"op": "replace", "path": "/laneId", "value": "lane-a"},
           {"op": "replace", "path": "/laneId", "value": "lane-b"}]
    assert laneid_op_value(ops) == "lane-b"


def test_laneid_op_value_never_mutates_ops_and_never_raises():
    ops = [{"op": "replace", "path": "/laneId", "value": "lane-b"}]
    ops_before = copy.deepcopy(ops)

    laneid_op_value(ops)

    assert ops == ops_before
    assert laneid_op_value([{"op": "replace", "path": "/other"}]) is None


# --- poisoned_card_ids ---------------------------------------------------------

def test_poisoned_card_ids_returns_only_the_poisoned_entries():
    card_ops = {
        "1": {"card": {"id": 1}, "ops": [], "notes": [], "lane_id": "a", "poisoned": True},
        "2": {"card": {"id": 2}, "ops": [], "notes": [], "lane_id": "b", "poisoned": False},
    }
    assert poisoned_card_ids(card_ops) == frozenset({"1"})


def test_poisoned_card_ids_returns_empty_frozenset_for_empty_card_ops():
    assert poisoned_card_ids({}) == frozenset()


def test_poisoned_card_ids_treats_missing_poisoned_key_as_not_poisoned():
    card_ops = {"1": {"card": {"id": 1}, "ops": [], "notes": [], "lane_id": "a"}}
    assert poisoned_card_ids(card_ops) == frozenset()


def test_poisoned_card_ids_treats_non_dict_entry_as_not_poisoned_instead_of_raising():
    card_ops = {
        "1": "not-a-dict",
        "2": {"card": {"id": 2}, "ops": [], "notes": [], "lane_id": "b", "poisoned": True},
    }
    assert poisoned_card_ids(card_ops) == frozenset({"2"})


def test_poisoned_card_ids_never_mutates_card_ops():
    card_ops = {
        "1": {"card": {"id": 1}, "ops": [], "notes": [], "lane_id": "a", "poisoned": True},
    }
    card_ops_before = copy.deepcopy(card_ops)

    poisoned_card_ids(card_ops)

    assert card_ops == card_ops_before
