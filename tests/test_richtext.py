"""Unit tests for richtext.py's module scaffold: the shared pure helpers both translation
directions depend on -- HTML text-node escaping, Markdown escape/unescape (a true inverse pair),
and href sanitization. Pins three invariants at the boundary: text-node safety, escape/unescape
round-tripping, and totality (never raises) over arbitrary content. No I/O.

Run: pytest -q
"""
from __future__ import annotations

import string
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from richtext import (  # noqa: E402
    _escape_html_text,
    _escape_markdown_text,
    _sanitize_href,
    _unescape_markdown_text,
    leankit_html_to_markdown,
)


# --- invariant: text-node safety (HTML) -------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("&", "&amp;"),
        ("<", "&lt;"),
        (">", "&gt;"),
        ('"', "&quot;"),
        ("<script>alert(1)</script>", "&lt;script&gt;alert(1)&lt;/script&gt;"),
        ('"><img src=x onerror=alert(1)>', "&quot;&gt;&lt;img src=x onerror=alert(1)&gt;"),
    ],
)
def test_escape_html_text_neutralizes_every_structurally_dangerous_char(raw, expected):
    assert _escape_html_text(raw) == expected


def test_escape_html_text_does_not_double_escape_ampersand_in_produced_entities():
    # '&' must be escaped before '<'/'>'/'"' or the entities this function itself produces
    # (e.g. "&lt;") would have their '&' re-escaped into "&amp;lt;".
    assert _escape_html_text("<") == "&lt;"
    assert "&amp;lt;" not in _escape_html_text("<")


def test_escape_html_text_output_never_contains_a_bare_dangerous_char():
    raw = "plain <b>&\"quoted\"</b> text > here"
    escaped = _escape_html_text(raw)
    for dangerous in ("<", ">", '"'):
        assert dangerous not in escaped
    # '&' is allowed to remain, but only as the leading char of a produced entity.
    assert "&amp;" in escaped or "&lt;" in escaped or "&gt;" in escaped or "&quot;" in escaped


# --- invariant: escape/unescape are true inverses ---------------------------------------------

ROUND_TRIP_FIXTURES = [
    "plain text, nothing special",
    "*not bold* _not italic_ ~not strike~ `not code` [not a link] back\\slash",
    "# not a heading when mid-sentence like a#b",
    "- not a list item when mid-sentence like a-b",
    "+ not a list item when mid-sentence like a+b",
    "! not an image marker mid-sentence like wow!really",
    "costs $5.00 mid-sentence, not an ordered list",
    "> not a blockquote mid-sentence a>b",
    "multi\nline\ntext\nwith structural chars per line:\n# heading-ish\n- list-ish\n1. ordered-ish",
    "",
    "just a backslash \\ alone",
    "trailing digits 123 with no dot",
    "42. actually opens like an ordered list item",
]


@pytest.mark.parametrize("at_line_start", [True, False])
@pytest.mark.parametrize("text", ROUND_TRIP_FIXTURES)
def test_escape_then_unescape_markdown_text_is_the_identity(text, at_line_start):
    escaped = _escape_markdown_text(text, at_line_start=at_line_start)
    assert _unescape_markdown_text(escaped) == text


def test_leading_structural_chars_are_escaped_only_at_true_line_start():
    assert _escape_markdown_text("# heading", at_line_start=True) == "\\# heading"
    # Same text, not at a line start: '#' is inert mid-sentence and must NOT be escaped.
    assert _escape_markdown_text("# heading", at_line_start=False) == "# heading"


def test_leading_structural_chars_escaped_again_after_embedded_newline():
    text = "intro\n# heading-like"
    escaped = _escape_markdown_text(text, at_line_start=False)
    assert escaped == "intro\n\\# heading-like"


def test_inline_ambiguous_chars_are_escaped_regardless_of_position():
    text = "a*b*c _d_ e~f~g `h` [i] j\\k"
    escaped = _escape_markdown_text(text, at_line_start=False)
    for ch in ("*", "_", "~", "`", "[", "]", "\\"):
        assert f"\\{ch}" in escaped


