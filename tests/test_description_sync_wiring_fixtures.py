"""Guards issue #65 task 5's fixture-repair invariant: wiring sync_description into sync.py's
per-issue loop must never let a pre-existing card fixture in test_sync_main.py,
test_vetting_latch.py, or test_run.py fall through to a real agileplace.api() call.

agileplace_description.card_description() takes a zero-I/O path whenever the card dict already carries a
'description' key (even ""); a fixture that omits it makes sync_description's
card_description(cfg, card) call fall back to agileplace.get_card(), which none of these three
files mock -- so it hits the real HTTP client. Confirmed live in the design spike: a real host
answers with HTTP 401 -> SystemExit inside test_sync_main.py/test_vetting_latch.py (which patch
agileplace at the function level, not the transport), and test_run.py's own request-dispatch stub
(FixtureWorld.open_url) raises its own AssertionError("unexpected AgilePlace request") for the
same reason -- neither is a legitimate assertion failure, both are this exact regression.

Rather than re-deriving by static analysis which fixtures reach the per-issue loop (fragile across
three differently-shaped harnesses -- test_sync_main.py's shared _mock_io, test_vetting_latch.py's
reuse of it, test_run.py's own FixtureWorld HTTP stub), this runs each file as its own subprocess
and requires zero failures/errors: the same boundary the spike's regression was actually caught at.

Run: pytest -q tests/test_description_sync_wiring_fixtures.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The three files task 5/7 is scoped to repair -- every pre-existing card fixture reaching
# sync.main()'s per-issue loop lives in one of these.
WIRED_TEST_FILES = (
    "tests/test_sync_main.py",
    "tests/test_vetting_latch.py",
    "tests/test_run.py",
)


def test_no_wired_test_file_lets_a_card_fixture_reach_real_agileplace_api():
    failures = []
    for relative_path in WIRED_TEST_FILES:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", relative_path],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            summary = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
            failures.append(
                f"{relative_path}: exit={result.returncode} summary={summary!r}\n"
                f"{result.stdout[-3000:]}"
            )

    assert not failures, (
        "one or more wired test files let a card fixture reach sync_description's "
        "agileplace_description.card_description() fallback (real agileplace.api() call) -- give the fixture "
        "a 'description' key or explicitly mock agileplace.get_card:\n\n" + "\n\n".join(failures)
    )
