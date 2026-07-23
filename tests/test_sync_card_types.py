"""sync.main()'s card-types wiring (Task 5/9, issue #82).

Thin, isolated from the large test_sync_main.py suite -- mirrors test_sync_dates.py's mock-boundary
idiom (patch exactly the collaborator, assert on the recorded queue()/patch_card()/create_card()
calls) but exercised through the REAL main(), since the wiring under test here (the BoardLayout
destructure, the resolve_card_type_ids warnings-print loop, threading type_by_name into the new-card
creation path, and the one new card_types.sync_card_type(...) per-issue call) all lives in main()
itself, not in a standalone function. card_types.py's own pure boundary invariants (derivation
precedence, _decide's branch table, resolve_card_type_ids idempotence) are tests/test_card_types.py's
job -- this file only pins that main() actually wires them together with the right arguments at the
right point in the pipeline.

Run: pytest -q
"""
from __future__ import annotations

import json
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agileplace  # noqa: E402
import board_layout  # noqa: E402
import sync  # noqa: E402

ISSUE_URL = "https://github.com/acme/repo/issues/1"

_BUG_CARD_TYPE = {"id": "CT-BUG", "title": "Bug", "isCardType": True}


def _issue(issue_type=None, labels=None):
    return {"number": 1, "title": "widget", "state": "OPEN", "labels": labels or [],
            "milestone": None, "assignees": [], "url": ISSUE_URL, "issue_type": issue_type}


def _card(type_obj=None, **overrides):
    # "description": "" (issue #65) keeps agileplace_description.card_description() on its zero-I/O
    # path -- without the key it falls back to the real (unmocked) agileplace.get_card(), which hits
    # the live HTTP client and SystemExits (see tests/test_description_sync_wiring_fixtures.py).
    card = {"id": "C1", "version": 1, "customId": "1",
            "externalLink": {"url": ISSUE_URL}, "tags": [],
            "plannedStart": None, "plannedFinish": None, "laneId": None,
            "description": ""}
    if type_obj is not None:
        card["type"] = type_obj
    card.update(overrides)
    return card


def _cfg(tmp_path, online=True):
    """`online=False` mirrors main()'s own `bool(cfg["token"] and cfg["host"] and cfg["board_id"])`
    gate (issue #82 review finding) -- an unconfigured/offline run never has all three set."""
    return {
        "token": "tok" if online else None,
        "host": "example.leankit.com" if online else None,
        "board_id": "42" if online else None,
        "target_repo_path": tmp_path,
        "label_sync_ignore": frozenset(),
        "stage_lane_map": {},
        "gh_project": {"owner": "acme", "number": "7", "status_field": "Status",
                       "start_field": "Start", "target_field": "Target"},
    }


def _run_main(tmp_path, issue, card_types=(), existing_cards=None, seed_issues_state=None,
             apply=True, create_card_return=None, get_card_return=None, online=True):
    """ExitStack covering every I/O boundary main() touches, with card_types under caller control
    (test_sync_main._mock_io hardcodes card_types=[] -- this file's whole point is exercising it
    non-empty). Returns the patch_card and create_card mocks for call-site assertions.

    `create_card_return`/`get_card_return` let a caller simulate a REALISTIC create response (an id,
    optionally refetched into a full card) instead of the bare `{}` every other test here uses --
    `{}` never registers into card_by_url/card_by_cid (no "id" key), so it can't exercise the
    same-pass create-then-sync_card_type wiring at all.

    `online=False` drives main() down its unconfigured/offline branch (cfg's token/host/board_id all
    None) -- board_layout is never even called there (main() substitutes an empty BoardLayout
    itself), so the mocked "board_layout.board_layout" return_value below is simply unused in that
    case."""
    cfg = _cfg(tmp_path, online=online)
    state_file = tmp_path / ".sync-state.json"
    if seed_issues_state is not None:
        state_file.write_text(json.dumps({"schema": sync.STATE_SCHEMA, "target": "acme/repo",
                                          "board": "42",
                                          "issues": {ISSUE_URL: seed_issues_state}}),
                              encoding="utf-8")
    stack = ExitStack()
    stack.enter_context(patch("ghkit.repo_name", return_value="acme/repo"))
    stack.enter_context(patch("ghkit.list_issues", return_value=[issue]))
    stack.enter_context(patch("ghkit.open_pr_issue_numbers", return_value=set()))
    stack.enter_context(patch("ghkit.blocked_by_map", return_value={}))
    stack.enter_context(patch("ghkit.run", return_value=Mock(stdout="")))
    stack.enter_context(patch("ghproject.configured", return_value=False))
    stack.enter_context(patch(
        "board_layout.board_layout",
        return_value=board_layout.BoardLayout(lanes=[], card_types=list(card_types)),
    ))
    cards = existing_cards if existing_cards is not None else []
    stack.enter_context(patch("agileplace.list_cards", return_value=cards))
    stack.enter_context(patch("agileplace.card_dependencies", return_value=[]))
    patch_card_mock = stack.enter_context(patch("agileplace.patch_card"))
    create_card_mock = stack.enter_context(
        patch("agileplace.create_card", return_value=create_card_return
              if create_card_return is not None else {}))
    if get_card_return is not None:
        stack.enter_context(patch("agileplace.get_card", return_value=get_card_return))

    argv = ["sync.py", "--apply"] if apply else ["sync.py"]
    with stack, patch("sync.env_config", return_value=cfg), patch("sync.STATE_FILE", state_file), \
         patch("sys.argv", argv):
        sync.main()
    return patch_card_mock, create_card_mock, state_file


