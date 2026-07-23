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
        "ap_description_max_length": 20000,  # issue #65: sync_description reads this unconditionally
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
        # issue #65: keeps agileplace.card_description() on its zero-I/O path.
        "description": "",
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
    assert "retired issue has only a customId card match" not in out, (
        "the contested-skip must fire before the retirement loop's customId-fallback WARN branch")
    assert "DRY   retire" not in out

    create_card.assert_not_called()
    patch_card.assert_not_called()


def test_retirement_customid_fallback_does_not_duplicate_the_layer1_contested_warn(
        tmp_path, monkeypatch, capsys):
    """A retired issue whose own GitHub URL is unrelated to a contested card must not re-trigger a
    second WARN for that same card id via the retirement loop's customId-only-match fallback, even
    when the retired issue's customId happens to equal the contested card's customId. Layer 1
    already printed the 'card N claimed by K issue URLs' WARN for that card once; the fallback
    branch reads the unfiltered all_card_by_cid and must exclude cards already reported as
    contested, or the same card id gets warned about twice under two different messages."""
    issue1 = _issue(1, "widget one")
    issue2 = _issue(2, "widget two")
    contested_card = _card_with_urls("100", "ABC", [issue1["url"], issue2["url"]])
    # customId "ABC" matches contested_card's customId, but issue 30's own URL claims no card.
    unrelated_retired = _issue(30, "[ABC] fix the thing", state="CLOSED", state_reason="NOT_PLANNED")

    create_card, patch_card = _run_main(
        tmp_path, monkeypatch, [issue1, issue2, unrelated_retired], cards=[contested_card])

    out = capsys.readouterr().out
    assert out.count("WARN  card 100 claimed by") == 1
    assert "retired issue has only a customId card match" not in out, (
        "exactly one WARN per contested card id -- never duplicated by the retirement-loop "
        "customId-match WARN branch")

    create_card.assert_not_called()
    patch_card.assert_not_called()


def test_card_by_cid_filter_checks_the_cards_own_id_not_the_customid_loop_variable(
        tmp_path, monkeypatch, capsys):
    """Regression guard for the spike-caught naming trap: card_by_cid's comprehension binds `cid` to
    each card's customId, NOT its id -- `contested` is keyed by card id, so the filter predicate must
    test the card's own id (`str(card["id"]) not in contested`), never the bare loop variable `cid`.
    A buggy `cid not in contested` compares a customId string against a set of numeric-id strings.

    Issue #75 widened contested_cards() to fence customId-only claims too, so an issue sharing the
    CONTESTED card's own customId is no longer a safe way to build an 'unrelated' fixture for this
    guard -- it now genuinely becomes a third claimant of the contested card (see
    test_sync_card_coherence.py's Invariant 3), which would invert this test's premise. This
    fixture instead uses a coincidental string collision between a SEPARATE, uncontested card's own
    customId and the contested card's id -- exactly the value the loop-var bug would wrongly
    compare against `contested`'s keys -- while staying entirely unrelated to the contested card's
    own customId ('ABC'), so issue #75's widened claim never reaches it."""
    issue1 = _issue(1, "widget one")
    issue2 = _issue(2, "widget two")
    contested_card = _card_with_urls("100", "ABC", [issue1["url"], issue2["url"]])
    # unrelated_card's OWN customId ("100") coincidentally equals contested_card's id string -- the
    # loop-var bug would compare this against `contested`'s keys -- but has nothing to do with the
    # contested card's own customId ("ABC"), and claims no URL of its own.
    unrelated_card = _card_with_urls("999", "100", [], lane_id="L-ELSEWHERE")
    issue3 = _issue(3, "[100] fix the thing")  # customId "100" matches unrelated_card's customId
    backlog_lane = {"id": "L-BACKLOG", "title": "Backlog", "cardStatus": "notStarted"}

    create_card, patch_card = _run_main(
        tmp_path, monkeypatch, [issue1, issue2, issue3],
        cards=[contested_card, unrelated_card], lanes=[backlog_lane])

    # issue3 must bind onto the existing unrelated card (999) via the customId fallback -- never
    # create a new card (which a loop-var bug excluding 999 from card_by_cid would force), and
    # never touch the contested card (100).
    create_card.assert_not_called()
    assert any(call.args[2].get("id") == "999" for call in patch_card.call_args_list), (
        "issue3 must match the existing unrelated card (999) via customId, not be starved into "
        "creating a new one by a loop-var bug that wrongly excludes it from card_by_cid")
    assert not any(call.args[2].get("id") == "100" for call in patch_card.call_args_list)
