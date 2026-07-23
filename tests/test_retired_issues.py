"""Regression coverage for retired GitHub issues (NOT_PLANNED/DUPLICATE)."""
from __future__ import annotations

import json
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agileplace  # noqa: E402
import board_layout  # noqa: E402
import ghkit  # noqa: E402
import sync  # noqa: E402


_PROJECT_DISABLED = object()


def _github_issue(number: int, state_reason: str) -> dict:
    return {
        "number": number,
        "title": f"issue {number}",
        "state": "CLOSED",
        "stateReason": state_reason,
        "labels": [],
        "milestone": None,
        "assignees": [],
        "url": f"https://github.com/acme/repo/issues/{number}",
    }


def _config(tmp_path) -> dict:
    return {
        "token": "token",
        "host": "example.leankit.com",
        "board_id": "42",
        "target_repo_path": tmp_path,
        "label_sync_ignore": frozenset(),
        "stage_lane_map": {},
        "gh_project": {},
        "ap_description_max_length": 20000,  # issue #65: sync_description reads this unconditionally
    }


def _run_main(tmp_path, monkeypatch, raw_issues, cards, blocked_by=None, lanes=(),
              open_pr_result=frozenset(), project_snapshot=_PROJECT_DISABLED):
    monkeypatch.setattr(
        ghkit,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=json.dumps(raw_issues)),
    )
    stack = ExitStack()
    stack.enter_context(patch("ghkit.repo_name", return_value="acme/repo"))
    stack.enter_context(patch("ghkit.open_pr_issue_numbers", return_value=open_pr_result))
    blocked_by_read = stack.enter_context(patch("ghkit.blocked_by_map", return_value=blocked_by or {}))
    edit_label = stack.enter_context(patch("ghkit.edit_label"))
    stack.enter_context(patch("ghkit.set_milestone"))
    project_configured = project_snapshot is not _PROJECT_DISABLED
    stack.enter_context(patch("ghproject.configured", return_value=project_configured))
    stack.enter_context(patch(
        "ghproject.items",
        return_value=project_snapshot if project_configured else {},
    ))
    stack.enter_context(patch("ghproject.field_meta", return_value=None))
    stack.enter_context(patch("ghproject.hydrate_item_dates", return_value=project_snapshot))
    stack.enter_context(patch(
        "board_layout.board_layout",
        return_value=board_layout.BoardLayout(lanes=list(lanes), card_types=[]),
    ))
    stack.enter_context(patch("agileplace.list_cards", return_value=cards))
    stack.enter_context(patch("agileplace.card_dependencies", return_value=[]))
    create_card = stack.enter_context(patch("agileplace.create_card", return_value={}))
    patch_card = stack.enter_context(patch("agileplace.patch_card"))
    with stack, patch("sync.env_config", return_value=_config(tmp_path)), \
         patch("sync.STATE_FILE", tmp_path / ".sync-state.json"), \
         patch("sys.argv", ["sync.py"]):
        sync.main()
    return create_card, patch_card, blocked_by_read, edit_label


def _card(number: int, lane_id: str, *, blocked: bool) -> dict:
    return {
        "id": f"C{number}",
        "version": 1,
        "customId": str(number),
        "externalLink": {"url": f"https://github.com/acme/repo/issues/{number}"},
        "laneId": lane_id,
        "tags": ["stale-card-tag"] if number == 10 else [],
        "blockedStatus": {"isBlocked": blocked, "reason": "Blocked by #10" if blocked else ""},
        "plannedStart": None,
        "plannedFinish": None,
        # issue #65: keeps agileplace_description.card_description() on its zero-I/O path.
        "description": "",
    }


def test_retired_issues_resolve_to_done_stage(monkeypatch):
    """Retired (NOT_PLANNED/DUPLICATE) issues normalize to stage Done -- this drives lane
    retirement, and (via _blocker_cards) keeps their dependency edges resolvable."""
    raw_issues = [
        _github_issue(10, "not_planned"),
        _github_issue(11, "DUPLICATE"),
    ]
    monkeypatch.setattr(
        ghkit,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=json.dumps(raw_issues)),
    )

    issues = ghkit.list_issues({})
    project_status = {issue["url"]: "Backlog" for issue in issues}
    stage_by_number = {
        issue["number"]: sync.resolve_issue_stage(issue, project_status, {}, None)
        for issue in issues
    }

    assert [issue["state_reason"] for issue in issues] == ["NOT_PLANNED", "DUPLICATE"]
    assert stage_by_number == {10: "Done", 11: "Done"}


