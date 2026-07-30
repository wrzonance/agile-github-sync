"""Run timing: main() stamps its start, its finish, and how long it took, on every exit branch.

The close stamp used to live in two of main()'s three branches, so an ordinary successful --apply
ended silently. Pinned per branch, since that's the axis the bug lived on.

Run: pytest -q
"""
from __future__ import annotations

import re
import sys
from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path
import pytest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agilesync.board import board_layout  # noqa: E402
from agilesync.gh import ghkit  # noqa: E402
from agilesync import sync  # noqa: E402
from agilesync import timestamps  # noqa: E402


def _cfg(tmp_path) -> dict:
    return {
        "token": "tok", "host": "example.leankit.com", "board_id": "42",
        "target_repo_path": tmp_path,
        "label_sync_ignore": frozenset(),
        "repo_context": ghkit.RepoContext(owner="acme", name="repo", host="github.com"),
        "stage_lane_map": {},
        "gh_project": {},
        "ap_description_max_length": 20000,
    }


def _run_main(tmp_path, apply: bool) -> None:
    """A no-op run of the real main() -- no issues, no cards -- so it reaches its exit branch
    without writing anything."""
    stack = ExitStack()
    stack.enter_context(patch("agilesync.gh.ghkit.resolve_repo_context",
                              return_value=ghkit.RepoContext(owner="acme", name="repo",
                                                             host="github.com")))
    stack.enter_context(patch("agilesync.gh.ghkit_snapshot.fetch_issue_graph", return_value=None))
    stack.enter_context(patch("agilesync.gh.ghkit.list_issues", return_value=[]))
    stack.enter_context(patch("agilesync.gh.ghkit.open_pr_issue_numbers", return_value=set()))
    stack.enter_context(patch("agilesync.gh.ghkit.blocked_by_map", return_value={}))
    stack.enter_context(patch("agilesync.gh.ghkit.run", return_value=Mock(stdout="")))
    stack.enter_context(patch("agilesync.gh.ghproject.configured", return_value=False))
    stack.enter_context(patch("agilesync.gh.ghproject.items", return_value={}))
    stack.enter_context(patch("agilesync.gh.ghproject.field_meta", return_value=None))
    stack.enter_context(patch("agilesync.gh.ghproject.hydrate_item_dates", return_value={}))
    stack.enter_context(patch("agilesync.board.board_layout.board_layout",
                              return_value=board_layout.BoardLayout(lanes=[], card_types=[])))
    stack.enter_context(patch("agilesync.board.agileplace.list_cards", return_value=[]))
    argv = ["sync.py", "--apply"] if apply else ["sync.py"]
    with stack, patch("agilesync.sync.env_config", return_value=_cfg(tmp_path)), \
            patch("agilesync.sync.STATE_FILE", tmp_path / ".sync-state.json"), \
            patch("sys.argv", argv):
        sync.main()


def _lines(out: str) -> list[str]:
    return [line.strip() for line in out.strip().splitlines() if line.strip()]


def test_an_apply_run_stamps_its_start_and_finish(tmp_path, capsys):
    before = datetime.now()

    _run_main(tmp_path, apply=True)

    printed = _lines(capsys.readouterr().out)
    started = datetime.fromisoformat(printed[0])
    finished = datetime.fromisoformat(printed[-2])
    assert before <= started <= finished


def test_a_dry_run_stamps_its_start_and_finish(tmp_path, capsys):
    before = datetime.now()

    _run_main(tmp_path, apply=False)

    printed = _lines(capsys.readouterr().out)
    started = datetime.fromisoformat(printed[0])
    finished = datetime.fromisoformat(printed[-2])
    assert before <= started <= finished


def test_the_run_closes_with_how_long_it_took(tmp_path, capsys):
    _run_main(tmp_path, apply=True)

    printed = _lines(capsys.readouterr().out)
    assert re.fullmatch(r"elapsed\s+\d+\.\ds", printed[-1]), printed[-1]


def test_an_aborted_run_still_reports_when_it_stopped_and_how_long_it_ran(capsys):
    """A run that fails loud is exactly when the timing is being read, so the stamps survive the
    abort -- and the SystemExit still propagates."""
    with patch("agilesync.sync.env_config", side_effect=SystemExit("AGILEPLACE_HOST is not set")), \
            patch("sys.argv", ["sync.py"]), pytest.raises(SystemExit):
        sync.main()

    printed = _lines(capsys.readouterr().out)
    datetime.fromisoformat(printed[-2])
    assert re.fullmatch(r"elapsed\s+\d+\.\ds", printed[-1]), printed[-1]


def test_elapsed_reads_in_the_largest_units_the_duration_needs():
    assert timestamps.format_elapsed(timedelta(seconds=27.44)) == "27.4s"
    assert timestamps.format_elapsed(timedelta(seconds=72.5)) == "1m 12.5s"
    assert timestamps.format_elapsed(timedelta(seconds=3723.0)) == "1h 2m 3.0s"
