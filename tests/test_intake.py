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
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agileplace  # noqa: E402
import ghkit  # noqa: E402
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


def test_intake_candidates_boundary_keeps_a_card_with_only_a_foreign_external_link():
    """Same invariant as test_foreign_external_link_does_not_disqualify_candidate, but pinned
    through the public intake_candidates() boundary -- so a regression that bypasses/duplicates
    _is_candidate's link-matching logic inside intake_candidates() itself is still caught."""
    cards = [_card("card-1", external_links=[{"url": "https://jira.example.test/TICKET-1"}])]

    result = intake.intake_candidates(cards, _lanes(), _stage_map(), _issues())

    assert [c["id"] for c in result] == ["card-1"]


def test_intake_candidates_boundary_excludes_a_card_with_a_matching_target_external_link():
    """Same invariant as test_matching_target_external_link_does_disqualify_candidate, but pinned
    through the public intake_candidates() boundary."""
    cards = [_card("card-1", external_links=[{"url": "https://github.com/o/r/issues/1"}])]

    result = intake.intake_candidates(cards, _lanes(), _stage_map(), _issues())

    assert result == []


def test_card_outside_intake_lane_is_never_a_candidate_regardless_of_links():
    card = _card("card-1", lane_id="lane-backlog")

    assert intake._is_candidate(card, {"lane-intake"}, set(), set()) is False


def test_intake_candidates_boundary_excludes_a_card_outside_the_intake_lane():
    """Same invariant as test_card_outside_intake_lane_is_never_a_candidate_regardless_of_links,
    but pinned through the public intake_candidates() boundary by identity of the returned list --
    so a regression that drops lane filtering inside intake_candidates() itself (rather than
    delegating to _is_candidate) would still be caught."""
    cards = [_card("card-1", lane_id="lane-backlog"), _card("card-2")]

    result = intake.intake_candidates(cards, _lanes(), _stage_map(), _issues())

    assert [c["id"] for c in result] == ["card-2"]


def test_card_with_managed_custom_id_is_not_a_candidate():
    card = _card("card-1", custom_id="EP-0C")

    assert intake._is_candidate(card, {"lane-intake"}, set(), {"EP-0C"}) is False


def test_card_with_unmatched_custom_id_stays_a_candidate():
    """Strict existence-based matching: a customId that doesn't match any known issue's
    issue_custom_id must not disqualify -- it is not a format guess."""
    card = _card("card-1", custom_id="stale-value")

    assert intake._is_candidate(card, {"lane-intake"}, set(), {"EP-0C"}) is True


def test_intake_candidates_boundary_excludes_a_card_with_a_managed_custom_id():
    """Same invariant as test_card_with_managed_custom_id_is_not_a_candidate, but pinned through
    the public intake_candidates() boundary -- so a regression that wires the wrong disqualifying
    set (e.g. an empty one, rather than _disqualifying_custom_ids(issues)) into _is_candidate from
    inside intake_candidates() itself would still be caught. _issues()' fixture issues both have a
    blank title, so their issue_custom_id() falls back to their bare issue number -- "1" here
    matches issue #1's fallback customId."""
    cards = [_card("card-1", custom_id="1"), _card("card-2")]

    result = intake.intake_candidates(cards, _lanes(), _stage_map(), _issues())

    assert [c["id"] for c in result] == ["card-2"]


# --- a card lacking a usable id is never a candidate --------------------------
#
# Otherwise promote() builds marker_for_card(None) -> "...card=None -->", card_web_url(cfg, None)
# -> ".../card/None", and _writeback eventually calls agileplace.patch_card with card.get("id")
# == None, which _card_path(None) turns into a live PATCH against "/card/None".

@pytest.mark.parametrize("card_id", [None, ""])
def test_card_lacking_a_usable_id_is_never_a_candidate(card_id):
    card = {"laneId": "lane-intake", "title": "no id here"}
    if card_id is not None or "id" not in card:
        card["id"] = card_id

    assert intake._is_candidate(card, {"lane-intake"}, set(), set()) is False


def test_card_missing_the_id_key_entirely_is_never_a_candidate():
    card = {"laneId": "lane-intake", "title": "no id key at all"}

    assert intake._is_candidate(card, {"lane-intake"}, set(), set()) is False


