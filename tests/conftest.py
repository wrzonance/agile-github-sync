"""Suite-wide defaults.

agileplace._LIMITER paces real requests, and it is process-wide by design -- left live, every test
that reaches api() would sleep out its slot (and a test's 429 fixture would slow every later test
in the run). Tests that exercise pacing install their own limiter.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agilesync.board import agileplace  # noqa: E402


class _UnpacedLimiter:
    total_requests = 0

    def acquire(self) -> None:
        pass

    def penalize(self) -> float:
        return 0.0

    def recent_requests(self, window_seconds: float = 60.0) -> int:
        return 0


@pytest.fixture(autouse=True)
def unpaced_agileplace(monkeypatch):
    monkeypatch.setattr(agileplace, "_LIMITER", _UnpacedLimiter())
