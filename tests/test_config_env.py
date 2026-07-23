"""Unit tests for issue #15: config.load_env_file() must never let a .env file inject GH_REPO or
GH_HOST into os.environ, even when the real environment doesn't already have them set (the case
os.environ.setdefault alone can't protect against). Uses tmp_path + monkeypatch so the real .env /
process environment is never touched and no state leaks into other tests.

Run: pytest -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

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
    the loader must not be the mechanism that could ever change it -- it must skip the blocklisted
    line entirely rather than relying on os.environ.setdefault's no-op-when-present behavior.

    Checking only the post-call environ value can't tell those two implementations apart: setdefault
    is *always* a no-op when the key already exists, blocklist or not. So this spies directly on
    os.environ.setdefault to prove it is never even called for GH_REPO/GH_HOST, while unrelated keys
    still go through it normally."""
    import os

    env_file = _write_env(
        tmp_path, "GH_REPO=someone/else\nGH_HOST=stale.example.com\nAGILEPLACE_TOKEN=abc123\n"
    )
    monkeypatch.setattr(config, "ENV_FILE", env_file)
    monkeypatch.setenv("GH_REPO", "correct/repo")
    monkeypatch.setenv("GH_HOST", "correct.example.com")
    monkeypatch.delenv("AGILEPLACE_TOKEN", raising=False)

    setdefault_calls: list[str] = []
    real_setdefault = os.environ.setdefault

    def spy_setdefault(key, value):
        setdefault_calls.append(key)
        return real_setdefault(key, value)

    monkeypatch.setattr(os.environ, "setdefault", spy_setdefault)

    config.load_env_file()

    assert "GH_REPO" not in setdefault_calls  # never even attempted, not just a no-op
    assert "GH_HOST" not in setdefault_calls
    assert "AGILEPLACE_TOKEN" in setdefault_calls  # unrelated key still goes through setdefault
    assert os.environ.get("GH_REPO") == "correct/repo"  # untouched, never overwritten
    assert os.environ.get("GH_HOST") == "correct.example.com"


# --- AP_DESCRIPTION_MAX_LENGTH: safe int parse with WARN fallback (issue #65 Task 1) -------------
#
# description_sync's truncation boundary depends on env_config() always handing back a usable
# positive int here, whatever garbage a .env file or the real environment supplies -- these tests
# pin that env_config() itself never raises and never silently produces a 0/negative ceiling that
# would degrade _truncate_for_agileplace to a marker-only result on every single run.

def test_default_ap_description_max_length_is_a_positive_int():
    assert isinstance(config.DEFAULT_AP_DESCRIPTION_MAX_LENGTH, int)
    assert config.DEFAULT_AP_DESCRIPTION_MAX_LENGTH > 0


