"""Call-site wiring for issue #84's board_layout.py split (Task 2/4).

Thin, isolated from the large per-module test suites: this file's only concern is that sync.py,
intake.py, vetting_latch.py, and smoke.py delegate their lane/board-topology work to board_layout.py
(not agileplace.py, which no longer defines those names -- see board_layout.py's own module
docstring for the one-way dependency this enforces) -- and that agileplace.py's own remaining public
API is untouched by the move. It does not re-verify resolve_lane_for_stage/stage_for_lane/lane_title/
board_layout's own behavior -- that is tests/test_board_layout.py's job -- so board_layout's functions
are monkeypatched here, matching this repo's low-level-transport-boundary convention for call-site
tests (test_sync_description_call_site.py patches sync.sync_description the same way; patching the
collaborator, not its internals).

Run: pytest -q tests/test_board_layout_call_sites.py
"""
from __future__ import annotations

import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agileplace  # noqa: E402
import board_layout  # noqa: E402
import intake  # noqa: E402
import smoke  # noqa: E402
import sync  # noqa: E402
import vetting_latch  # noqa: E402

# The 24 names agileplace.py's own design keeps -- api/mutate/reads/op-builders/patch-writes/
# dependency helpers. Board topology (BoardLayout, board_layout, lane_title,
# resolve_lane_for_stage, stage_for_lane, and their private helpers) must NOT be among these.
_AGILEPLACE_PUBLIC_API = {
    "api", "mutate", "list_cards", "get_card", "card_external_urls", "custom_id_value",
    "card_tags", "card_is_blocked", "card_block_reason", "card_child_ids",
    "op_custom_id", "op_lane", "op_tag", "ops_tag_remove", "op_planned_date", "ops_blocked",
    "patch_card", "create_card", "delete_card", "connect_children", "disconnect_children",
    "card_dependencies", "incoming_dependency_ids", "create_dependencies", "delete_dependencies",
}
_MOVED_BOARD_LAYOUT_NAMES = {
    "BoardLayout", "board_layout", "lane_title", "resolve_lane_for_stage", "stage_for_lane",
}


def test_agileplace_still_exposes_its_own_remaining_public_api():
    missing = _AGILEPLACE_PUBLIC_API - set(dir(agileplace))
    assert not missing, f"agileplace.py lost public API members: {missing}"


def test_agileplace_no_longer_exposes_the_moved_board_layout_names():
    still_present = _MOVED_BOARD_LAYOUT_NAMES & set(dir(agileplace))
    assert not still_present, f"agileplace.py still exposes moved names: {still_present}"


def test_protect_open_pr_stage_delegates_to_board_layout_resolve_lane_for_stage():
    with patch("board_layout.resolve_lane_for_stage",
               return_value=(None, {"L1"})) as resolve_mock:
        result = sync._protect_open_pr_stage(
            "Ready", "L1", lanes=[{"id": "L1"}], milestone="1.0", stage_map=None,
            open_pr_read_failed=True, has_explicit_status=False, issue_closed=False)

    resolve_mock.assert_called_once_with([{"id": "L1"}], "In review", "1.0", None, quiet=True)
    assert result == "In review"


def test_apply_lane_move_delegates_to_board_layout_resolve_lane_for_stage_and_lane_title():
    target_lane = {"id": "L2", "title": "Ready"}
    queued = []

    with patch("board_layout.resolve_lane_for_stage",
               return_value=(target_lane, {"L1"})) as resolve_mock, \
         patch("board_layout.lane_title", return_value="Ready") as lane_title_mock:
        sync._apply_lane_move(
            cfg={}, apply=True,
            issue={"milestone": None, "state": "OPEN", "url": "https://example.test/1"},
            card={"id": "C1"},
            key="K1", stage="Ready", current="L0", lanes=[{"id": "L1"}], stage_map=None,
            project_status={},
            queue=lambda card, ops, note: queued.append((card, ops, note)),
            open_pr_read_failed=False)

    resolve_mock.assert_called_once()
    lane_title_mock.assert_called_with(target_lane)
    assert queued and queued[0][2] == "lane->Ready"


def test_retire_card_delegates_to_board_layout_resolve_lane_for_stage_and_lane_title():
    target_lane = {"id": "L3", "title": "Done"}
    queued = []

    with patch("board_layout.resolve_lane_for_stage",
               return_value=(target_lane, set())) as resolve_mock, \
         patch("board_layout.lane_title", return_value="Done") as lane_title_mock:
        sync._retire_card(
            issue={"state_reason": "COMPLETED", "milestone": None, "title": "widget", "number": 1},
            card={"id": "C1", "laneId": "L1"},
            lanes=[{"id": "L1"}], stage_map=None, apply=True,
            queue=lambda card, ops, note: queued.append((card, ops, note)))

    resolve_mock.assert_called_once()
    lane_title_mock.assert_called_with(target_lane)
    assert queued and queued[0][2] == "retire:COMPLETED"


def test_intake_lane_ids_delegates_to_board_layout_resolve_lane_for_stage():
    stage_map = {"Intake": ["Intake Lane"]}
    with patch("board_layout.resolve_lane_for_stage",
               return_value=(None, {"L9"})) as resolve_mock:
        result = intake._intake_lane_ids(lanes=[{"id": "L9"}], stage_map=stage_map)

    resolve_mock.assert_called_once_with([{"id": "L9"}], "Intake", "", stage_map, quiet=True)
    assert result == {"L9"}


