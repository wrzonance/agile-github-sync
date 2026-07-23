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
    _unescape_markdown_text,
    leankit_html_to_markdown,
    markdown_to_leankit_html,
)

ALL_PRINTABLE = string.printable
UNICODE_SAMPLE = "héllo wörld ☃ \U0001F600 \u200b\u200c\u200d"


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


def test_lazy_continuation_line_after_list_item_folds_into_the_same_item_as_a_br():
    # A physical line immediately following a list item, with no blank-line separator and no
    # list/heading/code-fence marker of its own, is a hard-line-break continuation of that same
    # item (mirroring how a bare '\n' inside a <p> already becomes '<br>') -- it must not detach
    # into a sibling paragraph, which would also prematurely close the open list.
    markdown = "- line one\nline two"
    result = markdown_to_leankit_html(markdown)
    assert result == "<ul><li>line one<br>line two</li></ul>"


def test_lazy_continuation_line_does_not_reset_ordered_list_numbering():
    # Regression pin: if the continuation line instead detached into its own paragraph, the list
    # would close early and the next real list item would start a brand new <ol> at 1.
    markdown = "1. line one\nline two\n2. next item"
    result = markdown_to_leankit_html(markdown)
    assert result == "<ol><li>line one<br>line two</li><li>next item</li></ol>"
    assert result.count("<ol>") == 1


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


# --- invariant: variable-length backtick-run code-span matching --------------------------------

def test_code_span_delimiter_is_a_backtick_run_not_a_single_backtick():
    # A double-backtick delimiter lets a single literal backtick appear inside the span's content
    # without prematurely closing it -- the fence chosen (2 backticks) never occurs inside the
    # span's own content (a lone backtick), which is exactly what makes it a valid fence.
    result = markdown_to_leankit_html("``a`b``")
    assert result == "<p><code>a`b</code></p>"


def test_code_span_run_length_must_match_exactly_to_close():
    # A shorter/longer backtick run encountered mid-scan (here a literal "``" pair) must not
    # prematurely close a longer opening run (here 3 backticks) -- only an exact-length-3 run
    # closes it. Confirms the fence chosen (3 backticks) never occurs inside the extracted content.
    # ("text " prefix keeps this a paragraph, not a fenced code BLOCK -- that's a line-level
    # pattern requiring the triple-backtick at the very start of the line, an unrelated layer.)
    result = markdown_to_leankit_html("text ```a `` b``` end")
    assert result == "<p>text <code>a `` b</code> end</p>"


@pytest.mark.parametrize(
    "markdown, expected_html",
    [
        # Content has an internal 2-backtick run, so the chosen fence is 3 backticks -- landing
        # as the very first characters of the (single-line) document/paragraph.
        ("```a``b```", "<p><code>a``b</code></p>"),
        # Same collision one level up: an internal 3-backtick run needs a 4-backtick fence.
        ("````x```y````", "<p><code>x```y</code></p>"),
    ],
)
def test_code_span_fence_at_true_line_start_is_not_misparsed_as_a_fenced_code_block(markdown, expected_html):
    # Regression pin for the verified live bug: _CODE_FENCE_LINE_RE's naive "line starts with a
    # run of 3+ backticks" check couldn't tell a code span's own self-contained opening+closing
    # fence pair apart from an actual block-level fence opener whenever that pair landed as the
    # first characters of a line. Misfiring there sent the rest of the (never-closed) "block"
    # through _scan_code_fence, which silently discarded the paragraph's real text and emitted an
    # essentially-empty ``<pre><code>\n</code></pre>`` instead. Per CommonMark, a genuine fence
    # opener's info string may never itself contain a backtick -- exactly the property that
    # distinguishes the two shapes.
    assert markdown_to_leankit_html(markdown) == expected_html


