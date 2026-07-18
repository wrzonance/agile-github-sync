"""Unit tests for ghproject's field-key matching: camelCase resolution and key-presence detection
used by the date-sync unmatched-kind guard (issue #6). No network or gh -- pure functions only.
Run: pytest -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ghproject import _camel, _field, _field_candidates, _field_key_seen  # noqa: E402


# --- _camel -----------------------------------------------------------------

def test_camel_lowercases_only_first_rune():
    assert _camel("Start Date") == "start Date"


def test_camel_single_word():
    assert _camel("Status") == "status"


def test_camel_falsy_name_returned_unchanged():
    assert _camel("") == ""
    assert _camel(None) is None


# --- _field_candidates -------------------------------------------------------

def test_field_candidates_order_and_contents():
    assert _field_candidates("Start Date") == ("Start Date", "start date", "start Date")


def test_field_candidates_includes_alts_with_no_dedup():
    # alts are appended verbatim, even if they duplicate an earlier candidate.
    assert _field_candidates("Status", "status") == ("Status", "status", "status", "status")


# --- _field (superset of the old 2-variant probe) ---------------------------

def test_field_matches_multi_word_camel_case_key():
    # gh flattens "Start Date" -> "start Date" (only the first rune lowercased). The old 2-variant
    # probe (name, name.lower()) could never match this; _field must.
    item = {"start Date": "2026-01-02"}
    assert _field(item, "Start Date") == "2026-01-02"


def test_field_still_matches_exact_name():
    item = {"Status": "In progress"}
    assert _field(item, "Status") == "In progress"


def test_field_still_matches_lowercase_name():
    item = {"status": "In progress"}
    assert _field(item, "Status", "status") == "In progress"


def test_field_still_matches_alt():
    item = {"status": "In progress"}
    assert _field(item, "Status", "status") == "In progress"


def test_field_returns_none_when_value_empty_or_missing():
    assert _field({"Start Date": ""}, "Start Date") is None
    assert _field({}, "Start Date") is None


# --- _field_key_seen (presence-only, distinct from _field's value check) ----

def test_field_key_seen_true_when_key_present_with_value():
    assert _field_key_seen({"start Date": "2026-01-02"}, "Start Date") is True


def test_field_key_seen_true_when_key_present_but_empty():
    # present-but-empty is a genuinely-unset field, NOT a missing/mismatched key.
    assert _field_key_seen({"start Date": ""}, "Start Date") is True
    assert _field_key_seen({"start Date": None}, "Start Date") is True


def test_field_key_seen_false_when_key_absent():
    assert _field_key_seen({"other": "x"}, "Start Date") is False
    assert _field_key_seen({}, "Start Date") is False