def test_retired_issue_without_card_is_not_created(tmp_path, monkeypatch):
    create_card, patch_card, blocked_by_read, _ = _run_main(
        tmp_path,
        monkeypatch,
        [_github_issue(10, "NOT_PLANNED")],
        cards=[],
    )

    create_card.assert_not_called()
    patch_card.assert_not_called()
    blocked_by_read.assert_not_called()


def test_existing_retired_card_is_retired_and_flags_stay_human_owned(
        tmp_path, monkeypatch, capsys):
    """Since issue #57 Phase 2 the sync never writes /isBlocked or /blockReason: retirement
    moves the lane and nothing else, and a dependent card's flag is not 'unblocked' by the
    sync -- the native dependency (and its health display) carries that signal now."""
    dependent = {
        **_github_issue(20, ""),
        "state": "OPEN",
        "stateReason": "",
    }
    lanes = [
        {"id": "L1", "title": "Backlog", "cardStatus": "notStarted"},
        {"id": "L2", "title": "In review", "cardStatus": "started"},
        {"id": "L5", "title": "Done", "cardStatus": "finished"},
    ]

    create_card, patch_card, blocked_by_read, edit_label = _run_main(
        tmp_path,
        monkeypatch,
        [_github_issue(10, "NOT_PLANNED"), dependent],
        cards=[_card(10, "L2", blocked=True), _card(20, "L1", blocked=True)],
        blocked_by={10: [], 20: [10]},
        lanes=lanes,
        project_snapshot={},
    )

    out = capsys.readouterr().out
    assert "DRY   retire [10] -> 'Done' (NOT_PLANNED)" in out
    create_card.assert_not_called()
    edit_label.assert_not_called()
    blocked_by_read.assert_called_once_with(_config(tmp_path), [20])
    ops_by_card = {call.args[2]["id"]: call.args[3] for call in patch_card.call_args_list}
    assert ops_by_card == {
        "C10": [
            {"op": "replace", "path": "/laneId", "value": "L5"},
        ],
    }
    assert not any("/isBlocked" in json.dumps(ops) or "/blockReason" in json.dumps(ops)
                   for ops in ops_by_card.values())


def test_retirement_uses_authoritative_closure_when_other_reads_fail(
        tmp_path, monkeypatch, capsys):
    _, patch_card, _, _ = _run_main(
        tmp_path,
        monkeypatch,
        [_github_issue(10, "DUPLICATE")],
        cards=[_card(10, "L2", blocked=False)],
        lanes=[{"id": "L5", "title": "Done", "cardStatus": "finished"}],
        open_pr_result=None,
        project_snapshot=None,
    )

    out = capsys.readouterr().out
    assert "open-PR read FAILED" in out
    assert "Projects v2 read FAILED -- leaving active-issue lanes untouched" in out
    assert "DRY   retire [10] -> 'Done' (DUPLICATE)" in out
    assert patch_card.call_args.args[3] == [
        {"op": "replace", "path": "/laneId", "value": "L5"},
    ]


def test_retirement_refuses_custom_id_only_match(tmp_path, monkeypatch, capsys):
    unrelated_card = {
        **_card(99, "L1", blocked=False),
        "customId": "10",
    }

    _, patch_card, _, _ = _run_main(
        tmp_path,
        monkeypatch,
        [_github_issue(10, "NOT_PLANNED")],
        cards=[unrelated_card],
    )

    assert "retired issue has only a customId card match" in capsys.readouterr().out
    patch_card.assert_not_called()


