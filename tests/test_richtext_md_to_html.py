"""Unit tests for richtext.py's Markdown->HTML direction (markdown_to_leankit_html): the block
layer (headings, fenced code, cross-block list nesting/numbering, immutability of the folded list
stack) and the inline layer (bold/italic/strike/links/code spans). Pins nesting correctness,
content conservation, bounded work over pathological delimiter runs, and whitelist closure (the
HTML output never leaks unsupported tag syntax). No I/O.

Run: pytest -q
"""
from __future__ import annotations

import string
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from richtext import (  # noqa: E402
    _MAX_INLINE_DEPTH,
    _Block,
    _ListFrame,
    _parse_blocks,
    _render_block_html,
    leankit_html_to_markdown,
    markdown_to_leankit_html,
)

ALL_PRINTABLE = string.printable
UNICODE_SAMPLE = "héllo wörld ☃ \U0001F600 ​‌‍"


# =====================================================================================
# markdown_to_leankit_html -- MD->HTML block layer, plain-text inline content
# =====================================================================================

# --- invariant: nesting correctness (cross-block list folding) ---------------------------------

@pytest.mark.parametrize(
    "markdown, expected_html",
    [
        ("- one\n- two", "<ul><li>one</li><li>two</li></ul>"),
        ("1. a\n2. b", "<ol><li>a</li><li>b</li></ol>"),
        ("- parent\n  - child", "<ul><li>parent<ul><li>child</li></ul></li></ul>"),
        ("- item\n\nafter", "<ul><li>item</li></ul><p>after</p>"),
        ("before\n\n- item", "<p>before</p><ul><li>item</li></ul>"),
    ],
)
def test_list_folding_preserves_nesting_and_numbering_across_blocks(markdown, expected_html):
    assert markdown_to_leankit_html(markdown) == expected_html


def test_three_level_nesting_closes_in_correct_lifo_order():
    markdown = "- one\n  - two\n    - three"
    result = markdown_to_leankit_html(markdown)
    assert result == "<ul><li>one<ul><li>two<ul><li>three</li></ul></li></ul></li></ul>"


def test_returning_to_a_shallower_depth_closes_the_deeper_list_first():
    markdown = "- one\n  - nested\n- two"
    result = markdown_to_leankit_html(markdown)
    assert result == "<ul><li>one<ul><li>nested</li></ul></li><li>two</li></ul>"


@pytest.mark.parametrize(
    "markdown, expected_html",
    [
        # First list item already indented two levels deeper than the (nonexistent) parent --
        # there is no intermediate <li> to hang the synthetic intermediate <ul> off of, so the
        # jump is clamped to exactly one level deeper than what's open (none), not one container
        # per skipped indent level.
        ("    - deep item", "<ul><li>deep item</li></ul>"),
        # A sibling item's indent jumps from 0 to 6 spaces (3 levels) then back to 0 -- every
        # opened container must still be paired with exactly one <li>.
        ("- a\n      - c\n- d", "<ul><li>a<ul><li>c</li></ul></li><li>d</li></ul>"),
    ],
)
def test_indent_jump_of_more_than_one_level_clamps_to_a_single_new_level(markdown, expected_html):
    result = markdown_to_leankit_html(markdown)
    assert result == expected_html
    assert result.count("<li>") == result.count("</li>")


def test_switching_list_type_at_same_depth_closes_and_reopens_the_container():
    markdown = "- bullet\n1. ordered"
    result = markdown_to_leankit_html(markdown)
    assert result == "<ul><li>bullet</li></ul><ol><li>ordered</li></ol>"


def test_per_block_isolation_would_flatten_nesting_but_folding_does_not():
    # Regression pin for the spike's failure mode: rendering each list_item block against an
    # empty/local list_stack (instead of folding against the running stack) turns every item
    # into its own single-item <ul>/<ol>, resetting numbering and losing nesting entirely.
    markdown = "1. first\n2. second\n3. third"
    result = markdown_to_leankit_html(markdown)
    assert result.count("<ol>") == 1
    assert result.count("</ol>") == 1
    assert result == "<ol><li>first</li><li>second</li><li>third</li></ol>"


# --- invariant: immutability --------------------------------------------------------------------

def test_render_block_html_never_mutates_the_caller_supplied_list_stack():
    original_stack = [_ListFrame(ordered=False, index=1)]
    snapshot = list(original_stack)
    block = _Block(kind="list_item", level=1, ordered=False, text="sibling")
    _render_block_html(block, original_stack)
    assert original_stack == snapshot


def test_render_block_html_returns_a_new_list_stack_object():
    original_stack = [_ListFrame(ordered=False, index=1)]
    block = _Block(kind="list_item", level=1, ordered=False, text="sibling")
    _, new_stack = _render_block_html(block, original_stack)
    assert new_stack is not original_stack


