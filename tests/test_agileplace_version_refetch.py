"""Regression tests for version-less card refetches and state-safe PATCH aborts (issue #29)."""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agileplace import _card_with_version, patch_card  # noqa: E402

CFG = {"token": "t", "host": "h", "board_id": "b1"}


def test_card_with_version_refetches_and_returns_validated_fresh_snapshot():
    """A fresh version must stay attached to the fresh resource, never a stale card snapshot."""
    card = {"id": "42", "title": "stale", "laneId": "L1"}
    fresh_card = {"id": "42", "title": "fresh", "laneId": "L1", "version": 9}
    refetched = {"card": fresh_card}
    ops = [{"op": "replace", "path": "/laneId", "value": "L2"}]

    with patch("agileplace.api", return_value=refetched) as api_mock:
        result = _card_with_version(CFG, True, card, ops)

    api_mock.assert_called_once_with(CFG, "GET", "card/42")
    assert result == fresh_card
    assert result is not card
    assert "version" not in card


@pytest.mark.parametrize(
    ("card_fields", "fresh_fields", "ops"),
    [
        (
            {"laneId": "L1"},
            {"laneId": "L2"},
            [{"op": "replace", "path": "/laneId", "value": "L3"}],
        ),
        (
            {"tags": ["a"]},
            {"tags": ["a", "b"]},
            [{"op": "add", "path": "/tags/-", "value": "new"}],
        ),
        (
            {"customId": "OLD"},
            {"customId": "CONCURRENT"},
            [{"op": "replace", "path": "/customId", "value": "NEW"}],
        ),
        (
            {"plannedStart": "2026-01-01"},
            {"plannedStart": "2026-01-02"},
            [{"op": "replace", "path": "/plannedStart", "value": "2026-01-03"}],
        ),
        (
            {"blockedStatus": {"isBlocked": False, "reason": ""}},
            {"blockedStatus": {"isBlocked": True, "reason": "concurrent"}},
            [
                {"op": "replace", "path": "/isBlocked", "value": True},
                {"op": "add", "path": "/blockReason", "value": "sync"},
            ],
        ),
    ],
)
def test_refetch_refuses_ops_when_touched_snapshot_changed(card_fields, fresh_fields, ops, capsys):
    """A refetch may supply a version only when every field targeted by stale ops is unchanged."""
    card = {"id": "7", **card_fields}
    refetched = {"card": {"id": "7", "version": 5, **fresh_fields}}

    with patch("agileplace.api", return_value=refetched):
        result = _card_with_version(CFG, True, card, ops)

    assert result is None
    warning = capsys.readouterr().out
    assert "card 7" in warning
    assert "changed between snapshot and version refetch" in warning


@pytest.mark.parametrize(
    ("fresh_card", "ops", "warning"),
    [
        (
            {"id": "other", "version": 5, "laneId": "L1"},
            [{"op": "replace", "path": "/laneId", "value": "L2"}],
            "different card id",
        ),
        (
            {"id": "7", "version": 5, "laneId": "L1"},
            [{"op": "replace", "path": "/futureField", "value": "x"}],
            "stale ops",
        ),
    ],
)
def test_refetch_fails_closed_when_snapshot_cannot_be_validated(fresh_card, ops, warning, capsys):
    card = {"id": "7", "laneId": "L1"}

    with patch("agileplace.api", return_value={"card": fresh_card}):
        result = _card_with_version(CFG, True, card, ops)

    assert result is None
    assert warning in capsys.readouterr().out


def test_patch_card_aborts_after_double_miss_without_sending_patch(capsys):
    """A skipped apply PATCH is a failed write and must abort before callers persist merge state."""
    card = {"id": "42"}
    ops = [{"op": "replace", "path": "/laneId", "value": "L"}]

    with patch("agileplace.api", return_value={"card": {"id": "42"}}) as api_mock:
        with pytest.raises(SystemExit, match="card 42 PATCH"):
            patch_card(CFG, True, card, ops)

    api_mock.assert_called_once_with(CFG, "GET", "card/42")
    warn_lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("WARN")]
    assert len(warn_lines) == 1
    assert "42" in warn_lines[0]
