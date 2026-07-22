"""Unit tests for sync.sync_dependencies (issue #57, Phase 1).

The contract: GitHub blocked-by edges are mirrored as native AgilePlace dependencies,
EVERY edge (a Done blocker's edge is structural -- unlike the Blocked flag, which only
reflects incomplete blockers). GitHub is authoritative ONLY between two sync-managed
cards; dependencies touching non-managed cards are invisible in both directions. A
failed/malformed read (None) skips that card entirely -- duplicate-create behavior is
unconfirmed live, so nothing may be blindly re-created against unknown state.

Run: pytest -q
"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sync import (  # noqa: E402
    _blocker_cards,
    _dependency_changes,
    _removal_authority_card_ids,
    issue_custom_id,
    sync_dependencies,
)


# --- _dependency_changes (pure) -------------------------------------------

def test_changes_adds_missing_and_removes_stale_managed():
    adds, removes = _dependency_changes({"B1", "B2"}, {"B2", "B3"}, {"B1", "B2", "B3"})
    assert adds == ["B1"]
    assert removes == ["B3"]


def test_changes_never_removes_dependencies_on_non_managed_cards():
    adds, removes = _dependency_changes(set(), {"HUMAN1"}, {"B1"})
    assert adds == []
    assert removes == []


def test_changes_in_sync_is_a_no_op():
    assert _dependency_changes({"B1"}, {"B1"}, {"B1"}) == ([], [])


# --- sync_dependencies (step behavior) ------------------------------------

def _issue(number, key):
    return {"number": number, "title": f"[{key}] t", "url": f"https://github.com/o/r/issues/{number}"}


def _harness(cards):
    """card_for keyed by issue number; cards: {number: card_dict_or_None}."""
    return lambda issue: cards.get(issue["number"])


def _run(issues, blocked_by, cards, managed, reads, blocker_cards=None, poisoned=frozenset()):
    """Run sync_dependencies with agileplace fully faked; return recorded write calls.
    blocker_cards defaults to every issue's card (the _blocker_cards contract); pass it
    explicitly to model retired blockers that resolve outside the active-issue set."""
    calls = {"create": [], "delete": [], "reads": []}

    def fake_read(cfg, cid):
        calls["reads"].append(cid)
        return reads.get(cid)

    if blocker_cards is None:
        blocker_cards = {i["number"]: cards[i["number"]]
                         for i in issues if cards.get(i["number"])}
    with patch("sync.agileplace.card_dependencies", side_effect=fake_read), \
         patch("sync.agileplace.incoming_dependency_ids",
               side_effect=lambda entries: {e["cardId"] for e in entries
                                            if e.get("direction") == "incoming"}), \
         patch("sync.agileplace.create_dependencies",
               side_effect=lambda cfg, apply, cid, ids: calls["create"].append((cid, sorted(ids)))), \
         patch("sync.agileplace.delete_dependencies",
               side_effect=lambda cfg, apply, cid, ids: calls["delete"].append((cid, sorted(ids)))):
        sync_dependencies({}, True, issues, blocked_by, blocker_cards, _harness(cards), managed,
                          poisoned)
    return calls


def test_creates_missing_dependency_between_managed_cards():
    issues = [_issue(1, "A"), _issue(2, "B")]
    cards = {1: {"id": "C1"}, 2: {"id": "C2"}}
    calls = _run(issues, {1: [2]}, cards, {"C1", "C2"}, reads={"C1": [], "C2": []})
    assert calls["create"] == [("C1", ["C2"])]
    assert calls["delete"] == []


def test_removes_stale_managed_dependency_but_never_a_non_managed_one():
    issues = [_issue(1, "A")]
    cards = {1: {"id": "C1"}}
    reads = {"C1": [{"direction": "incoming", "cardId": "C9"},      # managed, no GH edge -> remove
                    {"direction": "incoming", "cardId": "HUMAN"}]}  # not managed -> untouchable
    calls = _run(issues, {1: []}, cards, {"C1", "C9"}, reads)
    assert calls["create"] == []
    assert calls["delete"] == [("C1", ["C9"])]


def test_failed_read_skips_that_card_but_not_others(capsys):
    issues = [_issue(1, "A"), _issue(2, "B"), _issue(3, "C")]
    cards = {1: {"id": "C1"}, 2: {"id": "C2"}, 3: {"id": "C3"}}
    reads = {"C1": None, "C2": [], "C3": []}  # C1's read fails
    calls = _run(issues, {1: [2], 2: [3]}, cards, {"C1", "C2", "C3"}, reads)
    assert calls["create"] == [("C2", ["C3"])]  # C1's desired add was NOT attempted
    assert "WARN" in capsys.readouterr().out


def test_plan_only_card_creates_all_desired_without_a_read():
    issues = [_issue(1, "A"), _issue(2, "B")]
    cards = {1: {"id": "planned-card:x", "_planOnly": True}, 2: {"id": "C2"}}
    calls = _run(issues, {1: [2]}, cards, {"planned-card:x", "C2"}, reads={"C2": []})
    assert calls["create"] == [("planned-card:x", ["C2"])]
    assert "planned-card:x" not in calls["reads"]  # a plan-only id never crosses a read boundary


def test_blocker_without_a_card_is_excluded_from_desired():
    issues = [_issue(1, "A"), _issue(2, "B")]
    cards = {1: {"id": "C1"}, 2: None}  # blocker issue 2 has no card
    calls = _run(issues, {1: [2]}, cards, {"C1"}, reads={"C1": []})
    assert calls["create"] == []
    assert calls["delete"] == []


# --- Issue #75 task 4: the poison guard skips a poisoned card's own dependency sync -----------

def test_poisoned_own_card_never_reads_or_writes_dependencies(capsys):
    """A card marked poisoned (Layer 2 lane-conflict) must have its dependency sync skipped
    entirely -- not even a read is attempted, since the card's own state this run was already
    refused persistence at flush; reading its dependencies to compute an add/remove would still
    write against a card whose PATCH never landed."""
    issues = [_issue(1, "A"), _issue(2, "B")]
    cards = {1: {"id": "C1"}, 2: {"id": "C2"}}
    calls = _run(issues, {1: [2]}, cards, {"C1", "C2"}, reads={"C1": [], "C2": []},
                poisoned=frozenset({"C1"}))
    assert calls["create"] == []
    assert calls["delete"] == []
    assert "C1" not in calls["reads"], "a poisoned card's dependencies must never even be read"
    assert "WARN" in capsys.readouterr().out


def test_poisoned_desired_blocker_card_is_never_added():
    """A poisoned card that is someone ELSE's desired blocker must be filtered out of the
    already-computed `adds`, not pre-filtered out of `desired` -- pre-filtering would only
    change which set membership check drops it, not the observable behavior here, but keeps the
    filter logic exercised against the same post-computation list sync.py actually filters."""
    issues = [_issue(1, "A")]
    cards = {1: {"id": "C1"}}
    blocker_cards = {1: {"id": "C1"}, 9: {"id": "C9"}}
    calls = _run(issues, {1: [9]}, cards, {"C1", "C9"}, reads={"C1": []},
                blocker_cards=blocker_cards, poisoned=frozenset({"C9"}))
    assert calls["create"] == [], "C9 is desired but poisoned -- never created"


def test_poisoned_stale_blocker_card_is_never_removed():
    """A poisoned card that is a stale (no-longer-desired) dependency must be filtered out of the
    already-computed `removes` -- never pre-filtered out of `desired`, since `desired` never
    contained it in the first place; pre-filtering would have no lever here at all, which is
    exactly why the guard must operate on `removes` post-`_dependency_changes`."""
    issues = [_issue(1, "A")]
    cards = {1: {"id": "C1"}}
    reads = {"C1": [{"direction": "incoming", "cardId": "C9"}]}  # stale edge, not desired
    calls = _run(issues, {1: []}, cards, {"C1", "C9"}, reads, poisoned=frozenset({"C9"}))
    assert calls["delete"] == [], "C9 is stale but poisoned -- never deleted"


def test_edge_to_retired_done_blocker_is_preserved_not_deleted():
    """P1 from the gpt-5.6-sol adversarial review: an active issue blocked by a RETIRED
    (NOT_PLANNED/DUPLICATE) issue whose URL-owned card survives must keep its native
    dependency. Blockers resolving through active issues only dropped the edge from
    `desired` while the retired card stayed managed -- deleting the valid dependency as
    stale on every run."""
    issues = [_issue(1, "A")]                      # only the active issue is syncable
    cards = {1: {"id": "C1"}}
    blocker_cards = {1: {"id": "C1"}, 9: {"id": "R9"}}   # 9 = retired blocker, card R9
    reads = {"C1": [{"direction": "incoming", "cardId": "R9"}]}  # edge already on the board
    calls = _run(issues, {1: [9]}, cards, {"C1", "R9"}, reads, blocker_cards=blocker_cards)
    assert calls["delete"] == []   # the edge is desired -- NOT stale
    assert calls["create"] == []   # and already present -- nothing to add


def test_regression_issue_60_dependency_onto_non_authority_card_is_preserved():
    """Regression, issue #60's exact reported scenario:

    1. Retired issue #9's card R9 lost its external link (a human edited it away), so
       retired_card_by_url can no longer claim it under #9's URL.
    2. Active issue #20 happens to share R9's customId, so _matching_card's customId
       fallback silently adopts R9 for #20 -- a customId collision.
    3. Active issue #1 is blocked by #9 on GitHub, and its card C1 already carries a live
       native dependency onto R9 on the board.

    _blocker_cards can then only resolve blocker #20 -> R9, never #9 itself (its card is
    gone from retired_card_by_url), so `desired` for #1 silently drops the R9 edge. The
    pre-fix code used the broad managed_card_ids set (which DOES include R9, via #20's
    customId adoption) as removal authority, so it deleted the still-valid C1 -> R9
    dependency as stale -- on every run. The fix narrows removal authority to
    _removal_authority_card_ids, which excludes R9 (issue #20 never reached it through
    its OWN url). Chains the real _blocker_cards / _removal_authority_card_ids
    computations into sync_dependencies -- pinning the composed pipeline, not just each
    half in isolation -- across two independent runs, confirming the edge survives on any
    run, not just the first."""
    issue_1 = _issue(1, "A")
    issue_20 = _issue(20, "T")
    retired_issue_9 = {"number": 9, "url": "https://github.com/o/r/issues/9"}

    card_by_url = {issue_1["url"]: {"id": "C1"}}   # issue 20's OWN url has no card entry
    card_by_cid = {"T": {"id": "R9"}}              # R9 only reachable via issue 20's customId
    retired_card_by_url = {}                       # #9's external link is gone

    def card_for(issue):
        return card_by_url.get(issue["url"]) or card_by_cid.get(issue_custom_id(issue))

    removal_authority = _removal_authority_card_ids(
        [issue_1, issue_20], card_by_url, retired_card_by_url)
    assert removal_authority == {"C1"}  # R9 excluded -- #20 only reached it via customId

    blocker_card_by_number = _blocker_cards(
        {1: issue_1, 20: issue_20}, card_for, [retired_issue_9], retired_card_by_url)
    assert 9 not in blocker_card_by_number  # #9's card is unresolvable -- its link is gone

    for _ in range(2):  # the edge must survive every run, not just the first
        calls = _run([issue_1, issue_20], {1: [9], 20: []},
                     {1: {"id": "C1"}, 20: {"id": "R9"}}, removal_authority,
                     reads={"C1": [{"direction": "incoming", "cardId": "R9"}], "R9": []},
                     blocker_cards=blocker_card_by_number)
        assert calls["delete"] == []
        assert calls["create"] == []


def test_sync_blocker_cards_helper_includes_retired_url_owned_cards():
    from sync import _blocker_cards
    by_number = {1: {"number": 1, "url": "u1"}}
    retired = [{"number": 9, "url": "u9"}, {"number": 8, "url": "u8-no-card"}]
    resolved = _blocker_cards(by_number, lambda i: {"id": f"C{i['number']}"},
                              retired, {"u9": {"id": "R9"}})
    assert resolved == {1: {"id": "C1"}, 9: {"id": "R9"}}  # 8 has no card -> excluded


# --- _removal_authority_card_ids (pure) -----------------------------------

def test_removal_authority_includes_url_matched_active_and_retired_cards():
    """Control: a card an active issue reached through its OWN external-link URL, and a
    URL-matched retired card, both carry strong identity -- both belong in the result."""
    issues = [_issue(1, "A")]
    card_by_url = {issues[0]["url"]: {"id": "C1"}}
    retired_card_by_url = {"https://github.com/o/r/issues/9": {"id": "R9"}}
    result = _removal_authority_card_ids(issues, card_by_url, retired_card_by_url)
    assert result == {"C1", "R9"}


def test_removal_authority_excludes_card_reached_only_by_customid_fallback():
    """Exclusion (issue #60): card_by_url holds an entry, but not under THIS issue's own
    URL -- modeling a card that some other issue's _matching_card call resolved only via
    the customId fallback (or a retired card whose link was manually removed and got
    silently adopted through a customId collision). That card must contribute nothing to
    issue 1's removal authority, even though a naive builder that pooled every value in
    card_by_url (rather than looking up each issue's own URL) would include it."""
    issues = [_issue(1, "A")]
    card_by_url = {"https://github.com/o/r/issues/999": {"id": "OTHER"}}  # not issue 1's url
    result = _removal_authority_card_ids(issues, card_by_url, retired_card_by_url={})
    assert result == set()


def test_every_edge_is_mirrored_not_only_incomplete_blockers():
    """blocked_by carries raw edges; sync_dependencies must not filter by stage/completion --
    an edge whose blocker is Done is still desired (the Blocked flag differs deliberately)."""
    issues = [_issue(1, "A"), _issue(2, "DONE-BLOCKER")]
    cards = {1: {"id": "C1"}, 2: {"id": "C2"}}
    calls = _run(issues, {1: [2]}, cards, {"C1", "C2"}, reads={"C1": [], "C2": []})
    assert calls["create"] == [("C1", ["C2"])]
