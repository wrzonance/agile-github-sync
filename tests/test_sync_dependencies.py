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

from sync import _dependency_changes, sync_dependencies  # noqa: E402


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


def _run(issues, blocked_by, cards, managed, reads):
    """Run sync_dependencies with agileplace fully faked; return recorded write calls."""
    calls = {"create": [], "delete": [], "reads": []}

    def fake_read(cfg, cid):
        calls["reads"].append(cid)
        return reads.get(cid)

    by_number = {i["number"]: i for i in issues}
    with patch("sync.agileplace.card_dependencies", side_effect=fake_read), \
         patch("sync.agileplace.incoming_dependency_ids",
               side_effect=lambda entries: {e["cardId"] for e in entries
                                            if e.get("direction") == "incoming"}), \
         patch("sync.agileplace.create_dependencies",
               side_effect=lambda cfg, apply, cid, ids: calls["create"].append((cid, sorted(ids)))), \
         patch("sync.agileplace.delete_dependencies",
               side_effect=lambda cfg, apply, cid, ids: calls["delete"].append((cid, sorted(ids)))):
        sync_dependencies({}, True, issues, blocked_by, by_number, _harness(cards), managed)
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


def test_every_edge_is_mirrored_not_only_incomplete_blockers():
    """blocked_by carries raw edges; sync_dependencies must not filter by stage/completion --
    an edge whose blocker is Done is still desired (the Blocked flag differs deliberately)."""
    issues = [_issue(1, "A"), _issue(2, "DONE-BLOCKER")]
    cards = {1: {"id": "C1"}, 2: {"id": "C2"}}
    calls = _run(issues, {1: [2]}, cards, {"C1", "C2"}, reads={"C1": [], "C2": []})
    assert calls["create"] == [("C1", ["C2"])]
