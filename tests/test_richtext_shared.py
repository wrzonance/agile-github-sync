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
)
from _richtext_shared import (  # noqa: E402
    _INLINE_AMBIGUOUS_CHARS,
    _UNESCAPABLE_CHARS,
)


# --- invariant: '<'/'>' are inline-ambiguous, and thus transitively unescapable ----------------

@pytest.mark.parametrize("ch", ["<", ">"])
def test_angle_brackets_are_inline_ambiguous_chars(ch):
    # '<'/'>' must be escaped wherever they occur in text content (not just at line start) --
    # otherwise a literal "<b>" typed into Markdown source round-trips as a real HTML tag instead
    # of escaped text (see richtext.py's degradation table: "Raw <tag> ... escaped to literal
    # text, never parsed as a tag").
    assert ch in _INLINE_AMBIGUOUS_CHARS


@pytest.mark.parametrize("ch", ["<", ">"])
def test_angle_brackets_are_unescapable_chars_via_the_inline_ambiguous_union(ch):
    # _UNESCAPABLE_CHARS is the union the MD->HTML unescaper strips a backslash before; '<'/'>'
    # must be included here too or _escape_markdown_text's new escaping of them would never be
    # undone on the way back to HTML.
    assert ch in _UNESCAPABLE_CHARS


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
UNICODE_SAMPLE = "héllo wörld ☃ \U0001F600 \u200b\u200c\u200d"


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
