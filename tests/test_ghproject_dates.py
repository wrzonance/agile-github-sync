"""Unit tests for ghproject's field-key matching: camelCase resolution and key-presence detection
used by the date-sync unmatched-kind guard (issue #6). No network or gh -- pure functions only.
Run: pytest -q
"""
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ghproject  # noqa: E402
from ghproject import (_camel, _field, _field_candidates,  # noqa: E402
                       _field_key_seen, unmatched_date_kinds)


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


# --- items() / items_and_raw() equivalence -----------------------------------
# items()'s external contract must not change: it is used by issue_status_map and
# issue_dates_map, so items(cfg) == items_and_raw(cfg)[0] for every input.

RAW_ITEMS = [
    {"id": "PVTI_1", "content": {"type": "Issue", "number": 5, "url": "https://github.com/o/r/issues/5"},
     "status": "In progress", "Start": "2026-01-02", "Target": "2026-01-09"},
]


def _cfg(**overrides):
    p = {"owner": "acme", "number": "7", "status_field": "Status",
         "start_field": "Start", "target_field": "Target"}
    p.update(overrides)
    return {"gh_project": p}


def _run_success(*_args, **_kwargs):
    return Mock(stdout=json.dumps({"items": RAW_ITEMS}))


def test_items_and_raw_matches_items_on_success():
    cfg = _cfg()
    with patch("ghproject.ghkit.run", side_effect=_run_success):
        via_items = ghproject.items(cfg)
    with patch("ghproject.ghkit.run", side_effect=_run_success):
        via_pair, raw = ghproject.items_and_raw(cfg)
    assert via_items == via_pair
    assert raw == RAW_ITEMS


def test_items_and_raw_matches_items_on_subprocess_failure():
    cfg = _cfg()
    err = subprocess.CalledProcessError(1, ["gh"])
    with patch("ghproject.ghkit.run", side_effect=err):
        assert ghproject.items(cfg) is None
    with patch("ghproject.ghkit.run", side_effect=err):
        assert ghproject.items_and_raw(cfg) == (None, None)


def test_items_and_raw_matches_items_on_json_decode_failure():
    cfg = _cfg()
    with patch("ghproject.ghkit.run", return_value=Mock(stdout="not json")):
        assert ghproject.items(cfg) is None
    with patch("ghproject.ghkit.run", return_value=Mock(stdout="not json")):
        assert ghproject.items_and_raw(cfg) == (None, None)


def test_items_and_raw_matches_items_on_key_error():
    cfg = _cfg()
    del cfg["gh_project"]["status_field"]  # parse_items needs p["status_field"] -> KeyError
    with patch("ghproject.ghkit.run", side_effect=_run_success):
        assert ghproject.items(cfg) is None
    with patch("ghproject.ghkit.run", side_effect=_run_success):
        assert ghproject.items_and_raw(cfg) == (None, None)


def test_items_and_raw_matches_items_when_not_configured():
    cfg = _cfg(owner=None)
    assert ghproject.items(cfg) is None
    assert ghproject.items_and_raw(cfg) == (None, None)


# --- unmatched_date_kinds -----------------------------------------------------
# Pure, no I/O. Flags a kind iff the field id resolved AND raw_items is non-empty AND no row
# exposes any candidate key for that field's name -- a present-but-empty value is NOT a flag.

def _field_meta(start_id="SF_1", target_id="TF_1"):
    return {"project_id": "PVT_1", "status_field_id": "STF", "status_options": {},
            "start_field_id": start_id, "target_field_id": target_id}


def test_unmatched_date_kinds_flags_on_zero_match():
    raw = [{"id": "PVTI_1", "content": {"url": "u1"}, "other": "x"}]
    assert unmatched_date_kinds(raw, _field_meta(), "Start", "Target") == frozenset({"start", "target"})


def test_unmatched_date_kinds_no_flag_when_any_row_matches():
    raw = [
        {"id": "PVTI_1", "content": {"url": "u1"}},                                  # no keys at all
        {"id": "PVTI_2", "content": {"url": "u2"}, "start Date": "", "target": "2026-01-01"},
    ]
    assert unmatched_date_kinds(raw, _field_meta(), "Start Date", "Target") == frozenset()


def test_unmatched_date_kinds_no_flag_on_empty_or_none_raw_items():
    assert unmatched_date_kinds([], _field_meta(), "Start", "Target") == frozenset()
    assert unmatched_date_kinds(None, _field_meta(), "Start", "Target") == frozenset()


def test_unmatched_date_kinds_no_flag_when_field_meta_lacks_field_id():
    raw = [{"id": "PVTI_1", "content": {"url": "u1"}}]
    meta = _field_meta(start_id=None, target_id=None)
    assert unmatched_date_kinds(raw, meta, "Start", "Target") == frozenset()
    assert unmatched_date_kinds(raw, None, "Start", "Target") == frozenset()


def test_unmatched_date_kinds_present_but_empty_value_does_not_flag():
    # A row that HAS the key but with an empty value is genuinely-unset, not a mismatch.
    raw = [{"id": "PVTI_1", "content": {"url": "u1"}, "Start": "", "Target": None}]
    assert unmatched_date_kinds(raw, _field_meta(), "Start", "Target") == frozenset()


def test_unmatched_date_kinds_flags_only_the_missing_kind():
    raw = [{"id": "PVTI_1", "content": {"url": "u1"}, "Start": "2026-01-01"}]  # no Target key at all
    assert unmatched_date_kinds(raw, _field_meta(), "Start", "Target") == frozenset({"target"})


# --- set_project_date returns bool (True iff a write was actually issued) ----
# False on the falsy item_id/field_id guard, with no ghkit.run call. True at the end of both the
# apply (live PATCH) and DRY (print-only) branches.

def test_set_project_date_false_and_no_write_when_item_id_missing():
    with patch("ghproject.ghkit.run") as run_mock:
        result = ghproject.set_project_date(_cfg(), True, "PVT_1", None, "SF_1", "2026-01-02")
    assert result is False
    run_mock.assert_not_called()


def test_set_project_date_false_and_no_write_when_field_id_missing():
    with patch("ghproject.ghkit.run") as run_mock:
        result = ghproject.set_project_date(_cfg(), True, "PVT_1", "PVTI_1", None, "2026-01-02")
    assert result is False
    run_mock.assert_not_called()


def test_set_project_date_true_and_writes_on_apply():
    with patch("ghproject.ghkit.run") as run_mock:
        result = ghproject.set_project_date(_cfg(), True, "PVT_1", "PVTI_1", "SF_1", "2026-01-02")
    assert result is True
    run_mock.assert_called_once()


def test_set_project_date_true_and_no_write_on_dry_run():
    with patch("ghproject.ghkit.run") as run_mock:
        result = ghproject.set_project_date(_cfg(), False, "PVT_1", "PVTI_1", "SF_1", "2026-01-02")
    assert result is True
    run_mock.assert_not_called()
