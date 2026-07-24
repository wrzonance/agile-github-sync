"""Bounded-concurrency AgilePlace read phase (issue #99).

Gather-then-reconcile: every per-card AgilePlace read the run needs (description hydration,
dependency snapshots, comment lists, epic children) is issued through one bounded
ThreadPoolExecutor, and reconciliation then proceeds strictly serially in stable issue order
against the returned maps. Workers only READ, and only via the existing agileplace/
agileplace_comments functions -- every write path in the run stays serial and ordered.

A worker failure maps to the same 'unknown' value each serial reader produces today (absent for
descriptions -> the lazy get_card fallback; None elsewhere -> the consumer's existing skip
contract), so consumers keep their exact semantics and a thread exception never propagates.
max_workers=8 stays well under AgilePlace rate limits; the client's own per-request 429 retry
still applies inside each thread. urllib opens one connection per request, so concurrency here
overlaps the TLS handshakes it cannot eliminate (~260 serial round-trips -> ~22 latency waves
on the reference board)."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import NamedTuple

import agileplace
import agileplace_comments


class BoardReads(NamedTuple):
    """One run's prefetched AgilePlace reads, keyed by str(card id). A card id absent from
    `descriptions` fell back to unknown (consumers lazily get_card as before); a None value in
    the other three maps is that reader's own failure sentinel, verbatim."""
    descriptions: dict[str, str]
    dependencies: dict[str, list | None]
    ap_comments: dict[str, list | None]
    children: dict[str, frozenset | None]


def _description(cfg: dict, card_id: str) -> str:
    fresh = agileplace.get_card(cfg, card_id)
    return fresh.get("description") or ""


def _comments(cfg: dict, card_id: str):
    try:
        return agileplace_comments.list_comments(cfg, card_id)
    except SystemExit:  # list_comments' own tri-state idiom -- same catch _fetch_both_sides uses
        return None


def gather_board_reads(cfg: dict, *, description_card_ids, dependency_card_ids,
                       comment_card_ids, child_parent_ids, max_workers: int = 8) -> BoardReads:
    """Fetch all four read families concurrently under one bound; never raises."""
    jobs = (
        [("desc", cid, _description) for cid in description_card_ids]
        + [("deps", cid, agileplace.card_dependencies) for cid in dependency_card_ids]
        + [("comm", cid, _comments) for cid in comment_card_ids]
        + [("kids", cid, agileplace.card_child_ids) for cid in child_parent_ids]
    )
    out: dict[str, dict] = {"desc": {}, "deps": {}, "comm": {}, "kids": {}}
    if jobs:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(fn, cfg, cid): (family, cid) for family, cid, fn in jobs}
            for future, (family, cid) in futures.items():
                try:
                    out[family][cid] = future.result()
                except (Exception, SystemExit):  # noqa: BLE001 -- agileplace's failure idiom is
                    # SystemExit (not an Exception subclass); fail toward "unknown" either way.
                    # A failed description stays ABSENT, so the consumer's serial get_card
                    # fallback re-raises at the same call site the run fails loud at today.
                    if family != "desc":
                        out[family][cid] = None
    return BoardReads(descriptions={k: v for k, v in out["desc"].items() if v is not None},
                      dependencies=out["deps"], ap_comments=out["comm"], children=out["kids"])
