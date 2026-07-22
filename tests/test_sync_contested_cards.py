"""Integration tests for issue #70 Layer 1 wiring: contested-card exclusion in sync.main().

card_coherence.contested_cards() itself is pure and unit-tested in test_card_coherence.py. These
tests instead exercise the REAL main() (every I/O boundary mocked: ghkit, ghproject, agileplace) to
pin that a card claimed by >= 2 distinct GitHub issue URLs is excluded consistently everywhere main()
matches or queues work -- card_by_url, card_by_cid, retired_card_by_url, and syncable_issues -- and
that exactly one WARN line is printed per contested card id regardless of how many URLs claim it.

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

import ghkit  # noqa: E402
import sync  # noqa: E402


def _issue(number: int, title: str, *, state: str = "OPEN", state_reason: str = "") -> dict:
    return {
        "number": number,
        "title": title,
        "state": state,
        "stateReason": state_reason,
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
    }


def _card_with_urls(card_id: str, custom_id: str, urls: list[str], *, lane_id: str = "L1") -> dict:
    """One AgilePlace card whose externalLinks claim every url in `urls` -- the natural shape that
    makes two distinct GitHub issue URLs resolve to the SAME card via all_card_by_url."""
    return {
        "id": card_id,
        "version": 1,
        "customId": custom_id,
        "externalLinks": [{"url": u} for u in urls],
        "laneId": lane_id,
        "tags": [],
        "plannedStart": None,
        "plannedFinish": None,
    }


def _run_main(tmp_path, monkeypatch, raw_issues, cards, lanes=()):
    monkeypatch.setattr(
        ghkit, "run", lambda *_a, **_k: SimpleNamespace(stdout=json.dumps(raw_issues)))
    stack = ExitStack()
    stack.enter_context(patch("ghkit.repo_name", return_value="acme/repo"))
    stack.enter_context(patch("ghkit.open_pr_issue_numbers", return_value=set()))
    stack.enter_context(patch("ghkit.blocked_by_map", return_value={}))
    stack.enter_context(patch("ghkit.edit_label"))
    stack.enter_context(patch("ghkit.set_milestone"))
    stack.enter_context(patch("ghproject.configured", return_value=False))
    stack.enter_context(patch("ghproject.items", return_value={}))
    stack.enter_context(patch("ghproject.field_meta", return_value=None))
    stack.enter_context(patch("ghproject.hydrate_item_dates", return_value={}))
    stack.enter_context(patch("agileplace.board_layout", return_value=list(lanes)))
    stack.enter_context(patch("agileplace.list_cards", return_value=cards))
    stack.enter_context(patch("agileplace.card_dependencies", return_value=[]))
    create_card = stack.enter_context(patch("agileplace.create_card", return_value={}))
    patch_card = stack.enter_context(patch("agileplace.patch_card"))
    with stack, patch("sync.env_config", return_value=_config(tmp_path)), \
         patch("sync.STATE_FILE", tmp_path / ".sync-state.json"), \
         patch("sys.argv", ["sync.py"]):
        sync.main()
    return create_card, patch_card


def test_contested_card_excluded_from_all_active_match_paths_and_warns_once(
        tmp_path, monkeypatch, capsys):
    issue1 = _issue(1, "widget one")
    issue2 = _issue(2, "widget two")
    card = _card_with_urls("100", "1", [issue1["url"], issue2["url"]])

    create_card, patch_card = _run_main(tmp_path, monkeypatch, [issue1, issue2], cards=[card])

    out = capsys.readouterr().out
    warn_lines = [line for line in out.splitlines() if line.startswith("WARN  card 100 claimed by")]
    assert len(warn_lines) == 1, "exactly one WARN line per contested card id, not one per claiming URL"
    assert "2 issue URLs" in warn_lines[0]
    assert issue1["url"] in warn_lines[0] and issue2["url"] in warn_lines[0]

    # Neither contested issue is synced this run: no new card, no patch to the contested card.
    create_card.assert_not_called()
    patch_card.assert_not_called()


def test_contested_card_exclusion_is_consistent_across_retirement_and_active_paths(
        tmp_path, monkeypatch, capsys):
    """A card contested between a retired issue's URL and an active issue's URL must be excluded from
    BOTH retired_card_by_url and syncable_issues -- not partially retired while also partially
    creatable -- and the retirement loop's contested-skip must pre-empt the older 'customId-only
    match' WARN branch (the card's customId equals the retired issue's customId here, which would
    otherwise trip that other, unrelated WARN)."""
    retired = _issue(10, "widget ten", state="CLOSED", state_reason="NOT_PLANNED")
    active = _issue(20, "widget twenty")
    card = _card_with_urls("100", "10", [retired["url"], active["url"]],
                           lane_id="L5")  # customId "10" == retired's own custom id

    create_card, patch_card = _run_main(
        tmp_path, monkeypatch, [retired, active], cards=[card],
        lanes=[{"id": "L5", "title": "Done", "cardStatus": "finished"}])

    out = capsys.readouterr().out
    assert "WARN  card 100 claimed by 2 issue URLs, deferring:" in out
    assert "customId-only match" not in out, (
        "the contested-skip must fire before the retirement loop's customId-fallback WARN branch")
    assert "DRY   retire" not in out

    create_card.assert_not_called()
    patch_card.assert_not_called()


def test_card_by_cid_filter_checks_the_cards_own_id_not_the_customid_loop_variable(
        tmp_path, monkeypatch, capsys):
    """Regression guard for the spike-caught naming trap: card_by_cid's comprehension binds `cid` to
    each card's customId, NOT its id -- `contested` is keyed by card id, so the filter predicate must
    test the card's own id (`str(card["id"]) not in contested`), never the bare loop variable `cid`.
    A buggy `cid not in contested` compares a customId string against a set of numeric-id strings,
    which never collides, so it would silently keep the contested card in card_by_cid and let an
    unrelated issue that only matches by customId bind onto it instead of getting its own new card."""
    issue1 = _issue(1, "widget one")
    issue2 = _issue(2, "widget two")
    contested_card = _card_with_urls("100", "ABC", [issue1["url"], issue2["url"]])
    issue3 = _issue(3, "[ABC] fix the thing")  # customId "ABC" matches contested_card's customId

    create_card, patch_card = _run_main(
        tmp_path, monkeypatch, [issue1, issue2, issue3], cards=[contested_card])

    create_card.assert_called_once()
    # create_card(cfg, apply, title, custom_id, external_url, lane_id) -- the un-contested issue3
    # must get its OWN new card, not silently bind onto the excluded contested card.
    assert create_card.call_args.args[3] == "ABC"
    assert not any(call.args[2].get("id") == "100" for call in patch_card.call_args_list)
