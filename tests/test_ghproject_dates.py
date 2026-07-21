"""Unit tests for ghproject's item parsing, metadata reads, and date writes."""
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ghkit  # noqa: E402
import ghproject  # noqa: E402
from ghproject import _camel, _field, parse_items  # noqa: E402
from sync import sync_dates  # noqa: E402


def _patch_ctx(host="github.com"):
    """Patch _repo_context so the project read path resolves a host and reaches the gh project call
    under test (rather than short-circuiting on host resolution)."""
    return patch("ghproject.ghkit._repo_context",
                 return_value=ghkit.RepoContext(owner="acme", name="widgets", host=host))


# --- _camel -----------------------------------------------------------------

def test_camel_lowercases_only_first_rune():
    assert _camel("Start Date") == "start Date"


def test_camel_single_word():
    assert _camel("Status") == "status"


def test_camel_falsy_name_returned_unchanged():
    assert _camel("") == ""
    assert _camel(None) is None


# --- _field_candidates -------------------------------------------------------
# Probe PRIORITY is observable behavior worth pinning (which value wins when multiple candidate keys
# are present on the same item); the exact private tuple _field_candidates returns is not -- test the
# priority through the public boundary (_field) instead, so a behavior-preserving reordering inside
# the private helper (e.g. checking camelCase before the plain lower-case form, which changes nothing
# observable when only one candidate key is ever present) doesn't force a test update.

def test_field_prefers_exact_name_over_lowercase_over_camelcase():
    item = {"start Date": "camel", "start date": "lower", "Start Date": "exact"}
    assert _field(item, "Start Date") == "exact"


def test_field_falls_back_to_lowercase_then_camelcase_in_order():
    assert _field({"start date": "lower", "start Date": "camel"}, "Start Date") == "lower"
    assert _field({"start Date": "camel"}, "Start Date") == "camel"


def test_field_matches_alt_key_unreachable_via_name_lower_or_camel():
    # An alt is the only way to reach a key that name/name.lower()/_camel(name) can never produce --
    # pinned through the public boundary (_field), not the private candidate tuple.
    item = {"iteration": "v1"}
    assert _field(item, "Sprint", "iteration") == "v1"


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


# --- items() -----------------------------------------------------------------

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


def test_items_returns_parsed_rows_on_success():
    cfg = _cfg()
    with patch("ghproject.ghkit.run", side_effect=_run_success), _patch_ctx():
        result = ghproject.items(cfg)
    assert result["https://github.com/o/r/issues/5"]["start"] == "2026-01-02"


def test_items_returns_none_on_subprocess_failure():
    cfg = _cfg()
    err = subprocess.CalledProcessError(1, ["gh"])
    with patch("ghproject.ghkit.run", side_effect=err), _patch_ctx():
        assert ghproject.items(cfg) is None


def test_items_returns_none_on_json_decode_failure():
    cfg = _cfg()
    with patch("ghproject.ghkit.run", return_value=Mock(stdout="not json")), _patch_ctx():
        assert ghproject.items(cfg) is None


def test_items_returns_none_on_key_error():
    cfg = _cfg()
    del cfg["gh_project"]["status_field"]  # parse_items needs p["status_field"] -> KeyError
    with patch("ghproject.ghkit.run", side_effect=_run_success), _patch_ctx():
        assert ghproject.items(cfg) is None


def test_fetch_raw_items_fails_closed_when_host_unresolved():
    # No resolvable target host -> no gh project call is attempted, and the read fails closed.
    cfg = _cfg()
    with patch("ghproject.ghkit._repo_context", return_value=None), \
        patch("ghproject.ghkit.run", side_effect=_run_success) as run_mock:
        assert ghproject.items(cfg) is None
    run_mock.assert_not_called()


def test_fetch_raw_items_pins_project_call_to_resolved_host():
    # gh project item-list has no --hostname flag; the resolved host must reach run() as host=.
    cfg = _cfg()
    with patch("ghproject.ghkit.run", side_effect=_run_success) as run_mock, _patch_ctx(host="ghes.acme.internal"):
        ghproject.items(cfg)
    assert run_mock.call_args.kwargs.get("host") == "ghes.acme.internal"


def test_items_returns_none_when_not_configured():
    cfg = _cfg(owner=None)
    assert ghproject.items(cfg) is None


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


