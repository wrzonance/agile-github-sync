"""Selection tests for the one-shot flag cleanup (issue #57 Phase 2).

The only thing that can go wrong destructively is clearing a HUMAN's Blocked flag, so
the signature match is pinned tightly here. Delete this file together with
clear_legacy_blocked_flags.py once the board is clean.

Run: pytest -q
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clear_legacy_blocked_flags import is_sync_authored  # noqa: E402


def _card(blocked: bool, reason: str) -> dict:
    return {"id": "C1", "blockedStatus": {"isBlocked": blocked, "reason": reason}}


@pytest.mark.parametrize("reason", [
    "Blocked by #31",
    "Blocked by #33, #65, #66",
])
def test_exact_sync_signatures_are_cleared(reason):
    assert is_sync_authored(_card(True, reason))


@pytest.mark.parametrize("reason", [
    "",                                  # flagged with no reason -- human, or unknown origin
    "waiting on vendor",                 # plainly human
    "blocked by #31",                    # wrong case -- not the sync's exact text
    "Blocked by #31 and the outage",     # human elaboration
    "Blocked by #31,#65",                # wrong separator -- not the sync's exact text
    "Also Blocked by #31",               # prefix noise
])
def test_anything_else_is_treated_as_human_and_kept(reason):
    assert not is_sync_authored(_card(True, reason))


def test_unblocked_cards_are_never_targets_even_with_leftover_reason_text():
    assert not is_sync_authored(_card(False, "Blocked by #31"))
