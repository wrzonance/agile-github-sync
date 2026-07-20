"""End-to-end wiring tests for sync.main() project reads and persisted sync state.

Unlike test_sync_dates.py (which calls sync_dates directly), these mock every I/O boundary (ghkit,
ghproject's gh-touching functions, agileplace's HTTP client) but exercise the REAL main(),
load_state/save_state, and sync_dates -- so they pin that main() actually plumbs authoritative date
hydration through, and that the merge-base advance invariant holds across real state persisted
to disk between two separate main() runs (not just within one sync_dates call).

TEST-CONSTRUCTION NOTE (final design decision #1): the two-run merge-base test holds the GitHub-side
date (pitem["start"]) CONSTANT across both runs and toggles ONLY item_id (None -> present) to simulate
"write skipped" -> "write confirmed". Do not fake this by changing the GitHub-side value itself -- that
exercises a different (legitimate) reconcile_value path ("GitHub genuinely changed it").

Run: pytest -q
"""
from __future__ import annotations

import json
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sync  # noqa: E402

ISSUE_URL = "https://github.com/acme/repo/issues/1"


def _issue():
    return {"number": 1, "title": "widget", "state": "OPEN", "labels": [],
            "milestone": None, "assignees": [], "url": ISSUE_URL}


def _card():
    return {"id": "C1", "version": 1, "customId": "1",
            "externalLink": {"url": ISSUE_URL}, "tags": [],
            "plannedStart": "2026-02-01", "plannedFinish": None, "laneId": None}


def _field_meta():
    return {"project_id": "PVT_1", "status_field_id": "STF", "status_options": {},
            "start_field_id": "SF_1", "target_field_id": "TF_1", "host": "github.com"}


def _cfg(tmp_path):
    return {
        "token": "tok", "host": "example.leankit.com", "board_id": "42",
        "target_repo_path": tmp_path,
        "label_sync_ignore": frozenset(),
        "stage_lane_map": {},
        "gh_project": {"owner": "acme", "number": "7", "status_field": "Status",
                       "start_field": "Start", "target_field": "Target"},
    }


_UNSET = object()


def _mock_io(card, items_and_raw_return, field_meta_return, open_pr_return=_UNSET, lanes_return=(),
             existing_cards=_UNSET, issue_return=_UNSET, hydrated_items_return=_UNSET):
    """ExitStack of patches covering every I/O boundary main() touches for one run. Returns the stack
    plus the ghkit.run, agileplace.patch_card, and agileplace.create_card mocks (for call-site
    assertions).

    open_pr_return defaults to set() (successful, empty read); pass None explicitly to simulate a
    failed ghkit.open_pr_issue_numbers() read (issue #14). lanes_return defaults to no lanes (the
    lane-move step is then always a no-op); pass real lane dicts to exercise lane-move decisions.
    existing_cards defaults to [card] (the issue already has a matching card); pass [] to force the
    "ensure a card per issue" creation path instead of the lane-move path. issue_return defaults to
    _issue(); pass a complete issue dict to exercise different live metadata."""
    stack = ExitStack()
    stack.enter_context(patch("ghkit.repo_name", return_value="acme/repo"))
    issue = _issue() if issue_return is _UNSET else issue_return
    issues = issue if isinstance(issue, list) else [issue]
    stack.enter_context(patch("ghkit.list_issues", return_value=issues))
    stack.enter_context(patch("ghkit.open_pr_issue_numbers",
                              return_value=set() if open_pr_return is _UNSET else open_pr_return))
    stack.enter_context(patch("ghkit.blocked_by_map", return_value={}))
    run_mock = stack.enter_context(patch("ghkit.run", return_value=Mock(stdout="")))
    stack.enter_context(patch("ghproject.configured", return_value=True))
    parsed_items = items_and_raw_return[0]
    stack.enter_context(patch("ghproject.items", return_value=parsed_items))
    stack.enter_context(patch("ghproject.field_meta", return_value=field_meta_return))
    hydrated = parsed_items if hydrated_items_return is _UNSET else hydrated_items_return
    stack.enter_context(patch("ghproject.hydrate_item_dates", return_value=hydrated))
    stack.enter_context(patch("agileplace.board_layout", return_value=list(lanes_return)))
    cards = [card] if existing_cards is _UNSET else list(existing_cards)
    stack.enter_context(patch("agileplace.list_cards", return_value=cards))
    patch_card_mock = stack.enter_context(patch("agileplace.patch_card"))
    create_card_mock = stack.enter_context(patch("agileplace.create_card", return_value={}))
    return stack, run_mock, patch_card_mock, create_card_mock


