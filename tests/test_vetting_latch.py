"""End-to-end wiring tests for the "Intake" vetting latch (issue #63), driven through the real
sync.main() -- same mocked-I/O-boundary approach as test_sync_main.py, whose harness (_mock_io,
_cfg, _card, _issue) this file reuses directly rather than duplicating it.

The invariant under test, stated once: apply_latch() must NEVER let a card be lane-moved out of
wherever a human placed it while this run's stage resolves to "Intake" -- independent of whether
this run's own attempt to vet the issue onto the Project (add_item/set_item_status) succeeds,
partially fails, or is never even attempted. patch_card_mock staying uncalled is the load-bearing
assertion in every test below: it proves no lane-move op was EVER queued for the card, not merely
that the "wrong" lane was avoided.

Run: pytest -q
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import sync  # noqa: E402
from test_sync_main import ISSUE_URL, _UNSET, _card, _cfg, _issue, _mock_io  # noqa: E402

_INTAKE_LANES = [
    {"id": "L_INTAKE", "title": "New Requests", "cardStatus": "notStarted"},
    {"id": "L_READY", "title": "Ready Lane", "cardStatus": "notStarted"},
]
_INTAKE_STAGE_MAP = {"Intake": ["New Requests"], "Ready": ["Ready Lane"]}


def _intake_issue():
    # Bare-else fallback ("Backlog"), no explicit Status, no work signal -- exactly the combination
    # resolve_issue_stage() collapses to "Intake" once the board declares an Intake lane mapping.
    return {**_issue(), "labels": [], "assignees": []}


def _run_intake_case(tmp_path, card, *, add_item_return=_UNSET, set_item_status_return=True):
    """Runs main() once with a stage_map that declares "Intake" and the issue off-board
    (project_items == {}, no explicit Status) -- so resolve_issue_stage() resolves "Intake" and the
    latch fires for `card`. Returns (add_item_mock, set_item_status_mock, patch_card_mock)."""
    parsed: dict = {}  # empty project_items -> issue is not yet a Project member
    cfg = {**_cfg(tmp_path), "stage_lane_map": _INTAKE_STAGE_MAP}
    stack, _, patch_card_mock, _ = _mock_io(
        card, (parsed, []), field_meta_return=None, lanes_return=_INTAKE_LANES,
        issue_return=_intake_issue(), add_item_return=add_item_return,
        set_item_status_return=set_item_status_return)
    state_file = tmp_path / ".sync-state.json"
    with stack, patch("sync.env_config", return_value=cfg), patch("sync.STATE_FILE", state_file), \
         patch("sys.argv", ["sync.py", "--apply"]):
        sync.main()
    return stack.add_item_mock, stack.set_item_status_mock, patch_card_mock


def test_apply_latch_demotion_trap_add_item_failure_never_promotes_or_moves(tmp_path, capsys):
    # Written FIRST per the design loop's own rule -- this is the single most safety-critical case.
    # The card sits in "Ready Lane" (a human already moved it out of Intake); this run's attempt to
    # vet the issue onto the Project fails at the very first step. The demotion trap must still hold:
    # set_item_status is never even attempted, and no lane-move op is ever queued for the card.
    card = {**_card(), "laneId": "L_READY"}
    add_item_mock, set_item_status_mock, patch_card_mock = _run_intake_case(
        tmp_path, card, add_item_return=None)

    out = capsys.readouterr().out
    assert "could not add issue to the Project" in out
    assert add_item_mock.call_args.args[1] is True          # apply
    assert add_item_mock.call_args.args[2] == ISSUE_URL
    set_item_status_mock.assert_not_called()
    patch_card_mock.assert_not_called()                     # card stays in Ready Lane, period


def test_apply_latch_demotion_trap_set_item_status_failure_never_moves(tmp_path, capsys):
    # add_item succeeds but the Status write fails -- a half-finished promotion. The card must still
    # never be lane-moved: apply_latch's True return does not depend on _promote_issue's own outcome.
    card = {**_card(), "laneId": "L_READY"}
    add_item_mock, set_item_status_mock, patch_card_mock = _run_intake_case(
        tmp_path, card, add_item_return="PVTI_NEW", set_item_status_return=False)

    out = capsys.readouterr().out
    assert "could not set Project Status to 'Ready'" in out
    add_item_mock.assert_called_once()
    assert set_item_status_mock.call_args.args[-2:] == ("PVTI_NEW", "Ready")
    patch_card_mock.assert_not_called()


def test_apply_latch_returns_false_when_card_already_parked_in_intake_lane(tmp_path, capsys):
    # The card's current lane maps back to "Intake" itself -- nothing to promote, nothing to demote.
    # apply_latch returns False so the ordinary lane-move runs, finds the card already there, and
    # no-ops harmlessly; add_item/set_item_status must never be reached at all.
    card = {**_card(), "laneId": "L_INTAKE"}
    add_item_mock, set_item_status_mock, patch_card_mock = _run_intake_case(tmp_path, card)

    add_item_mock.assert_not_called()
    set_item_status_mock.assert_not_called()
    patch_card_mock.assert_not_called()


def test_apply_latch_unmapped_current_lane_warns_and_skips_promotion_attempt(tmp_path, capsys):
    # The card's current lane isn't in the board snapshot at all -- stage_for_lane's reverse lookup
    # fails closed to None (this also covers stage_for_lane's own ambiguous-match collapse, unit-
    # tested directly against stage_for_lane itself). apply_latch must hold at Intake without ever
    # attempting a promotion it cannot ground in a known current stage.
    card = {**_card(), "laneId": "L_UNKNOWN"}
    add_item_mock, set_item_status_mock, patch_card_mock = _run_intake_case(tmp_path, card)

    out = capsys.readouterr().out
    assert "doesn't map back to a recognized stage" in out
    add_item_mock.assert_not_called()
    set_item_status_mock.assert_not_called()
    patch_card_mock.assert_not_called()


def test_apply_latch_full_success_promotes_and_prints_summary(tmp_path, capsys):
    # The positive case: promotion succeeds end-to-end. The demotion trap still holds -- a successful
    # vet-onto-the-board is not a lane-move, and none is ever queued for the card.
    card = {**_card(), "laneId": "L_READY"}
    add_item_mock, set_item_status_mock, patch_card_mock = _run_intake_case(
        tmp_path, card, add_item_return="PVTI_NEW", set_item_status_return=True)

    out = capsys.readouterr().out
    assert "latch  [1] vetted -> Status 'Ready'" in out
    add_item_mock.assert_called_once()
    set_item_status_mock.assert_called_once()
    patch_card_mock.assert_not_called()


def test_intake_card_creation_targets_the_intake_lane(tmp_path, capsys):
    # Loop 1 (ensure a card per active issue), not loop 2's lane-move: a brand-new issue with no
    # matching card, off-board, no work signal -- resolve_issue_stage() resolves "Intake" and the
    # new card must be created straight into the lane the board maps to "Intake", proving loop 1's
    # existing resolve_lane_for_stage plumbing needs no issue-#63-specific change to do so. The
    # latch's write surface belongs only to loop 2's existing-card path, so neither mock fires here.
    parsed: dict = {}
    cfg = {**_cfg(tmp_path), "stage_lane_map": _INTAKE_STAGE_MAP}
    stack, _, _, create_card_mock = _mock_io(
        _card(), (parsed, []), field_meta_return=None, lanes_return=_INTAKE_LANES,
        issue_return=_intake_issue(), existing_cards=[])
    state_file = tmp_path / ".sync-state.json"
    with stack, patch("sync.env_config", return_value=cfg), patch("sync.STATE_FILE", state_file), \
         patch("sys.argv", ["sync.py", "--apply"]):
        sync.main()

    out = capsys.readouterr().out
    assert "stage=Intake lane=New Requests" in out
    create_card_mock.assert_called_once()
    assert create_card_mock.call_args.args[-1] == "L_INTAKE"
    stack.add_item_mock.assert_not_called()
    stack.set_item_status_mock.assert_not_called()


def test_apply_latch_never_runs_when_project_read_failed(tmp_path, capsys):
    # An outright Projects v2 read failure leaves project_items == {} -- the same shape as
    # "genuinely never vetted" -- which unguarded would satisfy resolve_issue_stage's own
    # "not in project_items" clause and resolve "Intake". But move_lanes (`not project_read_failed`)
    # gates the entire loop-2 lane-move block, the latch call included, so apply_latch must never
    # even be reached: a transiently miscomputed "Intake" during a read outage can't drive a write.
    card = {**_card(), "laneId": "L_READY"}
    cfg = {**_cfg(tmp_path), "stage_lane_map": _INTAKE_STAGE_MAP}
    stack, _, patch_card_mock, _ = _mock_io(
        card, (None, None), field_meta_return=None, lanes_return=_INTAKE_LANES,
        issue_return=_intake_issue())
    state_file = tmp_path / ".sync-state.json"
    with stack, patch("sync.env_config", return_value=cfg), patch("sync.STATE_FILE", state_file), \
         patch("sys.argv", ["sync.py", "--apply"]):
        sync.main()

    out = capsys.readouterr().out
    assert "Projects v2 read FAILED" in out
    stack.add_item_mock.assert_not_called()
    stack.set_item_status_mock.assert_not_called()
    patch_card_mock.assert_not_called()


def test_flag_off_sync_main_never_touches_latch_write_surface(tmp_path, capsys):
    # Baseline pin at the sync.main() level (task 3/8 already pins resolve_issue_stage() directly
    # in test_sync_intake.py): with no "Intake" key in stage_map -- the default/legacy config -- an
    # off-board, no-work-signal issue's existing card must never reach the latch's write surface,
    # byte-identical to the sync's pre-issue-63 behavior.
    card = {**_card(), "laneId": "L_READY"}
    cfg = _cfg(tmp_path)  # stage_lane_map: {} -- no "Intake" key
    stack, _, patch_card_mock, _ = _mock_io(
        card, ({}, []), field_meta_return=None, lanes_return=_INTAKE_LANES,
        issue_return=_intake_issue())
    state_file = tmp_path / ".sync-state.json"
    with stack, patch("sync.env_config", return_value=cfg), patch("sync.STATE_FILE", state_file), \
         patch("sys.argv", ["sync.py", "--apply"]):
        sync.main()

    out = capsys.readouterr().out
    assert "Intake" not in out
    stack.add_item_mock.assert_not_called()
    stack.set_item_status_mock.assert_not_called()
