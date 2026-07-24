"""Unit tests for agileplace_comments.py (issue #66 Task 2/8): AP comment I/O -- list/create/
update/delete plus shape-tolerant normalization. No network -- mocks agileplace.api/mutate/get_card
only, exactly like test_agileplace_description.py's split-module precedent.

Pins:
  - list_comments: primary endpoint success (bare list AND {"comments": [...]} wrapper), fallback
    to get_card()["comments"] on ANY primary shape surprise, and a hard raise when NEITHER shape
    yields a usable list.
  - create/update/delete: apply=False takes the zero-network dry-run path; apply=True reaches the
    API and returns the documented shape (a normalized ApComment dict, or True/False).
  - _normalize_ap_comment: raises ValueError on a missing/non-numeric id; author (name -> email ->
    id) and created/edited timestamps degrade to None instead of raising, for any malformed shape.
  - _normalize_ap_comment's created/edited fields funnel through comment_sync._parse_timestamp
    (never raise, garbage/absent -> None) -- the exact invariant this task's RED step pins.

Run: pytest -q tests/test_agileplace_comments.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agileplace_comments import (  # noqa: E402
    _normalize_ap_comment,
    create_comment,
    delete_comment,
    list_comments,
    update_comment,
)

CFG = {"token": "t", "host": "h", "board_id": "b1"}


# --- _normalize_ap_comment: id boundary -------------------------------------------------------

def test_normalize_ap_comment_raises_on_missing_id():
    with pytest.raises(ValueError):
        _normalize_ap_comment({"text": "hi"})


def test_normalize_ap_comment_raises_on_non_numeric_id():
    with pytest.raises(ValueError):
        _normalize_ap_comment({"id": "not-a-number", "text": "hi"})


def test_normalize_ap_comment_raises_on_bool_id():
    """bool is a subclass of int in Python -- must be explicitly rejected, not silently accepted."""
    with pytest.raises(ValueError):
        _normalize_ap_comment({"id": True, "text": "hi"})


def test_normalize_ap_comment_raises_on_non_dict_payload():
    with pytest.raises(ValueError):
        _normalize_ap_comment(["not", "a", "dict"])


def test_normalize_ap_comment_accepts_valid_int_id():
    result = _normalize_ap_comment({"id": 42, "text": "hi"})
    assert result["id"] == 42


def test_normalize_ap_comment_accepts_digit_string_id_and_coerces_to_int():
    """The live POST /io/card/{id}/comment response serializes the new comment id as a STRING of
    digits (e.g. '1234567890' -- confirmed against a real tenant 2026-07-23, see API-VALIDATION.md).
    The normalizer must coerce it to int so the ledger's gh_id/ap_id int|None contract still holds."""
    result = _normalize_ap_comment({"id": "1234567890", "text": "hi"})
    assert result["id"] == 1234567890
    assert isinstance(result["id"], int)


def test_normalize_ap_comment_raises_on_float_id():
    """A float is numeric but not an integer id -- reject rather than silently truncate."""
    with pytest.raises(ValueError):
        _normalize_ap_comment({"id": 3.14, "text": "hi"})


def test_normalize_ap_comment_raises_on_non_digit_string_id():
    with pytest.raises(ValueError):
        _normalize_ap_comment({"id": "12x3", "text": "hi"})


# --- _normalize_ap_comment: body -------------------------------------------------------------

def test_normalize_ap_comment_reads_text_as_body():
    result = _normalize_ap_comment({"id": 1, "text": "<p>hello</p>"})
    assert result["body"] == "<p>hello</p>"


def test_normalize_ap_comment_missing_body_normalizes_to_empty_string():
    result = _normalize_ap_comment({"id": 1})
    assert result["body"] == ""


def test_normalize_ap_comment_non_string_body_normalizes_to_empty_string():
    result = _normalize_ap_comment({"id": 1, "text": 12345})
    assert result["body"] == ""


# --- _normalize_ap_comment: author name -> email -> id, never raises --------------------------

def test_normalize_ap_comment_author_prefers_full_name():
    raw = {"id": 1, "createdBy": {"fullName": "Ada Lovelace", "emailAddress": "ada@example.com",
                                   "id": "u1"}}
    result = _normalize_ap_comment(raw)
    assert (result["author_name"], result["author_email"], result["author_id"]) == (
        "Ada Lovelace", "ada@example.com", "u1")


def test_normalize_ap_comment_author_falls_back_to_email_when_name_absent():
    raw = {"id": 1, "createdBy": {"emailAddress": "ada@example.com", "id": "u1"}}
    result = _normalize_ap_comment(raw)
    assert result["author_name"] is None
    assert result["author_email"] == "ada@example.com"
    assert result["author_id"] == "u1"


