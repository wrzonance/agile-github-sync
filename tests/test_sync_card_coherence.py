"""Invariant tests for issue #70 Layer 1 (contested-card detection) and Layer 2 (queue()
lane-conflict poisoning), exercised through the REAL sync.main() with every I/O boundary mocked
(ghkit, ghproject, agileplace) -- mirrors the full-main harness shape used by
tests/test_sync_main.py (_cfg/_card/_issue/_mock_io/_run_main_once), adapted here to accept
multiple issues and multiple cards in one run.

card_coherence.contested_cards() and card_coherence.lane_conflict() are themselves pure and
unit-tested directly in test_card_coherence.py. These tests instead pin the invariants at the
sync.main() boundary:

  Invariant 1 -- for any card claimed by >= 2 distinct issue URLs (active or retired), that card is
    excluded from every match/queue path this run, and exactly one WARN line is emitted per
    contested card id, regardless of how many URLs claim it.
  Invariant 2 -- contested-card exclusion is total and consistent across the active and retired
    paths at once (a card contested between one active and one retired issue URL is excluded from
    both), and stays local to the contested card: an unrelated card retiring normally in the same
    run is unaffected.
  Invariant 3 -- queue()'s lane-conflict poisoning is monotonic within a run (a later call that
    happens to agree with the frozen, pre-conflict lane id can never un-poison an entry); same-value
    repeated /laneId ops never poison an entry (repeated agreement is not conflict); and a poisoned
    entry stays local to its own card -- an unrelated card/issue pair still syncs normally in the
    same run.

Invariant 3's fixtures deliberately use a duplicate-`[KEY]`-title-prefix construction (two distinct
GitHub issues, distinct numbers/URLs, sharing a title prefix so `issue_custom_id()` returns the same
key for both) rather than the two-URL-contested shape Invariant 1/2 exercise: two URLs claiming one
card is Layer 1's shape and never reaches queue() at all (Layer 1 excludes it first). A
customId-fallback collision has zero URL claims, so it sails past Layer 1 and both issues resolve to
the SAME existing card via the customId match path (`card_by_cid`) -- exactly the shape Layer 2
exists to catch. A naive first draft tried to force this via `_reconciled_custom_id_index`'s
URL-correction path instead, but that path only fires for a URL-matched issue reclaiming a
*previously-unclaimed* customId -- it requires a URL match in the first place, so it can never
produce two issues racing the same customId entry.

Run: pytest -q
"""
from __future__ import annotations

import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sync  # noqa: E402

_DONE_LANE = {"id": "L-DONE", "title": "Done", "cardStatus": "finished"}
_PROG_LANE = {"id": "L-PROG", "title": "In Progress", "cardStatus": "started"}
_REVIEW_LANE = {"id": "L-REVIEW", "title": "In Review", "cardStatus": "started"}


def _issue(number: int, title: str, *, state: str = "OPEN", state_reason: str = "",
           assignees: tuple[str, ...] = (), labels: tuple[str, ...] = ()) -> dict:
    """A normalized issue as ghkit.list_issues() would return it (snake_case state_reason) --
    main() is exercised with ghkit.list_issues mocked directly, so no raw-JSON normalization runs.
    `assignees`/`labels` drive stages.issue_stage()'s "In progress"/"In review" derivation for the
    Invariant 3 lane-conflict fixtures below (label "agent:in-review" for "In review", an assignee
    for "In progress" -- NOT `has_open_pr`, which main() unconditionally recomputes from
    ghkit.open_pr_issue_numbers() for every active issue, overwriting anything set here);
    Invariant 1/2 fixtures leave both at their stage-neutral defaults."""
    return {
        "number": number,
        "title": title,
        "state": state,
        "state_reason": state_reason,
        "labels": list(labels),
        "milestone": None,
        "assignees": list(assignees),
        "url": f"https://github.com/acme/repo/issues/{number}",
        "has_open_pr": False,
    }


def _card(card_id: str, custom_id: str, urls: list[str], *, lane_id: str = "L-ELSEWHERE") -> dict:
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


def _cfg(tmp_path) -> dict:
    return {
        "token": "token",
        "host": "example.leankit.com",
        "board_id": "42",
        "target_repo_path": tmp_path,
        "label_sync_ignore": frozenset(),
        "stage_lane_map": {},
        "gh_project": {},
    }


