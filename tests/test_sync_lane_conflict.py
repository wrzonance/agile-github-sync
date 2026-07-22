"""Integration tests for issue #70 Layer 2 wiring, revisited for issue #75.

card_coherence.lane_conflict() itself is pure and unit-tested in test_card_coherence.py. This file
originally exercised the REAL main() (every I/O boundary mocked: ghkit, ghproject, agileplace) to
pin that a card reached by two or more queue() calls carrying conflicting `/laneId` values gets
poisoned and its flush PATCH is skipped.

Issue #75 widened Layer 1 (card_coherence.contested_cards()) to fence a card claimed by >= 2
distinct issues via EITHER match path -- URL or customId fallback -- not just URL. The
duplicate-`[KEY]`-title-prefix shape these tests use (two distinct GitHub issues, distinct
numbers/URLs, sharing a title prefix so `issue_custom_id()` returns the same key for both, both
resolving to the SAME existing AgilePlace card via the customId match path with ZERO url claims of
their own) is now caught by the widened Layer 1 fence BEFORE either issue ever reaches queue() --
Layer 2's lane-conflict poisoning never fires for this shape any more, because the card is excluded
from every match/queue path this run (same "WARN card N claimed by K issue URLs, deferring" path
exercised directly in test_sync_contested_cards.py). This is issue #75's intended effect ("any-path
fence supersedes Layer 2 for that shape"), not a bug -- these tests now pin THAT outcome instead.

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


def _issue(number: int, key: str, *, assignees: tuple[str, ...] = (), state: str = "OPEN") -> dict:
    """A raw gh-CLI-shaped issue whose title-key customId is `key` -- shared across issues to force
    a customId-match collision onto one card."""
    return {
        "number": number,
        "title": f"[{key}] issue {number}",
        "state": state,
        "stateReason": "",
        "labels": [],
        "milestone": None,
        "assignees": [{"login": a} for a in assignees],
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


def _card_with_customid(card_id: str, custom_id: str, *, lane_id: str = "L-ELSEWHERE") -> dict:
    """One AgilePlace card matched only by customId (no externalLinks) -- so two distinct issues
    sharing that title-key customId both resolve to this SAME card via the customId path, with
    zero URL claims of their own -- exactly the shape issue #75's widened Layer 1 now fences."""
    return {
        "id": card_id,
        "version": 1,
        "customId": custom_id,
        "externalLinks": [],
        "laneId": lane_id,
        "tags": [],
        "plannedStart": None,
        "plannedFinish": None,
    }


_LANES = [
    {"id": "L-PROG", "title": "In Progress", "cardStatus": "started"},
    {"id": "L-REVIEW", "title": "In Review", "cardStatus": "started"},
]

# A third lane, used only by the three-distinct-values test below.
_LANES_WITH_DONE = _LANES + [{"id": "L-DONE", "title": "Done", "cardStatus": "finished"}]


def _run_main(tmp_path, monkeypatch, raw_issues, card, *, open_pr_numbers: frozenset = frozenset(),
              lanes: list[dict] = _LANES):
    monkeypatch.setattr(
        ghkit, "run", lambda *_a, **_k: SimpleNamespace(stdout=json.dumps(raw_issues)))
    stack = ExitStack()
    stack.enter_context(patch("ghkit.repo_name", return_value="acme/repo"))
    stack.enter_context(patch("ghkit.open_pr_issue_numbers", return_value=set(open_pr_numbers)))
    stack.enter_context(patch("ghkit.blocked_by_map", return_value={}))
    stack.enter_context(patch("ghkit.edit_label"))
    stack.enter_context(patch("ghkit.set_milestone"))
    stack.enter_context(patch("ghproject.configured", return_value=False))
    stack.enter_context(patch("ghproject.items", return_value={}))
    stack.enter_context(patch("ghproject.field_meta", return_value=None))
    stack.enter_context(patch("ghproject.hydrate_item_dates", return_value={}))
    stack.enter_context(patch("agileplace.board_layout", return_value=list(lanes)))
    stack.enter_context(patch("agileplace.list_cards", return_value=[card]))
    stack.enter_context(patch("agileplace.card_dependencies", return_value=[]))
    create_card = stack.enter_context(patch("agileplace.create_card", return_value={}))
    patch_card = stack.enter_context(patch("agileplace.patch_card"))
    with stack, patch("sync.env_config", return_value=_config(tmp_path)), \
         patch("sync.STATE_FILE", tmp_path / ".sync-state.json"), \
         patch("sys.argv", ["sync.py"]):
        sync.main()
    return create_card, patch_card