def test_longer_fence_opener_is_not_closed_by_a_shorter_backtick_run():
    # Per CommonMark, a closing fence must be at least as long as its opener -- a 4-backtick
    # fence commonly wraps code that itself contains a 3-backtick fence line, and that inner
    # line is content, not the closer.
    markdown = "````\ninner\n```\nstill code\n````\ntail"
    result = markdown_to_leankit_html(markdown)
    assert "inner\n```\nstill code" in result
    assert "<p>tail</p>" in result


def test_fence_closed_by_a_longer_backtick_run_still_closes():
    markdown = "```\ncode\n````\ntail"
    result = markdown_to_leankit_html(markdown)
    assert "<p>tail</p>" in result
    assert "code" in result


def test_code_span_strips_exactly_one_leading_and_trailing_space_of_padding():
    # Per GFM: when a span's content both begins and ends with a space (and isn't all spaces),
    # exactly one leading/trailing space is stripped -- this is what lets a code span's content
    # start or end with a backtick itself without merging into the delimiter run.
    result = markdown_to_leankit_html("`` `code` ``")
    assert result == "<p><code>`code`</code></p>"


def test_two_adjacent_double_backticks_form_one_span_not_two_spurious_empty_ones():
    # Regression pin for the verified live bug: fixed single-backtick matching treated the second
    # backtick of an opening "``" as if it could itself close a (now-empty) span, producing two
    # spurious empty code spans instead of one span containing "b". Run-length-aware matching
    # recognizes the opening run is 2 backticks wide and searches for the next *equal-length* run,
    # correctly landing on the closing "``" after "b".
    result = markdown_to_leankit_html("a ``b``")
    assert result == "<p>a <code>b</code></p>"


def test_unmatched_backtick_run_is_emitted_literally_advancing_past_the_whole_run():
    # No closing run of length 2 exists anywhere in the remaining text -- the entire opening run
    # must be emitted as literal text (not just its first character), and scanning must continue
    # past it (not raise, not loop) rather than re-splitting it into single backticks.
    result = markdown_to_leankit_html("text `` unmatched")
    assert result == "<p>text `` unmatched</p>"


def test_backslash_before_a_closing_backtick_run_does_not_prevent_it_from_closing():
    # A backslash has no special meaning before a code-span delimiter per GFM -- unlike every other
    # delimiter in this module (bold/italic/strike/links), which _is_backslash_escaped exempts, a
    # backslash immediately preceding a code span's opening or closing backtick run must have zero
    # effect on span-boundary matching: the backtick right after it still closes the span, and the
    # backslash itself becomes part of the (literal, unsubstituted) span content.
    result = markdown_to_leankit_html("`x\\`y`")
    assert result == "<p><code>x\\</code>y`</p>"


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


# --- invariant: angle brackets in HTML text content round-trip without leaking a backslash -------

@pytest.mark.parametrize(
    "html",
    [
        "<p>a &lt; b &gt; c</p>",
        "<p>&lt;b&gt;Title&lt;/b&gt; is not a real tag</p>",
        "<p><strong>&lt;x&gt;</strong></p>",
        "<h1>&lt;h2&gt;</h1>",
    ],
)
def test_literal_angle_brackets_round_trip_through_html_md_html_without_leaking_a_backslash(html):
    # Regression pin for the verified pre-existing bug: the HTML->MD escaper backslash-escapes a
    # literal '<'/'>' in text content ("a \< b"), but MD->HTML's _escape_html_text runs BEFORE the
    # trailing unescape pass and turns that literal '<' into the four-char entity "&lt;" -- leaving
    # the escaper's backslash stranded in front of an entity, not a bare unescapable char. Without
    # an entity-aware branch, the trailing _unescape_markdown_text pass never recognizes
    # "\&lt;"/"\&gt;" and the stray backslash leaks straight into the final HTML.
    md = leankit_html_to_markdown(html)
    result = markdown_to_leankit_html(md)
    assert result == html
    assert "\\" not in result


