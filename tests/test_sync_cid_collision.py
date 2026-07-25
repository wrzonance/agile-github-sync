"""Integration tests for issue #95: sync.main()'s board-wide customId index (`all_card_by_cid`)
fences colliding customId keys instead of letting AgilePlace's own listing order pick a winner.

card_coherence.fence_cid_index() itself is pure and exhaustively unit-tested in
test_card_coherence.py; test_sync_fence_cid_index_site1.py already pins the wiring itself (WARN
print order, drop-in-replacement for the non-colliding path). These tests instead pin the specific
acceptance-criteria shapes named for main()'s board-wide index: an exact-duplicate customId pair,
a mixed old/new-header-format pair, and that an unrelated, URL-matchable issue elsewhere on the
board is completely unaffected by a collision among other cards.

Run: pytest -q
"""
from __future__ import annotations

import json
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agileplace  # noqa: E402
import board_layout  # noqa: E402
import ghkit  # noqa: E402
import sync  # noqa: E402


def _issue(number: int, title: str) -> dict:
    return {
        "number": number,
        "title": title,
        "state": "OPEN",
        "stateReason": "",
        "labels": [],
        "milestone": None,
        "assignees": [],
        "url": f"https://github.com/acme/repo/issues/{number}",
    }


def _config(tmp_path) -> dict:
    return {
        "token": "token",
        "host": "example.leankit.com",
        "board_id": "42",
        "target_repo_path": tmp_path,
        "label_sync_ignore": frozenset(),
        "stage_lane_map": {},
        "gh_project": {},
        "ap_description_max_length": 20000,  # issue #65: sync_description reads this unconditionally
    }


def _cid_only_card(card_id: str, custom_id: str) -> dict:
    """One AgilePlace card whose customId carries a match key but with NO external link at all --
    the only way for a customId key to collide purely on the AgilePlace side, independent of any
    issue's own URL."""
    return {
        "id": card_id,
        "version": 1,
        "customId": custom_id,
        "laneId": "L1",
        "tags": [],
        "plannedStart": None,
        "plannedFinish": None,
        "description": "",
    }


def _card_with_url(card_id: str, custom_id: str, url: str) -> dict:
    """One AgilePlace card that matches its issue via URL -- unaffected by any customId collision
    elsewhere on the board."""
    return {
        "id": card_id,
        "version": 1,
        "customId": custom_id,
        "externalLinks": [{"url": url}],
        "laneId": "L1",
        "tags": [],
        "plannedStart": None,
        "plannedFinish": None,
        "description": "",
    }


def _run_main(tmp_path, monkeypatch, raw_issues, cards, lanes=()):
    monkeypatch.setattr(
        ghkit, "run", lambda *_a, **_k: SimpleNamespace(stdout=json.dumps(raw_issues)))
    stack = ExitStack()
    stack.enter_context(patch(
        "ghkit.resolve_repo_context",
        return_value=ghkit.RepoContext(owner="acme", name="repo", host="github.com")))
    stack.enter_context(patch("ghkit.open_pr_issue_numbers", return_value=set()))
    stack.enter_context(patch("ghkit.blocked_by_map", return_value={}))
    stack.enter_context(patch("ghkit.edit_label"))
    stack.enter_context(patch("ghkit.set_milestone"))
    stack.enter_context(patch("ghproject.configured", return_value=False))
    stack.enter_context(patch("ghproject.items", return_value={}))
    stack.enter_context(patch("ghproject.field_meta", return_value=None))
    stack.enter_context(patch("ghproject.hydrate_item_dates", return_value={}))
    stack.enter_context(patch(
        "board_layout.board_layout",
        return_value=board_layout.BoardLayout(lanes=list(lanes), card_types=[]),
    ))
    stack.enter_context(patch("agileplace.list_cards", return_value=cards))
    stack.enter_context(patch("agileplace.card_dependencies", return_value=[]))
    create_card = stack.enter_context(patch("agileplace.create_card", return_value={}))
    patch_card = stack.enter_context(patch("agileplace.patch_card"))
    with stack, patch("sync.env_config", return_value=_config(tmp_path)), \
         patch("sync.STATE_FILE", tmp_path / ".sync-state.json"), \
         patch("sys.argv", ["sync.py"]):
        sync.main()
    return create_card, patch_card


def test_exact_duplicate_customid_pair_both_excluded_with_one_warn_naming_both_ids(
        tmp_path, monkeypatch, capsys):
    """Two cards sharing the literal same customId string ('7', no header formatting involved at
    all) must both be fenced out of all_card_by_cid, with exactly one WARN line naming both ids --
    never a silent last-wins clobber."""
    first = _cid_only_card("300", "7")
    second = _cid_only_card("301", "7")
    issue = _issue(7, "widget")  # no [KEY] prefix -> issue_custom_id() falls back to "7"

    create_card, patch_card = _run_main(
        tmp_path, monkeypatch, [issue], cards=[first, second])

    out = capsys.readouterr().out
    warn_lines = [line for line in out.splitlines()
                  if line.startswith("WARN  customId 7 claimed by")]
    assert len(warn_lines) == 1, "exactly one WARN line for the colliding customId key"
    assert "2 cards" in warn_lines[0]
    assert "300" in warn_lines[0] and "301" in warn_lines[0]

    # Neither colliding card is adopted -- the unmatched issue takes the create path instead.
    create_card.assert_called_once()
    patch_card.assert_not_called()