def _run_main_once(tmp_path, items_and_raw_return, field_meta_return=None, seed_issues_state=None,
                   open_pr_return=_UNSET, lanes_return=(), card=None, existing_cards=_UNSET,
                   hydrated_items_return=_UNSET):
    """seed_issues_state pre-populates the on-disk state file's issues[ISSUE_URL] before main() runs.

    ``items_and_raw_return`` retains the older pair-shaped fixture interface so the extensive status
    tests below stay focused; only its parsed first element now feeds ghproject.items().
    """
    cfg = _cfg(tmp_path)
    state_file = tmp_path / ".sync-state.json"
    if seed_issues_state is not None:
        state_file.write_text(json.dumps({"schema": 2, "target": "acme/repo", "board": "42",
                                          "issues": {ISSUE_URL: seed_issues_state}}), encoding="utf-8")
    card = card if card is not None else _card()
    stack, run_mock, patch_card_mock, create_card_mock = _mock_io(
        card, items_and_raw_return, field_meta_return, open_pr_return=open_pr_return,
        lanes_return=lanes_return, existing_cards=existing_cards,
        hydrated_items_return=hydrated_items_return)
    with stack, patch("sync.env_config", return_value=cfg), patch("sync.STATE_FILE", state_file), \
         patch("sys.argv", ["sync.py", "--apply"]):
        sync.main()
    return json.loads(state_file.read_text(encoding="utf-8")), run_mock, patch_card_mock, create_card_mock


# --- state schema and legacy merge-base migration (issue #13) -------------------------------

def test_load_state_refuses_explicit_wrong_schema(tmp_path):
    state_file = tmp_path / ".sync-state.json"
    state_file.write_text(json.dumps({
        "schema": sync.STATE_SCHEMA - 1,
        "target": "acme/repo",
        "board": "42",
        "issues": {},
    }), encoding="utf-8")

    with patch("sync.STATE_FILE", state_file), pytest.raises(SystemExit) as raised:
        sync.load_state("acme/repo", "42")

    message = str(raised.value)
    assert f"uses state schema {sync.STATE_SCHEMA - 1}" in message
    assert f"requires schema {sync.STATE_SCHEMA}" in message
    assert "Inspect or delete it, then re-run." in message


@pytest.mark.parametrize("card_id", ["", 0])
def test_load_state_resets_falsy_card_id_merge_bases(tmp_path, card_id):
    state_file = tmp_path / ".sync-state.json"
    state_file.write_text(json.dumps({
        "schema": sync.STATE_SCHEMA,
        "target": "acme/repo",
        "board": "42",
        "issues": {
            ISSUE_URL: {
                "card_id": card_id,
                "start": "2026-01-01",
                "target": "2026-01-09",
            },
        },
    }), encoding="utf-8")

    with patch("sync.STATE_FILE", state_file):
        state = sync.load_state("acme/repo", "42")

    assert state["issues"][ISSUE_URL] == {}


def test_legacy_state_resets_merge_base_before_relearning_live_metadata(tmp_path):
    state_file = tmp_path / ".sync-state.json"
    state_file.write_text(json.dumps({
        "target": "acme/repo",
        "board": "42",
        "issues": {
            ISSUE_URL: {
                "labels": ["bug"],
                "milestone": "1.0",
                "start": "2026-01-01",
                "target": "2026-01-09",
            },
        },
    }), encoding="utf-8")
    issue = {**_issue(), "labels": ["bug"], "milestone": "1.0"}
    parsed = {ISSUE_URL: {
        "item_id": "PVTI_1", "number": 1, "status": "In progress",
        "start": None, "target": None,
    }}
    raw_items = [{"id": "PVTI_1", "content": {"url": ISSUE_URL}}]
    stack, run_mock, patch_card_mock, _ = _mock_io(
        _card(), (parsed, raw_items), field_meta_return=_field_meta(), issue_return=issue)

    with stack, patch("sync.env_config", return_value=_cfg(tmp_path)), \
         patch("sync.STATE_FILE", state_file), patch("sys.argv", ["sync.py", "--apply"]):
        sync.main()

    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["schema"] == sync.STATE_SCHEMA
    assert state["issues"][ISSUE_URL] == {
        "card_id": "C1", "labels": ["bug"], "milestone": "1.0",
        "start": "2026-02-01", "target": None,
    }
    run_mock.assert_called_once()
    assert run_mock.call_args.args[1][:2] == ["project", "item-edit"]
    assert "--date" in run_mock.call_args.args[1]  # live card date was relearned on the first run
    patch_card_mock.assert_called_once()
    ops = patch_card_mock.call_args.args[3]
    assert all(op["op"] != "remove" for op in ops)
    assert {op.get("value") for op in ops} == {"bug", "milestone:1.0"}


