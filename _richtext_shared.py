"""Pure helpers and immutable data shapes shared by both richtext.py translation directions
(HTML->Markdown and Markdown->HTML). Nothing here is direction-specific -- every name is either a
structural NamedTuple both directions fold list/block state against, or a sanitizer both directions
call at the exact same trust boundary (an href pulled from HTML or from Markdown link syntax). No
I/O; never raises.
"""
from __future__ import annotations

from typing import NamedTuple


class _ListFrame(NamedTuple):
    """One active list-nesting level while walking (HTML->MD) or folding (MD->HTML) list
    structure. Immutable -- advancing an ordered list's counter replaces the stack's top entry
    with a new frame rather than mutating one in place."""

    ordered: bool
    index: int


class _Block(NamedTuple):
    """One line-oriented Markdown block, pre-inline-rendering. ``level`` means heading depth
    (1-6) for kind=='heading' and list-nesting depth (1-based) for kind=='list_item'; it is 0 and
    unused for 'paragraph'/'code_block'/'blank'. ``ordered`` is only meaningful for 'list_item'.
    ``text`` is the raw block content -- HTML-escaped (and inline-rendered) by the block renderer,
    never by the parser."""

    kind: str
    level: int
    ordered: bool
    text: str


# Characters that are ambiguous Markdown syntax in ANY position within a line -- always
# backslash-escaped wherever they appear in text content. Consumed by the HTML->MD escaper.
# '<'/'>' are included so a literal angle bracket typed into source text (e.g. "a < b") can never
# be reinterpreted as HTML tag-soup once round-tripped back through MD->HTML -- see richtext.py's
# degradation table entry for raw "<tag>" text.
_INLINE_AMBIGUOUS_CHARS: frozenset[str] = frozenset({"*", "_", "~", "`", "[", "]", "\\", "<", ">"})

# Characters that only mean something to Markdown when they open a line (heading/list/image
# markers) -- backslash-escaped ONLY when at true line start, never mid-sentence. Consumed by the
# HTML->MD escaper.
_STRUCTURAL_LINE_START_CHARS: frozenset[str] = frozenset({"#", "-", "+", "!"})

# Union of everything the HTML->MD escaper can ever precede with a backslash; the MD->HTML
# unescaper's inverse strips exactly a backslash before one of these and nothing else. Kept here
# (not alongside either escaper) so the escape/unescape pair -- defined in different modules --
# can never drift out of sync with each other. '<' now reaches this union transitively through
# _INLINE_AMBIGUOUS_CHARS; '>' was already present via the trailing literal set below (kept as-is
# -- now redundant with _INLINE_AMBIGUOUS_CHARS, but harmless in a frozenset union).
_UNESCAPABLE_CHARS: frozenset[str] = _INLINE_AMBIGUOUS_CHARS | _STRUCTURAL_LINE_START_CHARS | {".", ">"}

# Href schemes considered safe to emit; anything else (javascript:, data:, bare relative paths,
# schemeless strings) degrades to link text with no href. Checked identically whether the href
# came from an HTML <a href> attribute or a Markdown ``[text](href)`` span.
_ALLOWED_HREF_SCHEMES: frozenset[str] = frozenset({"http", "https", "mailto"})

# Two spaces per nesting level, matching common Markdown renderers' expectation for a nested list
# item to be recognized as a child of the preceding item rather than a new top-level item. Used by
# the HTML->MD walker to emit indent and by MD->HTML's block parser to measure it back out.
_LIST_INDENT_UNIT = "  "

# Characters that must be backslash-escaped in an href before it is spliced into `](href)` link
# syntax. MD->HTML's _find_balanced_close scans forward from the opening '(' tracking nested
# '('/')' depth to find the link's closing paren, treating a backslash before ANY character as a
# non-structural escape pair (see _unescape_href_text) -- so a literal '(' or ')' in the href would
# otherwise shift or hide that closing paren, and a literal '\' would itself be swallowed as an
# escape marker. Scoped separately from _INLINE_AMBIGUOUS_CHARS/_STRUCTURAL_LINE_START_CHARS: an
# href is never treated as ordinary text, and _unescape_href_text's inverse strips a backslash
# before ANY character, not just these -- so escaping exactly this set is both necessary and
# sufficient for a href to round-trip through Markdown link syntax.
_HREF_ESCAPE_CHARS: frozenset[str] = frozenset({"\\", "(", ")"})


def _escape_href_for_markdown(href: str) -> str:
    """Backslash-escape '\\', '(', and ')' in ``href`` so splicing it into ``](href)`` can't have
    a literal paren misread as the link's closing paren (or a literal backslash swallowed as an
    escape marker) by MD->HTML's _find_balanced_close / _unescape_href_text pairing."""
    return "".join(f"\\{ch}" if ch in _HREF_ESCAPE_CHARS else ch for ch in href)


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