def test_intake_candidates_excludes_a_card_lacking_a_usable_id():
    """Boundary-level: a card with no id sitting in the Intake lane must never come out of
    intake_candidates(), regardless of the id-having candidate alongside it."""
    cards = [
        {"laneId": "lane-intake", "title": "no id here"},
        _card("card-1"),
    ]

    result = intake.intake_candidates(cards, _lanes(), _stage_map(), _issues())

    assert [c.get("id") for c in result] == ["card-1"]


# --- a card lacking a usable title is never a candidate -----------------------
#
# Otherwise promote() builds create_issue's `--title ''` (a CalledProcessError, uncaught) or, when
# the title key is present but None, a straight-up TypeError from subprocess -- either way an
# uncaught exception that crashes the whole sync run for one blank-titled Intake card.

@pytest.mark.parametrize("title", [None, "", "   "])
def test_card_lacking_a_usable_title_is_never_a_candidate(title):
    card = {"id": "card-1", "laneId": "lane-intake", "title": title}

    assert intake._is_candidate(card, {"lane-intake"}, set(), set()) is False


def test_card_missing_the_title_key_entirely_is_never_a_candidate():
    card = {"id": "card-1", "laneId": "lane-intake"}

    assert intake._is_candidate(card, {"lane-intake"}, set(), set()) is False


def test_card_with_non_string_title_is_never_a_candidate():
    """Defensive against malformed AgilePlace payloads -- a non-string title must never crash the
    check, and must never be treated as usable."""
    card = {"id": "card-1", "laneId": "lane-intake", "title": 42}

    assert intake._is_candidate(card, {"lane-intake"}, set(), set()) is False


def test_intake_candidates_excludes_a_card_lacking_a_usable_title():
    """Boundary-level: a card with a blank title sitting in the Intake lane must never come out of
    intake_candidates(), regardless of the title-having candidate alongside it."""
    cards = [
        {"id": "card-blank", "laneId": "lane-intake", "title": "   "},
        _card("card-1"),
    ]

    result = intake.intake_candidates(cards, _lanes(), _stage_map(), _issues())

    assert [c.get("id") for c in result] == ["card-1"]


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


def test_provenance_line_success_format_is_exact():
    """Pins the full success-path wording, not just substring containment of the name -- a reformat
    that drops "Requested by ... via AgilePlace." while keeping the name would otherwise go
    undetected (the substring-only test above would still pass)."""
    assert intake.provenance_line("Jane Doe") == "Requested by Jane Doe via AgilePlace."


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


def test_intake_lane_ids_stringifies_non_string_lane_ids():
    """resolve_lane_for_stage returns lane ids in whatever type the board gave them (AgilePlace
    board lane ids are commonly ints) -- but _card_lane_id() always returns a str. Without
    stringifying here, the membership check in _is_candidate would never match a non-string lane id
    against _card_lane_id()'s str output."""
    lanes = [{"id": 101, "title": "New Requests"}, {"id": 202, "title": "Approved"}]
    stage_map = {"Intake": ["New Requests"], "Backlog": ["Approved"]}

    assert intake._intake_lane_ids(lanes, stage_map) == {"101"}


def test_intake_candidates_recognizes_cards_in_a_non_string_id_lane():
    """End-to-end (through the public intake_candidates() boundary): a card sitting in an
    Intake-mapped lane whose id is an int must still be selected as a candidate."""
    lanes = [{"id": 101, "title": "New Requests"}, {"id": 202, "title": "Approved"}]
    stage_map = {"Intake": ["New Requests"], "Backlog": ["Approved"]}
    cards = [_card("card-1", lane_id=101)]

    result = intake.intake_candidates(cards, lanes, stage_map, _issues())

    assert [c["id"] for c in result] == ["card-1"]


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


# ================================================================================
# Task 5/8: marker resume, writeback, promote() orchestration
#
# These tests pin the boundary invariants:
#   (1) Marker resume (_find_marked_issue) is idempotent and state-order-independent.
#   (2) Writeback ordering is fixed: the customId is written before the external link, as two
#       separate patch_card calls.
#   (3) Promotion never queues a lane-move op for any candidate card.
#   (4) Dry-run performs zero writes at the low-level transport boundary (ghkit.run /
#       agileplace.mutate) while still reporting an accurate plan.
#   (5) prescan_failed is True iff ghkit.list_issue_bodies() returns None.
#
# Plus the two edge cases the design flags as needing explicit coverage: marker-resume succeeding
# on both an OPEN and a since-CLOSED promoted issue, and writeback's array-shaped-externalLinks
# case (link write skipped + WARN, customId write still proceeds).
# ================================================================================

