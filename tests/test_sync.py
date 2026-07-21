"""Unit tests for the pure sync logic: stage derivation, epic rollup, lane matching, 3-way reconcile.

These need no network or gh -- they pin the invariants the live sync depends on. Run: pytest -q
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agileplace import resolve_lane_for_stage, stage_for_lane  # noqa: E402
from ghproject import parse_items  # noqa: E402
from reconcile import reconcile, reconcile_value  # noqa: E402
from stages import (STAGES, epic_key_for_task, issue_stage,  # noqa: E402
                    lane_matches_stage, normalize_status, title_key)
from sync import (MS_PREFIX, _card_milestones, _child_connection_changes,  # noqa: E402
                  _epic_task_resolution, _protect_open_pr_stage, _reconciled_custom_id_index,
                  _stale_milestone_tags, epic_task_numbers, explicit_stage_status, issue_card_title,
                  resolve_issue_stage)


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


def test_stale_milestone_tags_removes_every_tag_when_milestone_unset():
    # new_ms is None -> reconcile resolved the milestone to UNSET this pass (GitHub cleared it, or it
    # was never set). EVERY milestone: tag is stale then, including an otherwise-"ambiguous" leftover
    # that the standing-milestone branch would preserve: with no current milestone there is nothing
    # legitimate for any tag to represent, and preserving one lets it resurrect the cleared value on a
    # later pass (Codex-flagged cross-run deletion resurrection). The preservation tradeoff applies
    # ONLY while a real milestone still stands.
    ms_tags = {f"{MS_PREFIX}0.2.0", f"{MS_PREFIX}0.1.0", MS_PREFIX}
    assert _stale_milestone_tags(ms_tags, "0.2.0", None) == frozenset(ms_tags)
    # subset invariant still holds (equality is a valid subset), and the None-old_base clear also wipes
    assert _stale_milestone_tags({f"{MS_PREFIX}0.1.0"}, None, None) == frozenset({f"{MS_PREFIX}0.1.0"})


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


# --- card identity --------------------------------------------------------

@pytest.mark.parametrize("reverse", [False, True])
def test_custom_id_disagreement_fails_independent_of_issue_order(reverse):
    issue_one = {"number": 1, "title": "[X] first", "url": "https://example.test/issues/1"}
    issue_two = {"number": 2, "title": "[A] second", "url": "https://example.test/issues/2"}
    card_one = {"id": "C1", "customId": "A"}
    card_two = {"id": "C2", "customId": "B"}
    issues = [issue_two, issue_one] if reverse else [issue_one, issue_two]

    with pytest.raises(SystemExit, match=r"by URL but card C1 by customId 'A'"):
        _reconciled_custom_id_index(
            issues,
            {issue_one["url"]: card_one, issue_two["url"]: card_two},
            {"A": card_one, "B": card_two},
        )


@pytest.mark.parametrize("reverse", [False, True])
def test_duplicate_desired_custom_ids_fail_independent_of_issue_order(reverse):
    issue_one = {"number": 1, "title": "[X] first", "url": "https://example.test/issues/1"}
    issue_two = {"number": 2, "title": "[X] second", "url": "https://example.test/issues/2"}
    card_one = {"id": "C1", "customId": "A"}
    card_two = {"id": "C2", "customId": "B"}
    issues = [issue_two, issue_one] if reverse else [issue_one, issue_two]

    with pytest.raises(SystemExit, match=r"by URL but card C[12] by customId 'X'"):
        _reconciled_custom_id_index(
            issues,
            {issue_one["url"]: card_one, issue_two["url"]: card_two},
            {"A": card_one, "B": card_two},
        )


# --- title-key convention (sub-issue fallback) ---------------------------

def test_sub_issue_fallback_never_yields_removals():
    epic = {"number": 1, "title": "[EP-0C] API epic"}
    task = {"number": 2, "title": "[0C2] Add endpoint"}
    with patch("sync.ghkit.sub_issue_numbers", return_value=None):
        numbers, authoritative = _epic_task_resolution(
            {}, epic, {"EP-0C": epic, "0C2": task})

    adds, removes = _child_connection_changes(
        desired={f"child-{number}" for number in numbers},
        existing={"stale-child"},
        managed={"stale-child"},
        authoritative=authoritative,
    )

    assert adds == ["child-2"]
    assert removes == []


def test_authoritative_empty_sub_issue_read_can_remove_managed_children():
    adds, removes = _child_connection_changes(
        desired=set(),
        existing={"managed-child", "foreign-child"},
        managed={"managed-child"},
        authoritative=True,
    )

    assert adds == []
    assert removes == ["managed-child"]


def test_unkeyed_epic_fallback_matches_nothing_and_warns(capsys):
    epic = {"number": 1, "title": "Unkeyed epic"}
    by_key = {
        "1": epic,
        "2": {"number": 2, "title": "Unkeyed task"},
        "3": {"number": 3, "title": "Another unkeyed issue"},
    }

    with patch("sync.ghkit.sub_issue_numbers", return_value=None):
        numbers = epic_task_numbers({}, epic, by_key)

    assert numbers == []
    assert "has no [KEY] prefix -- fallback matches nothing" in capsys.readouterr().out


def test_native_empty_sub_issue_read_is_authoritative():
    epic = {"number": 1, "title": "[EP-0C] API epic"}

    with patch("sync.ghkit.sub_issue_numbers", return_value=[]):
        numbers, authoritative = _epic_task_resolution({}, epic, {})

    assert numbers == []
    assert authoritative is True

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


def test_lane_resolution_skips_idless_lane_and_warns_with_lane_title(capsys):
    lanes = [
        {"title": "Broken Ready Lane", "cardStatus": "Not Started"},
        {"id": "ready", "title": "Ready", "cardStatus": "Not Started"},
    ]

    lane, acceptable = resolve_lane_for_stage(lanes, "Ready", "")

    assert lane == lanes[1]
    assert acceptable == {"ready"}
    warnings = [line for line in capsys.readouterr().out.splitlines() if line.startswith("WARN")]
    assert len(warnings) == 1
    assert "Broken Ready Lane" in warnings[0]


def test_inference_backlog_ambiguous_fails_closed():
    # 3 not-started leaves, none titled "Backlog", and the matching "Not Started..." lane is a parent
    # container (excluded) -> no move rather than a wrong guess.
    lane, acceptable = resolve_lane_for_stage(_board_lanes(), "Backlog", "")
    assert lane is None and acceptable == set()


def test_card_status_fallback_vetoes_lane_titled_for_a_different_stage():
    lanes = [{"id": "review", "title": "Under Review", "cardStatus": "started"}]

    lane, acceptable = resolve_lane_for_stage(lanes, "In progress", "")

    assert lane is None
    assert acceptable == set()


def test_stage_lane_map_multi_lane_backlog():
    smap = {"Backlog": ["New Requests", "Approved"]}
    lane, acceptable = resolve_lane_for_stage(_board_lanes(), "Backlog", "", smap)
    assert lane["id"] == "nr"              # first listed = move target
    assert acceptable == {"nr", "ap"}      # a card already in Approved is left alone


def test_release_disambiguation_does_not_match_release_as_substring():
    lanes = [
        {"id": "release-11", "title": "Release 11.0"},
        {"id": "doing-11", "title": "Doing", "cardStatus": "started",
         "parentLaneId": "release-11"},
        {"id": "release-2", "title": "Release 2.0"},
        {"id": "doing-2", "title": "Doing", "cardStatus": "started",
         "parentLaneId": "release-2"},
    ]

    lane, acceptable = resolve_lane_for_stage(lanes, "In progress", "1.0")

    assert lane is None
    assert acceptable == set()


def test_stage_lane_map_disambiguates_duplicate_titles_by_release_or_fails_closed():
    lanes = [
        {"id": "release-2", "title": "Release 2.0"},
        {"id": "queue-2", "title": "Queue", "cardStatus": "notStarted",
         "parentLaneId": "release-2"},
        {"id": "release-1", "title": "Release 1.0"},
        {"id": "queue-1", "title": "Queue", "cardStatus": "notStarted",
         "parentLaneId": "release-1"},
    ]
    smap = {"Ready": ["Queue"]}

    lane, acceptable = resolve_lane_for_stage(lanes, "Ready", "1.0", smap)
    assert lane["id"] == "queue-1"
    assert acceptable == {"queue-1"}

    lane, acceptable = resolve_lane_for_stage(lanes, "Ready", "", smap)
    assert lane is None
    assert acceptable == set()


def test_stage_lane_map_unknown_lane_falls_back_to_inference():
    smap = {"Ready": ["Nonexistent Lane"]}
    lane, _ = resolve_lane_for_stage(_board_lanes(), "Ready", "", smap)
    assert lane["id"] == "rs"


def test_stage_lane_map_unknown_lane_warns_by_default_but_not_when_quiet(capsys):
    smap = {"Ready": ["Nonexistent Lane"]}
    resolve_lane_for_stage(_board_lanes(), "Ready", "", smap)
    assert "WARN  STAGE_LANE_MAP" in capsys.readouterr().out       # default: decisive calls still warn

    resolve_lane_for_stage(_board_lanes(), "Ready", "", smap, quiet=True)
    assert "WARN" not in capsys.readouterr().out                  # quiet: internal checks stay silent


# --- "Intake" stage-model addition is inert for unrelated stages ---------
#
# resolve_lane_for_stage's inference fallback walks *every* member of STAGES to veto
# lane-title collisions (`for other in STAGES: lane_matches_stage(lane_title(lane), other)`).
# The moment "Intake" becomes a STAGES member, that walk indexes STAGE_TITLE_HINTS["Intake"]
# and (indirectly, via STAGE_CARD_STATUS) STAGE_CARD_STATUS["Intake"] for every unrelated-stage
# resolution too -- so both dicts must carry inert no-op entries for "Intake" from the moment
# it joins STAGES, or any other stage's inference KeyErrors. These tests pin resolve_lane_for_
# stage's output as byte-identical with vs. without "Intake" present in STAGES, on both the
# unmapped/inferred path (which walks STAGES) and the STAGE_LANE_MAP-mapped path (which doesn't).

_STAGES_WITHOUT_INTAKE = tuple(s for s in STAGES if s != "Intake")
_STAGES_WITH_INTAKE = ("Intake",) + _STAGES_WITHOUT_INTAKE


def test_intake_membership_is_inert_for_unmapped_stage_resolution():
    veto_lanes = [{"id": "review", "title": "Under Review", "cardStatus": "started"}]

    with patch("agileplace.STAGES", _STAGES_WITHOUT_INTAKE):
        veto_without_intake = resolve_lane_for_stage(veto_lanes, "In progress", "")
    with patch("agileplace.STAGES", _STAGES_WITH_INTAKE):
        veto_with_intake = resolve_lane_for_stage(veto_lanes, "In progress", "")
    assert veto_with_intake == veto_without_intake

    with patch("agileplace.STAGES", _STAGES_WITHOUT_INTAKE):
        backlog_without_intake = resolve_lane_for_stage(_board_lanes(), "Backlog", "")
    with patch("agileplace.STAGES", _STAGES_WITH_INTAKE):
        backlog_with_intake = resolve_lane_for_stage(_board_lanes(), "Backlog", "")
    assert backlog_with_intake == backlog_without_intake


def test_intake_membership_is_inert_for_mapped_stage_resolution():
    smap = {"Ready": ["New Requests", "Approved"]}

    with patch("agileplace.STAGES", _STAGES_WITHOUT_INTAKE):
        without_intake = resolve_lane_for_stage(_board_lanes(), "Ready", "", smap)
    with patch("agileplace.STAGES", _STAGES_WITH_INTAKE):
        with_intake = resolve_lane_for_stage(_board_lanes(), "Ready", "", smap)
    assert with_intake == without_intake


# --- stage_for_lane: reverse lane -> stage lookup (Task 2/8, issue #63) --
#
# stage_for_lane is the inverse of resolve_lane_for_stage's STAGE_LANE_MAP branch: given a card's
# current lane, which stage (if any) claims that lane's title. Used by the Intake vetting latch to
# tell "card already sitting in the Intake lane" apart from "card sitting somewhere else" without
# ever guessing on ambiguity.

def test_stage_for_lane_resolves_int_typed_lane_id():
    # AgilePlace lane ids can arrive int-typed from the API while every call site passes lane_id as
    # str (existing sync.py convention: str(card.get("laneId") or ...)). A naive {l["id"]: l}
    # lookup would miss an int-typed lane id and mis-report a genuinely mapped lane as "unmapped" --
    # this pins the str-coercion fix on both sides of the comparison.
    lanes = [{"id": 42, "title": "New Requests"}]
    smap = {"Intake": ["New Requests"]}
    assert stage_for_lane("42", smap, lanes) == "Intake"


def test_stage_for_lane_unknown_lane_id_is_none():
    lanes = [{"id": "nr", "title": "New Requests"}]
    smap = {"Intake": ["New Requests"]}
    assert stage_for_lane("nonexistent", smap, lanes) is None


def test_stage_for_lane_falsy_stage_map_is_none():
    lanes = [{"id": "nr", "title": "New Requests"}]
    assert stage_for_lane("nr", None, lanes) is None
    assert stage_for_lane("nr", {}, lanes) is None


def test_stage_for_lane_zero_matches_is_none():
    lanes = [{"id": "nr", "title": "New Requests"}]
    smap = {"Backlog": ["Some Other Lane"]}
    assert stage_for_lane("nr", smap, lanes) is None


def test_stage_for_lane_two_or_more_matches_is_none():
    # The same lane title configured under two different stages is ambiguous -- spec collapses this
    # to the same WARN+skip outcome as a zero-match miss, not a reopened/raised error.
    lanes = [{"id": "nr", "title": "New Requests"}]
    smap = {"Intake": ["New Requests"], "Backlog": ["New Requests"]}
    assert stage_for_lane("nr", smap, lanes) is None


def test_stage_for_lane_case_insensitive_exact_title_match():
    lanes = [{"id": "nr", "title": "NEW REQUESTS"}]
    smap = {"Intake": ["new requests"]}
    assert stage_for_lane("nr", smap, lanes) == "Intake"


def test_stage_for_lane_does_not_substring_match():
    # Matches _mapped_lanes's exact-title semantics, not lane_matches_stage's substring semantics.
    lanes = [{"id": "nr", "title": "New Requests For Review"}]
    smap = {"Intake": ["New Requests"]}
    assert stage_for_lane("nr", smap, lanes) is None


def test_stage_for_lane_never_raises_on_malformed_lane():
    lanes = [{"title": "No Id Lane"}, "not-a-dict", {"id": "nr", "title": "New Requests"}]
    smap = {"Intake": ["New Requests"]}
    assert stage_for_lane("nr", smap, lanes) == "Intake"


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


def test_parse_items_skips_pull_request_content_despite_having_a_url():
    # issue #5 follow-up: gh project item-list populates content.url for linked Pull Requests too,
    # not just Issues -- a PR row must never be counted as an issue-linked item (it would otherwise
    # pollute project_status / zero_status_despite_items and the lane-move mass-move gate).
    items = [
        {"id": "PVTI_1",
         "content": {"type": "Issue", "number": 5, "url": "https://github.com/o/r/issues/5"},
         "status": "In progress"},
        {"id": "PVTI_2",
         "content": {"type": "PullRequest", "number": 9, "url": "https://github.com/o/r/pull/9"},
         "status": "In review"},
    ]
    parsed = parse_items(items, "Status", "Start", "Target")
    assert set(parsed) == {"https://github.com/o/r/issues/5"}


def test_parse_items_treats_missing_content_type_as_an_issue():
    # backward compatibility: some fixtures/gh output paths never populate content.type at all -- an
    # absent type must not be mistaken for a PullRequest/DraftIssue exclusion.
    items = [{"id": "PVTI_1", "content": {"url": "https://github.com/o/r/issues/5"},
             "status": "In progress"}]
    parsed = parse_items(items, "Status", "Start", "Target")
    assert set(parsed) == {"https://github.com/o/r/issues/5"}


# --- Model 2 per-issue helpers -------------------------------------------

def test_issue_card_title_strips_key_prefix():
    assert issue_card_title({"title": "[EP-0C] API conventions"}) == "API conventions"
    assert issue_card_title({"title": "[0C2] versioning middleware"}) == "versioning middleware"
    assert issue_card_title({"title": "no key here"}) == "no key here"


def test_resolve_issue_stage_prefers_project_status_then_labels():
    issue = {"url": "u1", "state": "OPEN", "labels": ["agent:in-progress"]}
    assert resolve_issue_stage(issue, {"u1": "In review"}, {}, None) == "In review"  # Status wins
    assert resolve_issue_stage(issue, {}, {}, None) == "In progress"                 # fallback: label
    assert resolve_issue_stage(
        {"url": "u2", "state": "OPEN", "labels": []}, {}, {}, None) == "Backlog"


def test_resolve_issue_stage_closed_beats_stale_project_status():
    issue = {"url": "u1", "state": "CLOSED", "labels": ["agent:in-progress"]}

    assert resolve_issue_stage(issue, {"u1": "In progress"}, {}, None) == "Done"


def test_resolve_issue_stage_falls_back_to_labels_on_unrecognized_custom_status_option():
    # A custom board Status option name (e.g. "Triage") that doesn't map to one of our five canonical
    # stages must fall back to label/PR derivation exactly like having no Status at all -- it must
    # never be silently treated as an explicit "Backlog"/etc call.
    issue = {"url": "u1", "state": "OPEN", "labels": ["agent:in-progress"]}
    assert resolve_issue_stage(issue, {"u1": "Triage"}, {}, None) == "In progress"


def test_explicit_stage_status_none_when_missing_or_unrecognized():
    issue = {"url": "u1", "state": "OPEN", "labels": []}
    assert explicit_stage_status(issue, {}) is None                    # no Status at all
    assert explicit_stage_status(issue, {"u1": "Triage"}) is None      # truthy but unmapped custom option
    assert explicit_stage_status(issue, {"u1": "in review"}) == "In review"  # case-insensitive canonical


def test_protect_open_pr_stage_passthrough_when_read_succeeded():
    L = _board_lanes()
    # current lane is "ur" (In review), which would otherwise get frozen -- but the read succeeded,
    # so the guard must never engage: the stage argument comes back unchanged.
    assert _protect_open_pr_stage("In progress", "ur", L, "", None,
                                   open_pr_read_failed=False, has_explicit_status=False) == "In progress"


def test_protect_open_pr_stage_passthrough_when_status_explicit():
    L = _board_lanes()
    # read failed and the lane already matches "In review", but a human set Projects v2 Status
    # explicitly this run -- that always wins over the guard.
    assert _protect_open_pr_stage("In progress", "ur", L, "", None,
                                   open_pr_read_failed=True, has_explicit_status=True) == "In progress"


def test_protect_open_pr_stage_noop_when_stage_already_in_review():
    L = _board_lanes()
    assert _protect_open_pr_stage("In review", "ur", L, "", None,
                                   open_pr_read_failed=True, has_explicit_status=False) == "In review"


def test_protect_open_pr_stage_passthrough_when_lane_not_already_in_review():
    L = _board_lanes()
    # current lane "dn" (Doing Now / In progress) is not in the "In review" acceptable set --
    # the guard must never PROMOTE a card into In review, only freeze one already there.
    assert _protect_open_pr_stage("In progress", "dn", L, "", None,
                                   open_pr_read_failed=True, has_explicit_status=False) == "In progress"


def test_protect_open_pr_stage_freezes_in_review_lane_on_read_failure():
    L = _board_lanes()
    assert _protect_open_pr_stage("In progress", "ur", L, "", None,
                                   open_pr_read_failed=True, has_explicit_status=False) == "In review"


def test_protect_open_pr_stage_passthrough_when_issue_closed():
    L = _board_lanes()
    # A CLOSED issue resolves to "Done" from the authoritative state signal (stages.py), NOT from the
    # lost has_open_pr signal -- so the guard must never freeze it back into "In review" and strand a
    # finished card in review during a persistent open-PR read failure.
    assert _protect_open_pr_stage("Done", "ur", L, "", None,
                                   open_pr_read_failed=True, has_explicit_status=False,
                                   issue_closed=True) == "Done"


def test_protect_open_pr_stage_is_pure_no_mutation():
    L = _board_lanes()
    lanes_before = json.loads(json.dumps(L))
    _protect_open_pr_stage("In progress", "ur", L, "", None,
                            open_pr_read_failed=True, has_explicit_status=False)
    assert L == lanes_before


def test_protect_open_pr_stage_prints_no_warn_on_misconfigured_stage_lane_map(capsys):
    # The guard's internal "is the current lane already acceptable for In review" check must be a
    # quiet, side-effect-free evaluation (per its own docstring) -- it must never surface the
    # STAGE_LANE_MAP-inference WARN that resolve_lane_for_stage() prints for real, decisive lane-move
    # calls. Otherwise a single misconfiguration prints one duplicate WARN per issue in a run, since
    # main() invokes this guard once per issue whose stage != "In review" and Status isn't explicit.
    L = _board_lanes()
    bad_map = {"In review": ["Typo Lane Name"]}
    _protect_open_pr_stage("In progress", "ur", L, "", bad_map,
                            open_pr_read_failed=True, has_explicit_status=False)
    out = capsys.readouterr().out
    assert "WARN" not in out


