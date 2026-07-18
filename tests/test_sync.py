"""Unit tests for the pure sync logic: stage derivation, epic rollup, lane matching, 3-way reconcile.

These need no network or gh -- they pin the invariants the live sync depends on. Run: pytest -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agileplace import resolve_lane_for_stage  # noqa: E402
from ghproject import parse_items  # noqa: E402
from reconcile import reconcile, reconcile_value  # noqa: E402
from stages import (blocked_reason, epic_key_for_task, issue_stage,  # noqa: E402
                    lane_matches_stage, normalize_status, title_key)
from sync import _card_milestones, issue_card_title, resolve_issue_stage  # noqa: E402


def _board_lanes():
    """Models the user's real board: 3 cardStatus tiers, custom sub-lanes, one parent container lane."""
    return [
        {"id": "p", "title": "Not Started - Future Work", "cardStatus": "notStarted"},
        {"id": "nr", "title": "New Requests", "cardStatus": "notStarted", "parentLaneId": "p"},
        {"id": "ap", "title": "Approved", "cardStatus": "notStarted", "parentLaneId": "p"},
        {"id": "rs", "title": "Ready to Start", "cardStatus": "notStarted", "parentLaneId": "p"},
        {"id": "dn", "title": "Doing Now", "cardStatus": "started"},
        {"id": "ur", "title": "Under Review", "cardStatus": "started"},
        {"id": "rf", "title": "Recently Finished", "cardStatus": "finished"},
    ]


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


# --- lane matching: strict word boundaries --------------------------------

def test_lane_matches_word_boundaries():
    assert lane_matches_stage("Under Review", "In review")
    assert lane_matches_stage("Ready to Start", "Ready")
    assert not lane_matches_stage("Reviewers", "In review")   # no false hit inside a word
    assert not lane_matches_stage("Readying", "Ready")
    assert not lane_matches_stage("In Review", "In progress")


# --- milestone tag selection: base/gh-anchor precedence --------------------

def test_card_milestones_pure_and_deterministic():
    card = {"tags": ["milestone:0.3.0", "milestone:0.1.0", "milestone:"]}
    # same inputs -> same output, repeated calls, no hidden state
    assert _card_milestones(card, "0.1.0", "0.3.0") == _card_milestones(card, "0.1.0", "0.3.0")
    assert _card_milestones(card, None, None) == _card_milestones(card, None, None)


def test_card_milestones_raw_tags_is_every_ms_tag_verbatim():
    card = {"tags": ["bug", "milestone:0.3.0", "milestone:0.1.0", "milestone:"]}
    _, tags = _card_milestones(card, None, None)
    assert tags == {"milestone:0.3.0", "milestone:0.1.0", "milestone:"}
    assert _card_milestones({"tags": []}, None, None) == (None, set())
    assert _card_milestones({"tags": ["milestone:"]}, None, None) == (None, {"milestone:"})


def test_card_milestones_none_iff_no_nonempty_suffix():
    assert _card_milestones({"tags": []}, "0.1.0", "0.2.0")[0] is None
    assert _card_milestones({"tags": ["milestone:"]}, "0.1.0", "0.2.0")[0] is None
    assert _card_milestones({"tags": ["milestone:0.9.0"]}, None, None)[0] is not None


def test_card_milestones_prefers_base_anchor_regardless_of_sort_position():
    # base "0.2.0" sorts after "0.1.0" but must still win -- this is the issue #7 bug:
    # a stale extra tag must never override the confirmed-synced base value.
    card = {"tags": ["milestone:0.1.0", "milestone:0.2.0"]}
    assert _card_milestones(card, "0.2.0", "0.2.0")[0] == "0.2.0"


def test_card_milestones_falls_back_to_gh_anchor_when_base_absent():
    card = {"tags": ["milestone:0.1.0", "milestone:9.9"]}
    assert _card_milestones(card, "0.2.0", "9.9")[0] == "9.9"


def test_card_milestones_falls_back_to_sorted_first_when_fully_unanchored():
    card = {"tags": ["milestone:0.3.0", "milestone:0.1.0"]}
    assert _card_milestones(card, "0.2.0", "0.2.0")[0] == "0.1.0"


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