def _issue_with_body(number, url, body, state="OPEN"):
    return {"number": number, "url": url, "state": state, "body": body}


# --- invariant 1: marker resume is idempotent and order-independent ----------

def test_find_marked_issue_returns_none_when_no_issue_carries_the_marker():
    issues = [_issue_with_body(1, "u1", "no marker here")]
    assert intake._find_marked_issue("card-1", issues) is None


def test_find_marked_issue_finds_the_issue_carrying_the_marker():
    marker = intake.marker_for_card("card-1")
    issues = [
        _issue_with_body(1, "u1", "unrelated body"),
        _issue_with_body(2, "u2", f"provenance\n\n{marker}"),
    ]
    found = intake._find_marked_issue("card-1", issues)
    assert found is not None and found["number"] == 2


def test_find_marked_issue_is_independent_of_list_order():
    marker = intake.marker_for_card("card-9")
    a = _issue_with_body(1, "u1", "unrelated")
    b = _issue_with_body(2, "u2", marker)
    assert intake._find_marked_issue("card-9", [a, b])["number"] == 2
    assert intake._find_marked_issue("card-9", [b, a])["number"] == 2


def test_find_marked_issue_is_idempotent_across_repeated_calls():
    marker = intake.marker_for_card("card-1")
    issues = [_issue_with_body(1, "u1", marker)]
    assert intake._find_marked_issue("card-1", issues) == intake._find_marked_issue("card-1", issues)


def test_find_marked_issue_never_matches_a_different_cards_marker():
    issues = [_issue_with_body(1, "u1", intake.marker_for_card("card-OTHER"))]
    assert intake._find_marked_issue("card-1", issues) is None


@pytest.mark.parametrize("state", ["OPEN", "CLOSED"])
def test_find_marked_issue_resumes_regardless_of_issue_state(state):
    """Resume is a pure marker search over body text -- a human closing the promoted issue (by
    mistake or otherwise) must not hide it from marker-resume; the marker alone decides."""
    marker = intake.marker_for_card("card-1")
    issues = [_issue_with_body(1, "u1", marker, state=state)]

    found = intake._find_marked_issue("card-1", issues)

    assert found is not None and found["state"] == state


# --- title-derived customId collision guard (issue #62 follow-up) -------------
#
# _is_candidate accepts a card by its OWN (possibly blank) customId, but promotion writes back a
# customId DERIVED FROM THE CARD'S TITLE (_writeback_key -> title_key of a [KEY] prefix). If that
# derived key already belongs to a different URL-owned card -- an existing issue's issue_custom_id,
# or another candidate this run -- the writeback creates a customId collision that the next sync's
# _reconciled_custom_id_index fail-closed guard aborts on. promote() must skip such cards -- but
# ONLY when the card has no resumable issue of its own (the guard runs AFTER marker-resume, so a
# card whose "collision" IS its own crashed-run issue is resumed, never stranded).