def test_ordered_list_digit_run_dot_escaped_only_at_line_start():
    assert _escape_markdown_text("1. item", at_line_start=True) == "1\\. item"
    assert _escape_markdown_text("call 1. item", at_line_start=False) == "call 1. item"


def test_unescape_leaves_a_lone_backslash_not_followed_by_an_unescapable_char_untouched():
    # A backslash the escaper never produced (e.g. typed by a human as literal input elsewhere)
    # is not this function's concern to strip.
    assert _unescape_markdown_text("a\\z") == "a\\z"
    assert _unescape_markdown_text("trailing\\") == "trailing\\"


# --- invariant: totality over content (never raises) ------------------------------------------

ALL_PRINTABLE = string.printable
UNICODE_SAMPLE = "héllo wörld ☃ \U0001F600 ​‌‍"


@pytest.mark.parametrize(
    "content",
    [
        "",
        ALL_PRINTABLE,
        ALL_PRINTABLE * 50,
        UNICODE_SAMPLE,
        "\\" * 500,
        "#" * 500,
        ">" * 500,
        "\n".join(["1."] * 200),
    ],
)
def test_html_and_markdown_escapers_never_raise_over_arbitrary_content(content):
    html_escaped = _escape_html_text(content)
    assert isinstance(html_escaped, str)
    for at_line_start in (True, False):
        md_escaped = _escape_markdown_text(content, at_line_start=at_line_start)
        assert isinstance(md_escaped, str)
        round_tripped = _unescape_markdown_text(md_escaped)
        assert round_tripped == content


@pytest.mark.parametrize(
    "content",
    [None, "", "   ", "\t\n", ALL_PRINTABLE, UNICODE_SAMPLE, "http://" + "x" * 5000],
)
def test_sanitize_href_never_raises_over_arbitrary_content(content):
    result = _sanitize_href(content)
    assert result is None or isinstance(result, str)


# --- _sanitize_href: allowlisted schemes, obfuscation resistance -------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://example.com/path?q=1",
        "HTTPS://EXAMPLE.COM",  # scheme comparison is case-insensitive
        "mailto:someone@example.com",
        "  https://example.com  ",  # surrounding whitespace trimmed, not rejected
    ],
)
def test_sanitize_href_accepts_allowlisted_schemes(url):
    assert _sanitize_href(url) is not None


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "   ",
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "java\tscript:alert(1)",  # tab-obfuscated scheme bypass
        "java\nscript:alert(1)",  # newline-obfuscated scheme bypass
        "java script:alert(1)",  # space-obfuscated scheme bypass
        "relative/path",
        "no-scheme-at-all",
        "#anchor-only",
    ],
)
def test_sanitize_href_rejects_unsafe_or_missing_urls(url):
    assert _sanitize_href(url) is None


def test_sanitize_href_returns_trimmed_original_not_the_control_char_purged_copy():
    result = _sanitize_href("  https://example.com/a b  ")
    assert result == "https://example.com/a b"


# =====================================================================================
# leankit_html_to_markdown -- HTML->MD walker, captured devtools vocabulary:
# p, br, strong, em, u, code, pre>code
# =====================================================================================

# --- invariant: content conservation -----------------------------------------------------------

@pytest.mark.parametrize(
    "html, expected_markdown",
    [
        ("<p>Hello <strong>world</strong></p>", "Hello **world**"),
        ("<p>Some <em>italic</em> text.</p>", "Some *italic* text."),
        ("<p>Inline <code>code()</code> span.</p>", "Inline `code()` span."),
        ("<u>underlined</u> stays as plain text", "underlined stays as plain text"),
        ("<p>Line one<br>Line two</p>", "Line one\nLine two"),
        ("<p>First</p><p>Second</p>", "First\n\nSecond"),
        (
            "<p>Bold <strong>and <em>nested</em> italic</strong> together</p>",
            "Bold **and *nested* italic** together",
        ),
        ("plain text with no tags at all", "plain text with no tags at all"),
    ],
)
def test_content_is_conserved_across_supported_vocabulary(html, expected_markdown):
    assert leankit_html_to_markdown(html) == expected_markdown