def test_set_project_date_pins_write_to_host():
    # gh project item-edit has no --hostname flag; the write must reach run() as host=, never the
    # default host (writing to a same-number project on the wrong instance).
    with patch("ghproject.ghkit.run") as run_mock:
        ghproject.set_project_date(_cfg(), True, "PVT_1", "PVTI_1", "SF_1", "2026-01-02",
                                   "ghes.acme.internal")
    assert run_mock.call_args.kwargs.get("host") == "ghes.acme.internal"


# --- field_meta: resolves + threads the target host, fails closed -----------

def _field_meta_run(*args, **_kwargs):
    call = args[1]
    if call[:2] == ["project", "view"]:
        return Mock(stdout=json.dumps({"id": "PVT_1"}))
    if call[:2] == ["project", "field-list"]:
        return Mock(stdout=json.dumps({"fields": []}))
    raise AssertionError(f"unexpected gh call: {call}")


def test_field_meta_pins_calls_to_host_and_stores_it():
    with patch("ghproject.ghkit.run", side_effect=_field_meta_run) as run_mock, \
         _patch_ctx(host="ghes.acme.internal"):
        meta = ghproject.field_meta(_cfg())
    assert meta is not None
    assert meta["host"] == "ghes.acme.internal"
    assert all(c.kwargs.get("host") == "ghes.acme.internal" for c in run_mock.call_args_list)


def test_field_meta_fails_closed_when_host_unresolved():
    with patch("ghproject.ghkit._repo_context", return_value=None), \
         patch("ghproject.ghkit.run", side_effect=_field_meta_run) as run_mock:
        assert ghproject.field_meta(_cfg()) is None
    run_mock.assert_not_called()


# --- add_item: dry-run gate ---------------------------------------------------
# apply=False must never touch the subprocess boundary, must print a 'DRY ...' line, and must
# return the exact same-shaped value (str | None) apply=True would -- a deterministic placeholder
# derived from the issue url, not a dict, so callers can treat both branches identically.

URL9 = "https://github.com/o/r/issues/9"


def test_add_item_returns_none_when_not_configured():
    with patch("ghproject.ghkit.run") as run_mock:
        assert ghproject.add_item(_cfg(owner=None), False, URL9) is None
        assert ghproject.add_item(_cfg(owner=None), True, URL9) is None
    run_mock.assert_not_called()


def test_add_item_dry_run_returns_deterministic_placeholder_and_writes_nothing():
    import hashlib
    expected = ghproject.PLANNED_ITEM_ID_PREFIX + hashlib.sha256(URL9.encode()).hexdigest()[:16]
    with patch("ghproject.ghkit.run") as run_mock:
        result = ghproject.add_item(_cfg(), False, URL9)
    assert result == expected
    assert isinstance(result, str)
    run_mock.assert_not_called()


def test_add_item_dry_run_placeholder_is_stable_across_calls_and_varies_by_url():
    # Deterministic: same url -> same placeholder every call (idempotent across repeated dry runs).
    first = ghproject.add_item(_cfg(), False, URL9)
    second = ghproject.add_item(_cfg(), False, URL9)
    other = ghproject.add_item(_cfg(), False, "https://github.com/o/r/issues/10")
    assert first == second
    assert first != other


def test_add_item_apply_parses_id_from_json_on_success():
    run_mock = Mock(return_value=Mock(stdout=json.dumps({"id": "PVTI_9"})))
    with patch("ghproject.ghkit.run", run_mock), _patch_ctx():
        result = ghproject.add_item(_cfg(), True, URL9)
    assert result == "PVTI_9"
    args = run_mock.call_args.args[1]
    assert args[:2] == ["project", "item-add"]
    assert "--url" in args and URL9 in args


def test_add_item_apply_pins_call_to_resolved_host():
    run_mock = Mock(return_value=Mock(stdout=json.dumps({"id": "PVTI_9"})))
    with patch("ghproject.ghkit.run", run_mock), _patch_ctx(host="ghes.acme.internal"):
        ghproject.add_item(_cfg(), True, URL9)
    assert run_mock.call_args.kwargs.get("host") == "ghes.acme.internal"


def test_add_item_apply_fails_closed_when_host_unresolved():
    with patch("ghproject.ghkit._repo_context", return_value=None), \
         patch("ghproject.ghkit.run") as run_mock:
        assert ghproject.add_item(_cfg(), True, URL9) is None
    run_mock.assert_not_called()


def test_add_item_apply_returns_none_on_subprocess_failure():
    err = subprocess.CalledProcessError(1, ["gh"])
    with patch("ghproject.ghkit.run", side_effect=err), _patch_ctx():
        assert ghproject.add_item(_cfg(), True, URL9) is None


