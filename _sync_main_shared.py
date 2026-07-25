"""Shared sync.main() harness for the issue #95 customId-collision integration tests.

test_sync_cid_collision.py and test_sync_fence_cid_index_site1.py both drive the REAL sync.main()
with every I/O boundary (ghkit, ghproject, agileplace, board_layout) mocked, to assert how a
board-side customId collision flows through main()'s indexing and fencing. The mock setup and the
minimal issue/card/config builders are identical across both, so they live here once -- a change to
main()'s I/O boundaries updates this single harness rather than two byte-for-byte copies.
`_card_with_url` stays local to test_sync_cid_collision.py, its only user.

Mirrors the repo's existing shared-test-helper convention (_richtext_shared.py): a root-level
`_*_shared.py` module the test files import after adding the repo root to sys.path.

Run: pytest -q
"""
from __future__ import annotations

import json
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agileplace  # noqa: E402
import board_layout  # noqa: E402
import ghkit  # noqa: E402
import sync  # noqa: E402


def _issue(number: int, title: str) -> dict:
    return {
        "number": number,
        "title": title,
        "state": "OPEN",
        "stateReason": "",
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


def _cid_only_card(card_id: str, custom_id: str) -> dict:
    """One AgilePlace card whose customId carries a match key but with NO external link at all --
    the only way for a customId key to collide purely on the AgilePlace side, independent of any
    issue's own URL."""
    return {
        "id": card_id,
        "version": 1,
        "customId": custom_id,
        "laneId": "L1",
        "tags": [],
        "plannedStart": None,
        "plannedFinish": None,
        "description": "",
    }


def _run_main(tmp_path, monkeypatch, raw_issues, cards, lanes=()):
    monkeypatch.setattr(
        ghkit, "run", lambda *_a, **_k: SimpleNamespace(stdout=json.dumps(raw_issues)))
    stack = ExitStack()
    stack.enter_context(patch(
        "ghkit.resolve_repo_context",
        return_value=ghkit.RepoContext(owner="acme", name="repo", host="github.com")))
    stack.enter_context(patch("ghkit.open_pr_issue_numbers", return_value=set()))
    stack.enter_context(patch("ghkit.blocked_by_map", return_value={}))
    stack.enter_context(patch("ghkit.edit_label"))
    stack.enter_context(patch("ghkit.set_milestone"))
    stack.enter_context(patch("ghproject.configured", return_value=False))
    stack.enter_context(patch("ghproject.items", return_value={}))
    stack.enter_context(patch("ghproject.field_meta", return_value=None))
    stack.enter_context(patch("ghproject.hydrate_item_dates", return_value={}))
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
    return create_card, patch_card