def test_pre_code_block_preserves_literal_newlines_exactly():
    html = "<pre><code>def f():\n    return 1\n</code></pre>"
    assert leankit_html_to_markdown(html) == "```\ndef f():\n    return 1\n```"


def test_unsupported_tag_degrades_by_dropping_the_tag_but_keeping_its_content():
    html = '<div class="widget">payload text</div>'
    assert leankit_html_to_markdown(html) == "payload text"


def test_unclosed_strong_is_force_closed_rather_than_left_dangling():
    html = "<p>Unclosed <strong>bold"
    assert leankit_html_to_markdown(html) == "Unclosed **bold**"


# --- invariant: total over content (never raises) -----------------------------------------------

@pytest.mark.parametrize(
    "html",
    [
        "",
        "<p>Unclosed <strong>bold <em>and italic",
        "<strong><em><strong><em>deeply nested, never closed",
        "<script>alert(1)</script>survivor text",
        "<pre><code>unclosed code fence",
        "<u><u><u>triple nested u with no closers",
        "&amp;" * 10_000,
        ALL_PRINTABLE * 20,
        UNICODE_SAMPLE,
        "<notatag attr='x'>&&&<<<>>>",
    ],
)
def test_leankit_html_to_markdown_never_raises_over_arbitrary_or_malformed_html(html):
    result = leankit_html_to_markdown(html)
    assert isinstance(result, str)


@pytest.mark.parametrize("bad_input", [None, 123, 3.14, [], {}, b"bytes"])
def test_leankit_html_to_markdown_raises_typeerror_at_the_str_boundary(bad_input):
    with pytest.raises(TypeError, match="expected str, got"):
        leankit_html_to_markdown(bad_input)


# --- invariant: whitelist closure (Markdown output only uses the supported subset) --------------

@pytest.mark.parametrize(
    "html, leaked_tag_syntax",
    [
        ('<div class="x">payload</div>', "<div"),
        ("<script>evil()</script>harmless", "<script"),
        ("<iframe src=x></iframe>remainder", "<iframe"),
        ("<img src=x onerror=alert(1)>caption", "<img"),
        ("<style>body{color:red}</style>plain", "<style"),
    ],
)
def test_unsupported_tag_syntax_never_leaks_into_markdown_output(html, leaked_tag_syntax):
    result = leankit_html_to_markdown(html)
    assert leaked_tag_syntax not in result


@pytest.mark.parametrize(
    "html",
    [
        "<p>Unclosed <strong>bold</p>",
        "<p><em>Unclosed italic and text after</p>",
        "<p><code>unclosed inline code</p>",
    ],
)
def test_force_closed_format_markers_are_always_balanced(html):
    # A single mismatched tag type per case, with trailing text, so the appended closer never
    # sits directly adjacent to another marker -- isolates the balance guarantee from Markdown's
    # separate (and inherent, not this module's concern) delimiter-adjacency ambiguity when
    # several unclosed spans all flush back-to-back at end of input.
    result = leankit_html_to_markdown(html)
    for marker in ("**", "*", "`"):
        scan = result.replace("**", "") if marker == "*" else result
        assert scan.count(marker) % 2 == 0


def test_script_and_style_content_is_suppressed_not_leaked_as_text():
    html = "<p>before</p><script>var x = 1;</script><style>.a{}</style><p>after</p>"
    result = leankit_html_to_markdown(html)
    assert "var x = 1" not in result
    assert ".a{}" not in result
    assert "before" in result
    assert "after" in result


# =====================================================================================
# leankit_html_to_markdown -- headings, nested lists, links, strikethrough
# =====================================================================================

# --- invariant: content conservation -----------------------------------------------------------

