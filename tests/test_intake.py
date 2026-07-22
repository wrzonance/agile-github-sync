"""Unit tests for issue #62 Task 4/8: intake.py candidate selection (marker/lane/AgilePlace helpers
+ pure predicates).

These tests pin three boundary invariants:

  (1) Candidate selection (intake_candidates / _is_candidate) is deterministic and pure -- same
      inputs always produce the same output, in the same order, and neither the cards, lanes, nor
      issues arguments are ever mutated.
  (2) A foreign external link (one that doesn't match any known target-repo issue URL) never
      disqualifies a card by itself -- only a link that actually matches one of `target_urls` does.
  (3) provenance_line() never raises and always returns a non-empty string, for any input --
      a real name, None, blank/whitespace, or a malformed non-string value.

Plus focused coverage of the remaining Task 4 helpers (card_created_by_name, op_external_link,
card_web_url, marker_for_card, _intake_lane_ids, _disqualifying_custom_ids, _issue_body).

Run: pytest -q tests/test_intake.py
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import intake  # noqa: E402


# --- fixtures -----------------------------------------------------------------

def _lanes():
    return [
        {"id": "lane-intake", "title": "New Requests"},
        {"id": "lane-backlog", "title": "Approved"},
    ]


def _stage_map():
    return {"Intake": ["New Requests"], "Backlog": ["Approved"]}


def _card(card_id="card-1", lane_id="lane-intake", external_links=None, custom_id=None):
    card = {"id": card_id, "laneId": lane_id, "title": f"Card {card_id}"}
    if external_links is not None:
        card["externalLinks"] = external_links
    if custom_id is not None:
        card["customId"] = custom_id
    return card


def _issues():
    # Shape matches ghkit.list_issues() (the already-fetched, title-bearing issues list sync.py
    # passes to intake.promote()/intake_candidates() -- distinct from ghkit.list_issue_bodies(),
    # which promote() itself calls separately for the body-scanning marker-resume path).
    return [
        {"number": 1, "title": "", "url": "https://github.com/o/r/issues/1", "state": "OPEN"},
        {"number": 2, "title": "", "url": "https://github.com/o/r/issues/2", "state": "CLOSED"},
    ]


# --- invariant 1: candidate selection is deterministic and pure --------------

def test_intake_candidates_is_deterministic_across_repeated_calls():
    cards = [_card("card-1"), _card("card-2", lane_id="lane-backlog")]
    lanes, stage_map, issues = _lanes(), _stage_map(), _issues()

    first = intake.intake_candidates(cards, lanes, stage_map, issues)
    second = intake.intake_candidates(cards, lanes, stage_map, issues)

    assert first == second
    assert [c["id"] for c in first] == [c["id"] for c in second]


def test_intake_candidates_preserves_input_order():
    cards = [_card("card-3"), _card("card-1"), _card("card-2")]

    result = intake.intake_candidates(cards, _lanes(), _stage_map(), _issues())

    assert [c["id"] for c in result] == ["card-3", "card-1", "card-2"]


def test_intake_candidates_never_mutates_its_arguments():
    cards = [_card("card-1"), _card("card-2", lane_id="lane-backlog")]
    lanes, stage_map, issues = _lanes(), _stage_map(), _issues()
    cards_before = copy.deepcopy(cards)
    lanes_before = copy.deepcopy(lanes)
    stage_map_before = copy.deepcopy(stage_map)
    issues_before = copy.deepcopy(issues)

    intake.intake_candidates(cards, lanes, stage_map, issues)

    assert cards == cards_before
    assert lanes == lanes_before
    assert stage_map == stage_map_before
    assert issues == issues_before


def test_is_candidate_is_a_pure_function_of_its_arguments():
    card = _card("card-1")
    intake_lane_ids = {"lane-intake"}
    target_urls = {"https://github.com/o/r/issues/9"}
    managed_custom_ids = {"EP-0C"}
    card_before = copy.deepcopy(card)

    result_a = intake._is_candidate(card, intake_lane_ids, target_urls, managed_custom_ids)
    result_b = intake._is_candidate(card, intake_lane_ids, target_urls, managed_custom_ids)

    assert result_a is True
    assert result_a == result_b
    assert card == card_before


# --- invariant 2: a foreign external link never disqualifies by itself -------

def test_foreign_external_link_does_not_disqualify_candidate():
    card = _card("card-1", external_links=[{"url": "https://jira.example.test/TICKET-1"}])
    target_urls = {"https://github.com/o/r/issues/1"}

    assert intake._is_candidate(card, {"lane-intake"}, target_urls, set()) is True


def test_matching_target_external_link_does_disqualify_candidate():
    card = _card("card-1", external_links=[{"url": "https://github.com/o/r/issues/1"}])
    target_urls = {"https://github.com/o/r/issues/1"}

    assert intake._is_candidate(card, {"lane-intake"}, target_urls, set()) is False


def test_card_outside_intake_lane_is_never_a_candidate_regardless_of_links():
    card = _card("card-1", lane_id="lane-backlog")

    assert intake._is_candidate(card, {"lane-intake"}, set(), set()) is False


def test_card_with_managed_custom_id_is_not_a_candidate():
    card = _card("card-1", custom_id="EP-0C")

    assert intake._is_candidate(card, {"lane-intake"}, set(), {"EP-0C"}) is False


def test_card_with_unmatched_custom_id_stays_a_candidate():
    """Strict existence-based matching: a customId that doesn't match any known issue's
    issue_custom_id must not disqualify -- it is not a format guess."""
    card = _card("card-1", custom_id="stale-value")

    assert intake._is_candidate(card, {"lane-intake"}, set(), {"EP-0C"}) is True


# --- invariant 3: provenance_line never raises, always non-empty -------------

@pytest.mark.parametrize("name", [
    "Jane Doe", None, "", "   ", 42, [], {"not": "a name"},
])
def test_provenance_line_never_raises_and_is_always_non_empty(name):
    result = intake.provenance_line(name)
    assert isinstance(result, str)
    assert result.strip() != ""


def test_provenance_line_includes_the_given_name():
    assert "Jane Doe" in intake.provenance_line("Jane Doe")


def test_provenance_line_falls_back_when_name_is_missing():
    line = intake.provenance_line(None)
    assert "via AgilePlace" in line
    assert "None" not in line


# --- card_created_by_name -----------------------------------------------------

def test_card_created_by_name_reads_full_name_from_user_object():
    card = {"createdBy": {"fullName": "Jane Doe", "emailAddress": "jane@example.test"}}
    assert intake.card_created_by_name(card) == "Jane Doe"


def test_card_created_by_name_falls_back_to_email_when_full_name_missing():
    card = {"createdBy": {"emailAddress": "jane@example.test"}}
    assert intake.card_created_by_name(card) == "jane@example.test"


@pytest.mark.parametrize("created_by", [None, "bare-id-string", 12345, [], {}])
def test_card_created_by_name_returns_none_for_unusable_shapes(created_by):
    assert intake.card_created_by_name({"createdBy": created_by}) is None


def test_card_created_by_name_returns_none_when_key_absent():
    assert intake.card_created_by_name({}) is None


# --- op_external_link ---------------------------------------------------------

def test_op_external_link_builds_rfc6902_add_op():
    op = intake.op_external_link("GitHub Issue", "https://github.com/o/r/issues/1")
    assert op == {
        "op": "add",
        "path": "/externalLink",
        "value": {"label": "GitHub Issue", "url": "https://github.com/o/r/issues/1"},
    }


# --- card_web_url --------------------------------------------------------------

def test_card_web_url_uses_configured_host():
    url = intake.card_web_url({"host": "example.leankit.com"}, "card-123")
    assert url.startswith("https://example.leankit.com/")
    assert "card-123" in url


def test_card_web_url_handles_missing_host_without_raising():
    url = intake.card_web_url({}, "card-123")
    assert isinstance(url, str)
    assert "card-123" in url


# --- marker_for_card / MARKER_TEMPLATE ----------------------------------------

def test_marker_for_card_embeds_the_card_id():
    assert "card-42" in intake.marker_for_card("card-42")


def test_marker_for_card_coerces_non_string_ids():
    assert "12345" in intake.marker_for_card(12345)


def test_marker_for_card_is_stable_for_the_same_id():
    assert intake.marker_for_card("card-1") == intake.marker_for_card("card-1")


def test_marker_for_card_differs_across_ids():
    assert intake.marker_for_card("card-1") != intake.marker_for_card("card-2")


# --- _intake_lane_ids: guarded before delegating to resolve_lane_for_stage ----

def test_intake_lane_ids_empty_when_stage_map_missing():
    assert intake._intake_lane_ids(_lanes(), None) == set()


def test_intake_lane_ids_empty_when_stage_map_has_no_intake_key():
    assert intake._intake_lane_ids(_lanes(), {"Backlog": ["Approved"]}) == set()


def test_intake_lane_ids_resolves_configured_lane():
    assert intake._intake_lane_ids(_lanes(), _stage_map()) == {"lane-intake"}


# --- _disqualifying_custom_ids -------------------------------------------------

def test_disqualifying_custom_ids_uses_issue_custom_id_over_full_issue_list():
    issues = [
        {"number": 1, "url": "u1", "title": "[EP-0C] Thing"},
        {"number": 2, "url": "u2", "title": "no key here"},
    ]
    assert intake._disqualifying_custom_ids(issues) == {"EP-0C", "2"}


def test_disqualifying_custom_ids_empty_for_no_issues():
    assert intake._disqualifying_custom_ids([]) == set()


# --- _issue_body ---------------------------------------------------------------

def test_issue_body_includes_provenance_web_link_and_marker():
    card = {"id": "card-7", "createdBy": {"fullName": "Jane Doe"}}
    body = intake._issue_body(card, {"host": "example.leankit.com"})

    assert "Jane Doe" in body
    assert "example.leankit.com" in body
    assert "card-7" in body
    assert intake.marker_for_card("card-7") in body


def test_issue_body_never_raises_for_minimal_card():
    body = intake._issue_body({"id": "card-8"}, {})
    assert isinstance(body, str)
    assert body.strip() != ""
