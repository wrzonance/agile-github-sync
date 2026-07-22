"""Invariant tests for issue #70 Layer 1 (contested-card detection) and Layer 2 (queue()
lane-conflict poisoning), exercised through the REAL sync.main() with every I/O boundary mocked
(ghkit, ghproject, agileplace) -- mirrors the full-main harness shape used by
tests/test_sync_main.py (_cfg/_card/_issue/_mock_io/_run_main_once), adapted here to accept
multiple issues and multiple cards in one run.

card_coherence.contested_cards() and card_coherence.lane_conflict() are themselves pure and
unit-tested directly in test_card_coherence.py. These tests instead pin the invariants at the
sync.main() boundary:

  Invariant 1 -- for any card claimed by >= 2 distinct issues (active or retired, via EITHER the
    URL or the customId fallback match path -- issue #75), that card is excluded from every
    match/queue path this run, and exactly one WARN line is emitted per contested card id,
    regardless of how many issues claim it.
  Invariant 2 -- contested-card exclusion is total and consistent across the active and retired
    paths at once (a card contested between one active and one retired issue is excluded from
    both), and stays local to the contested card: an unrelated card retiring normally in the same
    run is unaffected.
  Invariant 3 -- a card reached by >= 2 issues ONLY via the customId fallback (zero URL claims of
    their own) is fenced by the SAME Layer 1 mechanism as Invariant 1/2, not by queue()'s Layer 2
    lane-conflict poisoning: issue #75 widened contested_cards() to cover this path too, so it now
    supersedes Layer 2 for this exact shape (Layer 2's pure logic itself is still fully covered by
    test_card_coherence.py -- these tests instead pin that the widened Layer 1 fence excludes the
    card before either issue ever reaches queue()), and a poisoned/fenced card stays local to
    itself -- an unrelated card/issue pair still syncs normally in the same run.

Invariant 1/2's fixtures use a two-URL-claiming-one-card construction (each issue's own url
resolves to the shared card). Invariant 3's fixtures instead use a duplicate-`[KEY]`-title-prefix
construction with ZERO url claims (two or three distinct GitHub issues, distinct numbers/URLs,
sharing a title prefix so `issue_custom_id()` returns the same key for all of them, each resolving
to the SAME existing card only via the customId fallback, `all_card_by_cid`) -- pre-#75 this shape
sailed past a URL-only Layer 1 straight into Layer 2's lane-conflict poisoning; post-#75 it is
fenced by Layer 1 itself, before queue() is ever called for the card.

Run: pytest -q
"""
from __future__ import annotations

import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

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


# --- Invariant 3: widened Layer 1 fences a duplicate-[KEY] customId collision -----------------

def test_poisoning_is_monotonic_within_a_run(tmp_path, capsys):
    """Three issues racing the same customId, with zero URL claims of their own: pre-#75 this
    reached queue() three times and exercised Layer 2's monotonic poisoning; post-#75 the widened
    Layer 1 fence excludes the card entirely (a single 'claimed by 3 issue URLs' WARN) before any
    of the three ever reaches queue() -- no Layer 2 poisoning WARN fires at all."""
    first = _issue(1, "[KEY] issue one", assignees=("dev",))     # -> "In progress"
    second = _issue(2, "[KEY] issue two", labels=("agent:in-review",))  # -> "In review"
    third = _issue(3, "[KEY] issue three", assignees=("dev",))   # -> "In progress" (agrees with first)
    card = _card("500", "KEY", [])  # zero URL claims -> matched only via the customId fallback

    create_card_mock, patch_card_mock = _run_main_once(
        tmp_path, [first, second, third], [card], lanes=(_PROG_LANE, _REVIEW_LANE))

    out = capsys.readouterr().out
    warn_lines = [line for line in out.splitlines() if line.startswith("WARN  card 500 claimed by")]
    assert len(warn_lines) == 1, "one Layer 1 WARN for the card, not one per issue"
    assert "3 issue URLs" in warn_lines[0]
    assert "poisoned" not in out, "Layer 1 excludes the card before Layer 2 ever sees it"

    create_card_mock.assert_not_called()
    patch_card_mock.assert_not_called()