# --- merge-base advance invariant, end-to-end through two real main() runs -----------------------

def test_merge_base_advances_only_after_confirmed_write_across_two_main_runs(tmp_path, capsys):
    raw_items = [{"id": "PVTI_1", "content": {"url": ISSUE_URL}, "Start": "2026-01-01", "Target": None}]
    field_meta = _field_meta()

    # Run 1: item_id missing -> ghproject.set_project_date's own guard skips the write.
    parsed_no_item_id = {ISSUE_URL: {"item_id": None, "number": 1, "status": "In progress",
                                     "start": "2026-01-01", "target": None}}
    state_after_1, _, _, _ = _run_main_once(tmp_path, (parsed_no_item_id, raw_items), field_meta)
    assert "start" not in state_after_1["issues"][ISSUE_URL], (
        "merge base must NOT advance when the GH-side write was skipped (item_id missing)")

    # Run 2: same GitHub-side date, item_id now present -> the write is confirmed.
    parsed_with_item_id = {ISSUE_URL: {"item_id": "PVTI_1", "number": 1, "status": "In progress",
                                       "start": "2026-01-01", "target": None}}
    state_after_2, run_mock, _, _ = _run_main_once(tmp_path, (parsed_with_item_id, raw_items), field_meta)
    assert state_after_2["issues"][ISSUE_URL]["start"] == "2026-02-01", (
        "merge base must advance once the GH-side write is confirmed")
    run_mock.assert_called_once()  # the real ghproject.set_project_date issued exactly one gh write


def test_successful_all_cleared_snapshot_syncs_on_first_rollout(tmp_path, capsys):
    # A successful field-ID snapshot with no values is authoritative even without prior history.
    raw_items = [{"id": "PVTI_1", "content": {"url": ISSUE_URL}, "unrelated": "x"}]
    parsed = {ISSUE_URL: {"item_id": "PVTI_1", "number": 1, "status": "In progress",
                          "start": None, "target": None}}
    state, run_mock, _, _ = _run_main_once(tmp_path, (parsed, raw_items), _field_meta())

    out = capsys.readouterr().out
    assert "WARN  Projects v2" not in out
    run_mock.assert_called_once()               # start: AgilePlace's plannedStart is written to GitHub
    assert state["issues"][ISSUE_URL]["start"] == "2026-02-01"


def test_all_cleared_date_snapshot_recovers_from_prior_history(tmp_path, capsys):
    """A successful field-ID snapshot with no Start values is an authoritative project-wide clear.

    Prior non-empty history must not turn that valid state into a permanent skip: a later AgilePlace
    edit writes back to GitHub and advances the merge base normally.
    """
    raw_items = [{"id": "PVTI_1", "content": {"url": ISSUE_URL}, "unrelated": "x"}]
    parsed = {ISSUE_URL: {"item_id": "PVTI_1", "number": 1, "status": "In progress",
                          "start": None, "target": None}}
    state, run_mock, _, _ = _run_main_once(
        tmp_path, (parsed, raw_items), _field_meta(),
        seed_issues_state={"card_id": "C1", "start": "2026-01-01"},
        hydrated_items_return=parsed)

    out = capsys.readouterr().out
    assert "resolved but no item ever exposed a matching key" not in out
    run_mock.assert_called_once()
    assert "2026-02-01" in run_mock.call_args.args[1]
    assert state["issues"][ISSUE_URL]["start"] == "2026-02-01"


