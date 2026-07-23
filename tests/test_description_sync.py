"""Unit tests for description_sync.py's pure merge (issue #65). No I/O -- pins the full 10-state
resolve_description matrix plus the two canonicalization helpers' (corrected) idempotence
invariants. Run: pytest -q
"""
from __future__ import annotations

import math
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import richtext  # noqa: E402
from description_sync import (  # noqa: E402
    TRUNCATION_MARKER,
    DescriptionResolution,
    _canonicalize_ap_description,
    _canonicalize_gh_body,
    _truncate_for_agileplace,
    resolve_description,
    sync_description,
)

ISSUE_URL = "https://github.com/acme/repo/issues/1"


def _issue(body="", url=ISSUE_URL, number=1, title="[T1] widget"):
    return {"number": number, "title": title, "url": url, "body": body}


def _card(card_id="C1"):
    return {"id": card_id}


def _cfg():
    return {"ap_description_max_length": 20000}


class _Queue:
    """Records every queue(card, ops, note) call for assertions -- same spy shape as
    test_sync_dates.py's _Queue."""
    def __init__(self):
        self.calls = []

    def __call__(self, card, ops, note):
        self.calls.append((card, ops, note))


def _assert_invariant(base: str | None, result: DescriptionResolution) -> None:
    """The invariant description_sync.py's own docstring states: conflict==True iff write_gh and
    write_ap are both False AND warning is not None AND merged == (base or "")."""
    conflict_shape = (
        result.write_gh is False and result.write_ap is False
        and result.warning is not None and result.merged == (base or "")
    )
    assert result.conflict == conflict_shape


# =====================================================================================
# resolve_description -- the 10 hand-traced states
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


def test_ap_edit_on_top_of_a_truncated_stub_is_not_blindly_pushed_to_github():
    # desc_base is the FULL agreed canonical; desc_ap_written is only the canonical of the
    # TRUNCATED stand-in a prior run actually wrote (see the truncated-steady-state test above).
    # If the card's truncated stub is edited further (a comment appended, a typo fixed on the
    # visible portion, ...), ap_canonical now differs from desc_ap_written -- but ap_canonical is
    # still only that truncated stub, never the untruncated tail that lives solely in `base`.
    # Blindly promoting it to `merged` and pushing it to GitHub would permanently destroy the lost
    # tail with no warning (critical review finding for issue #65). This must degrade to the same
    # warn-and-skip conflict policy as a genuine both-sides conflict: nothing is written, the full
    # base stays put, and a human is told to reconcile by hand.
    full = "A very long description " * 50
    truncated = full[:100] + "...[truncated by sync]"
    edited_truncated = truncated + " one more sentence appended on the card"
    result = resolve_description(full, truncated, full, edited_truncated)
    assert result.write_gh is False
    assert result.write_ap is False
    assert result.conflict is True
    assert result.merged == full
    assert result.warning is not None
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


# =====================================================================================
# _truncate_for_agileplace -- binary-search truncation (spike finding #1: replaces an O(n^2)
# per-word shrink loop that took 52+s on a 25k-char body). Pins: under-limit passthrough,
# over-limit truncation that fits max_length with TRUNCATION_MARKER appended, graceful
# degeneration on a tiny max_length, and an O(log n)-renders wall-clock bound on a >=20k body.
# =====================================================================================

def test_truncate_under_limit_returns_full_html_unchanged():
    markdown = "A short description that is well under any reasonable limit."
    full_html = richtext.markdown_to_leankit_html(markdown)
    html, was_truncated = _truncate_for_agileplace(markdown, max_length=len(full_html) + 100)
    assert html == full_html
    assert was_truncated is False


def test_truncate_exactly_at_limit_returns_full_html_unchanged():
    markdown = "Exactly at the boundary."
    full_html = richtext.markdown_to_leankit_html(markdown)
    html, was_truncated = _truncate_for_agileplace(markdown, max_length=len(full_html))
    assert html == full_html
    assert was_truncated is False


def test_truncate_over_limit_fits_max_length_and_appends_marker():
    markdown = " ".join(f"word{i}" for i in range(2000))
    max_length = 500
    html, was_truncated = _truncate_for_agileplace(markdown, max_length=max_length)
    assert was_truncated is True
    assert len(html) <= max_length
    assert html.endswith(TRUNCATION_MARKER)


def test_truncate_never_negative_length_slices_on_tiny_input():
    # A single "word" with no whitespace at all -- snapping to a whitespace boundary must degrade
    # to an empty prefix rather than slicing with a negative index.
    markdown = "x" * 50
    html, was_truncated = _truncate_for_agileplace(markdown, max_length=10)
    assert was_truncated is True
    assert html.endswith(TRUNCATION_MARKER)


