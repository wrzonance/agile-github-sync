"""Regression-budget invariants for issue #70 (card coherence: contested-card exclusion +
lane-conflict poisoning) and issue #75 (widened contested_cards() customId fencing +
poisoned-child/poisoned-dependency guards).

sync.py was already 828 lines before issue #70's first commit -- over the repo's 800-line hard
cap. The design deliberately extracted the two genuinely-new pieces of logic (contested_cards(),
lane_conflict()) into a new pure module, card_coherence.py, specifically so sync.py would only
grow by thin wiring rather than by re-inlining logic that belongs in card_coherence.py.

Between issue #70 and issue #75, three unrelated PRs (#62 reverse-intake, #64 richtext, plus the
#69/#72 latch-repair and conflict-retry work) merged into this same line of history and grew
sync.py to 908 lines by main()'s own intake-promotion wiring -- none of that growth is issue #75's
to own, and re-deriving PRE_CHANGE_SYNC_LINES from #70's original 828 baseline would blame #75 for
line growth #75 didn't cause. PRE_CHANGE_SYNC_LINES is therefore recaptured immediately before
issue #75's first commit (0a72eb3, sync.py at 908 lines) -- the same "baseline right before this
change's own commits" contract the constant has always documented, just re-anchored to the point
issue #75 actually started from. issue #75 added its own thin wiring (widen contested_cards() to
also fence pure-customId collisions, plus poisoned-child and poisoned-dependency guards sharing
the extracted card_coherence.filter_poisoned_edges() helper) totalling under WIRING_BUDGET_LINES.

These tests pin, at the repo boundary (not sync.py's internals):

  Invariant A -- sync.py's line count stays within the wiring-only budget: it must not grow
    past PRE_CHANGE_SYNC_LINES + WIRING_BUDGET_LINES, i.e. the change may not have re-grown the
    file by re-inlining logic that belongs in card_coherence.py.
  Invariant B -- the full pre-existing test suite (432 tests, before issue #70's own test files
    were added) remains green: running the whole suite reports zero failures and at least as many
    passing tests as the pre-existing baseline. A pass-count floor alone cannot notice a whole new
    test file silently dropping out of collection (renamed out of `test_*.py` discovery, or emptied)
    -- passed_count would merely fall back toward the baseline while still clearing it -- so the
    companion test below additionally asserts each of issue #70's four new test files is collected
    and contributes at least one test, making that failure loud.

Issue #79 (metadata_sync extraction) re-anchors PRE_CHANGE_SYNC_LINES again, for the same reason
#75 re-anchored it: three more merges (including #75 itself) grew sync.py back to 908 lines by the
time #79 started, and none of that growth is #79's to own. But #79's own change is not incremental
wiring -- it is a genuine reduction, pulling the label/milestone/date reconciliation logic (the old
sync_metadata/sync_dates plus four private helpers: _label_set, _filter_gh_safe_labels,
_card_milestones, _stale_milestone_tags) out into a new module, metadata_sync.py. Measuring that
reduction against the pre-#79 908-line figure would let sync.py re-grow most of the way back to 908
before the wiring-budget test would ever notice -- so PRE_CHANGE_SYNC_LINES is instead re-pinned
down to sync.py's own post-extraction size (726 lines, measured via `wc -l` right after the move),
making the smaller, de-bloated file the new baseline future changes are budgeted against.
WIRING_BUDGET_LINES itself is unchanged (40) -- it is a generic per-change slack, not specific to
any one issue's own addition.

Independently of that moving baseline, SYNC_PY_HARD_CAP_LINES pins the repo's own stated 800-line
file-size hard cap (CLAUDE.md's file-organization convention) as an absolute ceiling: a test that
does not depend on correctly tracking PRE_CHANGE_SYNC_LINES at all, so a future mis-anchored rebase
of the wiring budget still can't let sync.py silently cross the repo's own file-size convention.

Also pinned here (Task 3/5 of issue #79): no test file may import a name #79 moved out of sync.py
from sync's own namespace. sync.py still does `from metadata_sync import sync_dates,
sync_metadata` to wire its own call sites, so `from sync import sync_metadata` would keep working
by accident -- silently masking the move and coupling tests to sync.py's incidental re-export
instead of metadata_sync.py, the module that actually owns the logic now. MS_PREFIX and the four
private helpers are not re-exported from sync.py at all, so an import of those would fail loudly --
but sync_metadata/sync_dates need the explicit check.

Issue #82 (card-type sync + reverse-intake seeding) re-measured both budgeted files immediately
before its own first commit (d986dae, the tip of #79): sync.py was 726 lines -- exactly the figure
already pinned above, so PRE_CHANGE_SYNC_LINES needs no re-anchor this time; issue #82's own sync.py
wiring (importing card_types, resolving card-type ids once in main(), and one new per-issue
sync_card_type() call) added ~20 lines, comfortably inside WIRING_BUDGET_LINES. agileplace.py,
however, was already 805 lines at that same commit -- past the repo's 800-line convention for that
file too, and not issue #82's fault. Issue #82's own agileplace.py additions (the BoardLayout
return-shape, the _card_types_with_ids structural filter, a new typeId branch inside the existing
_card_value_for_patch_path, and trailing optional type_id/type_title params on
create_card/_planned_card_snapshot) were deliberately minimized -- card_type_title/op_type were
placed in the new card_types.py module instead, specifically to avoid growing agileplace.py further
-- but still landed at 864 lines. Per issue #82's design (decision #16), that pre-existing >800
breach is not silently absorbed: PRE_CHANGE_AGILEPLACE_LINES/AGILEPLACE_WIRING_BUDGET_LINES below
pin agileplace.py's *own* wiring-only delta the same way PRE_CHANGE_SYNC_LINES/WIRING_BUDGET_LINES
pin sync.py's, so a future PR can't silently pile more bulk onto an already-over-budget file. An
absolute hard-cap assertion (mirroring test_sync_py_never_exceeds_repo_hard_cap) is deliberately
*not* added for agileplace.py here -- it would fail immediately against debt that predates issue
#82's own commits, and a full-file refactor to clear that debt is out of scope for this feature PR.
That refactor is tracked instead as its own follow-up: issue #84.

Issue #82 also added three wholly new test files -- tests/test_card_types.py,
tests/test_sync_card_types.py, and tests/test_ghkit_issue_types.py -- covering the new card_types.py
module, sync.py's new per-issue card-type drift sync, and ghkit.py's new org_issue_types() probe,
respectively. PRE_CHANGE_TEST_COUNT is bumped from 432 to 988 -- the full suite's own passing count
measured at d986dae, immediately before issue #82's first commit -- rather than left at #70's
original figure, so the floor actually reflects "every test that existed before this change" rather
than a six-issues-stale number. NEW_TEST_FILES gains issue #82's three files alongside #70's original
four, for the same reason #70 tracked its own: a passed_count floor alone can't notice one of these
three files silently leaving discovery.

Issue #84 (agileplace.py split: board topology extracted into board_layout.py) bumps
PRE_CHANGE_TEST_COUNT again, from 988 to 1167 -- comfortably below the ~1194 tests collected
immediately after task 3/4 rewired every test file's imports/mock targets from agileplace to
board_layout, the same generous-slack convention every prior bump here follows. NEW_TEST_FILES
gains issue #84's three wholly new test files -- tests/test_board_layout.py,
tests/test_board_layout_call_sites.py, and tests/test_board_layout_import_boundary.py -- for the
same reason #70 and #82 tracked their own: a passed_count floor alone can't notice one of these
three files silently leaving discovery, and the floor's own slack (1194 actual vs. 1167 baseline)
is smaller than either test_board_layout.py's or test_board_layout_call_sites.py's individual test
count, so without this companion check one of those files losing its tests entirely would still
clear the floor undetected. The companion "no stale import of a moved name" invariant for this
issue lives in its own file,
tests/test_board_layout_import_boundary.py, rather than growing MOVED_TO_METADATA_SYNC-style here:
that file's own module docstring explains why its "full suite stays green" check was deliberately
*not* duplicated as a second subprocess-spawning test (two such tests in two different files would
recurse into each other indefinitely -- see that docstring, and the 149-orphaned-process incident
that originally motivated the by-path self-ignore below). For that same reason, this file does not
grow its own MOVED_TO_BOARD_LAYOUT/test_no_test_file_imports_board_layout_names_from_agileplace
pair -- test_board_layout_import_boundary.py's AST-walk test already pins that exact invariant (it
is a pure collection-time parse, no subprocess involved), and duplicating it here would just be the
same check maintained in two places for no added safety.

Task 4/4 of issue #84 re-anchors PRE_CHANGE_AGILEPLACE_LINES a second time, in the opposite
direction from #82's: where #82 could only budget *around* agileplace.py's pre-existing 805-line
breach (a full-file refactor being out of scope for that feature PR), #84 *is* that refactor.
Extracting the eleven board-topology symbols above into board_layout.py brought agileplace.py down
to 672 lines (measured via `wc -l agileplace.py` immediately after task 3 landed) -- back under the
repo's 800-line hard cap for the first time since #82. PRE_CHANGE_AGILEPLACE_LINES is re-pinned to
that smaller, de-bloated figure (mirroring how #79 re-pinned PRE_CHANGE_SYNC_LINES to sync.py's own
post-extraction size rather than the pre-extraction one), so future changes are budgeted against the
leaner file instead of the debt #84 just paid off. AGILEPLACE_WIRING_BUDGET_LINES's value (70) is
kept as-is and reframed as the same kind of generic per-change slack WIRING_BUDGET_LINES already is
for sync.py -- it was never specific to #82's own delta, just first introduced alongside it.

Because agileplace.py is now genuinely under the repo's 800-line convention rather than carrying
pre-existing debt, test_agileplace_py_never_exceeds_repo_hard_cap becomes honest in a way it
couldn't have been at #82: mirroring test_sync_py_never_exceeds_repo_hard_cap, it asserts the
absolute 800-line ceiling independent of the moving PRE_CHANGE_AGILEPLACE_LINES baseline, so a
future mis-anchored rebase of the wiring budget still can't let agileplace.py silently re-cross that
line. #82 deliberately skipped this exact assertion because it would have failed immediately against
debt that predated its own commits; #84 is the follow-up that clears that debt, so the assertion no
longer fails against anything but genuinely new growth.

Run: pytest -q
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Baseline re-anchored to sync.py's own size immediately after issue #79's metadata_sync extraction
# (measured via `wc -l sync.py` post-move) -- see module docstring for why #79 re-pins this down
# rather than budgeting against the pre-#79 908-line figure.
PRE_CHANGE_SYNC_LINES = 726

# Wiring-only budget for issue #75's own addition: widen contested_cards()'s call site to also
# fence pure-customId collisions, a poisoned-child guard in the step-3 child-connection loop, a
# poisoned-dependency guard in step 4, both sharing the extracted card_coherence.filter_poisoned_
# edges() helper (rather than duplicating the drop/WARN logic inline at each call site), plus the
# import line -- net ~29 lines. Deliberately generous slack over that actual addition. Reused as-is
# by every later re-anchor of PRE_CHANGE_SYNC_LINES (see #79 in the module docstring) -- it is a
# generic per-change slack, not specific to #75.
WIRING_BUDGET_LINES = 40

# Repo-wide absolute ceiling (CLAUDE.md file-organization convention: "800 hard cap"), independent
# of PRE_CHANGE_SYNC_LINES -- see module docstring.
SYNC_PY_HARD_CAP_LINES = 800

# agileplace.py's own baseline, re-anchored to its post-extraction size measured at task 3 of issue
# #84 (via `wc -l agileplace.py`, right after board_layout.py absorbed the eleven board-topology
# symbols) -- see module docstring's issue #84 section for why this moves down from #82's 805,
# mirroring how #79 re-pinned PRE_CHANGE_SYNC_LINES to sync.py's own post-extraction size.
PRE_CHANGE_AGILEPLACE_LINES = 672

# Generic per-change wiring-only slack for agileplace.py, reused across issues the same way
# WIRING_BUDGET_LINES is for sync.py. First introduced alongside issue #82's own agileplace.py
# additions (the BoardLayout NamedTuple, the _card_types_with_ids structural filter, a new typeId
# branch inside the existing _card_value_for_patch_path, and trailing optional type_id/type_title
# params on create_card/_planned_card_snapshot -- a measured delta of 59 lines), but not specific to
# that addition; kept as-is for issue #84, which added no new agileplace.py lines of its own.
AGILEPLACE_WIRING_BUDGET_LINES = 70

# Repo-wide absolute ceiling (CLAUDE.md file-organization convention: "800 hard cap"), independent
# of PRE_CHANGE_AGILEPLACE_LINES -- see module docstring's issue #84 section for why this assertion
# is only honest starting now (agileplace.py carried pre-existing >800 debt at issue #82's time that
# this exact check would have failed against).
AGILEPLACE_HARD_CAP_LINES = 800

# Names issue #79 moved out of sync.py into metadata_sync.py. sync_metadata/sync_dates are the two
# public entry points (still reachable as `sync.sync_metadata` via sync.py's own import -- hence the
# explicit no-stale-import check); the rest are metadata_sync-private and would fail an import
# outright.
MOVED_TO_METADATA_SYNC = (
    "sync_metadata",
    "sync_dates",
    "MS_PREFIX",
    "_label_set",
    "_filter_gh_safe_labels",
    "_card_milestones",
    "_stale_milestone_tags",
)

# Pre-existing suite size immediately before issue #82's first commit (measured at d986dae, the tip
# of #79) -- bumped up from #70's original 432 so the floor reflects every test that existed before
# this change, not a six-issues-stale figure. See module docstring's issue #82 section.
#
# Re-bumped from 988 to 1167 for issue #84's own task 3/4 (rewiring test imports/mock targets from
# agileplace to board_layout) -- comfortably below the ~1194 tests collected once that rewire lands,
# with the same generous slack every prior bump here leaves. See module docstring's issue #84
# section.
PRE_CHANGE_TEST_COUNT = 1167

# Issue #70's four new test files, plus issue #82's three (test_card_types.py,
# test_sync_card_types.py, test_ghkit_issue_types.py), plus issue #84's three
# (test_board_layout.py, test_board_layout_call_sites.py, test_board_layout_import_boundary.py).
# Invariant B's companion check asserts each is still collected (deleting/renaming/emptying one is
# exactly the silent-loss a >= baseline pass-count floor misses) -- without these three, the
# passed_count >= PRE_CHANGE_TEST_COUNT floor alone would be the only safety net for them, and its
# slack is smaller than either file's own test count, so one of them silently losing its tests
# would not be caught.
NEW_TEST_FILES = (
    "tests/test_card_coherence.py",
    "tests/test_sync_contested_cards.py",
    "tests/test_sync_lane_conflict.py",
    "tests/test_sync_card_coherence.py",
    "tests/test_card_types.py",
    "tests/test_sync_card_types.py",
    "tests/test_ghkit_issue_types.py",
    "tests/test_board_layout.py",
    "tests/test_board_layout_call_sites.py",
    "tests/test_board_layout_import_boundary.py",
)


def test_sync_py_stays_within_wiring_only_line_budget():
    line_count = len(Path(REPO_ROOT / "sync.py").read_text().splitlines())

    assert line_count <= PRE_CHANGE_SYNC_LINES + WIRING_BUDGET_LINES, (
        f"sync.py grew to {line_count} lines, past the wiring-only budget of "
        f"{PRE_CHANGE_SYNC_LINES + WIRING_BUDGET_LINES} "
        f"({PRE_CHANGE_SYNC_LINES} pre-change baseline + {WIRING_BUDGET_LINES} budget). "
        "New decision logic belongs in card_coherence.py, not inlined into sync.py."
    )


def test_sync_py_never_exceeds_repo_hard_cap():
    """Absolute ceiling, independent of the moving PRE_CHANGE_SYNC_LINES baseline above: sync.py must
    never cross the repo's own stated 800-line file-organization hard cap, regardless of what the
    wiring-budget arithmetic says. Catches sync.py crossing that convention even in the (unlikely)
    case a future re-anchor of PRE_CHANGE_SYNC_LINES + WIRING_BUDGET_LINES were miscalculated past
    800 itself."""
    line_count = len(Path(REPO_ROOT / "sync.py").read_text().splitlines())

    assert line_count <= SYNC_PY_HARD_CAP_LINES, (
        f"sync.py has grown to {line_count} lines, past the repo's own {SYNC_PY_HARD_CAP_LINES}-line "
        "file-organization hard cap. Extract cohesive logic into its own module rather than letting "
        "sync.py keep absorbing it."
    )


def test_agileplace_py_stays_within_wiring_only_line_budget():
    """agileplace.py exceeded the repo's 800-line hard cap (805 lines) between issue #82 and issue
    #84 -- #82's own pre-existing debt, cleared by #84's board_layout.py extraction (see module
    docstring). PRE_CHANGE_AGILEPLACE_LINES is now re-anchored to the post-extraction, under-cap
    figure; this test pins future changes' own wiring-only delta on top of that leaner baseline, so
    a change can't silently pile bulk back onto the file issue #84 just de-bloated."""
    line_count = len(Path(REPO_ROOT / "agileplace.py").read_text().splitlines())

    assert line_count <= PRE_CHANGE_AGILEPLACE_LINES + AGILEPLACE_WIRING_BUDGET_LINES, (
        f"agileplace.py grew to {line_count} lines, past the wiring-only budget of "
        f"{PRE_CHANGE_AGILEPLACE_LINES + AGILEPLACE_WIRING_BUDGET_LINES} "
        f"({PRE_CHANGE_AGILEPLACE_LINES} pre-change baseline + {AGILEPLACE_WIRING_BUDGET_LINES} "
        "budget). New decision logic belongs in its own module, not inlined into agileplace.py."
    )