def test_render_block_html_does_not_mutate_stack_when_closing_lists_for_a_non_list_block():
    original_stack = [_ListFrame(ordered=True, index=2)]
    snapshot = list(original_stack)
    block = _Block(kind="paragraph", level=0, ordered=False, text="after")
    _render_block_html(block, original_stack)
    assert original_stack == snapshot


def test_parse_blocks_is_deterministic_across_repeated_calls_on_the_same_input():
    # Python str values are immutable at the language level, so "the input string wasn't
    # mutated" can never fail regardless of _parse_blocks's implementation -- the only property
    # actually worth pinning here is that _parse_blocks carries no hidden mutable state (e.g. a
    # module-level cache keyed loosely, or an accumulator not reset between calls) that would make
    # two calls on identical input diverge.
    markdown = "- one\n- two"
    blocks_first = _parse_blocks(markdown)
    blocks_second = _parse_blocks(markdown)
    assert blocks_first == blocks_second


# --- invariant: whitelist closure (HTML output only uses the supported subset) -------------------

@pytest.mark.parametrize(
    "markdown, leaked_tag_syntax",
    [
        ("<script>alert(1)</script>", "<script"),
        ('<div class="x">payload</div>', "<div"),
        ("- <img src=x onerror=alert(1)>", "<img"),
        ("plain <iframe src=evil></iframe> text", "<iframe"),
    ],
)
def test_markdown_source_with_raw_html_never_leaks_unescaped_tags(markdown, leaked_tag_syntax):
    result = markdown_to_leankit_html(markdown)
    assert leaked_tag_syntax not in result


def test_code_block_content_is_html_escaped_not_emitted_as_live_tags():
    markdown = "```\n<script>alert(1)</script>\n```"
    result = markdown_to_leankit_html(markdown)
    assert "<script>alert(1)</script>" not in result
    assert "&lt;script&gt;" in result


def test_heading_and_list_item_text_is_html_escaped():
    markdown = "# <b>Title</b>\n\n- <b>item</b>"
    result = markdown_to_leankit_html(markdown)
    assert "<b>" not in result
    assert "&lt;b&gt;Title&lt;/b&gt;" in result
    assert "&lt;b&gt;item&lt;/b&gt;" in result


# --- boundary + totality --------------------------------------------------------------------------

@pytest.mark.parametrize("bad_input", [None, 123, 3.14, [], {}, b"bytes"])
def test_markdown_to_leankit_html_raises_typeerror_at_the_str_boundary(bad_input):
    with pytest.raises(TypeError, match="expected str, got"):
        markdown_to_leankit_html(bad_input)


@pytest.mark.parametrize(
    "markdown",
    [
        "",
        "   \n\n   ",
        "- unclosed list with no trailing content",
        "```\nunclosed fence",
        "# " * 100,
        ALL_PRINTABLE,
        UNICODE_SAMPLE,
    ],
)
def test_markdown_to_leankit_html_never_raises_over_arbitrary_content(markdown):
    result = markdown_to_leankit_html(markdown)
    assert isinstance(result, str)


# =====================================================================================
# markdown_to_leankit_html -- inline formatting layer: bold/italic/strike/links/code spans
# =====================================================================================

# --- invariant: content conservation (inline substitution) -------------------------------------

@pytest.mark.parametrize(
    "markdown, expected_html",
    [
        ("**bold**", "<p><strong>bold</strong></p>"),
        ("*italic*", "<p><em>italic</em></p>"),
        ("~~strike~~", "<p><s>strike</s></p>"),
        ("`code()`", "<p><code>code()</code></p>"),
        ("[click](https://example.com)", '<p><a href="https://example.com">click</a></p>'),
        (
            "**bold *and nested* text**",
            "<p><strong>bold <em>and nested</em> text</strong></p>",
        ),
        (
            "**[a link](https://example.com) inside bold**",
            '<p><strong><a href="https://example.com">a link</a> inside bold</strong></p>',
        ),
        ("# **Bold** heading", "<h1><strong>Bold</strong> heading</h1>"),
        ("- **bold** item", "<ul><li><strong>bold</strong> item</li></ul>"),
        ("plain text, no markers", "<p>plain text, no markers</p>"),
    ],
)
def test_inline_formatting_renders_the_supported_subset(markdown, expected_html):
    assert markdown_to_leankit_html(markdown) == expected_html


def test_link_with_disallowed_href_scheme_degrades_to_text_only():
    result = markdown_to_leankit_html("[bad](javascript:alert(1))")
    assert result == "<p>bad</p>"
    assert "javascript:" not in result


def test_code_span_content_is_never_inline_substituted():
    result = markdown_to_leankit_html("`*not bold* and _not italic_`")
    assert result == "<p><code>*not bold* and _not italic_</code></p>"


def test_unclosed_bold_marker_degrades_to_literal_text_not_a_dangling_tag():
    result = markdown_to_leankit_html("**unclosed bold")
    assert result == "<p>**unclosed bold</p>"
    assert "<strong>" not in result


# --- invariant: escape/unescape are true inverses outside protected code-span regions -----------