def test_truncate_degenerate_tiny_max_length_degrades_to_marker_only():
    markdown = "Several words that would normally survive truncation easily here."
    html, was_truncated = _truncate_for_agileplace(markdown, max_length=1)
    assert was_truncated is True
    # Even a budget smaller than the marker itself must terminate and produce the marker-only
    # result, never raise and never loop forever.
    assert html == TRUNCATION_MARKER


def test_truncate_result_never_exceeds_max_length_when_achievable():
    markdown = "Alpha beta gamma delta epsilon zeta eta theta iota kappa. " * 20
    max_length = 200
    html, was_truncated = _truncate_for_agileplace(markdown, max_length=max_length)
    assert was_truncated is True
    assert len(html) <= max_length


def test_truncate_large_body_uses_a_logarithmic_number_of_renders_not_linear():
    # Pins the O(log n)-RENDERS invariant _truncate_for_agileplace's own docstring claims, by
    # counting actual calls to richtext.markdown_to_leankit_html instead of inferring it from
    # wall-clock time. A wall-clock ceiling is only a proxy: a regression to a coarser-but-still-
    # polynomial scheme (fixed-percentage chunks, an accidental O(sqrt(n)) loop, ...) could still
    # finish under a generous time budget and pass, and a genuinely correct O(log n) run could
    # exceed a tight one on slow/loaded CI hardware. Counting calls pins the claim directly.
    # Wraps (never replaces) the real renderer so truncation behavior itself is unaffected.
    markdown = ("The quick brown fox jumps over the lazy dog. " * 1500)
    assert len(markdown) >= 20_000
    real_render = richtext.markdown_to_leankit_html
    calls = []

    def _counting_render(md):
        calls.append(md)
        return real_render(md)

    with patch("description_sync.richtext.markdown_to_leankit_html", side_effect=_counting_render):
        html, was_truncated = _truncate_for_agileplace(markdown, max_length=5000)

    assert was_truncated is True
    assert len(html) <= 5000
    # A binary search over len(markdown) candidate cuts takes ~log2(n) iterations plus a couple of
    # fixed renders (the initial full-length probe, the final confirmed cut) -- nowhere near
    # proportional to len(markdown). A regression back to a per-word/per-chunk shrink loop would
    # blow this bound by orders of magnitude on a ~70,000-char input.
    max_expected_renders = math.ceil(math.log2(len(markdown))) + 4
    assert len(calls) <= max_expected_renders, (
        f"_truncate_for_agileplace made {len(calls)} renders (expected <= {max_expected_renders}) "
        "-- the O(log n) fix may have regressed to a linear/near-linear scheme"
    )


# =====================================================================================
# sync_description -- wiring entry point + coupled base-advance gate (issue #65 task 4).
#
# Mirrors sync_dates' merge-base contract (test_sync_dates.py) exactly: desc_base and
# desc_ap_written only ever advance TOGETHER, and only when apply is True AND the GitHub-side
# write is confirmed (gh_write_ok -- trivially True whenever no GitHub write was needed at all).
# The AgilePlace-side queue write is unconditional -- it fires whenever write_ap is True
# regardless of apply/gh_write_ok, and that firing must never leak into the base-advance gate.
# =====================================================================================

def test_sync_description_dry_run_does_not_advance_base_despite_queued_ap_write():
    # gh-changed-only (seeding): write_ap fires and queue is called unconditionally even though
    # apply=False -- but the merge base must NOT advance on a dry run.
    issue = _issue(body="Hello from GitHub")
    card = _card()
    state = {ISSUE_URL: {}}
    queue = _Queue()
    with patch("description_sync.agileplace_description.card_description", return_value=""), \
         patch("description_sync.agileplace_description.op_description", return_value="OP") as op_mock, \
         patch("description_sync.ghkit.edit_issue_body") as edit_mock:
        sync_description(_cfg(), False, issue, card, state, queue)
    op_mock.assert_called_once()
    edit_mock.assert_not_called()
    assert len(queue.calls) == 1
    assert "desc_base" not in state[ISSUE_URL]
    assert "desc_ap_written" not in state[ISSUE_URL]


def test_sync_description_failed_gh_write_blocks_base_advance():
    # ap-changed-only (seeding): write_gh fires but the GitHub write is reported as failed/skipped
    # -- neither field may advance, even though apply is True.
    issue = _issue(body="")
    card = _card()
    state = {ISSUE_URL: {}}
    queue = _Queue()
    with patch("description_sync.agileplace_description.card_description", return_value="Hello from AgilePlace"), \
         patch("description_sync.agileplace_description.op_description") as op_mock, \
         patch("description_sync.ghkit.edit_issue_body", return_value=False) as edit_mock:
        sync_description(_cfg(), True, issue, card, state, queue)
    edit_mock.assert_called_once_with(_cfg(), True, issue["number"], "Hello from AgilePlace")
    op_mock.assert_not_called()
    assert queue.calls == []
    assert "desc_base" not in state[ISSUE_URL]
    assert "desc_ap_written" not in state[ISSUE_URL]