def test_agileplace_py_never_exceeds_repo_hard_cap():
    """Absolute ceiling, independent of the moving PRE_CHANGE_AGILEPLACE_LINES baseline above:
    agileplace.py must never cross the repo's own stated 800-line file-organization hard cap,
    regardless of what the wiring-budget arithmetic says. Mirrors
    test_sync_py_never_exceeds_repo_hard_cap. Issue #82 deliberately skipped this exact assertion
    for agileplace.py because it would have failed immediately against pre-existing debt (805
    lines) that predated #82's own commits; issue #84's board_layout.py extraction paid that debt
    down to 672 lines, so the assertion is only added now that it is honest -- it no longer fails
    against anything but genuinely new growth past the cap."""
    line_count = len(Path(REPO_ROOT / "agileplace.py").read_text().splitlines())

    assert line_count <= AGILEPLACE_HARD_CAP_LINES, (
        f"agileplace.py has grown to {line_count} lines, past the repo's own "
        f"{AGILEPLACE_HARD_CAP_LINES}-line file-organization hard cap. Extract cohesive logic into "
        "its own module rather than letting agileplace.py keep absorbing it."
    )


def test_no_test_file_imports_metadata_sync_names_from_sync():
    """Issue #79 moved sync_metadata/sync_dates and four private helpers out of sync.py into
    metadata_sync.py (see module docstring). Parses every tests/test_*.py file's AST (not just the
    ones currently known to import these names, so a stale import surviving anywhere is caught) for
    a `from sync import (...)` clause naming a moved symbol -- sync.py's own `from metadata_sync
    import sync_dates, sync_metadata` would otherwise let such a stale import keep working by
    accident, silently defeating the extraction."""
    offenders = []
    for path in sorted((REPO_ROOT / "tests").glob("test_*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "sync":
                hit = {alias.name for alias in node.names} & set(MOVED_TO_METADATA_SYNC)
                if hit:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {sorted(hit)}")

    assert not offenders, (
        "test file(s) still import issue #79-moved name(s) from sync instead of metadata_sync -- "
        + "; ".join(offenders)
    )


def test_full_suite_remains_green_with_no_regressions():
    """Runs the rest of the suite as a subprocess so the summary line reflects every other test
    actually collected, and asserts zero failures/errors with at least as many passing tests as
    the pre-existing baseline.

    Ignores this test's own FILE (by path, not by node id or function name): if collection
    included this file, the subprocess would re-run this very test, which would spawn another
    subprocess doing the same collection -- unbounded recursion. A node-id deselect was tried
    first and found unsafe: it only excludes one exact node id, so a differently-named or
    differently-pathed copy of this test (e.g. during ad-hoc debugging) still recurses -- verified
    live, it spawned 149 orphaned pytest processes before hitting the subprocess timeout.
    Ignoring the whole file by path is robust regardless of what the file's tests are named."""
    this_file = Path(__file__).resolve().relative_to(REPO_ROOT)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--ignore", str(this_file)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )

    summary = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    passed_match = re.search(r"(\d+) passed", summary)
    failed_match = re.search(r"(\d+) failed", summary)
    error_match = re.search(r"(\d+) error", summary)

    assert passed_match, f"could not parse a passing count from pytest summary: {summary!r}"
    assert not failed_match, f"suite reported failures: {summary!r}"
    assert not error_match, f"suite reported errors: {summary!r}"

    passed_count = int(passed_match.group(1))
    assert passed_count >= PRE_CHANGE_TEST_COUNT, (
        f"only {passed_count} tests passed, below the pre-existing baseline of "
        f"{PRE_CHANGE_TEST_COUNT} -- a pre-existing test appears to have been lost or broken."
    )


def test_every_new_test_file_is_still_collected():
    """Invariant B, companion check: a `passed_count >= PRE_CHANGE_TEST_COUNT` floor cannot detect a
    whole new test file silently leaving collection -- if one of issue #70's four files were renamed
    out of `test_*.py` discovery or emptied, the suite would merely shed ~a-few-dozen tests and still
    clear the pre-existing baseline. Collect each file explicitly (by the exact path it must live at)
    and require it to contribute at least one test, so that silent loss fails loudly instead.

    Collection-only, so it never executes the files (no recursion risk from re-running the suite);
    an explicit path that no longer exists makes pytest exit non-zero, which this catches directly."""
    for path in NEW_TEST_FILES:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", path],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, (
            f"{path} failed to collect (renamed away or removed?):\n{result.stdout}\n{result.stderr}")
        collected = re.search(r"(\d+) tests? collected", result.stdout)
        assert collected and int(collected.group(1)) >= 1, (
            f"{path} contributed no collected tests -- it was emptied or its tests were renamed out "
            f"of pytest discovery:\n{result.stdout}")