def test_mixed_old_and_new_header_format_pair_both_excluded_with_one_warn(
        tmp_path, monkeypatch, capsys):
    """A bare-key card ('12') and a header-format card ('KEY (GitHub Issue #12)') that both
    normalize to the same header_match_key must be fenced together, exactly like an exact-string
    duplicate -- normalization, not literal string equality, drives the collision."""
    old_format = _cid_only_card("400", "12")
    header_format = _cid_only_card("401", "GitHub Issue #12")
    issue = _issue(12, "widget")

    create_card, patch_card = _run_main(
        tmp_path, monkeypatch, [issue], cards=[old_format, header_format])

    out = capsys.readouterr().out
    warn_lines = [line for line in out.splitlines()
                  if line.startswith("WARN  customId 12 claimed by")]
    assert len(warn_lines) == 1, "exactly one WARN line for the colliding customId key"
    assert "2 cards" in warn_lines[0]
    assert "400" in warn_lines[0] and "401" in warn_lines[0]

    create_card.assert_called_once()
    patch_card.assert_not_called()


def test_unrelated_url_matchable_issue_elsewhere_on_board_is_unaffected(
        tmp_path, monkeypatch, capsys):
    """A customId collision between two cards must not disturb a completely unrelated issue that
    matches its own card via URL elsewhere on the board -- fence_cid_index only ever excludes the
    colliding customId key from the index, never touches all_card_by_url, and never affects any
    other card's processing this run."""
    colliding_a = _cid_only_card("500", "9")
    colliding_b = _cid_only_card("501", "9")
    collision_issue = _issue(9, "widget nine")

    elsewhere_issue = _issue(50, "widget fifty")
    elsewhere_card = _card_with_url("999", "50", elsewhere_issue["url"])

    create_card, patch_card = _run_main(
        tmp_path, monkeypatch, [collision_issue, elsewhere_issue],
        cards=[colliding_a, colliding_b, elsewhere_card])

    out = capsys.readouterr().out
    warn_lines = [line for line in out.splitlines()
                  if line.startswith("WARN  customId 9 claimed by")]
    assert len(warn_lines) == 1

    # The colliding issue gets a new card (its own customId match was fenced away)...
    create_card.assert_called_once()
    # ...while the unrelated issue is synced normally via its own card's URL match, untouched by
    # the collision among the other two cards.
    patch_card.assert_called_once()
    assert patch_card.call_args_list[0].args[2].get("id") == "999"


def test_two_active_issues_sharing_a_fenced_customid_are_deferred_not_collapsed(
        tmp_path, monkeypatch, capsys):
    """Regression (issue #95, Codex draft review): fencing a colliding customId out of the board
    index must not silently REOPEN the last-wins clobber one axis over. Two AgilePlace cards ('600',
    '601') collide on customId '5' -- fenced from all_card_by_cid -- while TWO active issues both
    carry a '[5]' prefix (issue_custom_id -> '5') and neither matches a card by URL. Pre-this-fix
    both issues resolved to no card, escaped contested_cards(), and were both syncable: the first
    created a fresh card registered under '5', the second then adopted THAT card via the reconciled
    customId index and silently overwrote the first's header/metadata/description -- exactly the
    data-loss the index fence was meant to stop, just moved from card-vs-card to issue-vs-issue.
    Post-fix, a fenced customId shared by >= 2 active issues fails closed: BOTH customId-only
    claimants are deferred (one WARN each), no card is created for either, and nothing is patched."""
    first = _cid_only_card("600", "5")
    second = _cid_only_card("601", "5")
    issue_a = _issue(7, "[5] alpha")   # title_key -> '5'
    issue_b = _issue(8, "[5] beta")    # title_key -> '5' (same fenced key, no URL match)

    create_card, patch_card = _run_main(
        tmp_path, monkeypatch, [issue_a, issue_b], cards=[first, second])

    out = capsys.readouterr().out
    # The board-index-build fence still fires once, naming both colliding cards.
    index_warn = [line for line in out.splitlines()
                  if line.startswith("WARN  customId 5 claimed by")]
    assert len(index_warn) == 1 and "600" in index_warn[0] and "601" in index_warn[0]
    # Both active issues are deferred rather than collapsed onto one card.
    defer_warn = [line for line in out.splitlines()
                  if line.startswith("WARN  deferring active card [5]")]
    assert len(defer_warn) == 2, "each customId-only claimant of the fenced key is deferred"
    # Fail closed: neither issue creates a card, and nothing is overwritten.
    create_card.assert_not_called()
    patch_card.assert_not_called()
