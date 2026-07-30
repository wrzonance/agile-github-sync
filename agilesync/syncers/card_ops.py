"""The run's per-card op queue: accumulate, poison, flush (issues #70, #107).

Every mutation one run makes to a card's own fields is batched into a SINGLE versioned PATCH --
that batching is what keeps a card's resource version from going stale between two writes of its
own. This module owns that accumulator end to end: the `queue` callable every syncer is handed, the
lane-conflict poisoning that guards it, and the one flush that sends it. sync.main() keeps the
ordering decision (WHEN to flush); the shape of the queue lives here, so no caller has to reach
into an entry's internals.

WHERE THE FLUSH BELONGS IN A RUN (issue #107). Every AgilePlace write bumps a card's resource
version by exactly 1 -- card PATCHes, connection POSTs, dependency POSTs and comment writes alike
(measured live 2026-07-30, see docs/API-VALIDATION.md). Each queued PATCH carries the version from
that card's own run snapshot, so ANY other write the run makes to that card first is a conflict the
run inflicts on itself: a failed PATCH, a refetch and a retry, reported under a "version bumped by
an unrelated change" note that is not true. So the flush runs BEFORE the connection/dependency
steps, and the comment sync -- which bumps the version too -- stays after it. Prevention over
recovery, the same reasoning as intake._card_for_link_write. The conflict retry (issue #105) stays
the safety net for genuine concurrent edits by humans, which no ordering can prevent.

Poisoning (issue #70 Layer 2) is unchanged by that ordering: an entry whose queued ops disagree
about /laneId is skipped WHOLESALE at flush, never half-applied, and its card id is what
`poisoned_ids()` reports to the connection/dependency steps so they leave that card's edges alone.

Run: pytest -q
"""
from __future__ import annotations

from agilesync.board import agileplace
from agilesync.syncers.card_coherence import lane_conflict, laneid_op_value, poisoned_card_ids


class CardOpQueue:
    """One run's card-op accumulator, keyed by card id. Not thread-safe: the run queues serially."""

    def __init__(self) -> None:
        self.entries: dict[str, dict] = {}

    def queue(self, card: dict, ops: list[dict], note: str) -> None:
        """Add `ops` to this card's batch. Two queue() calls for the same card can carry conflicting
        /laneId values (e.g. duplicate [KEY]-prefixed issue titles matching the same card through the
        customId fallback within one run). Detect and poison the entry rather than risk one issue's
        lane move clobbering another's -- the poisoned entry is skipped wholesale at flush."""
        cid = str(card["id"])
        entry = self.entries.setdefault(
            cid, {"card": card, "ops": [], "notes": [], "lane_id": None, "poisoned": False})
        new_lane_id, conflict = lane_conflict(ops, entry["lane_id"])
        if conflict:
            entry["poisoned"] = True
            print(f"WARN  card {cid} poisoned: conflicting /laneId ops "
                  f"({entry['lane_id']!r} vs {laneid_op_value(ops)!r})")
        else:
            entry["lane_id"] = new_lane_id
        entry["ops"].extend(ops)
        entry["notes"].append(note)

    def poisoned_ids(self) -> frozenset[str]:
        """Card ids whose queued ops conflict -- the edge steps must not touch these cards."""
        return poisoned_card_ids(self.entries)

    @property
    def clean(self) -> bool:
        """No card was poisoned this run, i.e. every queued op reached its PATCH."""
        return not any(entry["poisoned"] for entry in self.entries.values())

    def flush(self, cfg: dict, apply: bool) -> None:
        """ONE versioned PATCH per card (optimistic concurrency). See this module's docstring for
        why this must run before the run's connection/dependency writes."""
        for entry in self.entries.values():
            if entry["poisoned"]:
                continue  # Issue #70 Layer 2: conflicting /laneId ops -- discard, don't half-apply
            agileplace.patch_card(cfg, apply, entry["card"], entry["ops"], "; ".join(entry["notes"]))
