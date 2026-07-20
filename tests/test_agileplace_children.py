"""Boundary tests for authoritative AgilePlace child-connection reads. No network access."""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agileplace import card_child_ids  # noqa: E402

CFG = {"token": "t", "host": "h", "board_id": "b1"}


def test_card_child_ids_paginates_documented_response_to_complete_snapshot():
    pages = [
        {
            "cards": [{"id": "c1"}, {"id": 2}],
            "pageMeta": {"offset": 0, "limit": 2, "totalRecords": 3},
        },
        {
            "cards": [{"id": "c3"}],
            "pageMeta": {"offset": 2, "limit": 2, "totalRecords": 3},
        },
    ]

    with patch("agileplace.api", side_effect=pages) as api_mock:
        child_ids = card_child_ids(CFG, "parent")

    assert child_ids == frozenset({"c1", "2", "c3"})
    assert [call.args[2] for call in api_mock.call_args_list] == [
        "card/parent/connection/children",
        "card/parent/connection/children",
    ]
    assert [call.kwargs["params"]["offset"] for call in api_mock.call_args_list] == [0, 2]
    assert all(call.kwargs["params"]["limit"] == 200 for call in api_mock.call_args_list)


def test_card_child_ids_successful_empty_is_distinct_from_failure_and_quotes_parent_id():
    response = {
        "cards": [],
        "pageMeta": {"offset": 0, "limit": 25, "totalRecords": 0},
    }

    with patch("agileplace.api", return_value=response) as api_mock:
        child_ids = card_child_ids(CFG, "parent/../../?")

    assert child_ids == frozenset()
    assert api_mock.call_args.args[2] == "card/parent%2F..%2F..%2F%3F/connection/children"


@pytest.mark.parametrize(
    ("response", "warning"),
    [
        (None, "response is null"),
        ([], "response is list"),
        ({}, "missing cards"),
        ({"cards": [], "pageMeta": []}, "pageMeta is list"),
        ({"cards": {}, "pageMeta": {}}, "cards is dict"),
        (
            {"cards": [], "pageMeta": {"offset": 0, "limit": 25}},
            "invalid totalRecords",
        ),
        (
            {"cards": [{"title": "missing id"}],
             "pageMeta": {"offset": 0, "limit": 25, "totalRecords": 1}},
            "child card has invalid id",
        ),
    ],
)
def test_card_child_ids_malformed_response_is_non_authoritative(response, warning, capsys):
    with patch("agileplace.api", return_value=response):
        child_ids = card_child_ids(CFG, "parent")

    assert child_ids is None
    out = capsys.readouterr().out
    assert "WARN  card parent child-card read FAILED" in out
    assert warning in out


def test_card_child_ids_transport_failure_is_non_authoritative(capsys):
    with patch("agileplace.api", side_effect=SystemExit("AgilePlace unavailable")):
        child_ids = card_child_ids(CFG, "parent")

    assert child_ids is None
    assert "WARN  card parent child-card read FAILED: AgilePlace unavailable" in capsys.readouterr().out


def test_card_child_ids_incomplete_pagination_is_non_authoritative(capsys):
    pages = [
        {
            "cards": [{"id": "c1"}, {"id": "c2"}],
            "pageMeta": {"offset": 0, "limit": 2, "totalRecords": 3},
        },
        {
            "cards": [],
            "pageMeta": {"offset": 2, "limit": 2, "totalRecords": 3},
        },
    ]

    with patch("agileplace.api", side_effect=pages):
        child_ids = card_child_ids(CFG, "parent")

    assert child_ids is None
    assert "ended at 2 before totalRecords 3" in capsys.readouterr().out