@pytest.mark.parametrize(
    "html, expected_markdown",
    [
        ("<h1>Title</h1>", "# Title"),
        ("<h2>Sub</h2>", "## Sub"),
        ("<h3>SubSub</h3>", "### SubSub"),
        ("<h4>Four</h4>", "#### Four"),
        ("<h5>Five</h5>", "##### Five"),
        ("<h6>Six</h6>", "###### Six"),
        # Fixes the spike's observed heading-endtag run-on omission: a heading immediately
        # followed by text must not concatenate onto the same line.
        ("<h1>Heading</h1>This is a paragraph after.", "# Heading\n\nThis is a paragraph after."),
        ("<h2>Sub</h2><h3>SubSub</h3>", "## Sub\n\n### SubSub"),
        ("<p>Some <s>struck</s> text</p>", "Some ~~struck~~ text"),
        ("<del>deleted</del>", "~~deleted~~"),
        ("<strike>old</strike>", "~~old~~"),
        ('<a href="https://example.com">click</a>', "[click](https://example.com)"),
        ('<a href="javascript:alert(1)">bad</a>', "bad"),
        ("<a>no href</a>", "no href"),
        (
            '<a href="https://x.com"><strong>bold link</strong></a>',
            "[**bold link**](https://x.com)",
        ),
        ("<ul><li>one</li><li>two</li></ul>", "- one\n- two"),
        ("<ol><li>a</li><li>b</li></ol>", "1. a\n2. b"),
        (
            "<ul><li>parent<ul><li>child</li></ul></li></ul>",
            "- parent\n  - child",
        ),
        ("<ul><li>item</li></ul><p>after</p>", "- item\n\nafter"),
        ("<p>before</p><ul><li>item</li></ul>", "before\n\n- item"),
    ],
)
def test_content_is_conserved_across_headings_lists_links_strikethrough(html, expected_markdown):
    assert leankit_html_to_markdown(html) == expected_markdown


def test_unclosed_strike_is_force_closed_rather_than_left_dangling():
    html = "<p>Unclosed <s>struck"
    assert leankit_html_to_markdown(html) == "Unclosed ~~struck~~"


# --- invariant: total over content (never raises) -----------------------------------------------

@pytest.mark.parametrize(
    "html",
    [
        "<ul><li>unclosed list item and list",
        "<a href='javascript:evil()'>bad<a href='https://ok.com'>nested",
        "<h1><h2>nested headings, never closed",
        "<ol><li>one<ul><li>nested never closed",
        "<li>orphan li with no ul/ol wrapper</li>",
        "</a></ul></h1>stray closers with no matching opener",
    ],
)
def test_headings_lists_links_never_raise_over_malformed_html(html):
    result = leankit_html_to_markdown(html)
    assert isinstance(result, str)


# --- invariant: whitelist closure (Markdown output only uses the supported subset) --------------

def test_disallowed_href_scheme_never_leaks_into_markdown_output():
    html = '<a href="javascript:alert(1)">bad</a>'
    result = leankit_html_to_markdown(html)
    assert "javascript:" not in result


# =====================================================================================
# markdown_to_leankit_html -- MD->HTML block layer, plain-text inline content
# =====================================================================================

from richtext import (  # noqa: E402
    _Block,
    _ListFrame,
    _parse_blocks,
    _render_block_html,
    markdown_to_leankit_html,
)


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


def test_parse_blocks_does_not_mutate_or_depend_on_input_string_identity():
    markdown = "- one\n- two"
    blocks_first = _parse_blocks(markdown)
    blocks_second = _parse_blocks(markdown)
    assert blocks_first == blocks_second
    assert markdown == "- one\n- two"


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

import time  # noqa: E402

from richtext import _MAX_INLINE_DEPTH, _render_inline_html  # noqa: E402


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


# --- direct unit coverage of _render_inline_html --------------------------------------------------

def test_render_inline_html_reinsert_never_raises_on_out_of_range_placeholder_pattern():
    # A literal NUL-byte-and-digits sequence in the source text happens to be shaped like this
    # module's internal code-span placeholder token. Reinsertion must degrade gracefully (leave it
    # untouched) rather than raising an IndexError -- totality is a hard invariant regardless of
    # how contrived the input.
    result = _render_inline_html("\x009\x00 no real code span here")
    assert isinstance(result, str)