def test_date_snapshot_failure_skips_dates_but_keeps_status_sync(tmp_path, capsys):
    parsed = {ISSUE_URL: {"item_id": "PVTI_1", "number": 1, "status": "In progress",
                          "start": None, "target": None}}
    lanes = [
        {"id": "L1", "title": "Backlog", "cardStatus": "notStarted"},
        {"id": "L2", "title": "In progress", "cardStatus": "started"},
    ]
    card = {**_card(), "laneId": "L1"}

    state, run_mock, patch_card_mock, _ = _run_main_once(
        tmp_path, (parsed, []), _field_meta(),
        seed_issues_state={"card_id": "C1", "start": "2026-01-01"},
        hydrated_items_return=None, lanes_return=lanes, card=card)

    assert "date field-value read FAILED -- skipping all date sync" in capsys.readouterr().out
    run_mock.assert_not_called()
    assert state["issues"][ISSUE_URL]["start"] == "2026-01-01"
    patch_card_mock.assert_called_once()
    assert {op.get("value") for op in patch_card_mock.call_args.args[3]} == {"L2"}


def test_no_resolved_date_fields_skips_hydration_and_reports_dates_disabled(tmp_path, capsys):
    parsed = {ISSUE_URL: {"item_id": "PVTI_1", "number": 1, "status": "In progress",
                          "start": None, "target": None}}
    field_meta = {**_field_meta(), "start_field_id": None, "target_field_id": None}
    stack, _, _, _ = _mock_io(_card(), (parsed, []), field_meta)
    state_file = tmp_path / ".sync-state.json"

    with stack, patch("sync.env_config", return_value=_cfg(tmp_path)), \
         patch("sync.STATE_FILE", state_file), patch("sys.argv", ["sync.py", "--apply"]), \
         patch("ghproject.hydrate_item_dates") as hydrate_mock:
        sync.main()

    assert "dates enabled" not in capsys.readouterr().out
    hydrate_mock.assert_not_called()


# --- no date metadata: no hydration, no crash, no warning ------------------------------------------

def test_no_warn_and_no_crash_when_field_meta_is_none(tmp_path, capsys):
    parsed = {ISSUE_URL: {"item_id": "PVTI_1", "number": 1, "status": "In progress",
                          "start": "2026-01-01", "target": None}}
    raw_items = [{"id": "PVTI_1", "content": {"url": ISSUE_URL}}]
    state, run_mock, patch_card_mock, _ = _run_main_once(tmp_path, (parsed, raw_items), field_meta_return=None)

    out = capsys.readouterr().out
    assert "WARN  Projects v2" not in out
    run_mock.assert_not_called()
    patch_card_mock.assert_not_called()
    assert "start" not in state["issues"][ISSUE_URL]


# --- open-PR read failure (issue #14): degrade, don't crash or fabricate a positive signal ---------

_LANES = [
    {"id": "L1", "title": "Backlog", "cardStatus": "notStarted"},
    {"id": "L2", "title": "In review", "cardStatus": "started"},
]


# --- customId lifecycle (issue #11) --------------------------------------------------------------

def test_keyless_issue_uses_number_custom_id_after_external_link_is_lost(tmp_path):
    card = {**_card(), "customId": "1", "externalLink": {"url": "https://example.test/lost-link"}}
    parsed = {ISSUE_URL: {"item_id": "PVTI_1", "number": 1, "status": "Backlog",
                          "start": None, "target": None}}
    raw_items = [{"id": "PVTI_1", "content": {"url": ISSUE_URL}}]

    state, _, patch_card_mock, create_card_mock = _run_main_once(
        tmp_path, (parsed, raw_items), field_meta_return=None, card=card)

    create_card_mock.assert_not_called()
    patch_card_mock.assert_not_called()
    assert state["issues"][ISSUE_URL]["card_id"] == "C1"