# --- resolve_card_type_ids warnings surface through main() -----------------------------------

def test_main_prints_resolve_card_type_ids_warnings_for_unresolved_names(tmp_path, capsys):
    """No card types configured at all -> every CARD_TYPE_RULES target name is unresolved, and
    main() must print resolve_card_type_ids's one WARN per name (the same print-loop shape as the
    existing fenced.warnings loop just above it)."""
    _run_main(tmp_path, _issue(), card_types=[])

    out = capsys.readouterr().out
    assert "WARN  no eligible board card type named 'Bug'" in out
    assert "WARN  no eligible board card type named 'New Feature'" in out
    assert "WARN  no eligible board card type named 'Documentation'" in out
    assert "WARN  no eligible board card type named 'Improvement'" in out


def test_main_prints_no_card_type_warnings_when_agileplace_is_not_configured(tmp_path, capsys):
    """Issue #82 review finding: when AgilePlace is not configured at all (online == False), main()
    substitutes an empty BoardLayout(card_types=[]) -- resolve_card_type_ids([]) then finds zero
    eligible matches for every one of the 4 CARD_TYPE_RULES target names, so printing its warnings
    unconditionally would misleadingly WARN about a board that was never even queried. Offline runs
    must print none of them."""
    _run_main(tmp_path, _issue(), card_types=[], online=False, apply=False)

    out = capsys.readouterr().out
    assert "WARN  no eligible board card type named" not in out


def test_main_prints_no_card_type_warning_once_the_board_defines_it(tmp_path, capsys):
    """A board that DOES define an eligible 'Bug' card type must not warn about it, while the other
    (still-undefined) target names still do -- proves the warnings are per-name, not all-or-nothing."""
    _run_main(tmp_path, _issue(), card_types=[_BUG_CARD_TYPE])

    out = capsys.readouterr().out
    assert "WARN  no eligible board card type named 'Bug'" not in out
    assert "WARN  no eligible board card type named 'New Feature'" in out


# --- new-card creation threads the derived type_id/type_title ---------------------------------

def test_main_threads_resolved_type_id_into_create_card_for_a_new_card(tmp_path):
    """An issue whose native GitHub type derives to 'Bug', on a board that resolves 'Bug' to a real
    typeId, must have that typeId (and the derived name as type_title) passed straight into
    create_card's new trailing kwargs for a freshly-created card."""
    issue = _issue(issue_type="Bug")

    _, create_card_mock, _ = _run_main(tmp_path, issue, card_types=[_BUG_CARD_TYPE], existing_cards=[])

    create_card_mock.assert_called_once()
    assert create_card_mock.call_args.kwargs == {"type_id": "CT-BUG", "type_title": "Bug"}


def test_main_omits_type_id_when_derived_type_does_not_resolve(tmp_path):
    """Same derived-'Bug' issue, but the board has no eligible 'Bug' card type -- create_card must
    receive type_id=None, type_title=None rather than guessing or passing the unresolved name."""
    issue = _issue(issue_type="Bug")

    _, create_card_mock, _ = _run_main(tmp_path, issue, card_types=[], existing_cards=[])

    create_card_mock.assert_called_once()
    assert create_card_mock.call_args.kwargs == {"type_id": None, "type_title": None}


