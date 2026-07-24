"""sync.main()'s comment-sync call site (Task 6/8, issue #66).

Thin, isolated from the large test_sync_main.py suite: this file's only concern is that main()
actually calls comment_sync.sync_comments() with the right positional arguments (cfg, apply, issue,
card, issues_state) -- once per syncable issue, and that the call is gated correctly. Comment sync
writes live to both platforms (its endpoints aren't part of the card-PATCH queue), so main() runs it
in a dedicated pass AFTER the card-PATCH flush, only on a dry run or a CLEAN (non-poisoned) apply --
never on a poisoned apply where save_state is held (issue #66 Codex P1 #4). It does not re-verify
sync_comments()'s own planning/execution/ledger behavior -- that is tests/test_comment_sync.py's job
-- so sync_comments itself is monkeypatched here for the call-site assertions, matching the
low-level-transport-boundary convention this repo's other main()-level call-site tests use
(test_sync_description_call_site.py patches description_sync.sync_description the same way; patching
the collaborator, not its internals).

sync.py imports the name directly (`from comment_sync import sync_comments`), so the call site
lives in sync's own namespace -- the patch target is "sync.sync_comments", not
"comment_sync.sync_comments".

The self-disable-WARN-fires-at-most-once-per-run test below is the one exception: it exercises the
REAL comment_sync.sync_comments (not monkeypatched) across two issues in the same main() run, since
that "at most once per process run" invariant is specifically about comment_sync's own module-level
state as driven through the real wiring, not something a mock of sync_comments could observe.

Run: pytest -q tests/test_sync_comments_call_site.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import comment_sync  # noqa: E402
import sync  # noqa: E402

# _mock_io/_card/_cfg are test_sync_main.py's richer I/O-boundary-mocking helpers -- reused here
# rather than duplicated, matching test_sync_description_call_site.py's own precedent.
from test_sync_main import _card, _cfg, _issue, _mock_io  # noqa: E402

ISSUE_URL = "https://github.com/acme/repo/issues/1"


def _reset_warned_disabled(monkeypatch) -> None:
    monkeypatch.setattr(comment_sync, "_warned_disabled", False)


def test_main_calls_sync_comments_once_per_issue_with_expected_args(tmp_path):
    state_file = tmp_path / ".sync-state.json"
    cfg = _cfg(tmp_path)
    issue = _issue()
    card = _card()
    stack, _run_mock, _patch_card_mock, _create_card_mock = _mock_io(
        card, ({}, []), field_meta_return=None)
    sync_description_mock = stack.enter_context(patch("sync.sync_description"))
    sync_comments_mock = stack.enter_context(patch("sync.sync_comments"))

    with stack, patch("sync.env_config", return_value=cfg), patch("sync.STATE_FILE", state_file), \
         patch("sys.argv", ["sync.py", "--apply"]):
        sync.main()

    sync_comments_mock.assert_called_once()
    call_cfg, call_apply, call_issue, call_card, call_issues_state = (
        sync_comments_mock.call_args.args)
    assert call_cfg is cfg
    assert call_apply is True
    # main() annotates has_open_pr onto each issue (issue #14) before the per-issue loop, so the
    # issue sync_comments receives carries that field too -- same precedent
    # test_sync_description_call_site.py's own comment documents.
    assert call_issue == {**issue, "has_open_pr": False}
    assert call_card["id"] == card["id"]
    assert call_issues_state == {
        ISSUE_URL: {"card_id": "C1", "labels": [], "milestone": None},
    }
    # sync_description still runs (in the per-issue loop); sync_comments runs in the post-flush pass.
    assert sync_description_mock.call_count == 1


def test_main_defers_comment_sync_and_holds_state_when_a_card_is_poisoned(tmp_path, monkeypatch):
    """Issue #66 Codex P1 #4: comment sync writes live to both platforms, so it must NOT run on a
    poisoned apply -- the run where save_state is held (issue #70 hold-clean-bases). Otherwise one
    issue's poisoned card would strand another issue's applied comment writes with no persisted
    ledger. A poisoned card => sync_comments NOT called AND state NOT persisted."""
    state_file = tmp_path / ".sync-state.json"
    cfg = _cfg(tmp_path)
    # A customId mismatch forces a queue() call for this card; forcing lane_conflict poisons that entry.
    card = {**_card(), "customId": "999"}
    stack, _run_mock, _patch_card_mock, _create_card_mock = _mock_io(
        card, ({}, []), field_meta_return=None)
    stack.enter_context(patch("sync.sync_description"))
    sync_comments_mock = stack.enter_context(patch("sync.sync_comments"))
    save_state_mock = stack.enter_context(patch("sync.save_state"))
    monkeypatch.setattr(sync, "lane_conflict", lambda ops, lane_id: (None, True))

    with stack, patch("sync.env_config", return_value=cfg), patch("sync.STATE_FILE", state_file), \
         patch("sys.argv", ["sync.py", "--apply"]):
        sync.main()

    sync_comments_mock.assert_not_called()
    save_state_mock.assert_not_called()


def test_main_never_reaches_real_sync_comments_for_an_unresolved_card(tmp_path):
    """A syncable issue with no matching/created card this run (card_for(issue) is falsy) must
    `continue` before reaching sync_comments -- mirrors the existing lane/metadata/dates/description
    guard a few lines above the call site."""
    state_file = tmp_path / ".sync-state.json"
    cfg = _cfg(tmp_path)
    stack, _run_mock, _patch_card_mock, _create_card_mock = _mock_io(
        _card(), ({}, []), field_meta_return=None, existing_cards=[])
    stack.enter_context(patch("sync.sync_description"))
    sync_comments_mock = stack.enter_context(patch("sync.sync_comments"))
    # No card matches this issue and creation is suppressed by patching create_card to return {}
    # (no "id"), so card_for(issue) stays falsy and the per-issue loop's `continue` guard fires
    # before ever reaching sync_comments.

    with stack, patch("sync.env_config", return_value=cfg), patch("sync.STATE_FILE", state_file), \
         patch("sys.argv", ["sync.py", "--apply"]):
        sync.main()

    sync_comments_mock.assert_not_called()


def test_self_disable_warn_fires_at_most_once_across_multiple_issues_in_one_run(tmp_path,
                                                                                monkeypatch,
                                                                                capsys):
    """Drives the REAL comment_sync.sync_comments through main()'s wiring (not monkeypatched) for
    two issues in the same run. cfg from _cfg() carries no "comment_sync_identity" key, so comment
    sync is self-disabled for both issues -- the WARN comment_sync emits on that path must still
    only print once per process run, never once per issue."""
    _reset_warned_disabled(monkeypatch)
    state_file = tmp_path / ".sync-state.json"
    cfg = _cfg(tmp_path)
    assert "comment_sync_identity" not in cfg
    issues = [
        {**_issue(), "number": 1, "url": ISSUE_URL},
        {**_issue(), "number": 2, "url": ISSUE_URL + "-2"},
    ]
    cards = [
        {**_card(), "id": "C1", "customId": "1", "externalLink": {"url": ISSUE_URL}},
        {**_card(), "id": "C2", "customId": "2", "externalLink": {"url": ISSUE_URL + "-2"}},
    ]
    stack, _run_mock, _patch_card_mock, _create_card_mock = _mock_io(
        cards[0], ({}, []), field_meta_return=None, issue_return=issues,
        existing_cards=cards)
    stack.enter_context(patch("sync.sync_description"))

    with stack, patch("sync.env_config", return_value=cfg), patch("sync.STATE_FILE", state_file), \
         patch("sys.argv", ["sync.py", "--apply"]):
        sync.main()

    err = capsys.readouterr().err
    assert err.count("WARN") == 1