def test_same_value_repeated_laneid_ops_never_poison_the_entry(tmp_path, capsys):
    """Two distinct issues sharing a `[KEY]` customId that both resolve to the SAME target lane are
    STILL fenced by the widened Layer 1 -- unlike the pre-#75 world, lane-value agreement doesn't
    rescue the pair, because Layer 1 fences on claimant count alone, before any lane value is
    considered. Neither issue is synced this run."""
    first = _issue(1, "[KEY] issue one", assignees=("dev",))    # -> "In progress"
    second = _issue(2, "[KEY] issue two", assignees=("dev",))   # -> "In progress" (same target)
    card = _card("500", "KEY", [])

    create_card_mock, patch_card_mock = _run_main_once(
        tmp_path, [first, second], [card], lanes=(_PROG_LANE, _REVIEW_LANE))

    out = capsys.readouterr().out
    warn_lines = [line for line in out.splitlines() if line.startswith("WARN  card 500 claimed by")]
    assert len(warn_lines) == 1
    assert "poisoned" not in out

    create_card_mock.assert_not_called()
    patch_card_mock.assert_not_called()


def test_unrelated_card_and_issue_are_unaffected_by_a_poisoned_card(tmp_path, capsys):
    """A card fenced by a customId collision must stay local to itself: an unrelated issue/card
    pair -- matched normally by URL, no customId collision -- still gets its ordinary lane-move
    PATCH in the very same run."""
    in_progress_issue = _issue(1, "[KEY] issue one", assignees=("dev",))               # -> L-PROG
    in_review_issue = _issue(2, "[KEY] issue two", labels=("agent:in-review",))        # -> L-REVIEW
    fenced_card = _card("500", "KEY", [])

    unrelated_issue = _issue(3, "widget three", assignees=("dev",))        # -> "In progress" -> L-PROG
    unrelated_card = _card("600", "3", [unrelated_issue["url"]], lane_id="L-ELSEWHERE")

    create_card_mock, patch_card_mock = _run_main_once(
        tmp_path,
        [in_progress_issue, in_review_issue, unrelated_issue],
        [fenced_card, unrelated_card],
        lanes=(_PROG_LANE, _REVIEW_LANE),
    )

    out = capsys.readouterr().out
    warn_lines = [line for line in out.splitlines() if line.startswith("WARN  card 500 claimed by")]
    assert len(warn_lines) == 1
    assert "poisoned" not in out

    patched_ids = [call.args[2].get("id") for call in patch_card_mock.call_args_list]
    assert "500" not in patched_ids, "the fenced card must never reach patch_card"
    assert "600" in patched_ids, "an unrelated card must sync normally despite the fenced card"
    create_card_mock.assert_not_called()


# --- Codex P2#3: an id-less card in the board snapshot must not crash the whole run ----------------

def test_idless_card_in_snapshot_does_not_abort_the_run(tmp_path, capsys):
    """AgilePlace can return a partial card carrying a customId but no `id` yet. The issue #70 filters
    index `str(card["id"])` directly; on such a payload that raises KeyError and aborts the entire
    sync. An id-less card is unresolvable, so it must be deferred (skipped), never crash -- and an
    unrelated, fully-formed issue/card pair in the same snapshot must still sync normally."""
    idless = {"customId": "GHOST", "externalLinks": [], "laneId": "L-ELSEWHERE",
              "tags": [], "version": 1}  # no "id" key at all

    healthy_issue = _issue(1, "widget one", assignees=("dev",))            # -> In progress -> L-PROG
    healthy_card = _card("600", "1", [healthy_issue["url"]], lane_id="L-ELSEWHERE")

    create_card_mock, patch_card_mock = _run_main_once(
        tmp_path, [healthy_issue], [idless, healthy_card], lanes=(_PROG_LANE, _REVIEW_LANE))

    # The healthy card still syncs its ordinary lane move despite the id-less card sharing the run.
    patched_ids = [call.args[2].get("id") for call in patch_card_mock.call_args_list]
    assert "600" in patched_ids, "the id-less card must not abort an unrelated card's sync"