def test_main_omits_type_id_for_an_issue_that_derives_no_card_type(tmp_path):
    """A plain issue with no matching issue_type/label (derive_card_type_name -> None) must never
    even look up a typeId -- create_card still gets type_id=None, type_title=None."""
    issue = _issue()

    _, create_card_mock, _ = _run_main(tmp_path, issue, card_types=[_BUG_CARD_TYPE], existing_cards=[])

    create_card_mock.assert_called_once()
    assert create_card_mock.call_args.kwargs == {"type_id": None, "type_title": None}


# --- existing-card drift: card_types.sync_card_type is wired into the per-issue loop -----------

def test_main_queues_a_typeid_patch_when_an_existing_cards_type_has_drifted(tmp_path):
    """An existing card with no nested type at all, matched to an issue that derives 'Bug' on a
    board that resolves it -- main()'s new card_types.sync_card_type(...) call must queue the
    /typeId patch op, and it must reach the real patch_card() flush."""
    issue = _issue(issue_type="Bug")
    card = _card()

    patch_card_mock, create_card_mock, _ = _run_main(
        tmp_path, issue, card_types=[_BUG_CARD_TYPE], existing_cards=[card])

    create_card_mock.assert_not_called()  # card already exists -- only the drift-sync path applies
    patch_card_mock.assert_called_once()
    ops = patch_card_mock.call_args.args[3]
    assert {"op": "replace", "path": "/typeId", "value": "CT-BUG"} in ops


def test_main_persists_the_new_type_base_only_when_applying(tmp_path):
    """After a confirmed apply-mode type sync, issues_state[url]["type"] must be persisted as the
    new derived base -- confirming the sync_card_type call site is wired all the way through to
    save_state(), not just to queue()."""
    issue = _issue(issue_type="Bug")
    card = _card()

    _, _, state_file = _run_main(
        tmp_path, issue, card_types=[_BUG_CARD_TYPE], existing_cards=[card], apply=True)

    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["issues"][ISSUE_URL]["type"] == "Bug"


def test_main_does_not_requeue_once_the_cards_type_already_matches(tmp_path):
    """current == derived (the card's nested type.title already reads back as 'Bug') -> no /typeId
    op is queued at all -- proves sync_card_type's own no-op branch is reached through main(), not
    just always-fires."""
    issue = _issue(issue_type="Bug")
    card = _card(type_obj={"id": "CT-BUG", "title": "Bug"})

    patch_card_mock, _, _ = _run_main(
        tmp_path, issue, card_types=[_BUG_CARD_TYPE], existing_cards=[card])

    patch_card_mock.assert_not_called()


def test_main_does_not_double_queue_a_typeid_patch_for_a_card_created_this_same_pass(tmp_path):
    """Regression coverage for the create_card -> card_by_url -> card_for -> sync_card_type wiring
    chain (review finding on issue #82): every other test in this file mocks create_card to return a
    bare `{}`, which has no "id" key, so _ensure_cards_for_syncable_issues never registers the
    created card into card_by_url and step 2's card_types.sync_card_type never even sees it -- that
    starves the exact invariant the module docstring promises (a same-pass create must not also
    double-queue a typeId patch for the card it just made).

    Here create_card returns a realistic sparse response (just an id) and agileplace.get_card's
    refetch (issue #55's _created_card_snapshot) returns the full card echoing the typeId that was
    sent at create time -- current==derived once sync_card_type runs, so no /typeId patch is queued
    against the card this same pass just created."""
    issue = _issue(issue_type="Bug")
    created_sparse = {"id": "C-NEW"}
    refetched_full = _card(type_obj={"id": "CT-BUG", "title": "Bug"}, id="C-NEW", version=2)

    patch_card_mock, create_card_mock, _ = _run_main(
        tmp_path, issue, card_types=[_BUG_CARD_TYPE], existing_cards=[],
        create_card_return=created_sparse, get_card_return=refetched_full)

    create_card_mock.assert_called_once()
    assert create_card_mock.call_args.kwargs == {"type_id": "CT-BUG", "type_title": "Bug"}
    patch_card_mock.assert_not_called()
