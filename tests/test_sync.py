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
from sync import MS_PREFIX, _card_milestones, _stale_milestone_tags, issue_card_title, resolve_issue_stage  # noqa: E402


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
    candidate, tags = _card_milestones(card, "0.2.0", "0.2.0")
    assert candidate == "0.2.0"
    # raw_tags is the full verbatim tag set regardless of which candidate was selected --
    # the anchor rule must never truncate it down to just the winning tag.
    assert tags == {"milestone:0.1.0", "milestone:0.2.0"}


def test_card_milestones_falls_back_to_gh_anchor_when_base_absent():
    card = {"tags": ["milestone:0.1.0", "milestone:9.9"]}
    candidate, tags = _card_milestones(card, "0.2.0", "9.9")
    assert candidate == "9.9"
    assert tags == {"milestone:0.1.0", "milestone:9.9"}


def test_card_milestones_falls_back_to_sorted_first_when_fully_unanchored():
    card = {"tags": ["milestone:0.3.0", "milestone:0.1.0"]}
    assert _card_milestones(card, "0.2.0", "0.2.0")[0] == "0.1.0"


def test_card_milestones_single_tag_passthrough():
    # exactly one non-empty suffix, no anchor matches it -> it is still the candidate.
    card = {"tags": ["milestone:1.2.3"]}
    assert _card_milestones(card, None, None)[0] == "1.2.3"


def test_card_milestones_base_wins_over_unrelated_other_tag():
    # base present among suffixes, gh is a third value absent from the card -> base wins,
    # even though "other" sorts before base.
    card = {"tags": ["milestone:0.9.0", "milestone:0.1.0"]}
    candidate, tags = _card_milestones(card, "0.9.0", "5.0.0")
    assert candidate == "0.9.0"
    assert tags == {"milestone:0.9.0", "milestone:0.1.0"}


def test_card_milestones_gh_wins_over_unrelated_other_tag():
    # base is absent from the card entirely -> falls through to gh, which is present.
    card = {"tags": ["milestone:9.9", "milestone:0.1.0"]}
    candidate, tags = _card_milestones(card, None, "9.9")
    assert candidate == "9.9"
    assert tags == {"milestone:9.9", "milestone:0.1.0"}


def test_card_milestones_verified_repro_base_equals_gh_wins_over_stale_leftover():
    # issue #7's worked example: base and gh agree at "0.2.0"; a stale "0.1.0" leftover
    # tag must not out-sort the anchored value.
    card = {"tags": ["milestone:0.2.0", "milestone:0.1.0"]}
    candidate, tags = _card_milestones(card, "0.2.0", "0.2.0")
    assert candidate == "0.2.0"
    assert tags == {"milestone:0.2.0", "milestone:0.1.0"}


def test_card_milestones_fully_unanchored_upgrade_is_selected():
    # issue #7's other worked example: base and gh agree at "0.2.0", but the card carries
    # only a genuinely new, unanchored "9.9" tag -- that upgrade must be picked up, not
    # discarded in favor of the (absent) anchor.
    card = {"tags": ["milestone:9.9"]}
    assert _card_milestones(card, "0.2.0", "0.2.0")[0] == "9.9"


def test_card_milestones_empty_suffix_never_selected():
    # an empty-suffix "milestone:" tag must never be the candidate, even when it coexists
    # with unanchored non-empty suffixes.
    card = {"tags": ["milestone:", "milestone:0.2.0", "milestone:0.1.0"]}
    candidate, _ = _card_milestones(card, None, None)
    assert candidate != ""
    assert candidate == "0.1.0"


# --- _stale_milestone_tags: staleness is never fabricated ------------------

def test_stale_milestone_tags_never_exceeds_ms_tags():
    # spike-found gap: old_base != new_ms alone must NOT be enough to propose a removal --
    # the old-base tag must actually be a member of ms_tags. Here the card carries only an
    # unanchored "9.9" tag; base="0.2.0" was never re-tagged onto this card at all. old_base
    # ("0.2.0") != new_ms ("9.9") so the card HAS genuinely moved on -- the supersession
    # condition is true -- which is what actually forces the membership check
    # (`old_tag in ms_tags`) to be reached and do its job.
    ms_tags = {f"{MS_PREFIX}9.9"}
    stale = _stale_milestone_tags(ms_tags, "0.2.0", "9.9")
    assert stale <= ms_tags
    assert stale == frozenset()