def test_env_config_ap_description_max_length_defaults_when_unset(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env")  # no .env file present
    monkeypatch.delenv("AP_DESCRIPTION_MAX_LENGTH", raising=False)

    cfg = config.env_config()

    assert cfg["ap_description_max_length"] == config.DEFAULT_AP_DESCRIPTION_MAX_LENGTH


def test_env_config_ap_description_max_length_reads_valid_override(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setenv("AP_DESCRIPTION_MAX_LENGTH", "5000")

    cfg = config.env_config()

    assert cfg["ap_description_max_length"] == 5000


def test_env_config_ap_description_max_length_falls_back_and_warns_on_non_int(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setenv("AP_DESCRIPTION_MAX_LENGTH", "not-a-number")

    cfg = config.env_config()

    assert cfg["ap_description_max_length"] == config.DEFAULT_AP_DESCRIPTION_MAX_LENGTH
    out = capsys.readouterr().out
    assert "WARN" in out
    assert "AP_DESCRIPTION_MAX_LENGTH" in out


def test_env_config_ap_description_max_length_falls_back_and_warns_on_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setenv("AP_DESCRIPTION_MAX_LENGTH", "0")

    cfg = config.env_config()

    assert cfg["ap_description_max_length"] == config.DEFAULT_AP_DESCRIPTION_MAX_LENGTH
    assert "WARN" in capsys.readouterr().out


def test_env_config_ap_description_max_length_falls_back_and_warns_on_negative(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setenv("AP_DESCRIPTION_MAX_LENGTH", "-1")

    cfg = config.env_config()

    assert cfg["ap_description_max_length"] == config.DEFAULT_AP_DESCRIPTION_MAX_LENGTH
    assert "WARN" in capsys.readouterr().out


def test_env_config_ap_description_max_length_treats_blank_as_unset(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setenv("AP_DESCRIPTION_MAX_LENGTH", "   ")

    cfg = config.env_config()

    assert cfg["ap_description_max_length"] == config.DEFAULT_AP_DESCRIPTION_MAX_LENGTH
    assert capsys.readouterr().out == ""  # blank is "unset", not a malformed value -- no WARN noise


# --- comment_sync_identity: pure parse, ZERO print/WARN at env_config() parse time (issue #66 -----
# Task 1). The self-disable WARN belongs to comment_sync.sync_comments' first real invocation, not
# to config parsing -- these tests pin that env_config() never becomes an I/O side effect regardless
# of whether COMMENT_SYNC_GH_LOGIN/COMMENT_SYNC_AP_AUTHOR are set, unset, or blank, because two other
# live suites (this file, test_probe_dependencies.py) assert exact capsys output around env_config()
# calls that don't touch comment sync at all.

def test_parse_comment_sync_identity_returns_dict_when_both_present():
    identity = config._parse_comment_sync_identity("octocat", "Jane Doe")

    assert identity == {"gh_login": "octocat", "ap_author": "Jane Doe"}


def test_parse_comment_sync_identity_strips_whitespace():
    identity = config._parse_comment_sync_identity("  octocat  ", "  Jane Doe  ")

    assert identity == {"gh_login": "octocat", "ap_author": "Jane Doe"}


def test_parse_comment_sync_identity_none_when_gh_login_missing():
    assert config._parse_comment_sync_identity(None, "Jane Doe") is None


def test_parse_comment_sync_identity_none_when_ap_author_missing():
    assert config._parse_comment_sync_identity("octocat", None) is None


def test_parse_comment_sync_identity_none_when_both_missing():
    assert config._parse_comment_sync_identity(None, None) is None


def test_parse_comment_sync_identity_none_when_gh_login_blank():
    assert config._parse_comment_sync_identity("   ", "Jane Doe") is None


def test_parse_comment_sync_identity_none_when_ap_author_blank():
    assert config._parse_comment_sync_identity("octocat", "   ") is None


def test_parse_comment_sync_identity_never_prints(capsys):
    config._parse_comment_sync_identity(None, None)
    config._parse_comment_sync_identity("octocat", None)
    config._parse_comment_sync_identity(None, "Jane Doe")
    config._parse_comment_sync_identity("octocat", "Jane Doe")
    config._parse_comment_sync_identity("", "")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_env_config_wires_comment_sync_identity_when_both_set(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setenv("COMMENT_SYNC_GH_LOGIN", "octocat")
    monkeypatch.setenv("COMMENT_SYNC_AP_AUTHOR", "Jane Doe")

    cfg = config.env_config()

    assert cfg["comment_sync_identity"] == {"gh_login": "octocat", "ap_author": "Jane Doe"}


def test_env_config_comment_sync_identity_none_when_unset(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env")
    monkeypatch.delenv("COMMENT_SYNC_GH_LOGIN", raising=False)
    monkeypatch.delenv("COMMENT_SYNC_AP_AUTHOR", raising=False)

    cfg = config.env_config()

    assert cfg["comment_sync_identity"] is None


@pytest.mark.parametrize(
    "gh_login,ap_author",
    [
        (None, None),
        ("octocat", None),
        (None, "Jane Doe"),
        ("", ""),
        ("   ", "Jane Doe"),
        ("octocat", "   "),
    ],
)
def test_env_config_never_prints_for_any_comment_sync_identity_combination(
    tmp_path, monkeypatch, capsys, gh_login, ap_author
):
    """The invariant that matters most for finding #1: env_config() is silent no matter what shape
    COMMENT_SYNC_GH_LOGIN/COMMENT_SYNC_AP_AUTHOR come in as -- set, unset, or blank. The WARN, if any,
    belongs to comment_sync.sync_comments' first real call, never to config parsing."""
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env")
    for key, value in (("COMMENT_SYNC_GH_LOGIN", gh_login), ("COMMENT_SYNC_AP_AUTHOR", ap_author)):
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)

    config.env_config()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_deleted_comment_sync_identity_is_repopulated_by_dotenv_but_blank_is_not(tmp_path, monkeypatch):
    """Platform-independent reproduction of the Windows e2e failure: env_config() calls
    load_env_file(), which does os.environ.setdefault() from the repo .env. A merely-DELETED
    COMMENT_SYNC_* var is therefore REPOPULATED from a .env that exports the production identity --
    silently re-enabling comment sync (on the user's box this hit un-stubbed endpoints in 4
    test_run.py tests). A present-but-BLANK value survives setdefault (it only fills UNSET keys) and
    _parse_comment_sync_identity treats blank as disabled -- which is why the run harness's _configure
    blanks these vars rather than deleting them."""
    import os

    env_file = _write_env(
        tmp_path, "COMMENT_SYNC_GH_LOGIN=someone\nCOMMENT_SYNC_AP_AUTHOR=someone@example.com\n")
    monkeypatch.setattr(config, "ENV_FILE", env_file)

    # DELETED -> load_env_file's setdefault refills it from the .env -> identity comes back
    monkeypatch.delenv("COMMENT_SYNC_GH_LOGIN", raising=False)
    monkeypatch.delenv("COMMENT_SYNC_AP_AUTHOR", raising=False)
    assert config.env_config()["comment_sync_identity"] is not None
    assert os.environ["COMMENT_SYNC_GH_LOGIN"] == "someone"  # repopulated behind our back

    # BLANK -> setdefault is a no-op (key already set) -> identity stays disabled
    monkeypatch.setenv("COMMENT_SYNC_GH_LOGIN", "")
    monkeypatch.setenv("COMMENT_SYNC_AP_AUTHOR", "")
    assert config.env_config()["comment_sync_identity"] is None