def _mock_io(issues: list[dict], cards: list[dict], *, lanes: tuple = ()):
    """ExitStack of patches covering every I/O boundary main() touches for one run. Returns the
    stack plus the agileplace.create_card / agileplace.patch_card mocks for call-site assertions.
    ghproject is left unconfigured (False) -- these invariants concern card matching, not Projects
    v2 date/status sync, so that whole subsystem is kept out of the way."""
    stack = ExitStack()
    stack.enter_context(patch("ghkit.list_issues", return_value=list(issues)))
    stack.enter_context(patch("ghkit.repo_name", return_value="acme/repo"))
    stack.enter_context(patch("ghkit.open_pr_issue_numbers", return_value=set()))
    stack.enter_context(patch("ghkit.blocked_by_map", return_value={}))
    stack.enter_context(patch("ghkit.edit_label"))
    stack.enter_context(patch("ghkit.set_milestone"))
    stack.enter_context(patch("ghproject.configured", return_value=False))
    stack.enter_context(patch("agileplace.board_layout", return_value=list(lanes)))
    stack.enter_context(patch("agileplace.list_cards", return_value=list(cards)))
    stack.enter_context(patch("agileplace.card_dependencies", return_value=[]))
    create_card_mock = stack.enter_context(patch("agileplace.create_card", return_value={}))
    patch_card_mock = stack.enter_context(patch("agileplace.patch_card"))
    return stack, create_card_mock, patch_card_mock


def _run_main_once(tmp_path, issues: list[dict], cards: list[dict], *, lanes: tuple = ()):
    stack, create_card_mock, patch_card_mock = _mock_io(issues, cards, lanes=lanes)
    with stack, patch("sync.env_config", return_value=_cfg(tmp_path)), \
         patch("sync.STATE_FILE", tmp_path / ".sync-state.json"), \
         patch("sys.argv", ["sync.py", "--apply"]):
        sync.main()
    return create_card_mock, patch_card_mock


# --- Invariant 1: two ACTIVE issue URLs claiming the same card ------------------------------------

def test_two_active_urls_on_one_card_are_excluded_and_warned_once(tmp_path, capsys):
    issue1 = _issue(1, "widget one")
    issue2 = _issue(2, "widget two")
    card = _card("100", "1", [issue1["url"], issue2["url"]])

    create_card_mock, patch_card_mock = _run_main_once(tmp_path, [issue1, issue2], [card])

    out = capsys.readouterr().out
    warn_lines = [line for line in out.splitlines()
                  if line.startswith("WARN  card 100 claimed by")]
    assert len(warn_lines) == 1, "exactly one WARN line per contested card id, not one per claiming URL"
    assert "2 issue URLs" in warn_lines[0]
    assert issue1["url"] in warn_lines[0] and issue2["url"] in warn_lines[0]

    # Neither contested issue is synced this run: no new card, no patch to the contested card.
    create_card_mock.assert_not_called()
    patch_card_mock.assert_not_called()


# --- Invariant 2: one ACTIVE + one RETIRED URL on one card, with an unrelated normal retirement ---

def test_active_and_retired_urls_on_one_card_are_excluded_without_affecting_unrelated_retirement(
        tmp_path, capsys):
    """The contested card (claimed by one active and one retired issue URL) must be excluded from
    BOTH the retirement path and the active-match path -- while a second, unrelated card that
    retires normally in the same run must retire exactly as if the contested card didn't exist,
    proving the exclusion never leaks across cards."""
    contested_active = _issue(1, "widget one")
    contested_retired = _issue(2, "widget two", state="CLOSED", state_reason="NOT_PLANNED")
    contested_card = _card(
        "100", "2", [contested_active["url"], contested_retired["url"]], lane_id="L-ELSEWHERE")

    unrelated_retired = _issue(3, "widget three", state="CLOSED", state_reason="NOT_PLANNED")
    unrelated_card = _card("200", "3", [unrelated_retired["url"]], lane_id="L-ELSEWHERE")

    create_card_mock, patch_card_mock = _run_main_once(
        tmp_path,
        [contested_active, contested_retired, unrelated_retired],
        [contested_card, unrelated_card],
        lanes=(_DONE_LANE,),
    )

    out = capsys.readouterr().out
    warn_lines = [line for line in out.splitlines()
                  if line.startswith("WARN  card 100 claimed by")]
    assert len(warn_lines) == 1
    assert "2 issue URLs" in warn_lines[0]
    assert contested_active["url"] in warn_lines[0] and contested_retired["url"] in warn_lines[0]

    # The contested card is untouched: no retirement DRY/real print, no create, no patch to card 100.
    contested_lines = [line for line in out.splitlines() if "retire" in line.lower() and "[2]" in line]
    assert contested_lines == [], f"contested card must never be retired: {contested_lines}"
    create_card_mock.assert_not_called()

    # The unrelated card DOES retire normally: exactly one patch_card call, targeting card 200.
    patch_card_mock.assert_called_once()
    patched_card = patch_card_mock.call_args.args[2]
    assert patched_card.get("id") == "200"
    ops = patch_card_mock.call_args.args[3]
    assert {"op": "replace", "path": "/laneId", "value": "L-DONE"} in ops
    assert "retire [3]" in out


