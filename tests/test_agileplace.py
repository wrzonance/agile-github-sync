"""Unit tests for agileplace.py's pure op-builders. No network or gh -- pins the JSON Patch shapes
the live sync depends on. Run: pytest -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agileplace import card_block_reason, card_is_blocked, ops_blocked  # noqa: E402


def test_ops_blocked_block_with_reason():
    ops = ops_blocked(True, "waiting on design review")
    assert len(ops) == 2
    assert ops[0] == {"op": "replace", "path": "/isBlocked", "value": True}
    assert ops[1] == {"op": "add", "path": "/blockReason", "value": "waiting on design review"}
    # dict `==` doesn't distinguish bool from int (1 == True), so pin the type explicitly.
    assert ops[0]["value"] is True


def test_ops_blocked_unblock_clears_both():
    ops = ops_blocked(False, None)
    assert len(ops) == 2
    assert ops[0] == {"op": "replace", "path": "/isBlocked", "value": False}
    assert ops[1] == {"op": "add", "path": "/blockReason", "value": ""}
    # dict `==` doesn't distinguish bool from int (0 == False), so pin the type explicitly.
    assert ops[0]["value"] is False


def test_ops_blocked_true_with_no_reason_coerces_empty_string():
    """stages.blocked_reason() can return blocked=True with no reason text -- blockReason must
    still be a str, never None."""
    ops = ops_blocked(True, None)
    assert ops[1]["value"] == ""
    assert isinstance(ops[1]["value"], str)


def test_ops_blocked_op_verbs():
    ops = ops_blocked(True, "x")
    assert ops[0]["op"] == "replace"
    assert ops[1]["op"] == "add"


def test_ops_blocked_never_uses_nested_blockedstatus_path():
    """Guards against reintroducing the bug this test file exists to catch: the nested path is the
    read shape only (card_is_blocked/card_block_reason), never a write path."""
    for ops in (ops_blocked(True, "reason"), ops_blocked(False, None)):
        for op in ops:
            assert "blockedStatus" not in op["path"]


def test_ops_blocked_unblock_forces_empty_reason():
    """An unblocked card carries no reason: even when a truthy reason is passed, unblocking must
    write "" to /blockReason -- never the self-contradictory isBlocked=False + non-empty reason."""
    ops = ops_blocked(False, "some reason")
    assert ops[1] == {"op": "add", "path": "/blockReason", "value": ""}


def test_card_is_blocked_reads_nested_blockedstatus_isblocked():
    """card_is_blocked is the READ-side counterpart to ops_blocked's write-side flat shape -- it
    must keep reading the nested blockedStatus.isBlocked field the AgilePlace API actually returns,
    never the flat /isBlocked write path."""
    assert card_is_blocked({"blockedStatus": {"isBlocked": True, "reason": "x"}}) is True
    assert card_is_blocked({"blockedStatus": {"isBlocked": False, "reason": ""}}) is False
    assert card_is_blocked({}) is False
    assert card_is_blocked({"isBlocked": True}) is False  # flat write shape must not be read


def test_card_block_reason_reads_nested_blockedstatus_reason():
    """card_block_reason is the READ-side counterpart to ops_blocked's write-side flat shape -- it
    must keep reading the nested blockedStatus.reason field, never the flat /blockReason write path,
    and must coerce a missing/falsy reason to ''."""
    assert card_block_reason({"blockedStatus": {"isBlocked": True, "reason": "waiting"}}) == "waiting"
    assert card_block_reason({"blockedStatus": {"isBlocked": False, "reason": None}}) == ""
    assert card_block_reason({}) == ""
    assert card_block_reason({"blockReason": "waiting"}) == ""  # flat write shape must not be read