def test_sync_description_conflict_makes_no_writes_and_no_advance():
    issue = _issue(body="GH text")
    card = _card()
    state = {ISSUE_URL: {}}
    queue = _Queue()
    with patch("description_sync.agileplace_description.card_description", return_value="AP text"), \
         patch("description_sync.agileplace_description.op_description") as op_mock, \
         patch("description_sync.ghkit.edit_issue_body") as edit_mock:
        sync_description(_cfg(), True, issue, card, state, queue)
    edit_mock.assert_not_called()
    op_mock.assert_not_called()
    assert queue.calls == []
    assert "desc_base" not in state[ISSUE_URL]
    assert "desc_ap_written" not in state[ISSUE_URL]


def test_sync_description_confirmed_gh_write_advances_both_fields_together():
    # ap-changed-only, confirmed GitHub write -> both fields advance to the SAME merged value
    # (write_ap never fired here, so desc_ap_written simply reflects the unchanged AP canonical).
    issue = _issue(body="")
    card = _card()
    state = {ISSUE_URL: {}}
    queue = _Queue()
    with patch("description_sync.agileplace_description.card_description", return_value="Hello from AgilePlace"), \
         patch("description_sync.agileplace_description.op_description") as op_mock, \
         patch("description_sync.ghkit.edit_issue_body", return_value=True) as edit_mock:
        sync_description(_cfg(), True, issue, card, state, queue)
    edit_mock.assert_called_once_with(_cfg(), True, issue["number"], "Hello from AgilePlace")
    op_mock.assert_not_called()
    assert queue.calls == []
    assert state[ISSUE_URL]["desc_base"] == "Hello from AgilePlace"
    assert state[ISSUE_URL]["desc_ap_written"] == "Hello from AgilePlace"


def test_sync_description_confirmed_ap_write_advances_both_fields_together():
    # gh-changed-only (seeding), no GitHub write needed at all -> gh_write_ok is trivially True,
    # so both fields advance even though the only write that happened was the queued AP op.
    issue = _issue(body="Hello from GitHub")
    card = _card()
    state = {ISSUE_URL: {}}
    queue = _Queue()
    written_html = richtext.markdown_to_leankit_html("Hello from GitHub")
    expected_ap_written = _canonicalize_ap_description(written_html)
    with patch("description_sync.agileplace_description.card_description", return_value=""), \
         patch("description_sync.agileplace_description.op_description", return_value="OP") as op_mock, \
         patch("description_sync.ghkit.edit_issue_body") as edit_mock:
        sync_description(_cfg(), True, issue, card, state, queue)
    edit_mock.assert_not_called()
    op_mock.assert_called_once_with(written_html)
    assert queue.calls == [(card, ["OP"], "description")]
    assert state[ISSUE_URL]["desc_base"] == "Hello from GitHub"
    assert state[ISSUE_URL]["desc_ap_written"] == expected_ap_written


def test_sync_description_never_reads_a_plan_only_dry_run_card():
    # Discovered running the wiring end-to-end (tests/test_run.py's dry-run path, issue #65 task
    # 5/7): a dry-run-only card carries agileplace.create_card's synthetic "_planOnly" marker and
    # has no server-side identity yet, so it never has a 'description' key. Without a guard,
    # agileplace_description.card_description()'s lazy get_card() fallback fires a live GET for an id that was
    # never created on the server -- exactly the network read sync.py's own dependency-sync loop
    # already refuses for plan-only cards ("a fresh card has no server-side dependencies; never
    # read a plan-only id"). sync_description must apply that same convention: treat a plan-only
    # card's AgilePlace-side description as "" without calling agileplace_description.card_description() at all.
    issue = _issue(body="Hello from GitHub")
    card = {**_card(), "_planOnly": True}
    state = {ISSUE_URL: {}}
    queue = _Queue()
    written_html = richtext.markdown_to_leankit_html("Hello from GitHub")
    with patch("description_sync.agileplace_description.card_description") as card_description_mock, \
         patch("description_sync.agileplace_description.op_description", return_value="OP") as op_mock, \
         patch("description_sync.ghkit.edit_issue_body") as edit_mock:
        sync_description(_cfg(), False, issue, card, state, queue)
    card_description_mock.assert_not_called()
    edit_mock.assert_not_called()
    op_mock.assert_called_once_with(written_html)
    assert queue.calls == [(card, ["OP"], "description")]