# --- Invariant 3: queue() lane-conflict poisoning on a duplicate-[KEY] customId collision ---------

def test_poisoning_is_monotonic_within_a_run(tmp_path, capsys):
    """Three queue() calls against the same customId-collided card: L-PROG (adopted) -> L-REVIEW
    (conflicts, poisons, freezes at L-PROG) -> L-PROG (agrees with the now-frozen value). The third,
    non-conflicting call must NOT un-poison the entry -- poisoning is monotonic for the life of a
    run, so the card's flush PATCH stays skipped regardless of what a later call agrees with."""
    first = _issue(1, "[KEY] issue one", assignees=("dev",))     # -> "In progress" -> L-PROG (adopted)
    second = _issue(2, "[KEY] issue two", labels=("agent:in-review",))  # -> "In review" -> L-REVIEW (conflict)
    third = _issue(3, "[KEY] issue three", assignees=("dev",))   # -> "In progress" -> L-PROG (matches frozen)
    card = _card("500", "KEY", [])  # zero URL claims -> matched only via the customId fallback

    create_card_mock, patch_card_mock = _run_main_once(
        tmp_path, [first, second, third], [card], lanes=(_PROG_LANE, _REVIEW_LANE))

    out = capsys.readouterr().out
    poison_lines = [line for line in out.splitlines() if line.startswith("WARN  card 500 poisoned")]
    assert len(poison_lines) == 1, "poisoning WARN fires once, on the conflicting call only"

    create_card_mock.assert_not_called()
    patch_card_mock.assert_not_called()


def test_same_value_repeated_laneid_ops_never_poison_the_entry(tmp_path, capsys):
    """Two distinct issues sharing a `[KEY]` customId that both resolve to the SAME target lane must
    NOT poison the card: repeated agreement is not conflict, and the flush PATCH still fires."""
    first = _issue(1, "[KEY] issue one", assignees=("dev",))    # -> "In progress" -> L-PROG
    second = _issue(2, "[KEY] issue two", assignees=("dev",))   # -> "In progress" -> L-PROG (same target)
    card = _card("500", "KEY", [])

    create_card_mock, patch_card_mock = _run_main_once(
        tmp_path, [first, second], [card], lanes=(_PROG_LANE, _REVIEW_LANE))

    out = capsys.readouterr().out
    assert "poisoned" not in out

    create_card_mock.assert_not_called()
    patch_card_mock.assert_called_once()
    assert patch_card_mock.call_args.args[2].get("id") == "500"


def test_unrelated_card_and_issue_are_unaffected_by_a_poisoned_card(tmp_path, capsys):
    """A card poisoned by a customId collision must stay local to itself: an unrelated issue/card
    pair -- matched normally by URL, no customId collision -- still gets its ordinary lane-move
    PATCH in the very same run."""
    in_progress_issue = _issue(1, "[KEY] issue one", assignees=("dev",))               # -> L-PROG
    in_review_issue = _issue(2, "[KEY] issue two", labels=("agent:in-review",))        # -> L-REVIEW (conflict)
    poisoned_card = _card("500", "KEY", [])

    unrelated_issue = _issue(3, "widget three", assignees=("dev",))        # -> "In progress" -> L-PROG
    unrelated_card = _card("600", "3", [unrelated_issue["url"]], lane_id="L-ELSEWHERE")

    create_card_mock, patch_card_mock = _run_main_once(
        tmp_path,
        [in_progress_issue, in_review_issue, unrelated_issue],
        [poisoned_card, unrelated_card],
        lanes=(_PROG_LANE, _REVIEW_LANE),
    )

    out = capsys.readouterr().out
    poison_lines = [line for line in out.splitlines() if line.startswith("WARN  card 500 poisoned")]
    assert len(poison_lines) == 1

    patched_ids = [call.args[2].get("id") for call in patch_card_mock.call_args_list]
    assert "500" not in patched_ids, "the poisoned card must never reach patch_card"
    assert "600" in patched_ids, "an unrelated card must sync normally despite the poisoned card"
    create_card_mock.assert_not_called()