def test_unescape_markdown_text_strips_a_backslash_before_an_angle_bracket_entity():
    # Direct unit pin on the new branch: a backslash immediately preceding the exact four-char
    # entity produced by _escape_html_text for '<'/'>' is stripped, leaving the entity intact --
    # never reinterpreted char-by-char (a naive single-char strip would corrupt "&lt;" into
    # "lt;" by consuming its leading '&').
    assert _unescape_markdown_text("a\\&lt;b") == "a&lt;b"
    assert _unescape_markdown_text("a\\&gt;b") == "a&gt;b"


def test_unescape_markdown_text_still_strips_a_backslash_before_a_bare_angle_bracket():
    # The pre-existing single-char branch (via _UNESCAPABLE_CHARS) must still handle a bare '<'/'>'
    # backslash-escape exactly as before -- the new entity branch only adds a case, it never
    # replaces this one.
    assert _unescape_markdown_text("a\\<b") == "a<b"
    assert _unescape_markdown_text("a\\>b") == "a>b"


def test_unescape_markdown_text_does_not_treat_a_partial_entity_as_the_full_four_char_match():
    # "&lx;" is not "&lt;" -- the new branch must require the exact four-char entity, not just a
    # leading '&'. Neither branch matches here ('&' is not in _UNESCAPABLE_CHARS either), so the
    # backslash is left untouched -- it was never something the escaper produced.
    assert _unescape_markdown_text("a\\&lx;b") == "a\\&lx;b"
    assert _unescape_markdown_text("a\\&b") == "a\\&b"


def test_href_with_backslash_before_a_raw_angle_bracket_is_unaffected_by_the_entity_branch():
    # hrefs are extracted and unescaped by their own href-specific pass (_unescape_href_text,
    # inside _try_parse_link) before _protect_href_from_unescape re-doubles any surviving
    # backslash for the trailing global unescape pass -- so a href-embedded backslash never
    # reaches _unescape_markdown_text as a bare backslash in front of an entity. This pins that
    # the new entity-aware branch doesn't perturb that already-correct href path.
    result = markdown_to_leankit_html("[x](https://example.com/a\\<b)")
    assert result == '<p><a href="https://example.com/a&lt;b">x</a></p>'
    assert "\\" not in result


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
        # Many unmatched backtick runs of distinct lengths: each opener's closing-run search
        # must be window-bounded or this input is quadratic in the document size.
        " ".join("`" * k for k in range(1, 530)),
    ],
    # Short ids: the raw payloads are up to 30k chars and would overflow Windows'
    # 32,767-char PYTEST_CURRENT_TEST env var (issue #90).
    ids=["stars-30k", "brackets-20k", "double-stars-15k", "double-tildes-15k", "star-a-15k",
         "backtick-runs-1-530"],
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


@pytest.mark.parametrize("markdown_char", [".", "#"])
def test_href_backslash_before_an_unescapable_char_survives_the_trailing_unescape_pass(markdown_char):
    # The trailing _unescape_markdown_text pass in _render_inline_html strips a backslash before
    # any _UNESCAPABLE_CHARS character. A finalized href containing a literal backslash that
    # happens to precede such a character (the MD source carries it as '\\\\', the href escaper's
    # doubled form) must reach the emitted attribute intact -- not lose its backslash to a pass
    # meant only for text content.
    result = markdown_to_leankit_html(f"[x](https://example.com/a\\\\{markdown_char}b)")
    assert result == f'<p><a href="https://example.com/a\\{markdown_char}b">x</a></p>'


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


def test_inline_html_reinsert_never_raises_on_a_pathologically_long_placeholder_digit_run():
    # A NUL-delimited digit run longer than CPython's int-string-conversion limit (4300 digits by
    # default, 3.11+) would make an unguarded int() of the captured digits raise ValueError. Such a
    # run can only come from adversarial input -- this module's own placeholder indices are tiny --
    # so it must degrade to untouched text, never raise: totality is a hard invariant regardless of
    # how contrived the input.
    result = markdown_to_leankit_html("\x00" + ("9" * 5000) + "\x00")
    assert isinstance(result, str)
