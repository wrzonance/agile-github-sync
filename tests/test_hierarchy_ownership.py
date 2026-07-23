"""Boundary coverage for hierarchy ownership in ``sync.main()``."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agileplace  # noqa: E402
import ghkit  # noqa: E402
import ghproject  # noqa: E402
import sync  # noqa: E402


def _issue(number: int, title: str, *, epic: bool = False) -> dict:
    return {
        "number": number,
        "title": title,
        "state": "OPEN",
        "state_reason": "",
        "labels": ["type:epic"] if epic else [],
        "milestone": None,
        "assignees": [],
        "url": f"https://github.com/acme/repo/issues/{number}",
        "has_open_pr": False,
    }


def _card(number: int, custom_id: str, *, url: str | None = None,
          children: tuple[str, ...] = (), tags: tuple[str, ...] = ()) -> dict:
    card = {
        "id": f"C{number}",
        "version": 1,
        "customId": custom_id,
        "tags": list(tags),
        "plannedStart": None,
        "plannedFinish": None,
        "laneId": None,
        "blockedStatus": {"isBlocked": False, "reason": ""},
        "childCards": [{"id": child_id} for child_id in children],
    }
    return {**card, **({"externalLink": {"url": url}} if url else {})}


def _config(tmp_path: Path) -> dict:
    return {
        "token": "token",
        "host": "example.leankit.com",
        "board_id": "42",
        "target_repo_path": tmp_path,
        "label_sync_ignore": frozenset(),
        "stage_lane_map": {},
        "gh_project": {},
    }


def _run_hierarchy(tmp_path: Path, monkeypatch, issues: list[dict], cards: list[dict],
                   sub_issue_numbers: list[int], *,
                   created_cards: dict[str, object] | None = None) -> tuple[Mock, Mock, Mock]:
    connect_children = Mock()
    disconnect_children = Mock()
    children_by_parent = {
        str(card["id"]): frozenset(str(child["id"]) for child in card.get("childCards", []))
        for card in cards
    }
    child_reads = Mock(
        side_effect=lambda _cfg, parent_id: children_by_parent.get(str(parent_id), frozenset())
    )
    planned_cards = created_cards or {}

    monkeypatch.setattr(ghkit, "repo_name", lambda _cfg: "acme/repo")
    monkeypatch.setattr(ghkit, "list_issues", lambda _cfg: issues)
    monkeypatch.setattr(ghkit, "open_pr_issue_numbers", lambda _cfg: set())
    monkeypatch.setattr(ghkit, "sub_issue_numbers", lambda *_args: sub_issue_numbers)
    monkeypatch.setattr(ghkit, "blocked_by_map", lambda *_args: {})
    monkeypatch.setattr(ghproject, "configured", lambda _cfg: False)
    monkeypatch.setattr(
        agileplace, "board_layout",
        lambda _cfg: agileplace.BoardLayout(lanes=[], card_types=[]),
    )
    monkeypatch.setattr(agileplace, "list_cards", lambda _cfg: cards)
    monkeypatch.setattr(agileplace, "card_child_ids", child_reads)
    monkeypatch.setattr(agileplace, "card_dependencies", lambda *_args: [])
    monkeypatch.setattr(
        agileplace,
        "create_card",
        Mock(side_effect=lambda _cfg, _apply, _title, custom_id, _url, _lane_id:
             planned_cards.get(custom_id, {})),
    )
    monkeypatch.setattr(agileplace, "patch_card", Mock())
    monkeypatch.setattr(agileplace, "connect_children", connect_children)
    monkeypatch.setattr(agileplace, "disconnect_children", disconnect_children)

    with patch("sync.env_config", return_value=_config(tmp_path)), \
         patch("sync.STATE_FILE", tmp_path / ".sync-state.json"), \
         patch("sys.argv", ["sync.py"]):
        sync.main()

    return connect_children, disconnect_children, child_reads


def test_foreign_linked_child_survives_authoritative_reconciliation(tmp_path, monkeypatch):
    epic = _issue(1, "[EP] Epic", epic=True)
    epic_card = _card(1, "EP", url=epic["url"], children=("C99",), tags=("type:epic",))
    foreign_card = _card(99, "JIRA-99", url="https://jira.example.test/browse/JIRA-99")

    connect, disconnect, _child_reads = _run_hierarchy(
        tmp_path, monkeypatch, [epic], [epic_card, foreign_card], [])

    connect.assert_not_called()
    disconnect.assert_not_called()


def test_custom_id_only_desired_child_does_not_churn(tmp_path, monkeypatch):
    epic = _issue(1, "[EP] Epic", epic=True)
    child = _issue(2, "[CHILD] Task")
    epic_card = _card(1, "EP", url=epic["url"], children=("C2",), tags=("type:epic",))
    child_card = _card(2, "CHILD", url="https://wiki.example.test/child")

    connect, disconnect, _child_reads = _run_hierarchy(
        tmp_path, monkeypatch, [epic, child], [epic_card, child_card], [2])

    connect.assert_not_called()
    disconnect.assert_not_called()


def test_undesired_custom_id_only_managed_child_is_removed(tmp_path, monkeypatch):
    epic = _issue(1, "[EP] Epic", epic=True)
    child = _issue(2, "[CHILD] Task")
    epic_card = _card(1, "EP", url=epic["url"], children=("C2",), tags=("type:epic",))
    child_card = _card(2, "CHILD")

    connect, disconnect, _child_reads = _run_hierarchy(
        tmp_path, monkeypatch, [epic, child], [epic_card, child_card], [])

    connect.assert_not_called()
    disconnect.assert_called_once_with(_config(tmp_path), False, "C1", ["C2"])


def test_plan_only_epic_skips_server_child_read(tmp_path, monkeypatch):
    epic = _issue(1, "[EP] Epic", epic=True)
    child = _issue(2, "[CHILD] Task")
    planned_cards = {
        "EP": agileplace._planned_card_snapshot("Epic", "EP", epic["url"], None),
        "CHILD": agileplace._planned_card_snapshot("Task", "CHILD", child["url"], None),
    }

    connect, disconnect, child_reads = _run_hierarchy(
        tmp_path,
        monkeypatch,
        [epic, child],
        [],
        [2],
        created_cards=planned_cards,
    )

    child_reads.assert_not_called()
    connect.assert_called_once_with(
        _config(tmp_path),
        False,
        planned_cards["EP"]["id"],
        [planned_cards["CHILD"]["id"]],
    )
    disconnect.assert_not_called()