# =====================================================================================
# Round-trip fixed point -- combined document (heading + nested list + link + code +
# bold/italic/strike) in both directions. This is the spike's highest-risk item: individual
# vocabulary items round-trip in isolation, but nothing so far has pinned that they still do once
# combined into one realistic document, or that adjacent (not separated-by-text) nested inline
# spans don't collide into an ambiguous run of delimiter characters.
# =====================================================================================

_COMBINED_HTML_DOCUMENT = (
    "<h1>Title</h1>"
    "<p>Intro <strong>bold</strong> and <em>italic</em> and <s>strike</s> text with "
    '<code>code()</code> and a <a href="https://example.com">link</a>.</p>'
    "<ul><li>parent<ul><li>child <strong>bold child</strong></li></ul></li>"
    "<li>second top item</li></ul>"
    "<pre><code>def f():\n    return 1\n</code></pre>"
    "<p>Outro paragraph.</p>"
)


def test_combined_document_html_to_markdown_to_html_round_trip_is_the_identity():
    md = leankit_html_to_markdown(_COMBINED_HTML_DOCUMENT)
    assert markdown_to_leankit_html(md) == _COMBINED_HTML_DOCUMENT


def test_combined_document_markdown_round_trip_reaches_a_fixed_point_after_one_pass():
    # A second HTML->MD->HTML pass over the already-round-tripped Markdown must reproduce the
    # exact same Markdown -- the fixed point the spike's design doc calls out by name.
    md1 = leankit_html_to_markdown(_COMBINED_HTML_DOCUMENT)
    html2 = markdown_to_leankit_html(md1)
    md2 = leankit_html_to_markdown(html2)
    assert md1 == md2


@pytest.mark.parametrize(
    "html",
    [
        # strong directly wrapping em with zero separating text -- opening markers "**" + "*"
        # collapse into one run of three literal '*' characters ("***"), and closing markers
        # "*" + "**" collapse the same way. A naive nearest-delimiter-match parser mis-splits
        # this run and drops content or leaves a stray asterisk outside the tag.
        "<p><strong><em>bold italic</em></strong></p>",
        # three levels stacked directly adjacent: strong(em(s(text))).
        "<p><strong><em><s>all three</s></em></strong></p>",
        # the same triple nesting with a link immediately following, no separating text.
        (
            "<p><strong><em><s>all three</s></em></strong> and "
            '<a href="https://example.com"><strong><em>bold italic link</em></strong></a></p>'
        ),
    ],
)
def test_directly_adjacent_nested_inline_formats_round_trip_without_delimiter_collision(html):
    md = leankit_html_to_markdown(html)
    assert markdown_to_leankit_html(md) == html


@pytest.mark.parametrize(
    "markdown",
    [
        # irregular 3-space list continuation indent under a numbered marker, and headings glued
        # directly to the following block with no blank line -- both normalize on the first pass
        # (canonical 2-space indent, an inserted blank-line separator) rather than round-tripping
        # byte-for-byte, but the *normalized* form must itself be a genuine fixed point.
        "# Title\n\nIntro **bold** and *italic* and ~~strike~~ text with `code()` and a "
        "[link](https://example.com).\n\n- parent\n  - child **bold child**\n"
        "- second top item\n\n1. first\n2. second\n   - nested bullet inside ordered\n3. third"
        "\n\n```\ndef f():\n    return 1\n```\n\nOutro paragraph.",
        "## Sub\n### SubSub\n\n- a\n- b\n  1. nested ordered\n  2. two\n- c",
        "# H1\n## H2\n- item1\n- item2",
    ],
)
def test_markdown_authored_document_stabilizes_after_one_normalization_pass(markdown):
    html1 = markdown_to_leankit_html(markdown)
    md_normalized = leankit_html_to_markdown(html1)
    html2 = markdown_to_leankit_html(md_normalized)
    md_refixed = leankit_html_to_markdown(html2)
    assert md_normalized == md_refixed
    assert html1 == html2
