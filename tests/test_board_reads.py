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


# --- hydrate_run_reads: completing the run's card snapshots in place -------------------------

def _issue(number, title="[K] t", labels=(), url=None):
    return {"number": number, "title": title, "labels": list(labels),
            "url": url or f"https://github.com/o/r/issues/{number}"}


def test_hydrate_completes_card_snapshots_in_place(monkeypatch):
    """Matched real cards gain description (the real API key -- the zero-I/O path
    agileplace_description.card_description documents) and the _prefetched* keys the consumers
    read, following the run's own snapshot-hydration idiom (_planOnly, has_open_pr)."""
    monkeypatch.setattr(agileplace, "get_card",
                        lambda _cfg, cid: {"id": cid, "description": "<p>d</p>"})
    monkeypatch.setattr(agileplace, "card_dependencies", lambda _cfg, cid: [{"cardId": "X"}])
    monkeypatch.setattr(agileplace_comments, "list_comments", lambda _cfg, cid: [{"id": 7}])
    monkeypatch.setattr(agileplace, "card_child_ids", lambda _cfg, cid: frozenset({"C2"}))
    epic = _issue(1, labels=["type:epic"])
    task = _issue(2)
    epic_card = {"id": "E1"}
    task_card = {"id": "T1"}
    cards = {1: epic_card, 2: task_card}

    board_reads.hydrate_run_reads({"comment_sync_identity": {"gh": "a", "ap": "b"}}, True,
                                  [epic, task], lambda i: cards[i["number"]], [epic])

    assert task_card["description"] == "<p>d</p>"
    assert task_card["_prefetchedDeps"] == [{"cardId": "X"}]
    assert task_card["_prefetchedApComments"] == [{"id": 7}]
    assert epic_card["_prefetchedChildIds"] == frozenset({"C2"})
    assert "_prefetchedChildIds" not in task_card  # only epic parents get child snapshots


def test_hydrate_skips_descriptions_the_snapshot_already_carries(monkeypatch):
    monkeypatch.setattr(agileplace, "get_card",
                        Mock(side_effect=AssertionError("zero-I/O path must stay zero-I/O")))
    monkeypatch.setattr(agileplace, "card_dependencies", lambda _cfg, cid: [])
    card = {"id": "T1", "description": ""}

    board_reads.hydrate_run_reads({}, True, [_issue(2)], lambda _i: card, [])

    assert card["description"] == ""  # untouched -- an explicit "" is a real description


def test_hydrate_skips_comments_without_identity_and_all_reads_when_offline(monkeypatch):
    for name in ("get_card", "card_dependencies", "card_child_ids"):
        monkeypatch.setattr(agileplace, name, Mock(side_effect=AssertionError("no I/O")))
    monkeypatch.setattr(agileplace_comments, "list_comments",
                        Mock(side_effect=AssertionError("no identity -> no comment prefetch")))
    card = {"id": "T1"}

    board_reads.hydrate_run_reads({}, False, [_issue(2)], lambda _i: card, [])  # offline: no-op
    assert card == {"id": "T1"}

    monkeypatch.setattr(agileplace, "get_card", lambda _cfg, cid: {"id": cid})
    monkeypatch.setattr(agileplace, "card_dependencies", lambda _cfg, cid: [])
    board_reads.hydrate_run_reads({}, True, [_issue(2)], lambda _i: card, [])  # no identity
    assert "_prefetchedApComments" not in card


def test_hydrate_never_touches_plan_only_or_unmatched_cards(monkeypatch):
    for name in ("get_card", "card_dependencies", "card_child_ids"):
        monkeypatch.setattr(agileplace, name, Mock(side_effect=AssertionError("no I/O")))
    plan_only = {"id": "planned-card:x", "_planOnly": True}

    board_reads.hydrate_run_reads({}, True, [_issue(2), _issue(3)],
                                  lambda i: plan_only if i["number"] == 2 else None, [])

    assert plan_only == {"id": "planned-card:x", "_planOnly": True}


def test_hydrate_failed_reads_keep_todays_unknown_semantics(monkeypatch):
    monkeypatch.setattr(agileplace, "get_card", Mock(side_effect=SystemExit("boom")))
    monkeypatch.setattr(agileplace, "card_dependencies", lambda _cfg, cid: None)
    monkeypatch.setattr(agileplace_comments, "list_comments",
                        Mock(side_effect=SystemExit("read failed")))
    monkeypatch.setattr(agileplace, "card_child_ids", lambda _cfg, cid: None)
    epic = _issue(1, labels=["type:epic"])
    card = {"id": "E1"}

    board_reads.hydrate_run_reads({"comment_sync_identity": {"gh": "a", "ap": "b"}}, True,
                                  [epic], lambda _i: card, [epic])

    assert "description" not in card            # absent -> serial get_card fails loud, as today
    assert card["_prefetchedDeps"] is None      # None -> "state unknown", consumer skips
    assert card["_prefetchedApComments"] is None
    assert card["_prefetchedChildIds"] is None  # None -> add-only authority


def test_hydrate_skips_dependency_prefetch_when_reconciliation_is_off(monkeypatch):
    """Codex stack-review P2: when the run's blocked-by snapshot is unusable (blocked_by=None),
    sync_dependencies never runs -- prefetching one dependency read per matched card would spend
    the expensive requests solely to discard them."""
    monkeypatch.setattr(agileplace, "card_dependencies",
                        Mock(side_effect=AssertionError("no dependency prefetch expected")))
    monkeypatch.setattr(agileplace, "get_card", lambda _cfg, cid: {"id": cid})
    card = {"id": "T1"}

    board_reads.hydrate_run_reads({}, True, [_issue(2)], lambda _i: card, [],
                                  prefetch_deps=False)

    assert "_prefetchedDeps" not in card