def test_normalize_ap_comment_author_falls_back_to_id_when_name_and_email_absent():
    raw = {"id": 1, "createdBy": {"id": 7}}
    result = _normalize_ap_comment(raw)
    assert (result["author_name"], result["author_email"], result["author_id"]) == (None, None, "7")


@pytest.mark.parametrize("created_by", [None, "bare-id-string", 123, [], {}, True])
def test_normalize_ap_comment_author_all_absent_yields_all_none_never_raises(created_by):
    result = _normalize_ap_comment({"id": 1, "createdBy": created_by})
    assert (result["author_name"], result["author_email"], result["author_id"]) == (None, None, None)


def test_normalize_ap_comment_author_blank_name_falls_back_to_email():
    raw = {"id": 1, "createdBy": {"fullName": "   ", "emailAddress": "ada@example.com"}}
    result = _normalize_ap_comment(raw)
    assert result["author_name"] is None
    assert result["author_email"] == "ada@example.com"


# --- _normalize_ap_comment: created/edited funnel through _parse_timestamp, never raise --------

def test_normalize_ap_comment_keeps_parseable_created_on_verbatim():
    raw = {"id": 1, "createdOn": "2024-01-15T10:30:00Z"}
    result = _normalize_ap_comment(raw)
    assert result["created"] == "2024-01-15T10:30:00Z"


def test_normalize_ap_comment_keeps_parseable_edited_verbatim():
    raw = {"id": 1, "lastModified": "2024-01-16T10:30:00Z"}
    result = _normalize_ap_comment(raw)
    assert result["edited"] == "2024-01-16T10:30:00Z"


@pytest.mark.parametrize("timestamp_key", ["lastModified", "modifiedOn", "updatedOn", "editedOn"])
def test_normalize_ap_comment_accepts_any_known_edited_key_alias(timestamp_key):
    raw = {"id": 1, timestamp_key: "2024-01-16T10:30:00Z"}
    result = _normalize_ap_comment(raw)
    assert result["edited"] == "2024-01-16T10:30:00Z"


@pytest.mark.parametrize("blank_or_nonstring", [None, "", "   ", 12345, [], {}])
def test_normalize_ap_comment_blank_or_nonstring_created_is_none_never_raises(blank_or_nonstring):
    result = _normalize_ap_comment({"id": 1, "createdOn": blank_or_nonstring})
    assert result["created"] is None


@pytest.mark.parametrize("unparseable", ["not-a-timestamp", "2024-13-45T99:99:99Z", "Tuesday"])
def test_normalize_ap_comment_keeps_present_but_unparseable_created_raw(unparseable):
    """A present-but-unparseable timestamp STRING is kept verbatim (not nulled) so the planner's
    comment_sync._timestamp_warning can surface an unrecognized AgilePlace timestamp format (issue
    #66 Codex P2 #8); every comparison site parses at use, so it's still excluded from drift."""
    result = _normalize_ap_comment({"id": 1, "createdOn": unparseable})
    assert result["created"] == unparseable


def test_normalize_ap_comment_missing_created_and_edited_are_none():
    result = _normalize_ap_comment({"id": 1})
    assert result["created"] is None
    assert result["edited"] is None


def test_normalize_ap_comment_never_mutates_input_dict():
    raw = {"id": 1, "text": "hi", "createdBy": {"fullName": "Ada"}, "createdOn": "2024-01-15T10:30:00Z"}
    before = {"id": 1, "text": "hi", "createdBy": {"fullName": "Ada"}, "createdOn": "2024-01-15T10:30:00Z"}
    _normalize_ap_comment(raw)
    assert raw == before


# --- list_comments: primary success shapes ----------------------------------------------------

def test_list_comments_parses_bare_list_response():
    with patch("agileplace_comments.agileplace.api",
               return_value=[{"id": 1, "text": "hi"}]) as api_mock:
        result = list_comments(CFG, "card-1")
    api_mock.assert_called_once_with(CFG, "GET", "card/card-1/comment")
    assert result == [_normalize_ap_comment({"id": 1, "text": "hi"})]


def test_list_comments_parses_wrapped_comments_response():
    with patch("agileplace_comments.agileplace.api",
               return_value={"comments": [{"id": 1, "text": "hi"}, {"id": 2, "text": "yo"}]}):
        result = list_comments(CFG, "card-1")
    assert [c["id"] for c in result] == [1, 2]


def test_list_comments_empty_list_is_a_real_zero_comment_result():
    with patch("agileplace_comments.agileplace.api", return_value=[]):
        result = list_comments(CFG, "card-1")
    assert result == []


def test_list_comments_quotes_card_id_in_path():
    with patch("agileplace_comments.agileplace.api", return_value=[]) as api_mock:
        list_comments(CFG, "weird/id")
    api_mock.assert_called_once_with(CFG, "GET", "card/weird%2Fid/comment")


