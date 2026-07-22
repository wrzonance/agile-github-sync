"""Unit tests for card_coherence.py's pure boundary invariants (issue #70).

These need no network or gh -- they pin what the rest of the sync run depends on:
contested_cards() and lane_conflict() never mutate their inputs and never raise, and
contested_cards() only reports card ids claimed by >= 2 distinct issue URLs. Run: pytest -q
"""
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from card_coherence import contested_cards, laneid_op_value, lane_conflict  # noqa: E402


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
    result = contested_cards(issues, all_card_by_url)
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
    assert contested_cards(issues, all_card_by_url) == {}


def test_contested_cards_excludes_unmatched_and_uncontested():
    issues = [
        {"url": "https://github.com/o/r/issues/1"},
        {"url": "https://github.com/o/r/issues/2"},  # no card match at all
    ]
    all_card_by_url = {"https://github.com/o/r/issues/1": {"id": 100}}
    assert contested_cards(issues, all_card_by_url) == {}


def test_contested_cards_never_mutates_inputs_and_never_raises():
    issues = [
        {"url": "https://github.com/o/r/issues/1"},
        {"url": "https://github.com/o/r/issues/2"},
    ]
    all_card_by_url = {
        "https://github.com/o/r/issues/1": {"id": 100},
        "https://github.com/o/r/issues/2": {"id": 100},
    }
    issues_before = copy.deepcopy(issues)
    all_card_by_url_before = copy.deepcopy(all_card_by_url)

    contested_cards(issues, all_card_by_url)

    assert issues == issues_before
    assert all_card_by_url == all_card_by_url_before

    # Malformed/empty inputs must never raise.
    assert contested_cards([], {}) == {}
    assert contested_cards([{"url": "missing"}], {}) == {}


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
