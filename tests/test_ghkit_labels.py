"""Unit tests for ghkit's label-name safety guard, and for sync.py's reconcile-boundary filter that
keeps CSV-unsafe label names from ever reaching ghkit.edit_label.

gh's --add-label/--remove-label flag is a pflag StringSlice: it CSV-splits its value, so a label
name containing a comma or starting with a leading '"' would arrive at gh as multiple or garbled
labels rather than the one name the caller intended. These tests pin edit_label's guard clause and
is_gh_label_safe's pure/total contract at the module boundary -- no network, no gh CLI -- plus
sync._filter_gh_safe_labels and sync_metadata's persisted-merge-base arithmetic that must never
record a label as GitHub-side-applied when it was not actually written. Run: pytest -q
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ghkit import edit_label, is_gh_label_safe  # noqa: E402
from sync import _filter_gh_safe_labels, sync_metadata  # noqa: E402


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


# --- _filter_gh_safe_labels: pure subset + one WARN per rejected name -----

def test_filter_gh_safe_labels_keeps_all_safe_names(capsys):
    names = frozenset({"bug", "feature"})
    result = _filter_gh_safe_labels(names, key="42", action="add")
    assert result == names
    assert capsys.readouterr().out == ""


def test_filter_gh_safe_labels_drops_unsafe_and_warns(capsys):
    names = frozenset({"bug", "a,b"})
    result = _filter_gh_safe_labels(names, key="42", action="add")
    assert result == frozenset({"bug"})
    out = capsys.readouterr().out
    assert "WARN" in out
    assert "42" in out
    assert "a,b" in out or "'a,b'" in out
    assert "add" in out


def test_filter_gh_safe_labels_one_warn_per_rejected_name(capsys):
    names = frozenset({"a,1", "b,2", "ok"})
    result = _filter_gh_safe_labels(names, key="k", action="remove")
    assert result == frozenset({"ok"})
    lines = [l for l in capsys.readouterr().out.splitlines() if l.startswith("WARN")]
    assert len(lines) == 2  # exactly one per rejected name, not per retry


# --- sync_metadata: unsafe labels never reach edit_label; merge base is ----
# --- corrected to reflect what actually happened on GitHub -----------------

def _issue(number=42, labels=None, milestone=None):
    return {
        "number": number,
        "title": f"[T{number}] Issue {number}",
        "labels": labels or [],
        "milestone": milestone,
        "url": f"https://github.com/o/r/issues/{number}",
    }


def _card(tags=None):
    return {"id": "c1", "tags": tags or []}


def test_sync_metadata_skips_unsafe_labels_and_fixes_merge_base(monkeypatch, capsys):
    """base has an unsafe label GitHub still carries ('x,y'); AgilePlace introduces a new unsafe
    label ('a,b'). Reconcile would want to add 'a,b' on GitHub and remove 'x,y' from GitHub -- both
    unsafe, so both must be skipped, edit_label must never be called, and the persisted merge base
    must reflect reality: 'a,b' never landed on GitHub (must NOT be in the new base) and 'x,y' was
    never actually removed from GitHub (must STILL be in the new base)."""
    issue = _issue(labels=["x,y"])
    card = _card(tags=["a,b"])
    issues_state = {issue["url"]: {"labels": ["x,y"], "milestone": None}}

    calls = []
    monkeypatch.setattr("ghkit.edit_label", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr("ghkit.set_milestone", lambda *a, **k: calls.append(("set_milestone", a, k)))

    queued = []
    sync_metadata({}, True, issue, card, frozenset(), issues_state,
                  lambda c, ops, note: queued.append((c, ops, note)))

    assert calls == []  # unsafe names never reached ghkit
    prev = issues_state[issue["url"]]
    assert "a,b" not in prev["labels"]   # skipped add -> never actually on GitHub -> not in base
    assert "x,y" in prev["labels"]       # skipped remove -> still actually on GitHub -> stays in base

    out = capsys.readouterr().out
    warn_lines = [l for l in out.splitlines() if l.startswith("WARN")]
    assert len(warn_lines) == 2


def test_sync_metadata_dry_run_never_mutates_state(monkeypatch, capsys):
    issue = _issue(labels=["x,y"])
    card = _card(tags=["a,b"])
    issues_state = {issue["url"]: {"labels": ["x,y"], "milestone": None}}
    before = dict(issues_state[issue["url"]])

    monkeypatch.setattr("ghkit.edit_label", lambda *a, **k: None)
    monkeypatch.setattr("ghkit.set_milestone", lambda *a, **k: None)

    sync_metadata({}, False, issue, card, frozenset(), issues_state, lambda c, ops, note: None)

    assert issues_state[issue["url"]] == before  # apply=False -> no state mutation


def test_sync_metadata_backward_compatible_on_safe_labels(monkeypatch, capsys):
    """No comma-or-leading-quote names anywhere -> identical behavior to before this fix: edit_label
    called for the genuinely reconciled adds/removes, base updated to the full reconciled set."""
    issue = _issue(labels=["bug"])
    card = _card(tags=["feature"])
    issues_state = {issue["url"]: {"labels": [], "milestone": None}}

    calls = []
    monkeypatch.setattr("ghkit.edit_label", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr("ghkit.set_milestone", lambda *a, **k: None)

    sync_metadata({}, True, issue, card, frozenset(), issues_state, lambda c, ops, note: None)

    assert len(calls) == 1  # only "feature" needs adding on GitHub ("bug" is already there)
    prev = issues_state[issue["url"]]
    assert set(prev["labels"]) == {"bug", "feature"}
    assert capsys.readouterr().out.count("WARN") == 0