def test_add_item_apply_returns_none_on_malformed_json():
    with patch("ghproject.ghkit.run", return_value=Mock(stdout="not json")), _patch_ctx():
        assert ghproject.add_item(_cfg(), True, URL9) is None


def test_add_item_apply_returns_none_when_id_key_missing():
    with patch("ghproject.ghkit.run", return_value=Mock(stdout=json.dumps({}))), _patch_ctx():
        assert ghproject.add_item(_cfg(), True, URL9) is None


# --- set_item_status: resolves field_meta fresh, dry-run gate ----------------

def _status_meta(**overrides):
    meta = {"project_id": "PVT_1", "host": "github.com", "status_field_id": "SF_STATUS",
            "status_options": {"in progress": "OPT_INPROG", "intake": "OPT_INTAKE"}}
    meta.update(overrides)
    return meta


def test_set_item_status_false_when_field_meta_unavailable():
    with patch("ghproject.field_meta", return_value=None), patch("ghproject.ghkit.run") as run_mock:
        assert ghproject.set_item_status(_cfg(), True, "PVTI_1", "Intake") is False
    run_mock.assert_not_called()


def test_set_item_status_false_when_status_field_id_missing():
    meta = _status_meta(status_field_id=None)
    with patch("ghproject.field_meta", return_value=meta), patch("ghproject.ghkit.run") as run_mock:
        assert ghproject.set_item_status(_cfg(), True, "PVTI_1", "Intake") is False
    run_mock.assert_not_called()


def test_set_item_status_false_when_stage_not_in_status_options():
    meta = _status_meta()
    with patch("ghproject.field_meta", return_value=meta), patch("ghproject.ghkit.run") as run_mock:
        assert ghproject.set_item_status(_cfg(), True, "PVTI_1", "Nonexistent Stage") is False
    run_mock.assert_not_called()


def test_set_item_status_matches_stage_case_insensitively():
    meta = _status_meta()
    with patch("ghproject.field_meta", return_value=meta), patch("ghproject.ghkit.run") as run_mock:
        assert ghproject.set_item_status(_cfg(), True, "PVTI_1", "INTAKE") is True
    run_mock.assert_called_once()


def test_set_item_status_dry_run_returns_true_and_writes_nothing():
    meta = _status_meta()
    with patch("ghproject.field_meta", return_value=meta), patch("ghproject.ghkit.run") as run_mock:
        assert ghproject.set_item_status(_cfg(), False, "PVTI_1", "Intake") is True
    run_mock.assert_not_called()


def test_set_item_status_apply_writes_and_returns_true():
    meta = _status_meta()
    with patch("ghproject.field_meta", return_value=meta), patch("ghproject.ghkit.run") as run_mock:
        result = ghproject.set_item_status(_cfg(), True, "PVTI_1", "Intake")
    assert result is True
    run_mock.assert_called_once()
    args = run_mock.call_args.args[1]
    assert args[:2] == ["project", "item-edit"]
    assert "--single-select-option-id" in args and "OPT_INTAKE" in args
    assert run_mock.call_args.kwargs.get("host") == "github.com"


def test_set_item_status_apply_returns_false_on_subprocess_failure():
    meta = _status_meta()
    err = subprocess.CalledProcessError(1, ["gh"])
    with patch("ghproject.field_meta", return_value=meta), patch("ghproject.ghkit.run", side_effect=err):
        assert ghproject.set_item_status(_cfg(), True, "PVTI_1", "Intake") is False


def test_set_item_status_resolves_field_meta_fresh_every_call():
    # Must call the module-level field_meta() itself (not accept a threaded-in meta param) -- a
    # caller's own local (e.g. main()'s, unconditionally nulled on boards with no date fields) must
    # never be substituted in its place.
    meta = _status_meta()
    with patch("ghproject.field_meta", return_value=meta) as meta_mock, \
         patch("ghproject.ghkit.run"):
        ghproject.set_item_status(_cfg(), True, "PVTI_1", "Intake")
        ghproject.set_item_status(_cfg(), True, "PVTI_1", "Intake")
    assert meta_mock.call_count == 2


