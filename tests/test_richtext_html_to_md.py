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

from richtext import leankit_html_to_markdown, markdown_to_leankit_html  # noqa: E402

ALL_PRINTABLE = string.printable
UNICODE_SAMPLE = "héllo wörld ☃ \U0001F600 \u200b\u200c\u200d"


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


# =====================================================================================
# leankit_html_to_markdown -- GFM code-span fencing (variable-length backtick delimiters)
#
# Prior behavior wrapped every <code> span in a single literal backtick, regardless of the
# span's own content -- content containing a backtick could never round-trip (the content's own
# backtick would merge with, or prematurely close against, the single-backtick fence). These pin
# the fix's invariants: the fence chosen is always longer than any backtick run inside the
# content, padding keeps a content-edge backtick or bounding whitespace from merging with the
# fence, and an unclosed <code> at EOF still flushes rather than dropping/raising.
# =====================================================================================

# --- invariant: content conservation -- <code>c</code> round-trips through HTML->MD->HTML for
# any c, except the documented empty-span degrade (see below) ------------------------------------

@pytest.mark.parametrize(
    "code_content",
    [
        "plain code",
        "has a ` single backtick",
        "`leading backtick",
        "trailing backtick`",
        " padded with spaces ",
    ],
)
def test_code_span_html_to_markdown_to_html_round_trip_reproduces_exact_content(code_content):
    html = f"<p><code>{code_content}</code></p>"
    md = leankit_html_to_markdown(html)
    assert markdown_to_leankit_html(md) == html


@pytest.mark.parametrize(
    "code_content",
    [
        "has `` a double backtick run",
        "has ``` a triple backtick run",
        "``surrounded``",
        "````````````long run of backticks alone````````````",
    ],
)
def test_code_span_needing_a_three_plus_backtick_fence_round_trips_when_not_at_true_line_start(code_content):
    # A fence of 3+ backticks landing as the very first characters of a line collides with
    # _richtext_md_to_html's line-level fenced-CODE-BLOCK detection (_CODE_FENCE_LINE_RE) -- an
    # unrelated layer this module's own test suite already documents as out of scope for
    # code-span matching (see test_richtext_md_to_html.py's "text " prefix convention on its own
    # 3-backtick-run case). A leading word keeps the code span from ever starting its line, which
    # is also how this module's supported vocabulary is actually used in practice -- inline code
    # embedded in surrounding prose, never a bare code span alone at the top of a document.
    html = f"<p>Inline <code>{code_content}</code> span.</p>"
    md = leankit_html_to_markdown(html)
    assert markdown_to_leankit_html(md) == html


def test_code_span_with_embedded_newline_is_a_known_out_of_scope_limitation():
    # A literal '\n' inside an inline (non-<pre>) <code> span is buffered and re-emitted verbatim
    # by _flush_code_span, but Markdown's own line-oriented block/inline layers reinterpret a bare
    # newline as a hard line break (<br>) on the way back to HTML -- pre-existing behavior, wholly
    # unrelated to and unchanged by this fencing/padding fix (reproduced identically against the
    # pre-fix single-backtick fence). Only <pre><code> is documented to preserve literal newlines
    # (see test_pre_code_block_preserves_literal_newlines_exactly); inline <code> never claimed to.
    html = "<p><code>a\nmultiline\ncode span</code></p>"
    md = leankit_html_to_markdown(html)
    assert markdown_to_leankit_html(md) != html


# --- invariant: the fence chosen never occurs inside the span's own content ----------------------

def test_code_span_fence_length_exceeds_the_longest_internal_backtick_run():
    # Content itself contains a run of 2 backticks -- the chosen fence must be at least 3
    # backticks long, so re-parsing it never mistakes part of the content for the closing fence.
    html = "<p><code>a``b</code></p>"
    assert leankit_html_to_markdown(html) == "```a``b```"


def test_code_span_fence_length_matches_the_documented_run_plus_one_rule():
    html = "<p><code>x```y</code></p>"
    assert leankit_html_to_markdown(html) == "````x```y````"


# --- invariant: padding is added whenever a content edge would otherwise merge with the fence ----

@pytest.mark.parametrize(
    "code_content, expected_markdown",
    [
        ("`leading", "`` `leading ``"),
        ("trailing`", "`` trailing` ``"),
        ("`both`", "`` `both` ``"),
    ],
)
def test_code_span_padding_added_when_content_starts_or_ends_with_a_backtick(code_content, expected_markdown):
    html = f"<p><code>{code_content}</code></p>"
    assert leankit_html_to_markdown(html) == expected_markdown


def test_code_span_padding_added_when_content_is_bounded_by_spaces_but_not_all_spaces():
    html = "<p><code> spaced </code></p>"
    assert leankit_html_to_markdown(html) == "`  spaced  `"


def test_code_span_no_padding_added_when_content_needs_none():
    html = "<p><code>plain</code></p>"
    assert leankit_html_to_markdown(html) == "`plain`"


def test_code_span_content_that_is_only_spaces_is_kept_intact_without_padding():
    # GFM's own strip rule only fires when content isn't *entirely* whitespace -- an all-space
    # span must not be padded (padding it would change what round-trips back out).
    html = "<p><code>   </code></p>"
    assert leankit_html_to_markdown(html) == "`   `"


# --- invariant: an empty <code></code> degrades to no Markdown output, not a dangling fence ------

def test_empty_code_span_degrades_to_no_markdown_output():
    html = "<p><code></code></p>"
    assert leankit_html_to_markdown(html) == ""


# --- invariant: neither direction ever raises for malformed/unclosed <code> input ----------------

def test_unclosed_code_span_flushes_its_buffered_content_at_eof():
    html = "<p><code>unclosed code"
    assert leankit_html_to_markdown(html) == "`unclosed code`"


def test_unclosed_code_span_with_internal_backtick_run_still_picks_a_safe_fence_at_eof():
    html = "<p><code>a``b unclosed"
    assert leankit_html_to_markdown(html) == "```a``b unclosed```"


@pytest.mark.parametrize(
    "html",
    [
        "<code><strong>x",
        "<code><em>x</code>y",
        "<code>a<code>b",
        "<pre><code>unclosed fenced code",
        "<code>" + "`" * 5_000,
    ],
)
def test_malformed_or_nested_unclosed_code_tags_never_raise(html):
    result = leankit_html_to_markdown(html)
    assert isinstance(result, str)


# --- invariant: a directly-nested <code> (malformed input -- Markdown has no nested code-span
# syntax) never drops text captured before it reopens; all captured text merges into the one
# enclosing span rather than the outer text being silently discarded --------------------------

def test_nested_code_tag_preserves_text_captured_before_it_reopens():
    html = "<p><code>outer<code>inner</code>tail</code></p>"
    assert leankit_html_to_markdown(html) == "`outerinnertail`"


def test_doubly_nested_unclosed_code_tag_preserves_all_captured_text_at_eof():
    html = "<code>a<code>b<code>c"
    assert leankit_html_to_markdown(html) == "`abc`"