def test_promote_skips_a_fresh_candidate_whose_title_derived_key_is_claimed(monkeypatch, capsys):
    # Blank own customId (so _is_candidate accepts it) and NO resume marker, but a [EP-9] title whose
    # derived key EP-9 already belongs to an existing issue -- creating it would collide, so skip it.
    existing = [{"number": 3, "title": "[EP-9] Existing", "url": "https://github.com/o/r/issues/3",
                 "state": "OPEN"}]
    monkeypatch.setattr(ghkit, "list_issue_bodies", lambda *a, **k: [])  # no marker anywhere
    monkeypatch.setattr(ghkit, "create_issue", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not create an issue for a colliding, non-resumable candidate")))
    card = {"id": "card-1", "laneId": "lane-intake", "title": "[EP-9] Fresh request"}

    result = intake.promote({}, True, [card], _lanes(), _stage_map(), existing)

    assert result.created == 0 and result.resumed == 0
    assert "WARN" in capsys.readouterr().out


def test_promote_creates_only_the_first_of_two_candidates_sharing_a_title_derived_key(monkeypatch):
    monkeypatch.setattr(ghkit, "list_issue_bodies", lambda *a, **k: [])
    created_titles = []

    def fake_create(cfg, apply, title, body, issue_type=None):
        created_titles.append(title)
        n = 100 + len(created_titles)
        return {"number": n, "url": f"https://github.com/o/r/issues/{n}"}

    monkeypatch.setattr(ghkit, "create_issue", fake_create)
    monkeypatch.setattr(intake, "_writeback", lambda *a, **k: None)
    cards = [
        {"id": "card-1", "laneId": "lane-intake", "title": "[DUP-1] First"},
        {"id": "card-2", "laneId": "lane-intake", "title": "[DUP-1] Second"},
    ]

    result = intake.promote({}, True, cards, _lanes(), _stage_map(), _issues())

    assert result.created == 1
    assert created_titles == ["[DUP-1] First"]


def test_promote_creates_a_candidate_whose_title_derived_key_is_unclaimed(monkeypatch):
    """The guard drops only genuine collisions -- a bracket-titled card whose derived key matches no
    existing issue and no earlier candidate is promoted normally (no over-rejection)."""
    monkeypatch.setattr(ghkit, "list_issue_bodies", lambda *a, **k: [])
    monkeypatch.setattr(ghkit, "create_issue",
                        lambda *a, **k: {"number": 99, "url": "https://github.com/o/r/issues/99"})
    monkeypatch.setattr(intake, "_writeback", lambda *a, **k: None)
    card = {"id": "card-1", "laneId": "lane-intake", "title": "[NEW-1] Fresh"}

    result = intake.promote({}, True, [card], _lanes(), _stage_map(), _issues())

    assert result.created == 1


def test_promote_resumes_rather_than_skips_when_the_collision_is_the_cards_own_marked_issue(
        monkeypatch):
    """The collision guard is marker-aware. After a run crashes between create and writeback, the
    card's own issue is now in the issues snapshot, so its title-derived key EP-9 looks 'claimed'.
    Because that issue carries the card's resume marker, the card must be RESUMED (writeback
    completed), never dropped as a collision and stranded with an orphan issue."""
    card_id = "card-9"
    marker = intake.marker_for_card(card_id)
    existing = [{"number": 40, "title": "[EP-9] Fresh request",
                 "url": "https://github.com/o/r/issues/40", "state": "OPEN"}]
    monkeypatch.setattr(ghkit, "list_issue_bodies", lambda *a, **k: [
        {"number": 40, "url": "https://github.com/o/r/issues/40", "state": "OPEN", "body": marker}])
    monkeypatch.setattr(ghkit, "create_issue", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("resume must not create a new issue")))
    writeback = []
    monkeypatch.setattr(intake, "_writeback",
                        lambda cfg, apply, c, issue: writeback.append((c["id"], issue["number"])))
    card = {"id": card_id, "laneId": "lane-intake", "title": "[EP-9] Fresh request"}

    result = intake.promote({}, True, [card], _lanes(), _stage_map(), existing)

    assert result.resumed == 1
    assert result.created == 0
    assert writeback == [(card_id, 40)]


# --- _writeback_key ------------------------------------------------------------

def test_writeback_key_uses_bracketed_title_prefix_when_present():
    assert intake._writeback_key("[EP-0C] Some card", 42) == "EP-0C"


def test_writeback_key_falls_back_to_issue_number_without_bracket_prefix():
    assert intake._writeback_key("Plain title, no bracket", 42) == "42"


def test_writeback_key_is_sourced_from_the_cards_own_title_not_a_fetch():
    assert intake._writeback_key("[EP-01] A", 5) != intake._writeback_key("[EP-02] B", 5)


# --- invariant 2: writeback ordering is fixed ---------------------------------
#
# customId (the actual sync join key, and the only one of the two writes patch_card retries on a
# 409/428 version conflict -- see API-VALIDATION.md) is written FIRST, external link SECOND. This
# way, whatever partial failure strikes _writeback, the surviving state is either "nothing written"
# (still a full candidate next run, resumed via the marker) or "customId written, link missing"
# (still fully tracked by the ordinary sync's own customId-based matching) -- never the reverse:
# "link written, customId missing", which would disqualify the card from future intake candidacy
# (via the external-link match) while its join key was never actually established, permanently
# stranding it. See test_writeback_customid_failure_never_reaches_the_external_link_write below.

