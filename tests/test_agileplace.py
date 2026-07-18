"""Unit tests for agileplace.py's pure op-builders. No network or gh -- pins the JSON Patch shapes
the live sync depends on. Run: pytest -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agileplace import ops_blocked  # noqa: E402


def test_ops_blocked_block_with_reason():
    ops = ops_blocked(True, "waiting on design review")
    assert len(ops) == 2
    assert ops[0] == {"op": "replace", "path": "/isBlocked", "value": True}
    assert ops[1] == {"op": "add", "path": "/blockReason", "value": "waiting on design review"}


def test_ops_blocked_unblock_clears_both():
    ops = ops_blocked(False, None)
    assert len(ops) == 2
    assert ops[0] == {"op": "replace", "path": "/isBlocked", "value": False}
    assert ops[1] == {"op": "add", "path": "/blockReason", "value": ""}


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
