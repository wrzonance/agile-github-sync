"""The root `sync.py` shim is a thin launcher: run timing belongs to main(), so `python -m
agilesync.sync` reports a run the same way `python sync.py` does, and importing prints nothing.

Run: pytest -q
"""
from __future__ import annotations

import importlib
import runpy
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import agilesync.sync  # noqa: E402


def test_importing_the_shim_runs_nothing_and_prints_nothing(capsys):
    sys.modules.pop("sync", None)  # a cached module imports without executing its body

    with patch.object(agilesync.sync, "main") as main:
        importlib.import_module("sync")

    assert capsys.readouterr().out == ""
    main.assert_not_called()


def test_executing_the_shim_runs_the_sync_and_adds_no_output_of_its_own(capsys):
    with patch.object(agilesync.sync, "main") as main:
        runpy.run_path(str(REPO_ROOT / "sync.py"), run_name="__main__")

    main.assert_called_once_with()
    assert capsys.readouterr().out == ""