# --- Codex P1#2: a fenced run must not persist advanced merge bases for writes it never attempted ----

def test_poisoned_run_does_not_persist_advanced_merge_bases(tmp_path):
    """Originally: a Layer 2 lane conflict poisoned a card, its AgilePlace PATCH was skipped, but
    sync_metadata had already advanced this run's label merge base in apply mode -- persisting that
    base recorded a tag write that never reached AgilePlace, so the NEXT run's unchanged AgilePlace
    card read as a fresh external delete and destructively removed the GitHub label to match.

    Post-#75, this exact customId-collision shape (zero URL claims) is instead excluded wholesale by
    the widened Layer 1 fence before either issue ever reaches sync_metadata/queue() at all -- which
    would make this fixture unable to reach Layer 2 at all, and the merge-base-hold invariant it's
    meant to pin would go completely unexercised. So, mirroring the sibling Layer 2 tests below, this
    test forces `contested_cards` to report nothing (simulating a collision shape Layer 1 doesn't
    catch) to reach a GENUINE Layer 2 lane-conflict poisoning and exercise the actual merge-base-hold
    guard end to end. Across two runs of a persistently Layer-2-poisoned card, no GitHub label may
    ever be removed."""
    first = _issue(1, "[KEY] one", assignees=("dev",), labels=("feature",))   # In progress -> L-PROG
    second = _issue(2, "[KEY] two", labels=("agent:in-review",))              # In review -> L-REVIEW (conflict)
    card = _card("500", "KEY", [], lane_id="L-ELSEWHERE")

    removals: list = []

    def _capture_edit_label(cfg, apply, number, name, *, add):
        if not add:
            removals.append((number, name))

    for _ in range(2):  # same collision twice; state from run 1 feeds run 2
        stack, _create, _patch = _mock_io([first, second], [card], lanes=(_PROG_LANE, _REVIEW_LANE))
        with stack, patch("sync.contested_cards", return_value={}), \
                patch("ghkit.edit_label", side_effect=_capture_edit_label), \
                patch("sync.env_config", return_value=_cfg(tmp_path)), \
                patch("sync.STATE_FILE", tmp_path / ".sync-state.json"), \
                patch("sys.argv", ["sync.py", "--apply"]):
            sync.main()

    assert removals == [], (
        "a poisoned card's skipped AgilePlace tag write must not advance the persisted merge base; "
        f"doing so caused a phantom next-run GitHub label removal: {removals}")


# --- Review follow-up (issue #75): fencing must be a per-run defer, not a permanent blacklist -------

def test_fenced_card_syncs_normally_once_the_customid_collision_resolves(tmp_path, capsys):
    """A card excluded by the widened Layer 1 fence must be retried, not permanently poisoned: once
    the customId collision that caused the exclusion is resolved (here, by the second issue losing
    its shared `[KEY]` prefix), the previously-deferred issue must get its ordinary card sync on the
    very next run. Guards against a regression that persists the fenced id (e.g. into state) or an
    off-by-one in the exclusion set that survives across runs."""
    first = _issue(1, "[KEY] issue one", assignees=("dev",))              # -> In progress -> L-PROG
    second = _issue(2, "[KEY] issue two", labels=("agent:in-review",))    # -> In review (collision)
    card = _card("500", "KEY", [], lane_id="L-ELSEWHERE")

    # Run 1: zero URL claims, shared "[KEY]" customId -> fenced by Layer 1, card never touched.
    create_card_mock, patch_card_mock = _run_main_once(
        tmp_path, [first, second], [card], lanes=(_PROG_LANE, _REVIEW_LANE))
    out = capsys.readouterr().out
    assert any(line.startswith("WARN  card 500 claimed by") for line in out.splitlines())
    patch_card_mock.assert_not_called()
    create_card_mock.assert_not_called()

    # Run 2: the collision is resolved -- issue 2 renamed off the shared "[KEY]" prefix, so its
    # customId ("2") no longer collides with issue 1's ("KEY"). Issue 1's claim on card 500 is now
    # unique, and it must sync normally: no more fence WARN, and its overdue lane move fires.
    second_resolved = _issue(2, "unrelated issue two", labels=("agent:in-review",))
    _create_card_mock2, patch_card_mock2 = _run_main_once(
        tmp_path, [first, second_resolved], [card], lanes=(_PROG_LANE, _REVIEW_LANE))
    out2 = capsys.readouterr().out
    assert not any(line.startswith("WARN  card 500 claimed by") for line in out2.splitlines()), (
        "a resolved collision must not still be fenced on the next run")
    patched_ids = [call.args[2].get("id") for call in patch_card_mock2.call_args_list]
    assert "500" in patched_ids, (
        "the previously-fenced card must sync normally once its collision resolves")


