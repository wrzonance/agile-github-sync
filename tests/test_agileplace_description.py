"""Unit tests for agileplace_description.py's card-description read/write helpers (issue #65 Task
1/7). No network -- pins the JSON Patch shape and the lazy-refetch fallback contract. Split out of
test_agileplace.py alongside the card_description/op_description extraction (review finding: keep
agileplace.py, and its test file, from growing past the project's file-size cap). Run: pytest -q
"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agileplace_description import card_description, op_description  # noqa: E402

CFG = {"token": "t", "host": "h", "board_id": "b1"}


def test_op_description_returns_single_replace_op():
    """No validation here (description_sync owns write-vs-not) -- matches op_custom_id's shape."""
    op = op_description("<p>hello</p>")
    assert op == {"op": "replace", "path": "/description", "value": "<p>hello</p>"}


def test_card_description_returns_present_description_without_network_call():
    card = {"id": "1", "description": "<p>hi</p>"}
    with patch("agileplace_description.agileplace.get_card") as get_card_mock:
        result = card_description(CFG, card)
    get_card_mock.assert_not_called()
    assert result == "<p>hi</p>"


def test_card_description_present_but_empty_string_takes_zero_io_path():
    """A card literally carrying description="" is a real, current empty description -- NOT
    'unknown' -- and must not trigger the lazy get_card fallback (see struct #7 in the design:
    every fixture reaching this must either carry the key or explicitly mock get_card)."""
    card = {"id": "1", "description": ""}
    with patch("agileplace_description.agileplace.get_card") as get_card_mock:
        result = card_description(CFG, card)
    get_card_mock.assert_not_called()
    assert result == ""


def test_card_description_present_but_none_normalizes_to_empty_string():
    card = {"id": "1", "description": None}
    with patch("agileplace_description.agileplace.get_card") as get_card_mock:
        result = card_description(CFG, card)
    get_card_mock.assert_not_called()
    assert result == ""


def test_card_description_missing_key_falls_back_to_lazy_get_card():
    """list_cards() never returns description (no field-selection params sent) -- a card summary
    missing the key entirely must trigger exactly one get_card refetch."""
    card = {"id": "77"}
    with patch("agileplace_description.agileplace.get_card",
               return_value={"id": "77", "description": "<p>fresh</p>"}) as get_card_mock:
        result = card_description(CFG, card)
    get_card_mock.assert_called_once_with(CFG, "77")
    assert result == "<p>fresh</p>"


def test_card_description_lazy_fallback_normalizes_missing_description_to_empty_string():
    card = {"id": "78"}
    with patch("agileplace_description.agileplace.get_card", return_value={"id": "78"}):
        result = card_description(CFG, card)
    assert result == ""


def test_card_description_never_mutates_input_card():
    card = {"id": "1", "description": "<p>hi</p>"}
    before = dict(card)
    card_description(CFG, card)
    assert card == before