# --- list_comments: fallback to get_card()["comments"] on primary shape surprise --------------

def test_list_comments_falls_back_to_get_card_on_non_list_non_wrapped_primary_shape():
    with (
        patch("agileplace_comments.agileplace.api", return_value={"unexpected": "shape"}),
        patch("agileplace_comments.agileplace.get_card",
              return_value={"id": "card-1", "comments": [{"id": 9, "text": "fallback"}]}) as gc_mock,
    ):
        result = list_comments(CFG, "card-1")
    gc_mock.assert_called_once_with(CFG, "card-1")
    assert [c["id"] for c in result] == [9]


def test_list_comments_falls_back_to_get_card_on_primary_systemexit():
    with (
        patch("agileplace_comments.agileplace.api", side_effect=SystemExit("boom")),
        patch("agileplace_comments.agileplace.get_card",
              return_value={"id": "card-1", "comments": [{"id": 9, "text": "fallback"}]}),
    ):
        result = list_comments(CFG, "card-1")
    assert [c["id"] for c in result] == [9]


def test_list_comments_falls_back_when_a_primary_item_fails_to_normalize():
    with (
        patch("agileplace_comments.agileplace.api", return_value=[{"text": "no id"}]),
        patch("agileplace_comments.agileplace.get_card",
              return_value={"id": "card-1", "comments": [{"id": 9, "text": "fallback"}]}),
    ):
        result = list_comments(CFG, "card-1")
    assert [c["id"] for c in result] == [9]


def test_list_comments_raises_when_both_shapes_fail():
    with (
        patch("agileplace_comments.agileplace.api", return_value={"unexpected": "shape"}),
        patch("agileplace_comments.agileplace.get_card",
              return_value={"id": "card-1", "comments": "not-a-list"}),
        pytest.raises(SystemExit),
    ):
        list_comments(CFG, "card-1")


def test_list_comments_raises_when_get_card_itself_fails():
    with (
        patch("agileplace_comments.agileplace.api", side_effect=SystemExit("boom")),
        patch("agileplace_comments.agileplace.get_card", side_effect=SystemExit("also boom")),
        pytest.raises(SystemExit),
    ):
        list_comments(CFG, "card-1")


# --- create_comment ----------------------------------------------------------------------------

def test_create_comment_dry_run_makes_zero_network_calls_and_returns_none():
    with patch("agileplace_comments.agileplace.api") as api_mock:
        result = create_comment(CFG, False, "card-1", "<p>hi</p>")
    api_mock.assert_not_called()
    assert result is None


def test_create_comment_apply_mode_sends_text_body_and_returns_normalized_comment():
    with patch("agileplace_comments.agileplace.api",
               return_value={"id": 5, "text": "<p>hi</p>"}) as api_mock:
        result = create_comment(CFG, True, "card-1", "<p>hi</p>")
    api_mock.assert_called_once_with(CFG, "POST", "card/card-1/comment",
                                     body={"text": "<p>hi</p>"}, headers=None)
    assert result["id"] == 5
    assert result["body"] == "<p>hi</p>"


def test_create_comment_apply_mode_raises_when_response_carries_no_id():
    with patch("agileplace_comments.agileplace.api", return_value={"text": "<p>hi</p>"}):
        with pytest.raises(ValueError):
            create_comment(CFG, True, "card-1", "<p>hi</p>")


# --- update_comment ------------------------------------------------------------------------

def test_update_comment_dry_run_makes_zero_network_calls_and_returns_false():
    with patch("agileplace_comments.agileplace.api") as api_mock:
        result = update_comment(CFG, False, "card-1", 5, "<p>edited</p>")
    api_mock.assert_not_called()
    assert result is False


def test_update_comment_apply_mode_sends_put_and_returns_true():
    with patch("agileplace_comments.agileplace.api", return_value={}) as api_mock:
        result = update_comment(CFG, True, "card-1", 5, "<p>edited</p>")
    api_mock.assert_called_once_with(CFG, "PUT", "card/card-1/comment/5",
                                     body={"text": "<p>edited</p>"}, headers=None)
    assert result is True


# --- delete_comment ------------------------------------------------------------------------

def test_delete_comment_dry_run_makes_zero_network_calls_and_returns_false():
    with patch("agileplace_comments.agileplace.api") as api_mock:
        result = delete_comment(CFG, False, "card-1", 5)
    api_mock.assert_not_called()
    assert result is False


def test_delete_comment_apply_mode_sends_delete_and_returns_true():
    with patch("agileplace_comments.agileplace.api", return_value={}) as api_mock:
        result = delete_comment(CFG, True, "card-1", 5)
    api_mock.assert_called_once_with(CFG, "DELETE", "card/card-1/comment/5",
                                     body=None, headers=None)
    assert result is True