# --- two-run end-to-end regression: merge-base skip-write --------------------
# Exercises sync.sync_dates across two SIMULATED CONSECUTIVE RUNS with the real (unmocked)
# ghproject.set_project_date -- only ghkit.run (the actual subprocess boundary) is patched. This pins
# the fix for issue #6's merge-base bug at the real call boundary, not by hand-waving a mocked bool.
#
# SPIKE-LEARNED CONSTRUCTION (do not deviate): hold pitem['start'] constant at whatever GitHub actually
# has across both runs; toggle ONLY item_id (None on run 1, present on run 2). Do NOT set the GitHub-side
# read to None to simulate "write skipped" -- that exercises reconcile_value's legitimate "GitHub
# genuinely cleared it" branch instead, which is a different bug and would pin the wrong invariant.

def test_two_run_merge_base_does_not_advance_until_item_id_resolves():
    """Run 1: the Project item id isn't resolved yet (item_id=None) -- set_project_date's own falsy-
    guard skips the write before ever touching ghkit.run, so prev['start'] must NOT advance even though
    apply=True. Run 2: item_id has resolved; GitHub's real value is unchanged (the skipped write never
    took effect), so the same merge computes the same desired value and this time the write actually
    goes through -- prev['start'] converges to it."""
    issue = {"url": "https://github.com/o/r/issues/9", "title": "[T9] widget"}
    card = {"id": "C9", "plannedStart": "2026-02-01", "plannedFinish": None}  # AgilePlace changed the date
    meta = {"project_id": "PVT_1", "start_field_id": "SF_1", "target_field_id": None}
    state = {issue["url"]: {"start": "2026-01-01"}}  # base == GitHub's current (unchanged) value
    calls = []

    def queue(card, ops, note):
        calls.append((card, ops, note))

    # Run 1: item_id not yet resolved.
    with patch("ghproject.ghkit.run") as run_mock:
        pitem = {"item_id": None, "start": "2026-01-01", "target": None}
        sync_dates({}, True, issue, card, pitem, meta, state, queue)
    run_mock.assert_not_called()                              # skipped before reaching the subprocess boundary
    assert state[issue["url"]]["start"] == "2026-01-01"        # NOT advanced -- write was never confirmed

    # Run 2: item_id now resolved; GitHub's real value is unchanged (nothing actually wrote last run).
    with patch("ghproject.ghkit.run") as run_mock:
        pitem = {"item_id": "PVTI_1", "start": "2026-01-01", "target": None}
        sync_dates({}, True, issue, card, pitem, meta, state, queue)
    run_mock.assert_called_once()                               # the write now actually goes through
    args = run_mock.call_args.args[1]
    assert "--clear" not in args and "2026-02-01" in args       # a real date write, not a null-PATCH
    assert state[issue["url"]]["start"] == "2026-02-01"         # converged: base now matches the confirmed write


# --- two-run end-to-end regression: camelCase convergence --------------------

def test_two_run_camelcase_convergence_issues_no_null_patch():
    """Two consecutive runs against a raw Project row keyed 'start Date' (gh's camelCase flatten of a
    'Start Date' field). Real ghproject.parse_items must resolve this value on every run -- if it fell
    back to the old 2-variant probe and silently read None instead, reconcile_value would see base (the
    real date) matched by AgilePlace but diverged from GitHub-as-(mis)parsed, conclude 'only GitHub
    changed', and sync.sync_dates would issue a null-PATCH clearing a date GitHub actually still has.
    Proves the fix converges losslessly: no write is ever attempted across either run."""
    url = "https://github.com/o/r/issues/9"
    raw_items = [{"id": "PVTI_1", "content": {"type": "Issue", "number": 9, "url": url},
                  "start Date": "2026-01-05"}]
    parsed = parse_items(raw_items, status_field="Status", start_field="Start Date", target_field="Target")
    pitem = parsed[url]
    assert pitem["start"] == "2026-01-05"                       # camelCase match confirmed before the real test

    issue = {"url": url, "title": "[T9] widget"}
    card = {"id": "C9", "plannedStart": "2026-01-05", "plannedFinish": None}  # AgilePlace already matches
    meta = {"project_id": "PVT_1", "start_field_id": "SF_1", "target_field_id": None}
    state = {url: {"start": "2026-01-05"}}                      # already-converged base from a prior run
    calls = []

    def queue(card, ops, note):
        calls.append((card, ops, note))

    for _ in range(2):                                          # two consecutive runs, unchanged inputs
        with patch("ghproject.ghkit.run") as run_mock:
            sync_dates({}, True, issue, card, pitem, meta, state, queue)
        run_mock.assert_not_called()                            # no PATCH -- let alone a null one -- ever issued
    assert calls == []
    assert state[url]["start"] == "2026-01-05"                  # base holds steady, no oscillation
