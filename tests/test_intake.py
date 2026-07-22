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


# ================================================================================
# Task 5/8: marker resume, writeback, promote() orchestration
#
# These tests pin the boundary invariants:
#   (1) Marker resume (_find_marked_issue) is idempotent and state-order-independent.
#   (2) Writeback ordering is fixed: the external link is written before the customId, as two
#       separate patch_card calls.
#   (3) Promotion never queues a lane-move op for any candidate card.
#   (4) Dry-run performs zero writes at the low-level transport boundary (ghkit.run /
#       agileplace.mutate) while still reporting an accurate plan.
#   (5) prescan_failed is True iff ghkit.list_issue_bodies() returns None.
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


# --- _writeback_key ------------------------------------------------------------

def test_writeback_key_uses_bracketed_title_prefix_when_present():
    assert intake._writeback_key("[EP-0C] Some card", 42) == "EP-0C"


def test_writeback_key_falls_back_to_issue_number_without_bracket_prefix():
    assert intake._writeback_key("Plain title, no bracket", 42) == "42"


def test_writeback_key_is_sourced_from_the_cards_own_title_not_a_fetch():
    assert intake._writeback_key("[EP-01] A", 5) != intake._writeback_key("[EP-02] B", 5)


# --- invariant 2: writeback ordering is fixed ---------------------------------

def test_writeback_writes_link_before_custom_id_in_two_separate_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(agileplace, "patch_card",
                         lambda cfg, apply, card, ops, **k: calls.append(ops[0]))
    card = {"id": "card-1", "title": "Card 1"}
    issue = {"number": 7, "url": "https://github.com/o/r/issues/7"}

    intake._writeback({}, False, card, issue)

    assert len(calls) == 2
    assert calls[0]["path"] == "/externalLink"
    assert calls[1]["path"] == "/customId"


def test_writeback_customid_matches_writeback_key(monkeypatch):
    calls = []
    monkeypatch.setattr(agileplace, "patch_card",
                         lambda cfg, apply, card, ops, **k: calls.append(ops[0]))
    card = {"id": "card-1", "title": "[EP-0C] Card"}
    issue = {"number": 7, "url": "https://github.com/o/r/issues/7"}

    intake._writeback({}, False, card, issue)

    assert calls[1]["value"] == "EP-0C"


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


# --- invariant 5: prescan_failed iff list_issue_bodies() returns None --------

@pytest.mark.parametrize(("bodies_result", "expected_prescan_failed"), [(None, True), ([], False)])
def test_prescan_failed_matches_list_issue_bodies_tri_state(monkeypatch, bodies_result,
                                                             expected_prescan_failed):
    monkeypatch.setattr(ghkit, "list_issue_bodies", lambda *a, **k: bodies_result)
    monkeypatch.setattr(ghkit, "create_issue", lambda *a, **k: None)
    cards = [_card("card-1")]

    result = intake.promote({}, False, cards, _lanes(), _stage_map(), _issues())

    assert result.prescan_failed is expected_prescan_failed
