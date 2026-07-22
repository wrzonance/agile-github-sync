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

Run: pytest -q
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Baseline captured from `git log` immediately before issue #75's first commit (0a72eb3) -- i.e.
# immediately after the unrelated #62/#64/#69/#72 merges this branch bundles, none of which are
# issue #75's own scope to be budgeted against (see module docstring).
PRE_CHANGE_SYNC_LINES = 908

# Wiring-only budget for issue #75's own addition: widen contested_cards()'s call site to also
# fence pure-customId collisions, a poisoned-child guard in the step-3 child-connection loop, a
# poisoned-dependency guard in step 4, both sharing the extracted card_coherence.filter_poisoned_
# edges() helper (rather than duplicating the drop/WARN logic inline at each call site), plus the
# import line -- net ~29 lines. Deliberately generous slack over that actual addition.
WIRING_BUDGET_LINES = 40

# Pre-existing suite size before issue #70's own test files
# (test_card_coherence.py, test_sync_contested_cards.py, test_sync_lane_conflict.py,
# test_sync_card_coherence.py) were added.
PRE_CHANGE_TEST_COUNT = 432

# Issue #70's four new test files. Invariant B's companion check asserts each is still collected
# (deleting/renaming/emptying one is exactly the silent-loss a >= baseline pass-count floor misses).
NEW_TEST_FILES = (
    "tests/test_card_coherence.py",
    "tests/test_sync_contested_cards.py",
    "tests/test_sync_lane_conflict.py",
    "tests/test_sync_card_coherence.py",
)


def test_sync_py_stays_within_wiring_only_line_budget():
    line_count = len(Path(REPO_ROOT / "sync.py").read_text().splitlines())

    assert line_count <= PRE_CHANGE_SYNC_LINES + WIRING_BUDGET_LINES, (
        f"sync.py grew to {line_count} lines, past the wiring-only budget of "
        f"{PRE_CHANGE_SYNC_LINES + WIRING_BUDGET_LINES} "
        f"({PRE_CHANGE_SYNC_LINES} pre-change baseline + {WIRING_BUDGET_LINES} budget). "
        "New decision logic belongs in card_coherence.py, not inlined into sync.py."
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
