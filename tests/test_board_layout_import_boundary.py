"""Import-boundary invariants for issue #84 (agileplace.py split: board topology extracted into
board_layout.py).

Task 2/4 moved eleven board-topology symbols verbatim out of agileplace.py into a new
board_layout.py: lane_title, _lanes_with_ids, _card_types_with_ids, BoardLayout, board_layout,
_ancestor_titles, _leaf_lanes, _release_lane, _mapped_lanes, resolve_lane_for_stage, and
stage_for_lane. agileplace.py does not re-export any of them, so a stale
`from agileplace import <moved-name>` would fail loudly at collection time -- but that only
catches call sites that still exist; it does not stop a *new* test file from being added later
with the same stale import pattern copy-pasted from an old one. This module pins, at the repo
boundary, that no test file names any of the eleven moved symbols in a `from agileplace import
(...)` clause, so that failure mode is caught explicitly rather than relying on accidental
ImportErrors.

The companion "full suite stays green with >= N tests collected" invariant for this task lives in
tests/test_regression_budget.py's own test_full_suite_remains_green_with_no_regressions (its
PRE_CHANGE_TEST_COUNT is bumped here rather than duplicated into a second subprocess-spawning test
in this file: two such tests in two different files would each spawn a full-suite subprocess run
that includes the OTHER file, whose own full-suite test would in turn spawn another subprocess
including this file, recursing indefinitely -- see test_regression_budget.py's own docstring for
the 149-orphaned-process incident this shape is designed to avoid).

Run: pytest -q
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The eleven symbols issue #84 moved out of agileplace.py into board_layout.py (see module
# docstring). None of these are re-exported from agileplace.py, so importing any of them via
# `from agileplace import ...` fails outright -- this test exists to catch that failure mode
# explicitly and loudly, rather than relying on an incidental ImportError from whichever test
# file happens to still reference the old location.
MOVED_TO_BOARD_LAYOUT = (
    "lane_title",
    "_lanes_with_ids",
    "_card_types_with_ids",
    "BoardLayout",
    "board_layout",
    "_ancestor_titles",
    "_leaf_lanes",
    "_release_lane",
    "_mapped_lanes",
    "resolve_lane_for_stage",
    "stage_for_lane",
)


def test_no_test_file_imports_board_layout_names_from_agileplace():
    """Parses every tests/test_*.py file's AST (not just the ones currently known to import these
    names, so a stale import surviving anywhere -- or reintroduced later -- is caught) for a
    `from agileplace import (...)` clause naming a symbol issue #84 moved to board_layout.py."""
    offenders = []
    for path in sorted((REPO_ROOT / "tests").glob("test_*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "agileplace":
                hit = {alias.name for alias in node.names} & set(MOVED_TO_BOARD_LAYOUT)
                if hit:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {sorted(hit)}")

    assert not offenders, (
        "test file(s) still import issue #84-moved board-topology name(s) from agileplace instead "
        "of board_layout -- " + "; ".join(offenders)
    )