# --- Issue #75 task 3: the step-3 parent/child guard skips a Layer-2-poisoned parent card --------

def test_layer2_poisoned_epic_card_gets_no_connect_or_disconnect_calls(tmp_path, capsys):
    """Post-#75, a customId collision between two ordinary issues is fenced wholesale by the
    widened Layer 1 fence before either ever reaches queue() -- so this test forces `contested_cards`
    to report nothing (simulating a shape Layer 1 doesn't catch) in order to reach a genuine Layer 2
    lane-conflict poisoning for a card that is ALSO an epic's matched parent card, with a real child
    task to connect. Without the step-3 poison guard, the epics loop would call
    `agileplace.connect_children` for the poisoned card despite its flush PATCH already being
    skipped; the guard must keep BOTH sync surfaces consistent for a poisoned card."""
    epic_a = _issue(1, "[KEY] epic one", assignees=("dev",), labels=("type:epic",))       # In progress
    epic_b = _issue(2, "[KEY] epic two", labels=("type:epic", "agent:in-review"))         # In review (conflict)
    task = _issue(3, "task three")
    epic_card = _card("500", "KEY", [])  # zero URL claims -- matched only via the customId fallback
    task_card = _card("600", "3", [task["url"]], lane_id="L-ELSEWHERE")

    connect_children_mock = Mock()
    disconnect_children_mock = Mock()

    stack, create_card_mock, patch_card_mock = _mock_io(
        [epic_a, epic_b, task], [epic_card, task_card], lanes=(_PROG_LANE, _REVIEW_LANE))
    with stack, \
            patch("sync.contested_cards", return_value={}), \
            patch("ghkit.sub_issue_numbers", return_value=[task["number"]]), \
            patch("agileplace.card_child_ids", return_value=frozenset()), \
            patch("agileplace.connect_children", connect_children_mock), \
            patch("agileplace.disconnect_children", disconnect_children_mock), \
            patch("sync.env_config", return_value=_cfg(tmp_path)), \
            patch("sync.STATE_FILE", tmp_path / ".sync-state.json"), \
            patch("sys.argv", ["sync.py", "--apply"]):
        sync.main()

    out = capsys.readouterr().out
    assert "poisoned: conflicting /laneId ops" in out, "the fixture must genuinely reach Layer 2"
    assert any("skipping child connections" in line and "500" in line
               for line in out.splitlines()), "the poisoned parent must be named in a skip WARN"

    connect_children_mock.assert_not_called()
    disconnect_children_mock.assert_not_called()

    # The pre-existing Layer 2 flush skip still holds: no patch for the poisoned card.
    patched_ids = [call.args[2].get("id") for call in patch_card_mock.call_args_list]
    assert "500" not in patched_ids
    create_card_mock.assert_not_called()


# --- Issue #75 task 5: a satisfied-in-place claim + a differing customId claim is fenced, not
#     silently overwritten ---------------------------------------------------------------------