def test_writeback_writes_custom_id_before_link_in_two_separate_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(agileplace, "patch_card",
                         lambda cfg, apply, card, ops, **k: calls.append(ops[0]))
    card = {"id": "card-1", "title": "Card 1"}
    issue = {"number": 7, "url": "https://github.com/o/r/issues/7"}

    intake._writeback({}, False, card, issue)

    assert len(calls) == 2
    assert calls[0]["path"] == "/customId"
    assert calls[1]["path"] == "/externalLink"


def test_writeback_customid_matches_writeback_key(monkeypatch):
    calls = []
    monkeypatch.setattr(agileplace, "patch_card",
                         lambda cfg, apply, card, ops, **k: calls.append(ops[0]))
    card = {"id": "card-1", "title": "[EP-0C] Card"}
    issue = {"number": 7, "url": "https://github.com/o/r/issues/7"}

    intake._writeback({}, False, card, issue)

    assert calls[0]["value"] == "EP-0C"


def test_writeback_customid_failure_never_reaches_the_external_link_write(monkeypatch):
    """Pins the crash-recovery fix directly: if the customId write raises (a version conflict that
    exhausts patch_card's one retry, a network error, anything), the external link write must never
    have been attempted -- so the card is left in its original, fully-unwritten state and remains a
    full candidate (not a link-only, permanently-disqualified one) for the next run's marker-resume
    scan to retry."""
    calls = []

    def fake_patch_card(cfg, apply, card, ops, **k):
        calls.append(ops[0])
        if ops[0]["path"] == "/customId":
            raise RuntimeError("simulated transient failure")

    monkeypatch.setattr(agileplace, "patch_card", fake_patch_card)
    card = {"id": "card-1", "title": "Card 1"}
    issue = {"number": 7, "url": "https://github.com/o/r/issues/7"}

    with pytest.raises(RuntimeError):
        intake._writeback({}, False, card, issue)

    assert [c["path"] for c in calls] == ["/customId"]


