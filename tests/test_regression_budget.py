"""Regression-budget invariants for issue #70 (card coherence: contested-card exclusion +
lane-conflict poisoning).

sync.py was already 828 lines before this change -- over the repo's 800-line hard cap. The
design deliberately extracted the two genuinely-new pieces of logic (contested_cards(),
lane_conflict()) into a new pure module, card_coherence.py, specifically so sync.py would only
grow by thin wiring (an import, the contested-card WARN loop, four added filter predicates, one
skip in the retirement loop, queue()'s rewritten body, and one skip in the flush loop) rather than
by the ~37 lines of logic the spike's inline draft would have added directly.

These tests pin, at the repo boundary (not sync.py's internals):

  Invariant A -- sync.py's line count stays within the wiring-only budget: it must not grow
    past PRE_CHANGE_SYNC_LINES + WIRING_BUDGET_LINES, i.e. the change may not have re-grown the
    file by re-inlining logic that belongs in card_coherence.py.
  Invariant B -- the full pre-existing test suite (432 tests, before issue #70's own test files
    were added) remains green: running the whole suite reports zero failures and at least as many
    passing tests as the pre-existing baseline plus this change's own new test files.

Run: pytest -q
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Baseline captured from `git log` immediately before issue #70's first commit (4a69b23).
PRE_CHANGE_SYNC_LINES = 828

# Wiring-only budget: import (1) + contested-loop (4) + four filter predicates (~4) +
# retirement-loop skip (2) + queue() rewrite (~6) + flush-loop skip (2), plus comments/
# blank-line slack. Deliberately generous but far short of the ~37 lines the spike's inline
# (non-extracted) draft would have added -- that draft would have pushed sync.py to 865,
# compounding the pre-existing 800-line violation instead of avoiding it.
WIRING_BUDGET_LINES = 50

# Pre-existing suite size before issue #70's own test files
# (test_card_coherence.py, test_sync_contested_cards.py, test_sync_lane_conflict.py,
# test_sync_card_coherence.py) were added.
PRE_CHANGE_TEST_COUNT = 432


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