def test_reconcile_value_prefer_ap_for_dates():
    # both sides changed the value since base -> the preferred side wins (AgilePlace for dates)
    assert reconcile_value("2026-01-01", "2026-02-01", "2026-03-01", prefer="ap") == "2026-03-01"
    assert reconcile_value("2026-01-01", "2026-02-01", "2026-03-01", prefer="gh") == "2026-02-01"
    # only one side changed -> that side wins regardless of prefer
    assert reconcile_value("2026-01-01", "2026-02-01", "2026-01-01", prefer="ap") == "2026-02-01"
    assert reconcile_value("2026-01-01", "2026-01-01", "2026-03-01", prefer="ap") == "2026-03-01"


# --- lane resolution on the user's real board -----------------------------

def test_inference_resolves_distinct_titles():
    L = _board_lanes()
    assert resolve_lane_for_stage(L, "Ready", "")[0]["id"] == "rs"
    assert resolve_lane_for_stage(L, "In progress", "")[0]["id"] == "dn"
    assert resolve_lane_for_stage(L, "In review", "")[0]["id"] == "ur"
    assert resolve_lane_for_stage(L, "Done", "")[0]["id"] == "rf"


def test_inference_backlog_ambiguous_fails_closed():
    # 3 not-started leaves, none titled "Backlog", and the matching "Not Started..." lane is a parent
    # container (excluded) -> no move rather than a wrong guess.
    lane, acceptable = resolve_lane_for_stage(_board_lanes(), "Backlog", "")
    assert lane is None and acceptable == set()


def test_stage_lane_map_multi_lane_backlog():
    smap = {"Backlog": ["New Requests", "Approved"]}
    lane, acceptable = resolve_lane_for_stage(_board_lanes(), "Backlog", "", smap)
    assert lane["id"] == "nr"              # first listed = move target
    assert acceptable == {"nr", "ap"}      # a card already in Approved is left alone


def test_stage_lane_map_unknown_lane_falls_back_to_inference():
    smap = {"Ready": ["Nonexistent Lane"]}
    lane, _ = resolve_lane_for_stage(_board_lanes(), "Ready", "", smap)
    assert lane["id"] == "rs"


# --- Projects v2 (Phase 1: Status source) --------------------------------

def test_normalize_status():
    assert normalize_status("In Progress") == "In progress"
    assert normalize_status("done") == "Done"
    assert normalize_status("  Ready ") == "Ready"
    assert normalize_status("Icebox") is None
    assert normalize_status("") is None


def test_parse_items_maps_by_url_and_skips_urlless():
    items = [
        {"id": "PVTI_1",
         "content": {"type": "Issue", "number": 5, "url": "https://github.com/o/r/issues/5"},
         "status": "In progress", "Start": "2026-01-02", "Target": "2026-01-09"},
        {"id": "PVTI_2", "content": {"type": "DraftIssue", "title": "no url"}},  # skipped
    ]
    parsed = parse_items(items, "Status", "Start", "Target")
    assert set(parsed) == {"https://github.com/o/r/issues/5"}
    row = parsed["https://github.com/o/r/issues/5"]
    assert row == {"item_id": "PVTI_1", "number": 5, "status": "In progress",
                   "start": "2026-01-02", "target": "2026-01-09"}


# --- Model 2 per-issue helpers -------------------------------------------

def test_issue_card_title_strips_key_prefix():
    assert issue_card_title({"title": "[EP-0C] API conventions"}) == "API conventions"
    assert issue_card_title({"title": "[0C2] versioning middleware"}) == "versioning middleware"
    assert issue_card_title({"title": "no key here"}) == "no key here"


def test_resolve_issue_stage_prefers_project_status_then_labels():
    issue = {"url": "u1", "state": "OPEN", "labels": ["agent:in-progress"]}
    assert resolve_issue_stage(issue, {"u1": "In review"}) == "In review"   # Project Status wins
    assert resolve_issue_stage(issue, {}) == "In progress"                   # fallback: label
    assert resolve_issue_stage({"url": "u2", "state": "OPEN", "labels": []}, {}) == "Backlog"


def test_blocked_reason():
    stages = {10: "Done", 11: "In progress", 12: "Backlog"}
    assert blocked_reason([], stages) is None
    assert blocked_reason([10], stages) is None                       # blocker Done -> unblocked
    assert blocked_reason([10, 11], stages) == "Blocked by #11"
    assert blocked_reason([12, 11], stages) == "Blocked by #11, #12"  # incomplete, sorted