def test_writeback_skips_link_write_and_warns_for_array_shaped_external_links(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(agileplace, "patch_card",
                         lambda cfg, apply, card, ops, **k: calls.append(ops[0]))
    card = {"id": "card-1", "title": "Card 1", "externalLinks": []}
    issue = {"number": 7, "url": "https://github.com/o/r/issues/7"}

    intake._writeback({}, False, card, issue)

    assert len(calls) == 1
    assert calls[0]["path"] == "/customId"
    assert "WARN" in capsys.readouterr().out


def test_writeback_skips_link_write_and_warns_for_a_singular_foreign_external_link(monkeypatch,
                                                                                   capsys):
    """A candidate deliberately KEEPS a foreign singular externalLink (per _is_candidate -- only a
    link matching a known target issue URL disqualifies). The intake link write must be skipped: a
    singular `/externalLink` `add` REPLACES an occupied property, so writing it would silently
    destroy that foreign Jira/doc link. The customId writeback still proceeds."""
    calls = []
    monkeypatch.setattr(agileplace, "patch_card",
                         lambda cfg, apply, card, ops, **k: calls.append(ops[0]))
    card = {"id": "card-1", "title": "Card 1",
            "externalLink": {"url": "https://jira.example.test/TICKET-1"}}
    issue = {"number": 7, "url": "https://github.com/o/r/issues/7"}

    intake._writeback({}, False, card, issue)

    assert [c["path"] for c in calls] == ["/customId"]
    assert "WARN" in capsys.readouterr().out


# --- promote(): candidates / prescan_failed / resume / create ----------------

def test_promote_returns_zero_summary_and_makes_no_gh_calls_when_no_candidates(monkeypatch):
    monkeypatch.setattr(ghkit, "list_issue_bodies",
                         lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be called")))

    result = intake.promote({}, False, [], _lanes(), _stage_map(), _issues())

    assert result == intake.IntakeSummary(candidates=0, prescan_failed=False, resumed=0, created=0)


def test_promote_sets_prescan_failed_when_list_issue_bodies_returns_none(monkeypatch):
    monkeypatch.setattr(ghkit, "list_issue_bodies", lambda *a, **k: None)
    cards = [_card("card-1")]

    result = intake.promote({}, False, cards, _lanes(), _stage_map(), _issues())

    assert result.prescan_failed is True
    assert result.candidates == 1
    assert result.resumed == 0
    assert result.created == 0


def test_promote_prescan_failed_is_false_on_a_genuinely_empty_body_snapshot(monkeypatch):
    monkeypatch.setattr(ghkit, "list_issue_bodies", lambda *a, **k: [])
    monkeypatch.setattr(ghkit, "create_issue", lambda *a, **k: None)  # dry-run plan only
    cards = [_card("card-1")]

    result = intake.promote({}, False, cards, _lanes(), _stage_map(), _issues())

    assert result.prescan_failed is False


def test_promote_resumes_a_card_whose_issue_already_carries_the_marker(monkeypatch):
    marker = intake.marker_for_card("card-1")
    monkeypatch.setattr(ghkit, "list_issue_bodies", lambda *a, **k: [
        _issue_with_body(7, "https://github.com/o/r/issues/7", marker),
    ])
    monkeypatch.setattr(ghkit, "create_issue", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("marker-resume must never call create_issue -- a regression that skips "
                       "the marker check before creating would otherwise double-create silently")))
    writeback_calls = []
    monkeypatch.setattr(intake, "_writeback",
                         lambda cfg, apply, card, issue: writeback_calls.append((card["id"], issue)))
    cards = [_card("card-1")]

    result = intake.promote({}, False, cards, _lanes(), _stage_map(), _issues())

    assert result.resumed == 1
    assert result.created == 0
    assert writeback_calls == [("card-1", {
        "number": 7, "url": "https://github.com/o/r/issues/7", "state": "OPEN", "body": marker,
    })]


def test_promote_resumes_a_card_whose_marked_issue_was_since_closed(monkeypatch):
    """A promoted issue closed by a human (or an unrelated process) between runs must still be
    found by marker-resume -- resume is never gated on issue state."""
    marker = intake.marker_for_card("card-1")
    monkeypatch.setattr(ghkit, "list_issue_bodies", lambda *a, **k: [
        _issue_with_body(7, "https://github.com/o/r/issues/7", marker, state="CLOSED"),
    ])
    monkeypatch.setattr(ghkit, "create_issue", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("marker-resume must never call create_issue -- a regression that skips "
                       "the marker check before creating would otherwise double-create silently")))
    writeback_calls = []
    monkeypatch.setattr(intake, "_writeback",
                         lambda cfg, apply, card, issue: writeback_calls.append((card["id"], issue)))
    cards = [_card("card-1")]

    result = intake.promote({}, False, cards, _lanes(), _stage_map(), _issues())

    assert result.resumed == 1
    assert result.created == 0
    assert writeback_calls[0][1]["state"] == "CLOSED"


def test_promote_creates_a_new_issue_when_no_marker_found(monkeypatch):
    monkeypatch.setattr(ghkit, "list_issue_bodies", lambda *a, **k: [])
    created_issue = {"number": 99, "url": "https://github.com/o/r/issues/99"}
    monkeypatch.setattr(ghkit, "create_issue", lambda *a, **k: created_issue)
    writeback_calls = []
    monkeypatch.setattr(intake, "_writeback",
                         lambda cfg, apply, card, issue: writeback_calls.append((card["id"], issue)))
    cards = [_card("card-1")]

    result = intake.promote({}, True, cards, _lanes(), _stage_map(), _issues())

    assert result.created == 1
    assert result.resumed == 0
    assert writeback_calls == [("card-1", created_issue)]