def test_title_key_rename_joins_custom_id_repair_into_single_card_patch(tmp_path):
    issue = {**_issue(), "title": "[XYZ] renamed widget"}
    card = {**_card(), "customId": "ABC", "laneId": "L1"}
    parsed = {ISSUE_URL: {"item_id": "PVTI_1", "number": 1, "status": "In review",
                          "start": None, "target": None}}
    raw_items = [{"id": "PVTI_1", "content": {"url": ISSUE_URL}}]
    stack, _, patch_card_mock, create_card_mock = _mock_io(
        card, (parsed, raw_items), field_meta_return=None, lanes_return=_LANES, issue_return=issue)
    state_file = tmp_path / ".sync-state.json"

    with stack, patch("sync.env_config", return_value=_cfg(tmp_path)), \
         patch("sync.STATE_FILE", state_file), patch("sys.argv", ["sync.py", "--apply"]):
        sync.main()

    create_card_mock.assert_not_called()
    patch_card_mock.assert_called_once()
    ops = patch_card_mock.call_args.args[3]
    assert len(ops) == 2
    assert {"op": "replace", "path": "/customId", "value": "XYZ"} in ops
    assert {"op": "replace", "path": "/laneId", "value": "L2"} in ops


def test_url_and_custom_id_matching_different_cards_fails_before_writes(tmp_path):
    issue = {**_issue(), "title": "[XYZ] widget"}
    url_card = {**_card(), "customId": "ABC"}
    custom_id_card = {
        **_card(),
        "id": "C2",
        "customId": "XYZ",
        "externalLink": {"url": "https://github.com/acme/repo/issues/2"},
    }
    parsed = {ISSUE_URL: {"item_id": "PVTI_1", "number": 1, "status": "Backlog",
                          "start": None, "target": None}}
    raw_items = [{"id": "PVTI_1", "content": {"url": ISSUE_URL}}]
    stack, run_mock, patch_card_mock, create_card_mock = _mock_io(
        url_card,
        (parsed, raw_items),
        field_meta_return=None,
        existing_cards=[url_card, custom_id_card],
        issue_return=issue,
    )
    state_file = tmp_path / ".sync-state.json"

    with stack, patch("sync.env_config", return_value=_cfg(tmp_path)), \
         patch("sync.STATE_FILE", state_file), patch("sys.argv", ["sync.py", "--apply"]), \
         pytest.raises(SystemExit) as raised:
        sync.main()

    message = str(raised.value)
    assert ISSUE_URL in message
    assert "customId 'XYZ'" in message
    assert "C1" in message and "C2" in message
    create_card_mock.assert_not_called()
    patch_card_mock.assert_not_called()
    run_mock.assert_not_called()
    assert not state_file.exists()


def test_same_run_key_reuse_defers_creation_until_rename_repair_is_applied(tmp_path, capsys):
    renamed_issue = {**_issue(), "title": "[XYZ] renamed widget"}
    reused_key_issue = {
        **_issue(),
        "number": 2,
        "title": "[ABC] new widget",
        "url": "https://github.com/acme/repo/issues/2",
    }
    renamed_card = {**_card(), "customId": "ABC"}
    parsed = {ISSUE_URL: {"item_id": "PVTI_1", "number": 1, "status": "Backlog",
                          "start": None, "target": None}}
    raw_items = [{"id": "PVTI_1", "content": {"url": ISSUE_URL}}]
    stack, _, patch_card_mock, create_card_mock = _mock_io(
        renamed_card,
        (parsed, raw_items),
        field_meta_return=None,
        issue_return=[renamed_issue, reused_key_issue],
    )
    state_file = tmp_path / ".sync-state.json"

    with stack, patch("sync.env_config", return_value=_cfg(tmp_path)), \
         patch("sync.STATE_FILE", state_file), patch("sys.argv", ["sync.py", "--apply"]):
        sync.main()

    create_card_mock.assert_not_called()
    patch_card_mock.assert_called_once()
    assert patch_card_mock.call_args.args[3] == [
        {"op": "replace", "path": "/customId", "value": "XYZ"},
    ]
    assert "deferring card [ABC] until the renamed customId is released" in capsys.readouterr().out


