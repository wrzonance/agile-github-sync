"""Unit tests for issue #15: config.load_env_file() must never let a .env file inject GH_REPO or
GH_HOST into os.environ, even when the real environment doesn't already have them set (the case
os.environ.setdefault alone can't protect against). Uses tmp_path + monkeypatch so the real .env /
process environment is never touched and no state leaks into other tests.

Run: pytest -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402


def _write_env(tmp_path, contents: str) -> Path:
    env_file = tmp_path / ".env"
    env_file.write_text(contents)
    return env_file


def test_load_env_file_never_sets_gh_repo_or_gh_host(tmp_path, monkeypatch):
    env_file = _write_env(tmp_path, "GH_REPO=someone/else\nGH_HOST=stale.example.com\n")
    monkeypatch.setattr(config, "ENV_FILE", env_file)
    monkeypatch.delenv("GH_REPO", raising=False)
    monkeypatch.delenv("GH_HOST", raising=False)

    config.load_env_file()

    assert "GH_REPO" not in __import__("os").environ
    assert "GH_HOST" not in __import__("os").environ


def test_load_env_file_still_sets_unrelated_keys_from_same_file(tmp_path, monkeypatch):
    env_file = _write_env(
        tmp_path,
        "GH_REPO=someone/else\nAGILEPLACE_TOKEN=abc123\nGH_HOST=stale.example.com\n",
    )
    monkeypatch.setattr(config, "ENV_FILE", env_file)
    monkeypatch.delenv("GH_REPO", raising=False)
    monkeypatch.delenv("GH_HOST", raising=False)
    monkeypatch.delenv("AGILEPLACE_TOKEN", raising=False)

    config.load_env_file()

    import os
    assert os.environ.get("AGILEPLACE_TOKEN") == "abc123"  # unrelated key still loaded
    assert "GH_REPO" not in os.environ
    assert "GH_HOST" not in os.environ


def test_load_env_file_does_not_override_real_env_for_blocklisted_keys_either(tmp_path, monkeypatch):
    """Belt-and-suspenders: even if GH_REPO were already correctly set in the real environment,
    the loader must not be the mechanism that could ever change it -- it must skip the line
    entirely rather than relying on setdefault's no-op-when-present behavior."""
    env_file = _write_env(tmp_path, "GH_REPO=someone/else\n")
    monkeypatch.setattr(config, "ENV_FILE", env_file)
    monkeypatch.setenv("GH_REPO", "correct/repo")

    config.load_env_file()

    import os
    assert os.environ.get("GH_REPO") == "correct/repo"  # untouched, never overwritten