def test_promote_dry_run_create_plan_attempts_no_writeback(monkeypatch, capsys):
    monkeypatch.setattr(ghkit, "list_issue_bodies", lambda *a, **k: [])
    monkeypatch.setattr(ghkit, "create_issue", lambda *a, **k: None)  # dry-run gate
    monkeypatch.setattr(intake, "_writeback",
                         lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not writeback")))
    cards = [_card("card-1")]

    result = intake.promote({}, False, cards, _lanes(), _stage_map(), _issues())

    assert result.created == 0
    assert result.resumed == 0
    assert "DRY" in capsys.readouterr().out


# --- invariant 3: promotion never moves a card's lane -------------------------

def test_promote_never_queues_a_lane_move_op(monkeypatch):
    monkeypatch.setattr(ghkit, "list_issue_bodies", lambda *a, **k: [])
    monkeypatch.setattr(ghkit, "create_issue",
                         lambda *a, **k: {"number": 5, "url": "https://github.com/o/r/issues/5"})
    ops_seen = []
    monkeypatch.setattr(agileplace, "patch_card",
                         lambda cfg, apply, card, ops, **k: ops_seen.extend(ops))
    # apply=True below drives _writeback's second (link) write through _card_for_link_write's real
    # agileplace.get_card refetch -- stubbed here since this test's concern is the /laneId
    # invariant, not the refetch itself (see test_intake_writeback_version_conflict.py).
    monkeypatch.setattr(agileplace, "get_card", lambda cfg, card_id: {"id": card_id, "version": 2})
    cards = [_card("card-1")]

    intake.promote({}, True, cards, _lanes(), _stage_map(), _issues())

    assert all(op.get("path") != "/laneId" for op in ops_seen)


# --- invariant 4: dry-run never reaches the low-level transport boundary -----

def test_promote_dry_run_never_reaches_the_transport_write_boundary(monkeypatch, capsys):
    """A resumed candidate under apply=False must still complete the full plan (marker read,
    writeback attempt) while never letting a real write reach ghkit.run or agileplace.mutate --
    the low-level transport boundary, matching
    test_edit_label_dry_run_still_works_for_safe_labels's own monkeypatch altitude (never the
    higher-level ghkit.create_issue/agileplace.patch_card wrappers, which must still be called and
    self-gate)."""
    marker = intake.marker_for_card("card-1")
    run_calls = []

    def fake_run(cfg, args, **k):
        run_calls.append(args)
        assert args[:2] == ["issue", "list"], "dry-run must never call gh issue create"
        return Mock(stdout=json.dumps([
            {"number": 7, "url": "https://github.com/o/r/issues/7", "state": "OPEN", "body": marker},
        ]))

    mutate_calls = []

    def fake_mutate(cfg, apply, method, path, body=None, headers=None, *, note=""):
        mutate_calls.append((apply, method, path))
        assert apply is False, "dry-run must never call mutate with apply=True"
        print(f"DRY   {method} /io/{path} {note}")
        return {}

    monkeypatch.setattr(ghkit, "run", fake_run)
    monkeypatch.setattr(agileplace, "mutate", fake_mutate)
    cards = [_card("card-1")]

    result = intake.promote({}, False, cards, _lanes(), _stage_map(), _issues())

    assert len(run_calls) == 1
    assert len(mutate_calls) == 2  # link + customId, both dry-run
    assert result.resumed == 1
    assert "DRY" in capsys.readouterr().out


def test_promote_dry_run_create_path_never_reaches_the_transport_write_boundary(monkeypatch, capsys):
    """CREATE-path counterpart of test_promote_dry_run_never_reaches_the_transport_write_boundary
    above: here NO issue carries the candidate's marker, so promote() falls all the way
    through to ghkit.create_issue -- the code path the marker-resume version structurally can't
    reach. Monkeypatches ghkit.run itself (never the higher-level ghkit.create_issue) so a future
    refactor that dispatches `gh issue create` directly via ghkit.run, bypassing create_issue's own
    apply=False gate, would make this test fail: run_calls would grow past the single prescan
    "issue list" read."""
    run_calls = []

    def fake_run(cfg, args, **k):
        run_calls.append(args)
        assert args[:2] == ["issue", "list"], "dry-run must never dispatch gh issue create via run()"
        return Mock(stdout=json.dumps([]))  # no marker for any candidate

    monkeypatch.setattr(ghkit, "run", fake_run)
    cards = [_card("card-1")]

    result = intake.promote({}, False, cards, _lanes(), _stage_map(), _issues())

    assert len(run_calls) == 1
    assert result.candidates == 1
    assert result.created == 0
    assert result.resumed == 0
    assert "DRY" in capsys.readouterr().out


# --- invariant 5: prescan_failed iff list_issue_bodies() returns None --------

@pytest.mark.parametrize(("bodies_result", "expected_prescan_failed"), [(None, True), ([], False)])
def test_prescan_failed_matches_list_issue_bodies_tri_state(monkeypatch, bodies_result,
                                                             expected_prescan_failed):
    monkeypatch.setattr(ghkit, "list_issue_bodies", lambda *a, **k: bodies_result)
    monkeypatch.setattr(ghkit, "create_issue", lambda *a, **k: None)
    cards = [_card("card-1")]

    result = intake.promote({}, False, cards, _lanes(), _stage_map(), _issues())

    assert result.prescan_failed is expected_prescan_failed
