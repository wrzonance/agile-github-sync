"""sync.main()'s description-sync call site (Task 6/7, issue #65).

Thin, isolated from the large test_sync_main.py suite: this file's only concern is that main()'s
per-issue loop actually calls description_sync.sync_description() with the right positional
arguments (cfg, apply, issue, card, issues_state, queue) -- once per syncable issue, after the
lane/metadata/dates steps for that issue. It does not re-verify sync_description()'s own merge/
truncation/conflict behavior -- that is tests/test_description_sync.py's job -- so
sync_description itself is monkeypatched here, matching the low-level-transport-boundary
convention this repo's other main()-level call-site tests use (test_sync_intake_call_site.py
patches intake.promote() the same way; patching the collaborator, not its internals).

sync.py imports the name directly (`from description_sync import sync_description`), so the call
site lives in sync's own namespace -- the patch target is "sync.sync_description", not
"description_sync.sync_description".

Run: pytest -q tests/test_sync_description_call_site.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sync  # noqa: E402

# _mock_io/_card/_cfg are test_sync_main.py's richer I/O-boundary-mocking helpers -- reused here
# rather than duplicated, matching test_sync_intake_call_site.py's own precedent.
from test_sync_main import _card, _cfg, _issue, _mock_io  # noqa: E402

ISSUE_URL = "https://github.com/acme/repo/issues/1"


def test_main_calls_sync_description_once_per_issue_with_expected_args(tmp_path):
    state_file = tmp_path / ".sync-state.json"
    cfg = _cfg(tmp_path)
    issue = _issue()
    card = _card()
    stack, _run_mock, _patch_card_mock, _create_card_mock = _mock_io(
        card, ({}, []), field_meta_return=None)
    sync_description_mock = stack.enter_context(patch("sync.sync_description"))

    with stack, patch("sync.env_config", return_value=cfg), patch("sync.STATE_FILE", state_file), \
         patch("sys.argv", ["sync.py", "--apply"]):
        sync.main()

    sync_description_mock.assert_called_once()
    call_cfg, call_apply, call_issue, call_card, call_issues_state, call_queue = (
        sync_description_mock.call_args.args)
    assert call_cfg is cfg
    assert call_apply is True
    # main() annotates has_open_pr onto each issue (issue #14, pre-#65) before the per-issue loop,
    # so the issue sync_description receives carries that field too -- same precedent
    # test_sync_intake_call_site.py's own comment documents for intake.promote()'s issues arg.
    assert call_issue == {**issue, "has_open_pr": False}
    assert call_card["id"] == card["id"]
    # sync_metadata (labels/milestone) runs on this same issue immediately before sync_description
    # in the per-issue loop, so by the time sync_description is called issues_state already carries
    # the keys sync_metadata itself learned this run -- this call site doesn't own those keys, it
    # only asserts sync_description sees the same live per-issue state dict main() threads through.
    assert call_issues_state == {
        ISSUE_URL: {"card_id": "C1", "labels": [], "milestone": None},
    }
    assert callable(call_queue)


def test_main_never_reaches_real_sync_description_for_an_unresolved_card(tmp_path):
    """A syncable issue with no matching/created card this run (card_for(issue) is falsy) must
    `continue` before reaching sync_description -- mirrors the existing lane/metadata/dates guard
    a few lines above the call site."""
    state_file = tmp_path / ".sync-state.json"
    cfg = _cfg(tmp_path)
    stack, _run_mock, _patch_card_mock, _create_card_mock = _mock_io(
        _card(), ({}, []), field_meta_return=None, existing_cards=[])
    sync_description_mock = stack.enter_context(patch("sync.sync_description"))
    # No card matches this issue and creation is suppressed by patching create_card to return {}
    # (no "id"), so card_for(issue) stays falsy and the per-issue loop's `continue` guard fires
    # before ever reaching sync_description.

    with stack, patch("sync.env_config", return_value=cfg), patch("sync.STATE_FILE", state_file), \
         patch("sys.argv", ["sync.py", "--apply"]):
        sync.main()

    sync_description_mock.assert_not_called()
