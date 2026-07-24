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


def hydrate_run_reads(cfg: dict, online: bool, syncable_issues: list, card_for, epics: list,
                      max_workers: int = 8) -> None:
    """Complete the run's matched card snapshots in place with everything the reconciliation
    loops would otherwise fetch one card at a time: `description` (the real API key --
    agileplace_description.card_description's documented zero-I/O path), `_prefetchedDeps`,
    `_prefetchedApComments`, and (epic parents only) `_prefetchedChildIds`.

    In-place hydration follows the run's own snapshot idiom (`_planOnly`, `has_open_pr`): the
    card dict IS the run's snapshot, and completing it here keeps every consumer signature and
    sync.py's wiring unchanged. Only real, matched cards are touched -- plan-only cards keep
    their existing zero-network conventions. A key hydrated to None carries that reader's own
    failure sentinel (consumers already skip on it); a description that failed stays ABSENT so
    the consumer's serial get_card fallback fails loud at today's call site. Offline -> no-op."""
    if not online:
        return
    matched: dict[str, dict] = {}
    for issue in syncable_issues:
        card = card_for(issue)
        if card and card.get("id") and not card.get("_planOnly"):
            matched[str(card["id"])] = card
    epic_ids = [str(c["id"]) for e in epics
                if (c := card_for(e)) and c.get("id") and not c.get("_planOnly")]
    reads = gather_board_reads(
        cfg,
        description_card_ids=[cid for cid, c in matched.items() if "description" not in c],
        dependency_card_ids=list(matched),
        comment_card_ids=list(matched) if cfg.get("comment_sync_identity") else [],
        child_parent_ids=epic_ids,
        max_workers=max_workers,
    )
    for cid, card in matched.items():
        if cid in reads.descriptions:
            card["description"] = reads.descriptions[cid]
        card["_prefetchedDeps"] = reads.dependencies.get(cid)
        if cid in reads.ap_comments:
            card["_prefetchedApComments"] = reads.ap_comments[cid]
        if cid in reads.children:
            card["_prefetchedChildIds"] = reads.children[cid]
