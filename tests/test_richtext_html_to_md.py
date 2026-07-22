"""Unit tests for richtext.py's HTML->Markdown direction (leankit_html_to_markdown): captured
devtools vocabulary (p, br, strong, em, u, code, pre>code), headings, nested lists, links, and
strikethrough. Pins content conservation, totality (never raises) over malformed/arbitrary HTML,
and whitelist closure (the Markdown output never leaks unsupported tag syntax). No I/O.

Run: pytest -q
"""
from __future__ import annotations

import string
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from richtext import leankit_html_to_markdown  # noqa: E402

ALL_PRINTABLE = string.printable
UNICODE_SAMPLE = "héllo wörld ☃ \U0001F600 ​‌‍"


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
        # An href with an unmatched literal '(' (no closing ')') must be escaped before splicing
        # into `](href)` -- otherwise MD->HTML's balanced-paren scan for the link's closing paren
        # never finds one and the link fails to parse back. Also covers a lone ')' and a literal
        # backslash, both of which would otherwise be misread (or swallowed) by MD->HTML's
        # backslash-escape-aware closing-paren scan.
        (
            '<a href="https://example.com/(unclosed">text</a>',
            "[text](https://example.com/\\(unclosed)",
        ),
        (
            '<a href="https://example.com/)stray">text</a>',
            "[text](https://example.com/\\)stray)",
        ),
        (
            '<a href="https://example.com/back\\slash">text</a>',
            "[text](https://example.com/back\\\\slash)",
        ),
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
        ("<ul><li>line one<br>line two</li></ul>", "- line one\nline two"),
        ("<ol><li>line one<br>line two</li><li>next item</li></ol>", "1. line one\nline two\n2. next item"),
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
