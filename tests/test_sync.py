"""Unit tests for the pure sync logic: stage derivation, epic rollup, lane matching, 3-way reconcile.

These need no network or gh -- they pin the invariants the live sync depends on. Run: pytest -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reconcile import reconcile, reconcile_value  # noqa: E402
from stages import (epic_key_for_task, epic_rollup, issue_stage,  # noqa: E402
                    lane_matches_stage, title_key)


# --- issue_stage ----------------------------------------------------------

def test_closed_issue_is_done():
    assert issue_stage({"state": "CLOSED", "labels": ["agent:in-progress"]}) == "Done"


def test_open_pr_is_in_review():
    assert issue_stage({"state": "OPEN", "labels": ["agent:in-progress"], "has_open_pr": True}) == "In review"


def test_in_progress_label_or_assignee():
    assert issue_stage({"state": "OPEN", "labels": ["agent:in-progress"]}) == "In progress"
    assert issue_stage({"state": "OPEN", "labels": [], "assignees": ["alice"]}) == "In progress"


def test_ready_then_backlog():
    assert issue_stage({"state": "OPEN", "labels": ["agent:ready"]}) == "Ready"
    assert issue_stage({"state": "OPEN", "labels": []}) == "Backlog"


# --- epic_rollup ----------------------------------------------------------

def test_rollup_empty_is_backlog():
    assert epic_rollup([]) == "Backlog"


def test_rollup_all_done():
    assert epic_rollup(["Done", "Done"]) == "Done"


def test_rollup_any_in_progress_wins():
    assert epic_rollup(["Done", "In progress", "In review"]) == "In progress"


def test_rollup_in_review_when_no_in_progress():
    assert epic_rollup(["Done", "In review", "Backlog"]) == "In review"


def test_rollup_some_done_rest_untouched_is_in_progress():
    assert epic_rollup(["Done", "Backlog"]) == "In progress"


def test_rollup_ready_when_nothing_started():
    assert epic_rollup(["Ready", "Backlog"]) == "Ready"
    assert epic_rollup(["Backlog", "Backlog"]) == "Backlog"


# --- lane matching --------------------------------------------------------

def test_lane_matches_stage_disambiguates_started():
    assert lane_matches_stage("In Review", "In review")
    assert not lane_matches_stage("In Review", "In progress")
    assert lane_matches_stage("In Progress", "In progress")


# --- reconcile (3-way merge) ---------------------------------------------

def test_reconcile_add_on_github_propagates_to_agileplace():
    r = reconcile(base=set(), gh_now={"bug"}, ap_now=set())
    assert r.ap_add == frozenset({"bug"})
    assert r.gh_add == frozenset() and r.gh_remove == frozenset()
    assert r.new_base == frozenset({"bug"})


def test_reconcile_add_on_agileplace_propagates_to_github():
    r = reconcile(base=set(), gh_now=set(), ap_now={"feature"})
    assert r.gh_add == frozenset({"feature"})
    assert r.new_base == frozenset({"feature"})


def test_reconcile_removal_propagates_both_ways():
    # base had X; removed on GitHub; AgilePlace still has it -> remove from AgilePlace too.
    r = reconcile(base={"X"}, gh_now=set(), ap_now={"X"})
    assert r.ap_remove == frozenset({"X"})
    assert r.new_base == frozenset()


def test_reconcile_mixed_add_and_remove():
    r = reconcile(base={"X"}, gh_now=set(), ap_now={"X", "Y"})  # gh removed X, ap added Y
    assert r.new_base == frozenset({"Y"})
    assert r.ap_remove == frozenset({"X"})
    assert r.gh_add == frozenset({"Y"})


def test_reconcile_noop_when_all_equal():
    r = reconcile(base={"a", "b"}, gh_now={"a", "b"}, ap_now={"a", "b"})
    assert not (r.gh_add or r.gh_remove or r.ap_add or r.ap_remove)
    assert r.new_base == frozenset({"a", "b"})


# --- title-key convention (sub-issue fallback) ---------------------------

def test_title_key():
    assert title_key("[EP-0C] API conventions") == "EP-0C"
    assert title_key("[0C2] versioning middleware") == "0C2"
    assert title_key("no brackets here") is None


def test_epic_key_for_task():
    assert epic_key_for_task("0C2") == "EP-0C"
    assert epic_key_for_task("1A4") == "EP-1A"
    assert epic_key_for_task("0B5") == "EP-0B"


# --- reconcile_value (single-valued milestone merge) ---------------------

def test_reconcile_value_only_one_side_changed():
    assert reconcile_value(base="A", gh="B", ap="A") == "B"   # GitHub changed
    assert reconcile_value(base="A", gh="A", ap="B") == "B"   # AgilePlace changed


def test_reconcile_value_conflict_github_wins():
    assert reconcile_value(base="A", gh="B", ap="C") == "B"


def test_reconcile_value_unset_and_agree():
    assert reconcile_value(base="A", gh=None, ap="A") is None   # GitHub cleared it -> propagate
    assert reconcile_value(base=None, gh=None, ap=None) is None
    assert reconcile_value(base="A", gh="A", ap="A") == "A"     # no change