def test_reused_key_is_created_after_rename_repair_is_visible(tmp_path):
    renamed_issue = {**_issue(), "title": "[XYZ] renamed widget"}
    reused_key_issue = {
        **_issue(),
        "number": 2,
        "title": "[ABC] new widget",
        "url": "https://github.com/acme/repo/issues/2",
    }
    renamed_card = {**_card(), "customId": "XYZ"}
    parsed = {ISSUE_URL: {"item_id": "PVTI_1", "number": 1, "status": "Backlog",
                          "start": None, "target": None}}
    raw_items = [{"id": "PVTI_1", "content": {"url": ISSUE_URL}}]
    stack, _, patch_card_mock, create_card_mock = _mock_io(
        renamed_card,
        (parsed, raw_items),
        field_meta_return=None,
        issue_return=[renamed_issue, reused_key_issue],
    )
    state_file = tmp_path / ".sync-state.json"

    with stack, patch("sync.env_config", return_value=_cfg(tmp_path)), \
         patch("sync.STATE_FILE", state_file), patch("sys.argv", ["sync.py", "--apply"]):
        sync.main()

    create_card_mock.assert_called_once()
    assert create_card_mock.call_args.args[2:5] == (
        "new widget",
        "ABC",
        reused_key_issue["url"],
    )
    patch_card_mock.assert_not_called()


def test_open_pr_read_failure_does_not_crash_and_leaves_has_open_pr_false(tmp_path, capsys):
    # _issue() has no labels/assignees, so issue_stage() only reaches "In review" via has_open_pr.
    # If open_pr_issue_numbers() returning None ever got treated as "every issue's PR is open" (or
    # crashed on `number in None`), the card would be moved out of its current Backlog lane.
    parsed = {ISSUE_URL: {"item_id": "PVTI_1", "number": 1, "status": None, "start": None, "target": None}}
    raw_items = [{"id": "PVTI_1", "content": {"url": ISSUE_URL}}]
    card = _card()
    card["laneId"] = "L1"

    state, run_mock, patch_card_mock, _ = _run_main_once(
        tmp_path, (parsed, raw_items), field_meta_return=None,
        open_pr_return=None, lanes_return=_LANES, card=card)

    out = capsys.readouterr().out
    assert "open-PR read FAILED" in out
    patch_card_mock.assert_not_called()  # card stayed in its Backlog lane, never moved to "In review"
    assert state["issues"][ISSUE_URL]["card_id"] == "C1"  # run completed and persisted state normally


def test_open_pr_read_failure_freezes_card_already_in_review_lane(tmp_path, capsys):
    # Card is ALREADY sitting in the "In review" lane (from a prior run where the PR was open). This
    # run's open-PR read fails, so has_open_pr is never set -> issue_stage() falls all the way back to
    # "Backlog" (no labels/assignees). Without the _protect_open_pr_stage guard wired into the
    # existing-card lane-move site, main() would demote this card to "Backlog" on a transient read
    # failure alone -- exactly the regression issue #14 exists to prevent.
    parsed = {ISSUE_URL: {"item_id": "PVTI_1", "number": 1, "status": None, "start": None, "target": None}}
    raw_items = [{"id": "PVTI_1", "content": {"url": ISSUE_URL}}]
    card = _card()
    card["laneId"] = "L2"

    state, run_mock, patch_card_mock, _ = _run_main_once(
        tmp_path, (parsed, raw_items), field_meta_return=None,
        open_pr_return=None, lanes_return=_LANES, card=card)

    out = capsys.readouterr().out
    assert "open-PR read FAILED" in out
    patch_card_mock.assert_not_called()  # frozen: card must stay in "In review", never moved to Backlog
    assert state["issues"][ISSUE_URL]["card_id"] == "C1"


def test_explicit_status_overrides_open_pr_freeze_guard(tmp_path, capsys):
    # Same failed-read, same card sitting in "In review" -- but this time the issue carries an EXPLICIT
    # Projects v2 Status of "Backlog". A human's explicit call must always win over the freeze guard:
    # the card should move to Backlog exactly as it would if the open-PR read had succeeded.
    parsed = {ISSUE_URL: {"item_id": "PVTI_1", "number": 1, "status": "Backlog",
                          "start": None, "target": None}}
    raw_items = [{"id": "PVTI_1", "content": {"url": ISSUE_URL}}]
    card = _card()
    card["laneId"] = "L2"

    state, run_mock, patch_card_mock, _ = _run_main_once(
        tmp_path, (parsed, raw_items), field_meta_return=None,
        open_pr_return=None, lanes_return=_LANES, card=card)

    out = capsys.readouterr().out
    assert "open-PR read FAILED" in out
    assert "-> 'Backlog' (stage Backlog)" in out
    patch_card_mock.assert_called_once()  # explicit Status wins: card moved out of "In review" lane
    assert state["issues"][ISSUE_URL]["card_id"] == "C1"