def test_customid_collision_with_divergent_lanes_is_fenced_by_layer1_not_poisoned(
        tmp_path, monkeypatch, capsys):
    """Two distinct issues (distinct URLs), same title-key customId, whose stages diverge to
    different target lanes: under issue #75's widened Layer 1, both are deferred by the
    contested-card fence before either reaches queue() -- no Layer 2 poisoning WARN fires, and
    the card never reaches create_card or patch_card."""
    in_progress_issue = _issue(1, "KEY", assignees=("dev",))     # -> stage "In progress"
    in_review_issue = _issue(2, "KEY")                            # has_open_pr below -> "In review"
    card = _card_with_customid("500", "KEY")

    create_card, patch_card = _run_main(
        tmp_path, monkeypatch, [in_progress_issue, in_review_issue], card,
        open_pr_numbers=frozenset({2}))

    out = capsys.readouterr().out
    warn_lines = [line for line in out.splitlines() if line.startswith("WARN  card 500 claimed by")]
    assert len(warn_lines) == 1, "exactly one Layer 1 WARN for the contested card"
    assert "2 issue URLs" in warn_lines[0]
    assert "poisoned" not in out, "Layer 1 excludes the card before Layer 2 ever sees it"

    create_card.assert_not_called()
    patch_card.assert_not_called()


def test_customid_collision_with_convergent_lanes_is_still_fenced_by_layer1(
        tmp_path, monkeypatch, capsys):
    """Two distinct issues sharing a title-key customId that both resolve to the SAME target lane
    are STILL fenced by the widened Layer 1 -- unlike the pre-#75 world, agreement on the lane
    value doesn't rescue the pair, because Layer 1 fences on claimant COUNT alone, before any lane
    value is even considered."""
    first = _issue(1, "KEY", assignees=("dev",))   # -> stage "In progress"
    second = _issue(2, "KEY", assignees=("dev",))  # -> stage "In progress" (same target as first)
    card = _card_with_customid("500", "KEY")

    create_card, patch_card = _run_main(tmp_path, monkeypatch, [first, second], card)

    out = capsys.readouterr().out
    warn_lines = [line for line in out.splitlines() if line.startswith("WARN  card 500 claimed by")]
    assert len(warn_lines) == 1
    assert "poisoned" not in out

    create_card.assert_not_called()
    patch_card.assert_not_called()


def test_three_issue_customid_collision_is_fenced_once_regardless_of_lane_agreement(
        tmp_path, monkeypatch, capsys):
    """Three issues racing the same customId (one pair agreeing on a lane, one diverging) are all
    deferred together by a SINGLE Layer 1 WARN naming all three claiming URLs -- Layer 2 poisoning
    never fires because none of the three ever reaches queue()."""
    first = _issue(1, "KEY", assignees=("dev",))    # -> "In progress"
    second = _issue(2, "KEY")                        # has_open_pr below -> "In review"
    third = _issue(3, "KEY", assignees=("dev",))     # -> "In progress" (agrees with first)
    card = _card_with_customid("500", "KEY")

    create_card, patch_card = _run_main(
        tmp_path, monkeypatch, [first, second, third], card, open_pr_numbers=frozenset({2}))

    out = capsys.readouterr().out
    warn_lines = [line for line in out.splitlines() if line.startswith("WARN  card 500 claimed by")]
    assert len(warn_lines) == 1, "one WARN for the card, not one per issue"
    assert "3 issue URLs" in warn_lines[0]
    assert "poisoned" not in out

    create_card.assert_not_called()
    patch_card.assert_not_called()


def test_three_distinct_lane_values_still_collapse_to_a_single_layer1_warn(
        tmp_path, monkeypatch, capsys):
    """Three distinct /laneId targets (not just two) racing the same customId still produce exactly
    ONE Layer 1 WARN for the card -- Layer 1 excludes by claimant identity, not by counting distinct
    conflicting values, so a third divergent lane doesn't add a second warning."""
    first = _issue(1, "KEY", assignees=("dev",))          # -> "In progress"
    second = _issue(2, "KEY")                              # has_open_pr -> "In review"
    third = _issue(3, "KEY", state="CLOSED")               # -> "Done"
    card = _card_with_customid("500", "KEY")

    create_card, patch_card = _run_main(
        tmp_path, monkeypatch, [first, second, third], card,
        open_pr_numbers=frozenset({2}), lanes=_LANES_WITH_DONE)

    out = capsys.readouterr().out
    warn_lines = [line for line in out.splitlines() if line.startswith("WARN  card 500 claimed by")]
    assert len(warn_lines) == 1, "a single Layer 1 WARN regardless of how many distinct lane values raced"
    assert "3 issue URLs" in warn_lines[0]
    assert "poisoned" not in out

    create_card.assert_not_called()
    patch_card.assert_not_called()
