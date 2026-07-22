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
    fence_run_indices,
    filter_poisoned_edges,
    laneid_op_value,
    lane_conflict,
    poisoned_card_ids,
    same_card,
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


def test_contested_cards_never_raises_on_malformed_value_types():
    """The 'never raises for ANY issue dict shape' contract must hold for malformed VALUE types,
    not only missing keys: a non-string 'title' would blow up issue_custom_id()'s title_key(),
    and an unhashable 'url' would blow up the url-index lookup. Both must be treated as no claim,
    never propagated as AttributeError/TypeError."""
    issues = [
        {"url": "u", "title": 42, "number": 1},        # non-string title -> no customId claim
        {"url": ["a", "b"], "title": "T", "number": 2},  # unhashable url -> no url lookup
    ]
    all_card_by_cid = {"T": {"id": 800}}
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


# --- filter_poisoned_edges (issue #75) ----------------------------------------
# Shared by sync.py's child-connection loop and sync_dependencies() -- both compute an
# adds/removes pair and then must drop any poisoned card id from either list, reporting
# whether anything was actually dropped so the caller can print its own WARN.

def test_filter_poisoned_edges_passes_through_when_nothing_is_poisoned():
    adds, removes, dropped = filter_poisoned_edges(["1", "2"], ["3"], frozenset())
    assert adds == ["1", "2"]
    assert removes == ["3"]
    assert dropped is False


def test_filter_poisoned_edges_drops_poisoned_ids_from_adds():
    adds, removes, dropped = filter_poisoned_edges(["1", "2"], [], frozenset({"2"}))
    assert adds == ["1"]
    assert removes == []
    assert dropped is True


def test_filter_poisoned_edges_drops_poisoned_ids_from_removes():
    adds, removes, dropped = filter_poisoned_edges([], ["3", "4"], frozenset({"4"}))
    assert adds == []
    assert removes == ["3"]
    assert dropped is True


def test_filter_poisoned_edges_drops_from_both_lists_at_once():
    adds, removes, dropped = filter_poisoned_edges(["1", "2"], ["3", "4"], frozenset({"2", "3"}))
    assert adds == ["1"]
    assert removes == ["4"]
    assert dropped is True


def test_filter_poisoned_edges_never_mutates_inputs_and_never_raises():
    adds = ["1", "2"]
    removes = ["3"]
    adds_before, removes_before = list(adds), list(removes)

    filter_poisoned_edges(adds, removes, frozenset({"2"}))

    assert adds == adds_before
    assert removes == removes_before
    assert filter_poisoned_edges([], [], frozenset()) == ([], [], False)


# --- same_card (review follow-up on issue #75) --------------------------------

def test_same_card_identical_object_is_always_the_same_card():
    card = {"customId": "KEY"}  # no id at all -- identity alone must still be enough
    assert same_card(card, card) is True


def test_same_card_matching_non_empty_ids_are_the_same_card():
    assert same_card({"id": "100"}, {"id": "100"}) is True
    assert same_card({"id": 100}, {"id": "100"}) is True  # int vs str id, same value


def test_same_card_differing_ids_are_not_the_same_card():
    assert same_card({"id": "100"}, {"id": "200"}) is False


def test_same_card_either_side_falsy_is_never_the_same_card():
    assert same_card(None, {"id": "100"}) is False
    assert same_card({"id": "100"}, None) is False
    assert same_card({}, {}) is False


def test_same_card_two_distinct_idless_dicts_are_never_the_same_card():
    assert same_card({"customId": "A"}, {"customId": "B"}) is False


def test_same_card_never_mutates_inputs():
    left, right = {"id": "1"}, {"id": "1"}
    left_before, right_before = dict(left), dict(right)

    same_card(left, right)

    assert left == left_before
    assert right == right_before


# --- fence_run_indices (review follow-up on issue #75) -------------------------

def test_fence_run_indices_passes_through_cleanly_when_nothing_is_contested():
    active = [{"url": "https://github.com/o/r/issues/1", "title": "one", "number": 1}]
    all_card_by_url = {"https://github.com/o/r/issues/1": {"id": "100"}}

    result = fence_run_indices({}, active, [], all_card_by_url, {})

    assert result.card_by_url == all_card_by_url
    assert result.syncable_issues == active
    assert result.retired_card_by_url == {}
    assert result.contested_urls == frozenset()
    assert result.warnings == ()


def test_fence_run_indices_excludes_a_contested_card_and_warns_once():
    issue1 = {"url": "https://github.com/o/r/issues/1", "title": "one", "number": 1}
    issue2 = {"url": "https://github.com/o/r/issues/2", "title": "two", "number": 2}
    all_card_by_url = {
        "https://github.com/o/r/issues/1": {"id": "100"},
        "https://github.com/o/r/issues/2": {"id": "100"},
    }
    contested = {"100": {issue1["url"], issue2["url"]}}

    result = fence_run_indices(contested, [issue1, issue2], [], all_card_by_url, {})

    assert result.card_by_url == {}, "the contested card must be dropped from card_by_url"
    assert result.syncable_issues == [], "both claiming issues must be deferred"
    assert result.contested_urls == {issue1["url"], issue2["url"]}
    assert len(result.warnings) == 1
    assert result.warnings[0].startswith("WARN  card 100 claimed by 2 issue URLs")


def test_fence_run_indices_defers_an_active_issue_whose_card_is_held_by_retirement():
    """An active issue whose OWN url card is untouched by Layer 1, but whose customId is also
    carried by a DIFFERENT issue's retiring card, must still be deferred (a retirement
    reservation) -- and excluded from card_by_url/card_by_cid so it can't be adopted this run."""
    active = {"url": "https://github.com/o/r/issues/1", "title": "[KEY] one", "number": 1}
    retired = {"url": "https://github.com/o/r/issues/2", "title": "[KEY] two", "number": 2,
              "state_reason": "NOT_PLANNED"}
    retiring_card = {"id": "200", "customId": "KEY"}
    all_card_by_url = {retired["url"]: retiring_card}
    all_card_by_cid = {"KEY": retiring_card}

    result = fence_run_indices({}, [active], [retired], all_card_by_url, all_card_by_cid)

    assert result.syncable_issues == [], "the active issue must be deferred, not adopt the retiring card"
    assert result.card_by_url == {}, "the retiring card itself is reserved, not offered for matching"
    assert len(result.warnings) == 1
    assert result.warnings[0].startswith("WARN  deferring active card [KEY]: customId is held by")


def test_fence_run_indices_never_mutates_inputs_and_never_raises():
    active = [{"url": "https://github.com/o/r/issues/1", "title": "one", "number": 1}]
    retired = [{"url": "https://github.com/o/r/issues/2", "title": "two", "number": 2,
               "state_reason": "NOT_PLANNED"}]
    all_card_by_url = {"https://github.com/o/r/issues/1": {"id": "100"},
                       "https://github.com/o/r/issues/2": {"id": "200"}}
    contested = {"100": {active[0]["url"], "https://github.com/o/r/issues/3"}}
    active_before = copy.deepcopy(active)
    retired_before = copy.deepcopy(retired)
    all_card_by_url_before = copy.deepcopy(all_card_by_url)
    contested_before = copy.deepcopy(contested)

    fence_run_indices(contested, active, retired, all_card_by_url, {})

    assert active == active_before
    assert retired == retired_before
    assert all_card_by_url == all_card_by_url_before
    assert contested == contested_before

    # Malformed/empty inputs must never raise.
    assert fence_run_indices({}, [], [], {}, {}).syncable_issues == []