def test_satisfied_in_place_claim_plus_differing_lane_claim_is_fenced_not_silently_overwritten(
        tmp_path, capsys):
    """The concrete bug this issue exists to close: issue1's own desired lane already matches the
    card's CURRENT lane, so its lane-move body queues NO op at all (`_apply_lane_move` only calls
    `queue()` when `current not in acceptable`) -- while issue2, sharing the same `[KEY]` customId
    with zero URL claims of its own, wants a genuinely different lane and so queues exactly one op.

    Pre-#75 this was worse than a Layer-2 lane conflict: Layer 1 (URL-only) never saw either claim,
    and Layer 2's queue() conflict check needs >= 2 *competing* ops on the same card id to fire --
    with only ONE op ever queued (issue1 contributes none), queue() sees no conflict at all, so the
    card would be silently PATCHed to issue2's target lane as if issue1 never claimed it, with no
    WARN anywhere. Post-#75 the widened Layer 1 fence excludes the card on claimant count alone,
    before either issue ever reaches queue() -- regardless of whether their ops would collide.

    Fencing must be a *defer*, not a poison: an unrelated card syncs normally in the same run, and
    the run still reaches save_state so `.sync-state.json` persists for the rest of the run."""
    in_place_issue = _issue(1, "[KEY] issue one", assignees=("dev",))              # -> In progress
    moving_issue = _issue(2, "[KEY] issue two", labels=("agent:in-review",))       # -> In review
    # Card already sits in L-PROG: in_place_issue's own lane move queues no op (already acceptable);
    # only moving_issue's op would ever reach queue() pre-#75. Zero URL claims -> customId-only match.
    fenced_card = _card("500", "KEY", [], lane_id="L-PROG")

    unrelated_issue = _issue(3, "widget three", assignees=("dev",))               # -> In progress -> L-PROG
    unrelated_card = _card("600", "3", [unrelated_issue["url"]], lane_id="L-ELSEWHERE")

    create_card_mock, patch_card_mock = _run_main_once(
        tmp_path,
        [in_place_issue, moving_issue, unrelated_issue],
        [fenced_card, unrelated_card],
        lanes=(_PROG_LANE, _REVIEW_LANE),
    )

    out = capsys.readouterr().out
    warn_lines = [line for line in out.splitlines() if line.startswith("WARN  card 500 claimed by")]
    assert len(warn_lines) == 1, "the fence must fire even though only one of the two issues has an op"
    assert "2 issue URLs" in warn_lines[0]
    assert in_place_issue["url"] in warn_lines[0] and moving_issue["url"] in warn_lines[0]

    # The fenced card is never touched -- no silent overwrite to moving_issue's target lane.
    patched_ids = [call.args[2].get("id") for call in patch_card_mock.call_args_list]
    assert "500" not in patched_ids, "a satisfied-in-place claim must not let the other claim win silently"
    create_card_mock.assert_not_called()

    # Fencing stays local: the unrelated card still gets its ordinary lane-move PATCH this run.
    assert "600" in patched_ids, "an unrelated card must sync normally despite the fenced card"

    # Fencing is a defer, not a poison: the run completes and persists state for the rest of the run.
    assert (tmp_path / ".sync-state.json").exists(), \
        "a fenced card must not abort save_state for the rest of the run"


# --- Issue #75 task 3 (sibling case): a clean parent's poisoned CHILD card is dropped, not
#     just a poisoned parent -----------------------------------------------------------------------

