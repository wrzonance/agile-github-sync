"""The "Intake" vetting latch (issue #63): a freshly-discovered issue with no explicit Project
Status and no work signal parks at the "Intake" stage instead of landing straight in "Backlog",
waiting for a human to vet it onto the GitHub Project board. This module is what makes that parking
spot safe -- it exists to guarantee one invariant above all else:

    A card a human has moved OUT of the Intake lane can never be walked back into it, or off the
    board, by this sync -- regardless of whether this run's own attempt to vet the issue onto the
    Project succeeds or fails.

Call apply_latch() only when resolve_issue_stage() returned "Intake" for this issue, from inside
the same `move_lanes` gate that already covers the ordinary lane-move path (see sync.py's loop 2
wiring) -- so a transiently miscomputed "Intake" during a Projects v2 read outage can never drive a
wrong write.
"""
from __future__ import annotations

import agileplace
import ghproject


def apply_latch(cfg: dict, apply: bool, issue: dict, key: str, current_lane_id: str,
                lanes: list, stage_map: dict | None) -> bool:
    """True iff the caller must skip its ordinary lane-move this run; False iff the card's current
    lane already maps back to "Intake" itself, so the ordinary lane-move can proceed (it will find
    the card already parked there and no-op harmlessly).

    True covers two cases: the card sits somewhere a human clearly moved it to (not Intake) --
    _promote_issue attempts to vet it onto the Project at that stage -- or the current lane can't be
    resolved back to a known stage at all, in which case we hold at Intake without moving anything
    rather than guess. Either way, a failed promotion still returns True: the demotion trap holds
    regardless of _promote_issue's own success or failure. Never raises.
    """
    reverse = agileplace.stage_for_lane(current_lane_id, stage_map, lanes)
    if reverse is None:
        print(f"WARN  [{key}] card's current lane doesn't map back to a recognized stage -- "
              "holding at Intake without moving it (cannot tell if a human already vetted it)")
        return True
    if reverse == "Intake":
        return False
    _promote_issue(cfg, apply, issue, key, reverse)
    return True


def _promote_issue(cfg: dict, apply: bool, issue: dict, key: str, target_stage: str) -> None:
    """Vet the issue onto the configured Project at `target_stage`. Never raises; a failure at
    either step leaves the card exactly where the human placed it -- apply_latch's own True return
    already guarantees the caller skips the ordinary lane-move regardless of what happens here."""
    item_id = ghproject.add_item(cfg, apply, issue["url"])
    if item_id is None:
        print(f"WARN  [{key}] could not add issue to the Project -- leaving card where it is")
        return
    ok = ghproject.set_item_status(cfg, apply, item_id, target_stage)
    if not ok:
        print(f"WARN  [{key}] could not set Project Status to '{target_stage}' -- "
              "leaving card where it is")
        return
    print(f"{'latch ' if apply else 'DRY  '} [{key}] vetted -> Status '{target_stage}'")
