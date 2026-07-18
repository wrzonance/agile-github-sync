"""Unit tests for ghkit's label-name safety guard.

gh's --add-label/--remove-label flag is a pflag StringSlice: it CSV-splits its value, so a label
name containing a comma or starting with a leading '"' would arrive at gh as multiple or garbled
labels rather than the one name the caller intended. These tests pin edit_label's guard clause and
is_gh_label_safe's pure/total contract at the module boundary -- no network, no gh CLI. Run: pytest -q
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ghkit import edit_label, is_gh_label_safe  # noqa: E402


# --- is_gh_label_safe: pure, total predicate ------------------------------

def test_safe_plain_label():
    assert is_gh_label_safe("bug") is True


def test_unsafe_label_with_comma():
    assert is_gh_label_safe("bug,feature") is False


def test_unsafe_label_leading_quote():
    assert is_gh_label_safe('"quoted') is False


def test_safe_label_with_internal_quote():
    # only a *leading* quote is CSV-parse-significant to the flag value
    assert is_gh_label_safe('quo"ted') is True


def test_safe_empty_string():
    assert is_gh_label_safe("") is True


def test_is_gh_label_safe_returns_bool_for_arbitrary_input():
    for name in ["", ",", '"', "a,b,c", '"""', "milestone:1.0", "a" * 200]:
        result = is_gh_label_safe(name)
        assert isinstance(result, bool)


# --- edit_label: guard clause raises before any gh call or DRY print -----

def test_edit_label_raises_on_comma_when_applying(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr("ghkit.run", lambda *a, **k: calls.append((a, k)))
    with pytest.raises(ValueError):
        edit_label({}, True, 5, "bug,feature", add=True)
    assert calls == []                       # never shelled out
    assert capsys.readouterr().out == ""      # never printed a gh/DRY line


def test_edit_label_raises_on_leading_quote_when_dry_run(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr("ghkit.run", lambda *a, **k: calls.append((a, k)))
    with pytest.raises(ValueError):
        edit_label({}, False, 5, '"bug', add=False)
    assert calls == []
    assert capsys.readouterr().out == ""      # never printed the DRY line either


def test_edit_label_still_works_for_safe_labels(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr("ghkit.run", lambda *a, **k: calls.append((a, k)))
    result = edit_label({}, True, 5, "bug", add=True)
    assert result is None
    assert len(calls) == 1
    assert capsys.readouterr().out.startswith("gh    issue 5 add-label bug")
