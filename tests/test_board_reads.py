"""board_reads.gather_board_reads (issue #99): the bounded-concurrency AgilePlace read phase.

Contracts pinned here:
  - all four read families (descriptions, dependencies, comments, children) are collected
    through the pool and keyed by card id;
  - a worker failure maps to the same 'unknown' value the serial reader produces today
    (absent for descriptions, None elsewhere) and never propagates out of gather;
  - agileplace_comments.list_comments' SystemExit tri-state idiom maps to None;
  - zero requested ids -> zero I/O and empty maps.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agileplace  # noqa: E402
import agileplace_comments  # noqa: E402
import board_reads  # noqa: E402


def test_gather_collects_all_four_families(monkeypatch):
    monkeypatch.setattr(agileplace, "get_card",
                        lambda _cfg, cid: {"id": cid, "description": f"D{cid}"})
    monkeypatch.setattr(agileplace, "card_dependencies", lambda _cfg, cid: [{"cardId": cid}])
    monkeypatch.setattr(agileplace_comments, "list_comments", lambda _cfg, cid: [{"id": 1}])
    monkeypatch.setattr(agileplace, "card_child_ids", lambda _cfg, cid: frozenset({"K"}))

    reads = board_reads.gather_board_reads({}, description_card_ids=["A"],
                                           dependency_card_ids=["A", "B"],
                                           comment_card_ids=["A"], child_parent_ids=["E"])

    assert reads.descriptions == {"A": "DA"}
    assert reads.dependencies == {"A": [{"cardId": "A"}], "B": [{"cardId": "B"}]}
    assert reads.ap_comments == {"A": [{"id": 1}]}
    assert reads.children == {"E": frozenset({"K"})}


def test_description_none_normalizes_to_empty_string(monkeypatch):
    monkeypatch.setattr(agileplace, "get_card", lambda _cfg, cid: {"id": cid})

    reads = board_reads.gather_board_reads({}, description_card_ids=["A"],
                                           dependency_card_ids=[], comment_card_ids=[],
                                           child_parent_ids=[])

    assert reads.descriptions == {"A": ""}  # same "" normalization as card_description()


def test_worker_failures_map_to_unknown_not_raise(monkeypatch):
    monkeypatch.setattr(agileplace, "get_card", Mock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(agileplace_comments, "list_comments",
                        Mock(side_effect=SystemExit("AP read failed")))
    monkeypatch.setattr(agileplace, "card_dependencies", Mock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(agileplace, "card_child_ids", Mock(side_effect=RuntimeError("boom")))

    reads = board_reads.gather_board_reads({}, description_card_ids=["A"],
                                           dependency_card_ids=["B"], comment_card_ids=["A"],
                                           child_parent_ids=["E"])

    assert "A" not in reads.descriptions      # absent -> the serial lazy fallback path
    assert reads.dependencies == {"B": None}  # None -> "state unknown", today's skip contract
    assert reads.ap_comments == {"A": None}   # None -> "skip this issue", today's contract
    assert reads.children == {"E": None}      # None -> add-only authority, today's contract


def test_zero_requested_ids_do_zero_io(monkeypatch):
    for name, mod in (("get_card", agileplace), ("card_dependencies", agileplace),
                      ("card_child_ids", agileplace)):
        monkeypatch.setattr(mod, name, Mock(side_effect=AssertionError("no I/O expected")))
    monkeypatch.setattr(agileplace_comments, "list_comments",
                        Mock(side_effect=AssertionError("no I/O expected")))

    reads = board_reads.gather_board_reads({}, description_card_ids=[], dependency_card_ids=[],
                                           comment_card_ids=[], child_parent_ids=[])

    assert reads == board_reads.BoardReads({}, {}, {}, {})


def test_many_ids_all_complete_under_the_bound(monkeypatch):
    """More jobs than max_workers still all complete (the pool queues, never drops)."""
    monkeypatch.setattr(agileplace, "card_dependencies", lambda _cfg, cid: [cid])

    ids = [f"C{i}" for i in range(30)]
    reads = board_reads.gather_board_reads({}, description_card_ids=[], dependency_card_ids=ids,
                                           comment_card_ids=[], child_parent_ids=[],
                                           max_workers=4)

    assert reads.dependencies == {cid: [cid] for cid in ids}
