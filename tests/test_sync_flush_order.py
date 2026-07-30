"""Write-ordering invariant for sync.main() (issue #107).

Every AgilePlace write bumps the card's resource version by exactly 1 -- PATCHes, connection POSTs,
dependency POSTs and comment writes alike (measured live 2026-07-30, see docs/API-VALIDATION.md).
The run's flush PATCH carries the version from each card's own snapshot, so ANY write this run makes
to a card between that snapshot and its flush is a conflict the run inflicts on itself: a wasted
PATCH, a refetch and a retry, reported under a "version bumped by an unrelated change" note that is
not true.

The invariant pinned here: **no card is written between its snapshot and its flush PATCH.** That
puts the flush ahead of the child-connection and dependency steps, and keeps comment sync (which
bumps the version too) behind it -- comment sync must stay after the flush for the same reason the
flush must come before the connection POSTs.

These drive the REAL main() with every I/O boundary mocked, recording the ORDER of the writes rather
than just their arguments -- what each step writes is covered by test_sync_dependencies.py,
test_agileplace_children.py and test_sync_comments_call_site.py.

Run: pytest -q
"""
from __future__ import annotations

import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agilesync.board import board_layout  # noqa: E402
from agilesync.gh import ghkit  # noqa: E402
from agilesync import sync  # noqa: E402

EPIC_URL = "https://github.com/acme/repo/issues/1"
TASK_URL = "https://github.com/acme/repo/issues/2"


def _issue(number: int, url: str, labels: list[str]) -> dict:
    return {"number": number, "title": f"widget {number}", "state": "OPEN", "labels": labels,
            "milestone": None, "assignees": [], "url": url}


def _card(card_id: str, url: str) -> dict:
    # customId is empty, so step 2 queues a /customId op for this card -- i.e. the card reaches the
    # flush with real work, which is what makes its version staleness observable at all.
    # "description": "" keeps card_description() on its zero-I/O path (see test_sync_main.py).
    return {"id": card_id, "version": 1, "customId": "", "externalLink": {"url": url},
            "tags": [], "plannedStart": None, "plannedFinish": None, "laneId": None,
            "description": ""}


def _cfg(tmp_path) -> dict:
    return {
        "token": "tok", "host": "example.leankit.com", "board_id": "42",
        "target_repo_path": tmp_path,
        "label_sync_ignore": frozenset(),
        "repo_context": ghkit.RepoContext(owner="acme", name="repo", host="github.com"),
        "stage_lane_map": {},
        "gh_project": {},
        "ap_description_max_length": 20000,
    }