def test_layer2_poisoned_child_card_is_dropped_from_connect_children(tmp_path, capsys):
    """The epic's own card (500) is clean -- URL-matched, no lane conflict of its own -- but its
    desired child card (700) is poisoned by a genuine Layer 2 lane conflict between two OTHER
    issues (task1/task2) that collide on the same customId with zero URL claims of their own.
    Without the child-poison filter in the epics loop, connect_children would still be called with
    the poisoned child id included, because the parent-poisoned guard above never fires for this
    shape (the parent card 500 is never poisoned at all)."""
    epic = _issue(1, "epic one", labels=("type:epic",))
    task1 = _issue(2, "[KEY] task one", assignees=("dev",))              # -> In progress
    task2 = _issue(3, "[KEY] task two", labels=("agent:in-review",))     # -> In review (conflict)
    epic_card = _card("500", "1", [epic["url"]], lane_id="L-ELSEWHERE")
    poisoned_task_card = _card("700", "KEY", [])  # zero URL claims -- matched only via customId

    connect_children_mock = Mock()
    disconnect_children_mock = Mock()

    stack, create_card_mock, patch_card_mock = _mock_io(
        [epic, task1, task2], [epic_card, poisoned_task_card], lanes=(_PROG_LANE, _REVIEW_LANE))
    with stack, \
            patch("sync.contested_cards", return_value={}), \
            patch("ghkit.sub_issue_numbers", return_value=[task1["number"], task2["number"]]), \
            patch("agileplace.card_child_ids", return_value=frozenset()), \
            patch("agileplace.connect_children", connect_children_mock), \
            patch("agileplace.disconnect_children", disconnect_children_mock), \
            patch("sync.env_config", return_value=_cfg(tmp_path)), \
            patch("sync.STATE_FILE", tmp_path / ".sync-state.json"), \
            patch("sys.argv", ["sync.py", "--apply"]):
        sync.main()

    out = capsys.readouterr().out
    assert "poisoned: conflicting /laneId ops" in out, "the fixture must genuinely reach Layer 2"
    assert any("dropping poisoned child card id(s)" in line for line in out.splitlines()), (
        "the poisoned child id must be dropped from connect/disconnect even though the parent "
        "card is clean")

    connect_children_mock.assert_not_called()
    disconnect_children_mock.assert_not_called()

    # The poisoned child card never reaches patch_card either (pre-existing Layer 2 flush skip),
    # while the epic's own clean card is untouched by this invariant.
    patched_ids = [call.args[2].get("id") for call in patch_card_mock.call_args_list]
    assert "700" not in patched_ids
    create_card_mock.assert_not_called()


# --- Issue #75 task 4: the step-4 dependency guard skips a Layer-2-poisoned card ------------------

def test_layer2_poisoned_card_gets_no_dependency_writes(tmp_path, capsys):
    """Post-#75, a card poisoned by Layer 2's lane-conflict check must also skip step 4's
    dependency sync entirely -- not just step 3's child connections (task 3) and step 5's flush
    PATCH. Forces `contested_cards` to report nothing (Layer 1 doesn't catch this shape) to reach
    a genuine Layer 2 poisoning on card 500, which is ALSO blocked-by a real GitHub edge onto task
    card 600 -- without the step-4 poison guard, sync_dependencies would still call
    agileplace.create_dependencies for the poisoned card's desired edge."""
    first = _issue(1, "[KEY] issue one", assignees=("dev",))            # -> In progress
    second = _issue(2, "[KEY] issue two", labels=("agent:in-review",))  # -> In review (conflict)
    task = _issue(3, "task three")
    poisoned_card = _card("500", "KEY", [])  # zero URL claims -- matched only via the customId fallback
    task_card = _card("600", "3", [task["url"]], lane_id="L-ELSEWHERE")

    create_dependencies_mock = Mock()
    delete_dependencies_mock = Mock()

    stack, create_card_mock, patch_card_mock = _mock_io(
        [first, second, task], [poisoned_card, task_card], lanes=(_PROG_LANE, _REVIEW_LANE))
    with stack, \
            patch("sync.contested_cards", return_value={}), \
            patch("ghkit.blocked_by_map", return_value={1: [3]}), \
            patch("agileplace.create_dependencies", create_dependencies_mock), \
            patch("agileplace.delete_dependencies", delete_dependencies_mock), \
            patch("sync.env_config", return_value=_cfg(tmp_path)), \
            patch("sync.STATE_FILE", tmp_path / ".sync-state.json"), \
            patch("sys.argv", ["sync.py", "--apply"]):
        sync.main()

    out = capsys.readouterr().out
    assert "poisoned: conflicting /laneId ops" in out, "the fixture must genuinely reach Layer 2"
    assert any("skipping dependency sync" in line and "500" in line
               for line in out.splitlines()), "the poisoned card must be named in a skip WARN"

    create_dependencies_mock.assert_not_called()
    delete_dependencies_mock.assert_not_called()

    # The pre-existing Layer 2 flush skip still holds: no patch for the poisoned card.
    patched_ids = [call.args[2].get("id") for call in patch_card_mock.call_args_list]
    assert "500" not in patched_ids
    create_card_mock.assert_not_called()