def test_active_issue_cannot_claim_url_owned_retired_card_by_custom_id(
        tmp_path, monkeypatch, capsys):
    """This is issue #60's asymmetric-ownership shape: an active issue's customId collides with a
    retired card that a DIFFERENT (retired) issue owns by URL. Pre-#75, contested_cards() was
    URL-only, so this customId collision was invisible to Layer 1 -- the retired card kept sole
    ownership via its URL claim, and the active issue was individually deferred by the
    retirement-reservation check ("customId is held by retired card") while the retired card still
    retired normally.

    Post-#75, contested_cards() fences customId claims too: the retired card is now claimed by
    BOTH the retired issue's own URL and the active issue's customId fallback, so it becomes
    contested (2 distinct claiming issues) and BOTH issues are deferred together by the widened
    Layer 1 fence -- the retired card no longer retires this run either. This is the accepted cost
    issue #75's design explicitly calls out: a legit URL owner can lose a sync cycle to a
    customId-colliding active issue, in exchange for never risking a clobber either way."""
    active = {
        **_github_issue(20, ""),
        "title": "[ABC] active replacement",
        "state": "OPEN",
        "stateReason": "",
    }
    retired_card = {
        **_card(10, "L2", blocked=False),
        "customId": "ABC",
    }
    lanes = [
        {"id": "L1", "title": "Backlog", "cardStatus": "notStarted"},
        {"id": "L5", "title": "Done", "cardStatus": "finished"},
    ]

    create_card, patch_card, _, edit_label = _run_main(
        tmp_path,
        monkeypatch,
        [_github_issue(10, "NOT_PLANNED"), active],
        cards=[retired_card],
        lanes=lanes,
    )

    out = capsys.readouterr().out
    assert "WARN  card C10 claimed by 2 issue URLs, deferring:" in out
    assert "deferring active card [ABC]: customId is held by retired card C10" not in out, (
        "the widened Layer 1 fence defers both issues together -- the older per-issue "
        "customId-reservation WARN never fires once the card is already contested")
    create_card.assert_not_called()
    edit_label.assert_not_called()
    patch_card.assert_not_called()


def test_retirement_leaves_blocked_flag_and_reason_to_humans(tmp_path, monkeypatch):
    """Pre-Phase-2 the sync scrubbed stale reason text during retirement; now the flag and
    its reason belong to humans, so a card already in its Done lane gets NO patch at all."""
    card = {
        **_card(10, "L5", blocked=False),
        "blockedStatus": {"isBlocked": False, "reason": "stale reason"},
    }

    _, patch_card, _, _ = _run_main(
        tmp_path,
        monkeypatch,
        [_github_issue(10, "DUPLICATE")],
        cards=[card],
        lanes=[{"id": "L5", "title": "Done", "cardStatus": "finished"}],
    )

    patch_card.assert_not_called()


def test_active_card_is_not_renamed_to_retired_card_custom_id(
        tmp_path, monkeypatch, capsys):
    active = {
        **_github_issue(20, ""),
        "title": "[ABC] active replacement",
        "state": "OPEN",
        "stateReason": "",
    }
    retired_card = {
        **_card(10, "L2", blocked=False),
        "customId": "ABC",
    }
    active_card = {
        **_card(20, "L1", blocked=False),
        "customId": "OLD",
    }
    lanes = [
        {"id": "L1", "title": "Backlog", "cardStatus": "notStarted"},
        {"id": "L5", "title": "Done", "cardStatus": "finished"},
    ]

    create_card, patch_card, _, edit_label = _run_main(
        tmp_path,
        monkeypatch,
        [_github_issue(10, "NOT_PLANNED"), active],
        cards=[retired_card, active_card],
        lanes=lanes,
    )

    out = capsys.readouterr().out
    assert "deferring active card [ABC]: customId is held by retired card C10" in out
    create_card.assert_not_called()
    edit_label.assert_not_called()
    assert len(patch_card.call_args_list) == 1
    assert patch_card.call_args.args[2]["id"] == "C10"
    assert patch_card.call_args.args[3] == [
        {"op": "replace", "path": "/laneId", "value": "L5"},
    ]


def test_epic_disconnects_only_retired_url_matched_child(tmp_path, monkeypatch):
    epic = {
        **_github_issue(1, ""),
        "state": "OPEN",
        "stateReason": "",
        "labels": [{"name": "type:epic"}],
    }
    active_child = {
        **_github_issue(2, ""),
        "state": "OPEN",
        "stateReason": "",
    }
    epic_card = _card(1, "L1", blocked=False)
    lanes = [
        {"id": "L1", "title": "Backlog", "cardStatus": "notStarted"},
        {"id": "L5", "title": "Done", "cardStatus": "finished"},
    ]
    connect_children = Mock()
    disconnect_children = Mock()
    monkeypatch.setattr("ghkit.sub_issue_numbers", lambda *_args: [2, 10])
    monkeypatch.setattr("agileplace.card_child_ids", lambda *_args: frozenset({"C2", "C10"}))
    monkeypatch.setattr("agileplace.connect_children", connect_children)
    monkeypatch.setattr("agileplace.disconnect_children", disconnect_children)

    _run_main(
        tmp_path,
        monkeypatch,
        [epic, active_child, _github_issue(10, "NOT_PLANNED")],
        cards=[epic_card, _card(2, "L1", blocked=False), _card(10, "L1", blocked=False)],
        lanes=lanes,
    )

    connect_children.assert_not_called()
    disconnect_children.assert_called_once_with(_config(tmp_path), False, "C1", ["C10"])
