"""Bidirectional translator between GitHub-flavored Markdown and AgilePlace's HTML subset. No I/O --
pure string transforms, exhaustively unit-tested.

Supported subset (both directions)
-----------------------------------
| Markdown              | AgilePlace HTML          |
|------------------------|---------------------------|
| ``**bold**``           | ``<strong>``              |
| ``*italic*``           | ``<em>``                  |
| ``~~strike~~``         | ``<s>``                   |
| `` `code` ``           | ``<code>``                |
| fenced code block      | ``<pre><code>``           |
| ``# Heading`` .. ``######`` | ``<h1>`` .. ``<h6>`` |
| ``- item`` / ``1. item`` | ``<ul><li>`` / ``<ol><li>`` |
| ``[text](href)``       | ``<a href="...">``        |
| blank-line paragraphs  | ``<p>``                   |
| single newline         | ``<br>``                  |

Degradation table (asymmetries -- never a crash, never unsanitized passthrough)
--------------------------------------------------------------------------------
| Input                                          | Output                                        |
|--------------------------------------------------|-------------------------------------------------|
| ``<u>...</u>`` (no Markdown equivalent)          | inline content kept, tag silently dropped       |
| Any HTML tag outside the supported set           | inline content kept, tag silently dropped       |
| ``javascript:``/other disallowed href scheme     | link degrades to text, href omitted             |
| Unclosed ``<strong>``/``<em>``/``<s>``/``<code>`` | force-closed at end of output (stays balanced)  |
| Raw ``<tag>`` typed directly into Markdown source | escaped to literal text, never parsed as a tag  |
"""
from __future__ import annotations

# Characters that are ambiguous Markdown syntax in ANY position within a line -- always
# backslash-escaped wherever they appear in text content.
_INLINE_AMBIGUOUS_CHARS: frozenset[str] = frozenset({"*", "_", "~", "`", "[", "]", "\\"})

# Characters that only mean something to Markdown when they open a line (heading/list/image
# markers) -- backslash-escaped ONLY when at true line start, never mid-sentence.
_STRUCTURAL_LINE_START_CHARS: frozenset[str] = frozenset({"#", "-", "+", "!"})

# Union of everything _escape_markdown_text can ever precede with a backslash; the inverse strips
# exactly a backslash before one of these and nothing else.
_UNESCAPABLE_CHARS: frozenset[str] = _INLINE_AMBIGUOUS_CHARS | _STRUCTURAL_LINE_START_CHARS | {".", ">"}

# Href schemes considered safe to emit; anything else (javascript:, data:, bare relative paths,
# schemeless strings) degrades to link text with no href.
_ALLOWED_HREF_SCHEMES: frozenset[str] = frozenset({"http", "https", "mailto"})


def _escape_html_text(text: str) -> str:
    """Entity-escape &, <, >, and " so ``text`` is safe inside an HTML text node or a
    double-quoted attribute value. Order matters: '&' must be escaped first or the entities
    produced by the later replacements would themselves be re-escaped."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _digit_run_end(text: str, start: int) -> int:
    """Index just past the contiguous run of ASCII digits beginning at ``start`` (== start if
    text[start] isn't a digit)."""
    end = start
    while end < len(text) and text[end].isdigit():
        end += 1
    return end


def _leading_structural_escape(text: str, i: int) -> tuple[str, int] | None:
    """If ``text[i:]`` opens a line with a marker Markdown would reinterpret as structure --
    '>', a _STRUCTURAL_LINE_START_CHARS char, or an ordered-list digit-run followed by '.' --
    return (escaped_text, chars_consumed). Only valid to call when ``i`` is a true line start;
    returns None when nothing at ``i`` needs structural escaping."""
    ch = text[i]
    if ch == ">" or ch in _STRUCTURAL_LINE_START_CHARS:
        return f"\\{ch}", 1
    end = _digit_run_end(text, i)
    if end > i and end < len(text) and text[end] == ".":
        return f"{text[i:end]}\\.", end - i + 1
    return None


def _escape_markdown_text(text: str, at_line_start: bool) -> str:
    """Backslash-escape ``text`` so it round-trips as literal content rather than being
    reinterpreted as Markdown syntax on re-render. _INLINE_AMBIGUOUS_CHARS are escaped wherever
    they occur; structural line-start markers are escaped only where they truly open a line --
    at position 0 when ``at_line_start`` is set by the caller, and again after every '\\n'
    encountered while scanning ``text`` itself."""
    out: list[str] = []
    line_start = at_line_start
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\n":
            out.append(ch)
            line_start = True
            i += 1
            continue
        structural = _leading_structural_escape(text, i) if line_start else None
        if structural is not None:
            escaped, consumed = structural
            out.append(escaped)
            i += consumed
            line_start = False
            continue
        out.append(f"\\{ch}" if ch in _INLINE_AMBIGUOUS_CHARS else ch)
        line_start = False
        i += 1
    return "".join(out)


def _unescape_markdown_text(text: str) -> str:
    """Inverse of _escape_markdown_text: strip a backslash immediately preceding any character
    the escaper ever emits a backslash before (_UNESCAPABLE_CHARS), leaving that character
    literal. A backslash not followed by such a character, or at end of string, is left as-is --
    it was never something this module's escaper produced."""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n and text[i + 1] in _UNESCAPABLE_CHARS:
            out.append(text[i + 1])
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _sanitize_href(url: str | None) -> str | None:
    """Validate ``url`` as a safe href, or return None if it's missing/unsafe (caller then
    degrades to escaped link-text only, no href emitted). Strips ASCII whitespace and control
    characters (0x00-0x20) from anywhere in the string before the scheme check -- not just the
    ends -- closing the "java\\tscript:alert(1)" tab-obfuscation bypass. The scheme comparison
    against _ALLOWED_HREF_SCHEMES is lowercase-only; on success the trimmed original (not the
    control-char-purged copy) is returned."""
    if not url:
        return None
    trimmed = url.strip()
    if not trimmed:
        return None
    scheme_check_copy = "".join(ch for ch in trimmed if ord(ch) > 0x20)
    scheme, sep, _rest = scheme_check_copy.partition(":")
    if not sep or scheme.lower() not in _ALLOWED_HREF_SCHEMES:
        return None
    return trimmed