def test_apply_latch_delegates_to_board_layout_stage_for_lane():
    with patch("board_layout.stage_for_lane", return_value=None) as stage_for_lane_mock:
        result = vetting_latch.apply_latch(
            cfg={}, apply=True, issue={"url": "https://example.test/1"}, key="K1",
            current_lane_id="L1", lanes=[{"id": "L1"}], stage_map={})

    stage_for_lane_mock.assert_called_once_with("L1", {}, [{"id": "L1"}])
    assert result is True  # unmapped lane -> hold at Intake, never reaching ghproject


def test_repair_statusless_member_delegates_to_board_layout_stage_for_lane():
    with patch("board_layout.stage_for_lane", return_value=None) as stage_for_lane_mock:
        result = vetting_latch.repair_statusless_member(
            cfg={}, apply=True, issue={"url": "https://example.test/1"}, key="K1",
            current_lane_id="L1", lanes=[{"id": "L1"}], stage_map={}, item=None)

    stage_for_lane_mock.assert_called_once_with("L1", {}, [{"id": "L1"}])
    assert result is True


def test_smoke_check_type_id_writes_delegates_to_board_layout_board_layout():
    empty_layout = board_layout.BoardLayout(lanes=[], card_types=[])
    with patch("board_layout.board_layout", return_value=empty_layout) as board_layout_mock:
        results = []
        smoke._check_type_id_writes(
            cfg={"board_id": "1"}, lane_id=None, parent_id="P1", run_id="r1", created=[],
            results=results)

    board_layout_mock.assert_called_once_with({"board_id": "1"})
    # No eligible card type on an empty layout -> both steps report an informational skip.
    assert len(results) == 2


def test_smoke_preview_delegates_to_board_layout_lane_title():
    board = {"title": "Board", "lanes": [{"id": "L1", "title": "Ready",
                                          "isDefaultDropLane": False}]}
    with patch("agileplace.api", return_value=board), \
         patch("agileplace.list_cards", return_value=[]), \
         patch("board_layout.lane_title", return_value="Ready") as lane_title_mock:
        lanes = smoke._preview({"board_id": "1", "host": "example.leankit.com"})

    lane_title_mock.assert_called_once_with(board["lanes"][0])
    assert lanes == board["lanes"]


def _sync_main_cfg(tmp_path):
    return {
        "token": "tok", "host": "example.leankit.com", "board_id": "42",
        "target_repo_path": tmp_path,
        "label_sync_ignore": frozenset(),
        "stage_lane_map": {},
        "gh_project": {"owner": "acme", "number": "7", "status_field": "Status",
                       "start_field": "Start", "target_field": "Target"},
        "ap_description_max_length": 20000,
    }


def test_main_calls_board_layout_board_layout_when_online(tmp_path):
    """main()'s one live board read (issue #82's card-types wiring comment block) must call
    board_layout.board_layout(cfg), not agileplace.board_layout(cfg) -- the deepest of this task's
    call sites, so covered end-to-end rather than as a smaller unit, mirroring the precedent
    test_sync_main.py's own _mock_io sets for every other main() I/O boundary."""
    cfg = _sync_main_cfg(tmp_path)
    state_file = tmp_path / ".sync-state.json"
    card = {"id": "C1", "version": 1, "customId": "1",
            "externalLink": {"url": "https://github.com/acme/repo/issues/1"}, "tags": [],
            "plannedStart": None, "plannedFinish": None, "laneId": None, "description": ""}
    issue = {"number": 1, "title": "widget", "state": "OPEN", "labels": [],
             "milestone": None, "assignees": [], "url": "https://github.com/acme/repo/issues/1"}

    stack = ExitStack()
    stack.enter_context(patch("ghkit.repo_name", return_value="acme/repo"))
    stack.enter_context(patch("ghkit.list_issues", return_value=[issue]))
    stack.enter_context(patch("ghkit.open_pr_issue_numbers", return_value=set()))
    stack.enter_context(patch("ghkit.blocked_by_map", return_value={}))
    stack.enter_context(patch("ghkit.run", return_value=Mock(stdout="")))
    stack.enter_context(patch("ghproject.configured", return_value=True))
    stack.enter_context(patch("ghproject.items", return_value=[]))
    stack.enter_context(patch("ghproject.field_meta", return_value=None))
    stack.enter_context(patch("ghproject.hydrate_item_dates", return_value=[]))
    stack.enter_context(patch("ghproject.can_set_status", return_value=True))
    stack.enter_context(patch("ghproject.add_item", return_value="planned:test"))
    stack.enter_context(patch("ghproject.set_item_status", return_value=True))
    board_layout_mock = stack.enter_context(patch(
        "board_layout.board_layout",
        return_value=board_layout.BoardLayout(lanes=[], card_types=[]),
    ))
    stack.enter_context(patch("agileplace.list_cards", return_value=[card]))
    stack.enter_context(patch("agileplace.card_dependencies", return_value=[]))
    stack.enter_context(patch("agileplace.patch_card"))
    stack.enter_context(patch("agileplace.create_card", return_value={}))

    with stack, patch("sync.env_config", return_value=cfg), patch("sync.STATE_FILE", state_file), \
         patch("sys.argv", ["sync.py", "--apply"]):
        sync.main()

    board_layout_mock.assert_called_once_with(cfg)
