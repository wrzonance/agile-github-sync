"""Integration tests for issue #70 Layer 2 wiring: queue() poisoning + flush skip in sync.main().

card_coherence.lane_conflict() itself is pure and unit-tested in test_card_coherence.py. These tests
instead exercise the REAL main() (every I/O boundary mocked: ghkit, ghproject, agileplace) to pin
that a card reached by two or more queue() calls carrying conflicting `/laneId` values gets poisoned
and its flush PATCH is skipped -- while same-value repeated `/laneId` ops never poison an entry, and
poisoning never resets back to False once set within a single run.

The duplicate-`[KEY]`-title-prefix shape is used deliberately: two distinct GitHub issues (distinct
numbers/URLs) share a title prefix, so `issue_custom_id()` returns the same key for both, and both
resolve to the SAME existing AgilePlace card via the customId match path (`card_by_cid`), each with
its own stage-derived target lane. This is NOT the two-URL-contested shape Layer 1 already excludes
(that shape never reaches queue() at all -- see test_sync_contested_cards.py); a naive first draft
tried to force this via `_reconciled_custom_id_index`'s URL-correction path instead, but that path
only fires for a URL-matched issue reclaiming a *previously-unclaimed* customId, which requires a URL
match in the first place and doesn't produce two issues racing the same customId entry -- the
duplicate-title-prefix shape above is the one actually verified to reach queue() twice.

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
    sharing that title-key customId both resolve to this SAME card via the customId path."""
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

# A third lane, used only by the three-distinct-values test below to exercise a second conflicting
# call (rather than a second call that merely re-poisons the same already-seen value).
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


def test_conflicting_laneid_ops_poison_the_card_and_skip_its_flush(tmp_path, monkeypatch, capsys):
    """Two distinct issues (distinct URLs), same title-key customId, resolving to the SAME card via
    the customId match path, whose stages diverge to different target lanes: the card must be
    poisoned (WARN'd once) and NEVER reach patch_card at flush -- and never create_card either, since
    both issues match an EXISTING card, not a new one."""
    in_progress_issue = _issue(1, "KEY", assignees=("dev",))     # -> stage "In progress" -> L-PROG
    in_review_issue = _issue(2, "KEY")                            # has_open_pr below -> "In review"
    card = _card_with_customid("500", "KEY")

    create_card, patch_card = _run_main(
        tmp_path, monkeypatch, [in_progress_issue, in_review_issue], card,
        open_pr_numbers=frozenset({2}))

    out = capsys.readouterr().out
    poison_lines = [line for line in out.splitlines() if line.startswith("WARN  card 500 poisoned")]
    assert len(poison_lines) == 1, "exactly one poisoning WARN for the card, not one per op"
    assert "'L-REVIEW'" in poison_lines[0], (
        "WARN must name the actual conflicting /laneId value, not a placeholder")
    assert "new value" not in poison_lines[0]

    create_card.assert_not_called()
    patch_card.assert_not_called()


def test_same_value_repeated_laneid_ops_never_poison_the_entry(tmp_path, monkeypatch, capsys):
    """Two distinct issues sharing a title-key customId that both resolve to the SAME target lane
    must NOT poison the card: repeated agreement is not conflict, and the flush PATCH still fires."""
    first = _issue(1, "KEY", assignees=("dev",))   # -> stage "In progress" -> L-PROG
    second = _issue(2, "KEY", assignees=("dev",))  # -> stage "In progress" -> L-PROG (same target)
    card = _card_with_customid("500", "KEY")

    create_card, patch_card = _run_main(tmp_path, monkeypatch, [first, second], card)

    out = capsys.readouterr().out
    assert "poisoned" not in out

    create_card.assert_not_called()
    patch_card.assert_called_once()
    patched_card = patch_card.call_args.args[2]
    assert patched_card.get("id") == "500"


def test_poisoning_is_monotonic_within_a_run(tmp_path, monkeypatch, capsys):
    """A third, later queue() call whose value matches the frozen (pre-conflict) lane id must NOT
    un-poison an already-poisoned entry: poisoning is monotonic for the life of the run. Order:
    L-PROG (adopted) -> L-REVIEW (conflicts, poisons, freezes at L-PROG) -> L-PROG (agrees with the
    frozen value, but must not reset poisoned back to False)."""
    first = _issue(1, "KEY", assignees=("dev",))    # -> "In progress" -> L-PROG (adopted)
    second = _issue(2, "KEY")                        # has_open_pr below -> "In review" -> L-REVIEW (conflict)
    third = _issue(3, "KEY", assignees=("dev",))     # -> "In progress" -> L-PROG (matches frozen value)
    card = _card_with_customid("500", "KEY")

    create_card, patch_card = _run_main(
        tmp_path, monkeypatch, [first, second, third], card, open_pr_numbers=frozenset({2}))

    out = capsys.readouterr().out
    poison_lines = [line for line in out.splitlines() if line.startswith("WARN  card 500 poisoned")]
    assert len(poison_lines) == 1, "poisoning WARN fires once, on the conflicting call only"


def test_each_distinct_conflicting_value_gets_its_own_warn_line(tmp_path, monkeypatch, capsys):
    """Three distinct /laneId values (not just two) must print a WARN for EVERY conflicting call
    after the first-seen value, not just the first: L-PROG (adopted) -> L-REVIEW (conflicts vs the
    frozen L-PROG, WARN #1) -> L-DONE (also conflicts vs the still-frozen L-PROG, WARN #2), each
    naming its own conflicting value."""
    first = _issue(1, "KEY", assignees=("dev",))          # -> "In progress" -> L-PROG (adopted)
    second = _issue(2, "KEY")                              # has_open_pr -> "In review" -> L-REVIEW (conflict #1)
    third = _issue(3, "KEY", state="CLOSED")               # -> "Done" -> L-DONE (conflict #2)
    card = _card_with_customid("500", "KEY")

    create_card, patch_card = _run_main(
        tmp_path, monkeypatch, [first, second, third], card,
        open_pr_numbers=frozenset({2}), lanes=_LANES_WITH_DONE)

    out = capsys.readouterr().out
    poison_lines = [line for line in out.splitlines() if line.startswith("WARN  card 500 poisoned")]
    assert len(poison_lines) == 2, "a fresh WARN for every conflicting call, not just the first"
    assert "'L-REVIEW'" in poison_lines[0]
    assert "'L-DONE'" in poison_lines[1]

    create_card.assert_not_called()
    patch_card.assert_not_called()