def test_unrecognized_custom_status_option_does_not_bypass_freeze_guard(tmp_path, capsys):
    # Same failed-read, same card sitting in "In review" -- but this time the issue's Projects v2
    # Status carries a CUSTOM option name ("Triage") that doesn't map to any of our five canonical
    # stages. project_status[url] is truthy, but resolve_issue_stage() can't use it and falls all the
    # way back to issue_stage() (label/PR-derived) -- so no human's EXPLICIT canonical call was ever
    # actually made. has_explicit_status must reflect that (False), not the raw truthiness of the
    # Status field, or this unrecognized option would silently bypass the freeze guard and demote the
    # card out of "In review" on the same transient read failure issue #14 exists to protect against.
    parsed = {ISSUE_URL: {"item_id": "PVTI_1", "number": 1, "status": "Triage",
                          "start": None, "target": None}}
    raw_items = [{"id": "PVTI_1", "content": {"url": ISSUE_URL}}]
    card = _card()
    card["laneId"] = "L2"

    state, run_mock, patch_card_mock, _ = _run_main_once(
        tmp_path, (parsed, raw_items), field_meta_return=None,
        open_pr_return=None, lanes_return=_LANES, card=card)

    out = capsys.readouterr().out
    assert "open-PR read FAILED" in out
    patch_card_mock.assert_not_called()  # frozen: unrecognized option must not count as explicit
    assert state["issues"][ISSUE_URL]["card_id"] == "C1"


# --- card creation path (issue #14 follow-up): must use `stage`, never a guard-adjusted lane_stage ---

def test_ensure_card_creation_uses_stage_unaffected_by_open_pr_read_failure(tmp_path, capsys):
    # No existing card matches the issue -> the "ensure a card per issue" creation path runs. It must
    # use resolve_issue_stage()'s `stage` directly and never route through _protect_open_pr_stage (that
    # guard only ever freezes a card ALREADY sitting in a lane -- a brand-new card has no current lane
    # to freeze). Explicit Status "In review" + a failed open-PR read must still create the new card
    # straight into the "In review" lane, proving the creation path was never touched by the guard.
    parsed = {ISSUE_URL: {"item_id": "PVTI_1", "number": 1, "status": "In review",
                          "start": None, "target": None}}
    raw_items = [{"id": "PVTI_1", "content": {"url": ISSUE_URL}}]

    state, run_mock, patch_card_mock, create_card_mock = _run_main_once(
        tmp_path, (parsed, raw_items), field_meta_return=None,
        open_pr_return=None, lanes_return=_LANES, existing_cards=[])

    out = capsys.readouterr().out
    assert "open-PR read FAILED" in out
    assert "stage=In review lane=In review" in out
    create_card_mock.assert_called_once()
    assert create_card_mock.call_args.args[-1] == "L2"  # created straight into the In review lane


# --- issue #5: fail-closed gate widened to zero-recognized-statuses-despite-items, and extended to
# gate new-card lane assignment (not just existing-card lane moves) -------------------------------

def _zero_status_inputs():
    """A technically-successful item-list read: the Project has one issue-linked item (item_id/number
    populated) but no candidate key for the configured Status field resolved to a value -- the
    misspelled-GH_PROJECT_STATUS_FIELD / gh output-shape-drift signature this issue targets. project_status
    ends up {} even though project_items is non-empty."""
    parsed = {ISSUE_URL: {"item_id": "PVTI_1", "number": 1, "status": None, "start": None, "target": None}}
    raw_items = [{"id": "PVTI_1", "content": {"url": ISSUE_URL, "number": 1}, "unrelated": "x"}]
    return parsed, raw_items


def test_zero_recognized_statuses_treated_as_failed_read(tmp_path, capsys):
    _, _, patch_card_mock, create_card_mock = _run_main_once(
        tmp_path, _zero_status_inputs(), field_meta_return=None)

    out = capsys.readouterr().out
    assert ("WARN  Projects v2 has 1 issue item(s) but none carry a recognized 'Status' Status -- "
            "check GH_PROJECT_STATUS_FIELD") in out
    patch_card_mock.assert_not_called()         # no lane-move (or any) op reached the existing card
    create_card_mock.assert_not_called()        # card already existed -- nothing created this run


