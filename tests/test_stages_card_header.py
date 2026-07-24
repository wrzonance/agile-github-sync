"""issue #93: the customId header format (issue_card_header) and its inverse (header_match_key).

The header is the string WRITTEN to a card's customId; issue_custom_id() remains the internal
match key used by matching, the coherence fence, and intake. Round-trip invariant:
header_match_key(issue_card_header(i)) == issue_custom_id(i) for every issue shape.
Run: pytest -q
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stages import header_match_key, issue_card_header, issue_custom_id  # noqa: E402


def test_keyed_task_header_carries_key_and_issue_number():
    assert issue_card_header({"number": 5, "title": "[0C1] RFC 9457 errors"}) == \
        "0C1 (GitHub Issue #5)"


def test_epic_header_uses_the_same_uniform_rule():
    assert issue_card_header({"number": 12, "title": "[EP-0C] Some epic"}) == \
        "EP-0C (GitHub Issue #12)"


def test_unkeyed_header_is_the_bare_github_reference():
    """No redundant '5 (GitHub Issue #5)' -- the suffix alone carries the info."""
    assert issue_card_header({"number": 5, "title": "No key here"}) == "GitHub Issue #5"


def test_match_key_strips_the_header_suffix():
    assert header_match_key("0C1 (GitHub Issue #5)") == "0C1"


def test_match_key_maps_the_bare_header_to_the_issue_number():
    assert header_match_key("GitHub Issue #5") == "5"


def test_match_key_passes_old_format_through_unchanged():
    assert header_match_key("0C1") == "0C1"


def test_match_key_normalizes_empty_and_none_to_empty_string():
    assert header_match_key("") == ""
    assert header_match_key(None) == ""


def test_match_key_strips_only_the_final_suffix():
    assert header_match_key("A (GitHub Issue #5) (GitHub Issue #6)") == "A (GitHub Issue #5)"


def test_match_key_ignores_a_suffix_not_at_the_end():
    assert header_match_key("0C1 (GitHub Issue #5) trailing") == "0C1 (GitHub Issue #5) trailing"


def test_match_key_ignores_a_non_digit_issue_number():
    assert header_match_key("0C1 (GitHub Issue #x)") == "0C1 (GitHub Issue #x)"


@pytest.mark.parametrize("issue", [
    {"number": 5, "title": "[0C1] task"},
    {"number": 12, "title": "[EP-0C] epic"},
    {"number": 7, "title": "unkeyed title"},
])
def test_round_trip_invariant(issue):
    assert header_match_key(issue_card_header(issue)) == issue_custom_id(issue)