def test_literal_ambiguous_char_survives_full_round_trip_without_becoming_a_delimiter():
    html = "<p>a*b</p>"
    md = leankit_html_to_markdown(html)
    assert md == "a\\*b"
    assert markdown_to_leankit_html(md) == html


@pytest.mark.parametrize(
    "html",
    [
        "<p>Hello <strong>world</strong></p>",
        "<p>Some <em>italic</em> text.</p>",
        "<p>Some <s>struck</s> text</p>",
        "<p>Inline <code>code()</code> span.</p>",
        '<p><a href="https://example.com">click</a></p>',
        "<p>Bold <strong>and <em>nested</em> italic</strong> together</p>",
    ],
)
def test_html_to_markdown_to_html_round_trip_is_the_identity_for_supported_vocabulary(html):
    assert markdown_to_leankit_html(leankit_html_to_markdown(html)) == html


def test_backslash_escaped_delimiter_inside_code_span_stays_literal_after_reinsertion():
    # A backslash preceding an ambiguous char INSIDE a code span was never produced by this
    # module's escaper for code content (code bypasses _escape_markdown_text on the HTML->MD
    # side), so it must survive the MD->HTML code-span path completely untouched -- no unescape
    # pass reaches inside a protected region.
    result = markdown_to_leankit_html("`a\\*b`")
    assert result == "<p><code>a\\*b</code></p>"


# --- invariant: bounded work (no catastrophic blowup on pathological delimiter runs) ------------

@pytest.mark.parametrize(
    "pathological_markdown",
    [
        "*" * 30_000,
        "[" * 20_000,
        "**" * 15_000,
        "~~" * 15_000,
        "*a" * 15_000,
    ],
)
def test_pathological_repeated_delimiters_complete_in_bounded_time(pathological_markdown):
    start = time.monotonic()
    result = markdown_to_leankit_html(pathological_markdown)
    elapsed = time.monotonic() - start
    assert isinstance(result, str)
    assert elapsed < 3.0


def test_deeply_nested_links_are_capped_by_max_inline_depth_without_recursion_error():
    depth = _MAX_INLINE_DEPTH + 10
    markdown = "text"
    for _ in range(depth):
        markdown = f"[{markdown}](https://example.com)"
    result = markdown_to_leankit_html(markdown)
    assert isinstance(result, str)
    # Depth is capped: strictly fewer <a> tags open than the requested nesting depth, and never
    # more than the cap itself.
    assert result.count("<a href") == _MAX_INLINE_DEPTH


# --- invariant: whitelist closure (HTML output only uses the supported subset) ------------------

@pytest.mark.parametrize(
    "markdown, leaked_tag_syntax",
    [
        ("**<script>evil()</script>**", "<script"),
        ("[click](javascript:alert(1))", "javascript:"),
        ("`<img src=x onerror=alert(1)>`", "<img"),
    ],
)
def test_inline_formatting_never_leaks_unsupported_tag_syntax(markdown, leaked_tag_syntax):
    result = markdown_to_leankit_html(markdown)
    assert leaked_tag_syntax not in result


# --- invariant: text-node safety (inline link text / attribute injection) -----------------------

def test_link_text_containing_raw_html_is_escaped_not_parsed_as_tags():
    result = markdown_to_leankit_html("[<script>alert(1)</script>](https://example.com)")
    assert "<script>" not in result
    assert "&lt;script&gt;" in result


def test_href_attribute_cannot_be_broken_out_of_by_a_quote_character():
    result = markdown_to_leankit_html('[x](https://example.com/" onmouseover="alert(1))')
    assert '" onmouseover="' not in result
    assert "&quot;" in result


def test_backslash_escaped_paren_in_href_unescapes_to_a_literal_paren():
    # _find_balanced_close treats a backslash-escaped ')' as non-structural specifically so a
    # literal paren can be embedded in an href without prematurely closing the link syntax -- the
    # extracted href must have that backslash stripped, not carry it into the emitted attribute.
    result = markdown_to_leankit_html("[text](http://example.com/a\\)b)")
    assert result == '<p><a href="http://example.com/a)b">text</a></p>'
    assert "\\" not in result


# --- inline placeholder reinsertion, exercised through the public boundary -------------------------

def test_inline_html_reinsert_never_raises_on_out_of_range_placeholder_pattern():
    # A literal NUL-byte-and-digits sequence in the source text happens to be shaped like this
    # module's internal code-span placeholder token. Reinsertion must degrade gracefully (leave it
    # untouched) rather than raising an IndexError -- totality is a hard invariant regardless of
    # how contrived the input. Exercised through the public markdown_to_leankit_html boundary
    # (rather than the private _render_inline_html helper directly) so this test survives a
    # refactor that inlines, renames, or reshapes that helper as long as the documented totality
    # guarantee holds.
    result = markdown_to_leankit_html("\x009\x00 no real code span here")
    assert isinstance(result, str)
