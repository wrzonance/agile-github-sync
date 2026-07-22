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