def test_stale_milestone_tags_subset_invariant_holds_generally():
    # broader fuzz-by-hand over several old_base/new_ms combinations: never propose removing
    # something that was never on the card.
    ms_tags = {f"{MS_PREFIX}0.1.0", f"{MS_PREFIX}"}
    for old_base, new_ms in [("0.2.0", "0.2.0"), ("0.1.0", "0.1.0"), (None, None), ("9.9", "0.1.0")]:
        assert _stale_milestone_tags(ms_tags, old_base, new_ms) <= ms_tags


def test_stale_milestone_tags_always_includes_every_empty_suffix_tag():
    ms_tags = {f"{MS_PREFIX}"}
    assert _stale_milestone_tags(ms_tags, None, None) == frozenset({f"{MS_PREFIX}"})
    assert _stale_milestone_tags(ms_tags, "0.2.0", "0.2.0") == frozenset({f"{MS_PREFIX}"})


def test_stale_milestone_tags_includes_old_base_only_when_superseded_and_present():
    old_tag = f"{MS_PREFIX}0.2.0"
    # superseded AND present -> stale
    assert _stale_milestone_tags({old_tag}, "0.2.0", "9.9") == frozenset({old_tag})
    # superseded but NOT present on the card -> never fabricated (the spike-found gap)
    assert _stale_milestone_tags({f"{MS_PREFIX}9.9"}, "0.2.0", "9.9") == frozenset()
    # present but NOT superseded (old_base == new_ms) -> not stale
    assert _stale_milestone_tags({old_tag}, "0.2.0", "0.2.0") == frozenset()
    # old_base is None -> never stale via this path
    assert _stale_milestone_tags({old_tag}, None, "9.9") == frozenset()


def test_stale_milestone_tags_includes_empty_suffix_tag_when_actually_superseded():
    # The "every empty-suffix tag is always stale" guarantee must hold with EQUALITY (not just
    # subset) in the actually-superseded branch too, not only in the old_base-is-None/unchanged
    # branches -- a regressed implementation that special-cases the empty-suffix scan away
    # whenever a real supersession is detected must fail this.
    old_tag = f"{MS_PREFIX}0.2.0"
    ms_tags = {old_tag, f"{MS_PREFIX}9.9", MS_PREFIX}
    stale = _stale_milestone_tags(ms_tags, "0.2.0", "9.9")  # old_base != new_ms, old_tag present
    assert stale == frozenset({old_tag, MS_PREFIX})


def test_stale_milestone_tags_preserves_unanchored_other_tags():
    # issue #7's leftover-vs-upgrade ambiguity: a tag matching neither the old base nor the
    # new value is preserved, never destroyed, in the same pass it first appears.
    other = f"{MS_PREFIX}0.1.0"
    ms_tags = {f"{MS_PREFIX}0.2.0", other}
    stale = _stale_milestone_tags(ms_tags, "0.2.0", "0.2.0")  # nothing superseded this pass
    assert other not in stale
    assert stale == frozenset()


def test_stale_milestone_tags_preserves_unanchored_other_tag_during_actual_supersession():
    # Same guarantee as above, but pinned in the one scenario the invariant text calls out by
    # name: an ACTIVE transition (old_base present in ms_tags and genuinely != new_ms). A third,
    # unrelated non-empty-suffix tag must survive untouched alongside the genuinely-removed old
    # tag -- a regressed implementation that marks "everything but the new value" stale once a
    # supersession is detected must fail this.
    old_tag = f"{MS_PREFIX}0.2.0"
    other = f"{MS_PREFIX}5.5"
    ms_tags = {old_tag, f"{MS_PREFIX}9.9", other}
    stale = _stale_milestone_tags(ms_tags, "0.2.0", "9.9")
    assert other not in stale
    assert stale == frozenset({old_tag})


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