def test_new_card_gets_no_lane_when_statuses_unrecognized(tmp_path, capsys):
    _, _, _, create_card_mock = _run_main_once(
        tmp_path, _zero_status_inputs(), field_meta_return=None, existing_cards=[])

    create_card_mock.assert_called_once()
    assert create_card_mock.call_args.args[-1] is None, (
        "new card must be created laneless when the Project read yields zero recognized statuses")


def test_new_card_gets_no_lane_when_project_read_outright_fails(tmp_path, capsys):
    _, _, _, create_card_mock = _run_main_once(
        tmp_path, (None, None), field_meta_return=None, existing_cards=[])

    out = capsys.readouterr().out
    assert "WARN  Projects v2 read FAILED -- leaving lanes untouched this run (Status is the source of truth)" in out, (
        "the outright-failure WARN string must be printed byte-for-byte on the (None, None) call-failed path")
    create_card_mock.assert_called_once()
    assert create_card_mock.call_args.args[-1] is None, (
        "new card must be created laneless when the Project item-list call fails outright")


def test_zero_issue_linked_items_does_not_trip_zero_status_warn(tmp_path, capsys):
    """False-positive guard: a Project whose configured board legitimately has zero issue-linked items
    (every row is a draft/PR, so project_items == {}) must NOT be mistaken for the
    zero-recognized-statuses failure mode -- that WARN, and the fail-closed lane gating that goes with
    it, exist only for a NON-empty item set with no recognized Status. An empty item set must leave
    move_lanes True (lane resolution still attempted for new cards)."""
    raw_items = [{"id": "PVTI_9", "content": {}}]  # draft item: no linked issue/PR at all
    parsed: dict = {}  # ghproject.items resolved zero issue-linked items -- not a failure
    fake_lane = {"id": "L1", "title": "Planning"}

    with patch("agileplace.resolve_lane_for_stage", return_value=(fake_lane, {"L1"})) as resolve_mock:
        _, _, _, create_card_mock = _run_main_once(
            tmp_path, (parsed, raw_items), field_meta_return=None, existing_cards=[])

    out = capsys.readouterr().out
    assert "WARN  Projects v2 has" not in out, "zero issue-linked items must never trip the zero-status WARN"
    assert "WARN  Projects v2 read FAILED" not in out
    resolve_mock.assert_called_once()          # lane resolution was attempted -- move_lanes stayed True
    create_card_mock.assert_called_once()
    assert create_card_mock.call_args.args[-1] == "L1", (
        "new card must get a real lane when the Project legitimately has zero issue-linked items")


def test_new_card_lane_resolution_is_never_called_when_project_read_failed(tmp_path, capsys):
    """Pins the `if not project_read_failed:` gate directly on the new-card path, rather than relying
    on agileplace.board_layout being mocked to []. resolve_lane_for_stage is mocked to return a REAL
    lane -- if the gate were ever dropped (or turned into 'call it, then null the result'), this would
    fail even though board_layout is empty, unlike a test that only asserts the final lane_id."""
    fake_lane = {"id": "L1", "title": "Planning"}
    with patch("agileplace.resolve_lane_for_stage", return_value=(fake_lane, {"L1"})) as resolve_mock:
        _, _, _, create_card_mock = _run_main_once(
            tmp_path, _zero_status_inputs(), field_meta_return=None, existing_cards=[])

    resolve_mock.assert_not_called()
    create_card_mock.assert_called_once()
    assert create_card_mock.call_args.args[-1] is None, (
        "new card must be created laneless -- resolve_lane_for_stage must not even be consulted")


def test_existing_card_lane_resolution_is_never_called_when_project_read_failed(tmp_path, capsys):
    """Analogous pin for the existing-card loop's `if move_lanes:` gate: resolve_lane_for_stage is
    mocked to return a REAL lane, so a dropped/weakened gate would surface as a call that this test
    catches, unlike asserting patch_card_mock alone (which empty `lanes` already satisfies for free)."""
    fake_lane = {"id": "L1", "title": "Planning"}
    with patch("agileplace.resolve_lane_for_stage", return_value=(fake_lane, {"L1"})) as resolve_mock:
        _, _, patch_card_mock, _ = _run_main_once(
            tmp_path, _zero_status_inputs(), field_meta_return=None)

    resolve_mock.assert_not_called()
    patch_card_mock.assert_not_called()
