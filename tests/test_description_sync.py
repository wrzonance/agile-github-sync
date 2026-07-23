"""Unit tests for description_sync.py's pure merge (issue #65). No I/O -- pins the full 9-state
resolve_description matrix plus the two canonicalization helpers' (corrected) idempotence
invariants. Run: pytest -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import richtext  # noqa: E402
from description_sync import (  # noqa: E402
    DescriptionResolution,
    _canonicalize_ap_description,
    _canonicalize_gh_body,
    resolve_description,
)


def _assert_invariant(base: str | None, result: DescriptionResolution) -> None:
    """The invariant description_sync.py's own docstring states: conflict==True iff write_gh and
    write_ap are both False AND warning is not None AND merged == (base or "")."""
    conflict_shape = (
        result.write_gh is False and result.write_ap is False
        and result.warning is not None and result.merged == (base or "")
    )
    assert result.conflict == conflict_shape


# =====================================================================================
# resolve_description -- the 9 hand-traced states
# =====================================================================================

def test_seeding_card_empty_pushes_gh_body_to_agileplace():
    result = resolve_description(None, None, "Hello from GitHub", "")
    assert result == DescriptionResolution("Hello from GitHub", False, True, False, None)
    _assert_invariant(None, result)


def test_seeding_gh_body_empty_pulls_card_description_to_github():
    result = resolve_description(None, None, "", "Hello from AgilePlace")
    assert result == DescriptionResolution("Hello from AgilePlace", True, False, False, None)
    _assert_invariant(None, result)


def test_seeding_both_nonempty_and_different_is_a_conflict_that_writes_nothing():
    result = resolve_description(None, None, "GH text", "AP text")
    assert result.write_gh is False
    assert result.write_ap is False
    assert result.conflict is True
    assert result.merged == ""
    assert result.warning is not None
    _assert_invariant(None, result)


def test_steady_state_neither_side_changed_writes_nothing():
    result = resolve_description("Same text", "Same text", "Same text", "Same text")
    assert result == DescriptionResolution("Same text", False, False, False, None)
    _assert_invariant("Same text", result)


def test_truncated_steady_state_compares_ap_side_against_its_own_written_form():
    # desc_base is the FULL agreed canonical; desc_ap_written is the canonical of the truncated
    # text a prior run actually wrote. Neither side changed since ITS OWN reference point, so this
    # must be silent steady state -- comparing ap_canonical against the full base instead would
    # wrongly look like an AP-side edit on every subsequent run.
    full = "A very long description " * 50
    truncated = full[:100] + "...[truncated by sync]"
    result = resolve_description(full, truncated, full, truncated)
    assert result == DescriptionResolution(full, False, False, False, None)
    _assert_invariant(full, result)


def test_gh_only_changed_propagates_to_agileplace():
    result = resolve_description("Old text", "Old text", "New GH text", "Old text")
    assert result == DescriptionResolution("New GH text", False, True, False, None)
    _assert_invariant("Old text", result)


def test_ap_only_changed_propagates_to_github():
    result = resolve_description("Old text", "Old text", "Old text", "New AP text")
    assert result == DescriptionResolution("New AP text", True, False, False, None)
    _assert_invariant("Old text", result)


def test_both_changed_to_different_values_is_a_conflict_that_writes_nothing():
    result = resolve_description("Old text", "Old text", "GH new", "AP new")
    assert result.write_gh is False
    assert result.write_ap is False
    assert result.conflict is True
    assert result.merged == "Old text"
    assert result.warning is not None
    _assert_invariant("Old text", result)


def test_both_changed_but_independently_converged_on_the_same_value_is_not_a_conflict():
    result = resolve_description("Old text", "Old text", "Same new text", "Same new text")
    assert result == DescriptionResolution("Same new text", False, False, False, None)
    _assert_invariant("Old text", result)


# =====================================================================================
# None and "" are indistinguishable inputs on both reference points
# =====================================================================================

def test_none_and_empty_string_base_normalize_identically():
    assert resolve_description(None, None, "x", "") == resolve_description("", "", "x", "")


def test_none_and_empty_string_ap_written_base_normalize_identically():
    assert (resolve_description("b", None, "b", "y")
            == resolve_description("b", "", "b", "y"))


# =====================================================================================
# _canonicalize_gh_body -- genuine md->html->md round trip, self-composition-idempotent
# =====================================================================================

def test_canonicalize_gh_body_normalizes_none_and_missing_to_empty_string():
    assert _canonicalize_gh_body(None) == ""
    assert _canonicalize_gh_body("") == ""


def test_canonicalize_gh_body_is_self_composition_idempotent():
    body = "Some **bold** and *italic* text\n\n- item one\n- item two\n"
    once = _canonicalize_gh_body(body)
    twice = _canonicalize_gh_body(once)
    assert once == twice


# =====================================================================================
# _canonicalize_ap_description -- one-directional html->md, NOT self-composition-idempotent;
# the real invariant is a round trip THROUGH HTML (spike finding #3 correction).
# =====================================================================================

def test_canonicalize_ap_description_normalizes_none_and_missing_to_empty_string():
    assert _canonicalize_ap_description(None) == ""
    assert _canonicalize_ap_description("") == ""


def test_canonicalize_ap_description_round_trips_through_html():
    html = "<p>Intro <strong>bold</strong> and <em>italic</em> text with <code>code()</code>.</p>"
    once = _canonicalize_ap_description(html)
    rerendered_html = richtext.markdown_to_leankit_html(once)
    twice = _canonicalize_ap_description(rerendered_html)
    assert once == twice


def test_canonicalize_ap_description_is_not_self_composition_idempotent():
    # Feeding the function's own Markdown output back in AS IF it were HTML is a type mismatch,
    # not a no-op: the HTML->Markdown walker escapes the literal '*' characters it sees as plain
    # text, so a naive f(f(x)) != f(x) here -- this is the CORRECTED invariant's whole point.
    html = "<p>Intro <strong>bold</strong> text.</p>"
    once = _canonicalize_ap_description(html)
    naive_twice = _canonicalize_ap_description(once)
    assert once != naive_twice
