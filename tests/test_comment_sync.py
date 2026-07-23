"""Unit tests for comment_sync.py's module scaffold (issue #66 Task 1): the shared timestamp
normalization helper that both the drift check and orphan-adjacency gap computation funnel through
before ever comparing two comment timestamps. Pins one invariant at the boundary: totality --
_parse_timestamp never raises for any input, degrading unparseable/absent input to None instead.

Run: pytest -q
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import comment_sync  # noqa: E402


# --- totality: never raises, whatever shape shows up -------------------------------------------

@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "not-a-timestamp",
        "2024-13-45T99:99:99Z",  # syntactically ISO-ish, semantically invalid
        "12345",
        "Tuesday, 15 Jan 2024",
        123,  # wrong type entirely -- must not raise, not just "not a str"
        123.456,
        [],
        {},
    ],
)
def test_parse_timestamp_never_raises_and_degrades_to_none(raw):
    assert comment_sync._parse_timestamp(raw) is None


# --- successful parses: GH's ISO-8601 (Z suffix) and offset/naive variants ---------------------

def test_parse_timestamp_parses_gh_style_z_suffix():
    result = comment_sync._parse_timestamp("2024-01-15T10:30:00Z")

    assert result == datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)


def test_parse_timestamp_result_is_tz_aware():
    result = comment_sync._parse_timestamp("2024-01-15T10:30:00Z")

    assert result.tzinfo is not None


def test_parse_timestamp_normalizes_explicit_offset_to_utc():
    result = comment_sync._parse_timestamp("2024-01-15T05:30:00-05:00")

    assert result == datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)


def test_parse_timestamp_assumes_utc_for_naive_input():
    result = comment_sync._parse_timestamp("2024-01-15T10:30:00")

    assert result == datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)


def test_parse_timestamp_two_equivalent_instants_compare_equal_after_parsing():
    """The whole point of the helper: two representations of the same instant from two different
    sides (GH's Z-suffixed UTC vs. AP's hypothetical offset form) must compare equal once both have
    gone through _parse_timestamp -- raw lexical string comparison can't guarantee that."""
    gh_side = comment_sync._parse_timestamp("2024-01-15T10:30:00Z")
    ap_side = comment_sync._parse_timestamp("2024-01-15T05:30:00-05:00")

    assert gh_side == ap_side