def _run_main(tmp_path, cards: list[dict]) -> list[tuple]:
    """One --apply run of the real main() over an epic (#1) with one sub-issue (#2) that is also
    blocked by it, so the SAME two cards carry queued ops, gain a child connection AND gain a
    dependency. Returns the recorded writes in call order."""
    writes: list[tuple] = []
    stack = ExitStack()
    stack.enter_context(patch("agilesync.gh.ghkit.resolve_repo_context",
                              return_value=ghkit.RepoContext(owner="acme", name="repo", host="github.com")))
    stack.enter_context(patch("agilesync.gh.ghkit_snapshot.fetch_issue_graph", return_value=None))
    stack.enter_context(patch("agilesync.gh.ghkit.list_issues", return_value=[
        _issue(1, EPIC_URL, ["type:epic"]), _issue(2, TASK_URL, [])]))
    stack.enter_context(patch("agilesync.gh.ghkit.open_pr_issue_numbers", return_value=set()))
    stack.enter_context(patch("agilesync.gh.ghkit.blocked_by_map", return_value={1: [], 2: [1]}))
    stack.enter_context(patch("agilesync.gh.ghkit.sub_issue_numbers", return_value=[2]))
    stack.enter_context(patch("agilesync.gh.ghkit.run", return_value=Mock(stdout="")))
    stack.enter_context(patch("agilesync.gh.ghproject.configured", return_value=False))
    stack.enter_context(patch("agilesync.gh.ghproject.items", return_value={}))
    stack.enter_context(patch("agilesync.gh.ghproject.field_meta", return_value=None))
    stack.enter_context(patch("agilesync.gh.ghproject.hydrate_item_dates", return_value={}))
    stack.enter_context(patch("agilesync.board.board_layout.board_layout",
                              return_value=board_layout.BoardLayout(lanes=[], card_types=[])))
    stack.enter_context(patch("agilesync.board.agileplace.list_cards", return_value=cards))
    stack.enter_context(patch("agilesync.board.agileplace.card_dependencies", return_value=[]))
    stack.enter_context(patch("agilesync.board.agileplace.card_child_ids", return_value=[]))
    stack.enter_context(patch("agilesync.board.agileplace.create_card", return_value={}))
    stack.enter_context(patch(
        "agilesync.board.agileplace.patch_card",
        side_effect=lambda cfg, apply, card, ops, note="": writes.append(("patch", str(card["id"])))))
    stack.enter_context(patch(
        "agilesync.board.agileplace.connect_children",
        side_effect=lambda cfg, apply, parent, children: writes.append(
            ("connect", str(parent), *[str(c) for c in children]))))
    stack.enter_context(patch(
        "agilesync.board.agileplace.create_dependencies",
        side_effect=lambda cfg, apply, card_id, ids: writes.append(
            ("dependency", str(card_id), *[str(i) for i in ids]))))
    stack.enter_context(patch(
        "agilesync.sync.sync_comments",
        side_effect=lambda cfg, apply, issue, card, state, **_kw: writes.append(
            ("comment", str(card["id"])))))
    with stack, patch("agilesync.sync.env_config", return_value=_cfg(tmp_path)), \
            patch("agilesync.sync.STATE_FILE", tmp_path / ".sync-state.json"), \
            patch("sys.argv", ["sync.py", "--apply"]):
        sync.main()
    return writes


def _first_index(writes: list[tuple], kind: str, card_id: str) -> int | None:
    for index, write in enumerate(writes):
        if write[0] == kind and card_id in write[1:]:
            return index
    return None


def _both_cards() -> list[dict]:
    return [_card("C1", EPIC_URL), _card("C2", TASK_URL)]


def test_run_writes_every_card_ops_flush_before_touching_that_card_again(tmp_path):
    """The core invariant: for every card this run writes, its flush PATCH lands before any other
    write this run makes to that card -- so the version its snapshot carries is still current."""
    writes = _run_main(tmp_path, _both_cards())

    flushed = {write[1]: index for index, write in enumerate(writes) if write[0] == "patch"}
    assert flushed, "expected the run to flush queued card ops"
    for index, write in enumerate(writes):
        if write[0] == "patch":
            continue
        for card_id in write[1:]:
            if card_id in flushed:
                assert flushed[card_id] < index, (
                    f"{write[0]} write to card {card_id} lands BEFORE its flush PATCH, "
                    f"staling the version that PATCH carries: {writes}")


def test_child_connection_post_follows_the_parent_and_child_flush(tmp_path):
    writes = _run_main(tmp_path, _both_cards())
    connect = _first_index(writes, "connect", "C1")
    assert connect is not None, f"expected a child-connection write: {writes}"
    assert _first_index(writes, "patch", "C1") < connect
    assert _first_index(writes, "patch", "C2") < connect


def test_dependency_post_follows_the_flush_of_both_cards_it_links(tmp_path):
    writes = _run_main(tmp_path, _both_cards())
    dependency = _first_index(writes, "dependency", "C2")
    assert dependency is not None, f"expected a dependency write: {writes}"
    assert _first_index(writes, "patch", "C2") < dependency
    assert _first_index(writes, "patch", "C1") < dependency


def test_comment_sync_stays_after_the_flush(tmp_path):
    """Comment writes bump the version too, so comment sync must NEVER move ahead of the flush --
    that would recreate exactly the staleness this ordering exists to prevent."""
    writes = _run_main(tmp_path, _both_cards())
    comments = [index for index, write in enumerate(writes) if write[0] == "comment"]
    patches = [index for index, write in enumerate(writes) if write[0] == "patch"]
    assert comments, f"expected comment sync to run: {writes}"
    assert min(comments) > max(patches)
