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
| Markdown delimiter with no matching close         | degrades to literal delimiter text, no tag      |
| Nesting past _MAX_INLINE_DEPTH levels deep         | remainder emitted as literal text, never raises |
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
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
    ``text`` is the raw block content -- HTML-escaped (and, in a later task, inline-rendered) by
    the block renderer, never by the parser."""

    kind: str
    level: int
    ordered: bool
    text: str


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

# ATX heading tags, GFM strikethrough tags (three HTML spellings, one Markdown marker), and the
# list-container tags -- broken out so both _SUPPORTED_TAGS and the walker's branch logic can
# test membership without repeating the tag names.
_HEADING_TAGS: frozenset[str] = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_STRIKE_TAGS: frozenset[str] = frozenset({"s", "del", "strike"})
_LIST_CONTAINER_TAGS: frozenset[str] = frozenset({"ul", "ol"})

# HTML tags the HTML->Markdown walker currently translates. Anything outside this set, including
# ``<u>`` (no Markdown equivalent), degrades via the unified no-op path: the tag is dropped but
# its text content is kept.
_SUPPORTED_TAGS: frozenset[str] = (
    frozenset({"p", "br", "strong", "em", "code", "pre", "a", "li"})
    | _HEADING_TAGS
    | _STRIKE_TAGS
    | _LIST_CONTAINER_TAGS
)

# Tags that open/close a symmetric Markdown inline-format span with a single marker string. All
# three strikethrough spellings collapse onto the one GFM marker.
_FORMAT_MARKERS: dict[str, str] = {
    "strong": "**",
    "em": "*",
    **{tag: "~~" for tag in _STRIKE_TAGS},
}

# Two spaces per nesting level, matching common Markdown renderers' expectation for a nested list
# item to be recognized as a child of the preceding item rather than a new top-level item.
_LIST_INDENT_UNIT = "  "

# Consecutive newlines beyond a single blank line collapse to the documented one-blank-line
# block-separator convention.
_BLANK_LINE_RUN = re.compile(r"\n{3,}")

# Line-oriented block patterns for the MD->HTML direction. Only the documented supported subset
# is recognized as structure; anything else falls through to plain paragraph text.
_HEADING_LINE_RE = re.compile(r"^(#{1,6})[ \t]+(.*)$")
_LIST_ITEM_LINE_RE = re.compile(r"^(?P<indent> *)(?:-|(?P<num>\d+)\.)[ \t]+(?P<content>.*)$")
_CODE_FENCE_LINE_RE = re.compile(r"^```")


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


class _MarkdownWalker(HTMLParser):
    """Streams AgilePlace HTML through stdlib's tolerant tokenizer and accumulates the
    equivalent Markdown. Tags outside ``_SUPPORTED_TAGS`` (including ``<u>``) take the unified
    degrade path: the tag itself is dropped but ``handle_data`` still fires, so their text
    content survives. ``<script>``/``<style>`` are the one exception -- their content is not
    meant to be read as prose, so it is suppressed entirely rather than degraded to text."""

    def __init__(self) -> None:
        # convert_charrefs=True decodes entities (e.g. &amp;) before handle_data sees them, so
        # this module never has to parse entities itself -- and rules out entity-bomb
        # amplification structurally, since decoding happens once during tokenization.
        super().__init__(convert_charrefs=True)
        self.buffer: list[str] = []
        self.format_stack: list[str] = []
        self.list_stack: list[_ListFrame] = []
        self.href_stack: list[str | None] = []
        self.in_code = False
        self.in_pre = False
        self.suppress_text = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "p":
            self._ensure_block_separator()
        elif tag == "br":
            self.buffer.append("\n")
        elif tag == "pre":
            self._ensure_block_separator()
            self.in_pre = True
            self.buffer.append("```\n")
        elif tag == "code":
            self._open_code()
        elif tag in _FORMAT_MARKERS:
            self._open_format(_FORMAT_MARKERS[tag])
        elif tag in _HEADING_TAGS:
            self._open_heading(tag)
        elif tag in _LIST_CONTAINER_TAGS:
            self._open_list(ordered=tag == "ol")
        elif tag == "li":
            self._open_list_item()
        elif tag == "a":
            self._open_link(attrs)
        elif tag in ("script", "style"):
            self.suppress_text = True
        # else: unsupported tag (including "u") -- unified degrade, no markdown emitted for the
        # tag itself; handle_data still runs so its content is kept.

    def handle_endtag(self, tag: str) -> None:
        if tag == "pre":
            self._close_pre()
        elif tag == "code":
            self._close_code()
        elif tag in _FORMAT_MARKERS:
            self._close_format(_FORMAT_MARKERS[tag])
        elif tag in _HEADING_TAGS:
            self._close_heading()
        elif tag in _LIST_CONTAINER_TAGS:
            self._close_list()
        elif tag == "a":
            self._close_link()
        elif tag in ("script", "style"):
            self.suppress_text = False
        # else: "p"/"br"/"li"/degraded tags -- nothing to close.

    def handle_data(self, data: str) -> None:
        # handle_data can fire multiple times for one logical run of text -- always append,
        # never overwrite the buffer.
        if self.suppress_text or not data:
            return
        if self.in_pre or self.in_code:
            # Code content is literal Markdown (backtick/fence spans don't reinterpret syntax
            # inside them), so it bypasses _escape_markdown_text entirely.
            self.buffer.append(data)
            return
        self.buffer.append(_escape_markdown_text(data, at_line_start=self._at_line_start()))

    def get_markdown(self) -> str:
        """Flush any still-open inline-format markers (an unclosed ``<strong>`` degrades to a
        balanced ``**word**`` instead of a dangling ``**word``), join the buffer, and collapse
        blank-line runs to the documented single-blank-line block-separator convention."""
        while self.format_stack:
            self.buffer.append(self.format_stack.pop())
        text = _BLANK_LINE_RUN.sub("\n\n", "".join(self.buffer))
        return text.strip("\n")

    def _open_format(self, marker: str) -> None:
        self.buffer.append(marker)
        self.format_stack.append(marker)

    def _close_format(self, marker: str) -> None:
        if self.format_stack and self.format_stack[-1] == marker:
            self.format_stack.pop()
        self.buffer.append(marker)

    def _open_code(self) -> None:
        self.in_code = True
        if not self.in_pre:
            self._open_format("`")

    def _close_code(self) -> None:
        self.in_code = False
        if not self.in_pre:
            self._close_format("`")
        # else: still inside <pre> -- the fence itself closes on </pre>, not here.

    def _close_pre(self) -> None:
        if self.buffer and not self.buffer[-1].endswith("\n"):
            self.buffer.append("\n")
        self.buffer.append("```")
        self.in_pre = False
        self.in_code = False

    def _ensure_block_separator(self) -> None:
        if self.buffer and not self.buffer[-1].endswith("\n\n"):
            self.buffer.append("\n\n")

    def _ensure_line_start(self) -> None:
        if self.buffer and not self.buffer[-1].endswith("\n"):
            self.buffer.append("\n")

    def _at_line_start(self) -> bool:
        return not self.buffer or self.buffer[-1].endswith("\n")

    def _open_heading(self, tag: str) -> None:
        self._ensure_block_separator()
        level = int(tag[1])
        self.buffer.append("#" * level + " ")

    def _close_heading(self) -> None:
        # Explicit blank-line separator here (rather than relying on the next block to supply
        # one) is what fixes the run-on omission: without it, text immediately following the
        # heading in the source (no intervening <p>) would land on the same Markdown line.
        self.buffer.append("\n\n")

    def _open_list(self, *, ordered: bool) -> None:
        if self.list_stack:
            self._ensure_line_start()
        else:
            self._ensure_block_separator()
        self.list_stack.append(_ListFrame(ordered=ordered, index=0))

    def _close_list(self) -> None:
        if self.list_stack:
            self.list_stack.pop()
        if not self.list_stack:
            self.buffer.append("\n\n")

    def _open_list_item(self) -> None:
        self._ensure_line_start()
        if not self.list_stack:
            # Malformed input: a bare <li> with no enclosing <ul>/<ol>. Degrade to a top-level
            # bullet rather than raising or dropping the content.
            self.list_stack.append(_ListFrame(ordered=False, index=0))
        frame = self.list_stack[-1]
        marker = f"{frame.index + 1}. " if frame.ordered else "- "
        self.list_stack[-1] = frame._replace(index=frame.index + 1)
        indent = _LIST_INDENT_UNIT * (len(self.list_stack) - 1)
        self.buffer.append(f"{indent}{marker}")

    def _open_link(self, attrs: list[tuple[str, str | None]]) -> None:
        href = _sanitize_href(dict(attrs).get("href"))
        self.href_stack.append(href)
        if href:
            self.buffer.append("[")
        # else: degraded -- no "[" marker emitted, so the following text/inline content renders
        # as plain text with no dangling bracket.

    def _close_link(self) -> None:
        href = self.href_stack.pop() if self.href_stack else None
        if href:
            self.buffer.append(f"]({href})")
        # else: degraded open (or a stray, unmatched close tag) -- nothing to emit.


def leankit_html_to_markdown(html: str) -> str:
    """Translate AgilePlace's HTML subset into GitHub-flavored Markdown. Raises TypeError if
    ``html`` isn't a str; otherwise never raises -- HTMLParser tolerates malformed/unclosed
    markup, and any tag outside the supported set degrades to its text content rather than
    crashing or leaking raw tag syntax into the output."""
    if not isinstance(html, str):
        raise TypeError(f"leankit_html_to_markdown: expected str, got {type(html).__name__}")
    walker = _MarkdownWalker()
    walker.feed(html)
    walker.close()
    return walker.get_markdown()


# =====================================================================================
# MD->HTML direction
# =====================================================================================


def _flush_paragraph(lines: list[str], blocks: list[_Block]) -> list[str]:
    """Return a new (empty) paragraph-line buffer, appending the accumulated lines as one
    _Block to ``blocks`` first if there were any. ``blocks`` is appended to in place (it is the
    caller's own accumulator, not a shared/aliased input); ``lines`` itself is never mutated --
    the caller always receives a fresh list back."""
    if lines:
        blocks.append(_Block(kind="paragraph", level=0, ordered=False, text="\n".join(lines)))
    return []


def _parse_blocks(md: str) -> list[_Block]:
    """Line-oriented scan of ``md`` into _Block values: fenced code, ATX headings (1-6 '#'),
    '-'/'N.' list items (nesting depth from 2-space indent units), blank-line separators, and
    paragraphs (consecutive otherwise-unrecognized lines, newline-joined). Never raises -- any
    line that doesn't match a structural pattern falls through to paragraph text."""
    lines = md.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[_Block] = []
    paragraph_lines: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if _CODE_FENCE_LINE_RE.match(line.lstrip()):
            paragraph_lines = _flush_paragraph(paragraph_lines, blocks)
            i, code_text = _scan_code_fence(lines, i)
            blocks.append(_Block(kind="code_block", level=0, ordered=False, text=code_text))
            continue
        heading_match = _HEADING_LINE_RE.match(line)
        if heading_match:
            paragraph_lines = _flush_paragraph(paragraph_lines, blocks)
            hashes, text = heading_match.groups()
            blocks.append(_Block(kind="heading", level=len(hashes), ordered=False, text=text.strip()))
            i += 1
            continue
        list_match = _LIST_ITEM_LINE_RE.match(line)
        if list_match:
            paragraph_lines = _flush_paragraph(paragraph_lines, blocks)
            level = len(list_match.group("indent")) // len(_LIST_INDENT_UNIT) + 1
            ordered = list_match.group("num") is not None
            blocks.append(_Block(kind="list_item", level=level, ordered=ordered, text=list_match.group("content")))
            i += 1
            continue
        if line.strip() == "":
            paragraph_lines = _flush_paragraph(paragraph_lines, blocks)
            blocks.append(_Block(kind="blank", level=0, ordered=False, text=""))
            i += 1
            continue
        paragraph_lines = paragraph_lines + [line]
        i += 1
    _flush_paragraph(paragraph_lines, blocks)
    return blocks


def _scan_code_fence(lines: list[str], start: int) -> tuple[int, str]:
    """Collect the literal lines of a fenced code block opening at ``lines[start]``. Returns the
    index just past the closing fence (or past EOF if the fence is never closed -- tolerated, not
    an error) and the joined code text."""
    i = start + 1
    n = len(lines)
    code_lines: list[str] = []
    while i < n and not _CODE_FENCE_LINE_RE.match(lines[i].lstrip()):
        code_lines.append(lines[i])
        i += 1
    return i + 1, "\n".join(code_lines)


# Bold/italic/strikethrough markers tried in this fixed precedence order at each inline scan
# position -- "**" is checked before "*" so a bold span is never mis-split into two italic opens.
_INLINE_FORMAT_DELIMITERS: tuple[tuple[str, str], ...] = (
    ("**", "strong"),
    ("*", "em"),
    ("~~", "s"),
)

# Caps how many levels of nested inline-format spans (bold-containing-italic-containing-strike, a
# link containing more links, etc.) _parse_inline_span will open before giving up and treating
# every remaining character as literal text -- bounds recursion depth against pathologically deep
# nested-delimiter input rather than raising RecursionError or doing unbounded work.
_MAX_INLINE_DEPTH = 20

# Caps how far _find_closing_delimiter scans looking for a matching close marker. Without this, a
# pathological input with many unmatched open delimiters (e.g. "[" * 20_000 with no "]" anywhere)
# would force a scan of the entire remaining text at every one of n positions -- O(n^2) total work
# even though each individual scan is fast. Bounding the window keeps total work linear regardless
# of how the input is shaped; every legitimate span in this module's supported vocabulary (short
# prose fragments) is far shorter than this.
_MAX_DELIMITER_SCAN = 2000

# Bracket/paren balance-matching (_find_balanced_close, used for link syntax) walks every char of
# its scan window in Python rather than via C-accelerated str.find, since it must track nesting
# depth. A pathological run of unmatched openers (e.g. "[" * 20_000) still costs O(window) per
# position -- kept safely linear-in-practice with a tighter window than _MAX_DELIMITER_SCAN, since
# link text in this module's supported vocabulary is always a short phrase, never a long span.
_MAX_LINK_SCAN = 500

# Positional placeholder _extract_code_spans swaps in for protected code-span content. NUL is not
# a character this module's HTML/Markdown escaping ever produces, so a placeholder-shaped
# substring in the rendered HTML can only originate from a NUL byte already present in the
# *original* input, never from this module's own output.
_CODE_SPAN_PLACEHOLDER_RE = re.compile(r"\x00(\d+)\x00")


def _is_backslash_escaped(text: str, index: int) -> bool:
    """True if ``text[index]`` is immediately preceded by an odd number of backslashes -- i.e. it
    is escaped (a literal char), not a live delimiter. An even count (including zero) means the
    backslashes themselves are escaped pairs and ``text[index]`` is unescaped."""
    count = 0
    i = index - 1
    while i >= 0 and text[i] == "\\":
        count += 1
        i -= 1
    return count % 2 == 1


def _find_closing_delimiter(text: str, start: int, delimiter: str) -> int:
    """Index of the next unescaped occurrence of ``delimiter`` at or after ``start``, or -1 if
    none exists within the bounded scan window (_MAX_DELIMITER_SCAN chars past ``start``) or
    before the end of ``text``. Uses str.find (C-speed substring search) to jump between
    candidate occurrences rather than scanning character-by-character in Python -- what keeps a
    pathological run of unmatched delimiters (which never finds a match at all) from costing
    Python-level work proportional to the scan window at every position."""
    limit = min(len(text), start + _MAX_DELIMITER_SCAN)
    search_from = start
    while search_from < limit:
        candidate = text.find(delimiter, search_from, limit)
        if candidate == -1:
            return -1
        if not _is_backslash_escaped(text, candidate):
            return candidate
        search_from = candidate + 1
    return -1


def _extract_code_spans(text: str) -> tuple[str, list[str]]:
    """Replace each backtick-delimited code span in already-HTML-escaped ``text`` with a
    positional placeholder, returning (placeholder_text, protected_contents) so the span's content
    bypasses both delimiter substitution and the closing unescape pass -- mirroring in_code/in_pre's
    literal handling on the HTML->MD side. A backslash-escaped backtick (``\\```) is not treated as
    a delimiter, matching _find_closing_delimiter's escape-aware scanning used everywhere else in
    this module."""
    out: list[str] = []
    protected: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "\\" and i + 1 < n:
            out.append(text[i:i + 2])
            i += 2
            continue
        if text[i] == "`":
            close = _find_closing_delimiter(text, i + 1, "`")
            if close != -1:
                protected.append(text[i + 1:close])
                out.append(f"\x00{len(protected) - 1}\x00")
                i = close + 1
                continue
        out.append(text[i])
        i += 1
    return "".join(out), protected


def _reinsert_code_spans(html: str, protected: list[str]) -> str:
    """Splice protected code-span content back into ``html`` at each positional placeholder,
    verbatim (already HTML-escaped, never re-passed through the unescape pass). A placeholder-
    shaped substring that doesn't correspond to a real protected span -- possible only if the
    original input happened to contain a literal NUL byte shaped just like this module's internal
    marker -- is left untouched rather than raising: totality over arbitrary content is a hard
    invariant of this module's public functions."""
    def _replace(match: re.Match[str]) -> str:
        index = int(match.group(1))
        if 0 <= index < len(protected):
            return f"<code>{protected[index]}</code>"
        return match.group(0)

    return _CODE_SPAN_PLACEHOLDER_RE.sub(_replace, html)


def _find_balanced_close(text: str, start: int, open_char: str, close_char: str) -> int:
    """Index of the char in ``text[start:]`` that balances a single already-consumed
    ``open_char`` (the one immediately before ``start``), tracking nested open/close pairs so
    ``[a[b]c](url)`` finds the outer ``]`` rather than the inner one -- unlike
    _find_closing_delimiter's nearest-occurrence search, which is only correct for symmetric
    markers (the same string opens and closes, e.g. ``**``) and would wrongly pair an inner
    link's close with an outer link's open. Backslash-escaped chars don't count toward the
    balance. Bounded by _MAX_LINK_SCAN so a pathological run of unmatched openers costs linear,
    not quadratic, work."""
    depth = 1
    limit = min(len(text), start + _MAX_LINK_SCAN)
    i = start
    while i < limit:
        if text[i] == "\\" and i + 1 < limit:
            i += 2
            continue
        if text[i] == open_char:
            depth += 1
        elif text[i] == close_char:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _try_parse_link(text: str, pos: int, depth: int) -> tuple[str, int] | None:
    """If ``text[pos]`` opens a well-formed ``[text](href)`` link, render it -- recursing into the
    link text at ``depth + 1`` so nested formatting inside link text still works -- and return
    (html, position_after_the_closing_paren). Returns None when it isn't a link, so the caller
    falls through to treating ``[`` as a literal character."""
    close_bracket = _find_balanced_close(text, pos + 1, "[", "]")
    if close_bracket == -1:
        return None
    if close_bracket + 1 >= len(text) or text[close_bracket + 1] != "(":
        return None
    close_paren = _find_balanced_close(text, close_bracket + 2, "(", ")")
    if close_paren == -1:
        return None
    link_text = text[pos + 1:close_bracket]
    href = _sanitize_href(text[close_bracket + 2:close_paren])
    inner_html = _render_inline_run(link_text, depth + 1)
    if href is None:
        return inner_html, close_paren + 1
    return f'<a href="{href}">{inner_html}</a>', close_paren + 1


def _try_parse_delimited_span(
    text: str, pos: int, depth: int, delimiter: str, tag: str
) -> tuple[str, int] | None:
    """If ``text[pos:]`` opens with ``delimiter``, find its matching close and render the content
    between them (recursing at ``depth + 1``) wrapped in ``<tag>``. Returns None -- so the caller
    falls through to literal-character handling -- when there's no close, or the span would be
    empty (adjacent delimiters with nothing between them, e.g. ``****``)."""
    dlen = len(delimiter)
    if text[pos:pos + dlen] != delimiter:
        return None
    close = _find_closing_delimiter(text, pos + dlen, delimiter)
    if close == -1 or close == pos + dlen:
        return None
    inner_html = _render_inline_run(text[pos + dlen:close], depth + 1)
    return f"<{tag}>{inner_html}</{tag}>", close + dlen


def _parse_inline_span(text: str, pos: int, depth: int) -> tuple[str, int]:
    """Render exactly one token of ``text[pos:]`` -- a link, a nested format span, a backslash-
    escaped literal pair, or a single literal char -- in fixed precedence order (link, then
    **bold**, *italic*, ~~strike~~) and return (html_fragment, position_after_the_token). A
    backslash-escaped pair is emitted verbatim (untouched) rather than treated as a delimiter --
    the trailing _unescape_markdown_text pass in _render_inline_html is what later strips the
    backslash. Once ``depth`` reaches _MAX_INLINE_DEPTH, no further spans are opened and every
    remaining character is emitted literally one at a time; this is the recursion-depth bound
    against pathologically deep nested-delimiter input."""
    if pos >= len(text):
        return "", pos
    ch = text[pos]
    if ch == "\\" and pos + 1 < len(text):
        return text[pos:pos + 2], pos + 2
    if depth < _MAX_INLINE_DEPTH:
        if ch == "[":
            link = _try_parse_link(text, pos, depth)
            if link is not None:
                return link
        for delimiter, tag in _INLINE_FORMAT_DELIMITERS:
            span = _try_parse_delimited_span(text, pos, depth, delimiter, tag)
            if span is not None:
                return span
    return ch, pos + 1


def _render_inline_run(text: str, depth: int) -> str:
    """Render the whole of ``text`` -- a block's full content, or one delimiter span's inner
    slice -- by repeatedly invoking _parse_inline_span until it's consumed, concatenating each
    fragment. Passing each recursive call a fresh substring (rather than an (outer_text, end_bound)
    pair into the original string) is what keeps a span's inner search from ever running past its
    own closing delimiter into the surrounding text."""
    out: list[str] = []
    pos = 0
    n = len(text)
    while pos < n:
        fragment, pos = _parse_inline_span(text, pos, depth)
        out.append(fragment)
    return "".join(out)


def _render_inline_html(text: str) -> str:
    """Render one block's raw Markdown text to its AgilePlace HTML subset. HTML-escape first (so
    raw '<'/'>'/'&'/'"' typed directly into the Markdown source is inert before any tag-producing
    substitution runs, and any href later pulled out of a ``(...)`` is already attribute-safe),
    protect code spans (their content stays literal -- it bypasses both delimiter substitution and
    the closing unescape pass), apply link/bold/italic/strike substitution via the recursive-
    descent _parse_inline_span, unescape backslash-escaped Markdown syntax chars back to their
    literal form (the true inverse of _escape_markdown_text applied on the HTML->MD side), and
    finally splice the protected code-span content back in verbatim."""
    escaped = _escape_html_text(text)
    protected_text, code_spans = _extract_code_spans(escaped)
    rendered = _render_inline_run(protected_text, depth=0)
    unescaped = _unescape_markdown_text(rendered)
    return _reinsert_code_spans(unescaped, code_spans)


def _open_list_container_html(ordered: bool) -> str:
    return "<ol>" if ordered else "<ul>"


def _close_list_frame_html(frame: _ListFrame) -> str:
    """Close one nesting level's still-open <li> and its container -- every frame on a
    list_stack always represents a currently-open <li>, so closing a frame always closes both."""
    return "</li>" + ("</ol>" if frame.ordered else "</ul>")


def _close_open_lists_html(list_stack: list[_ListFrame]) -> str:
    return "".join(_close_list_frame_html(frame) for frame in reversed(list_stack))


def _render_list_item(block: _Block, list_stack: list[_ListFrame]) -> tuple[str, list[_ListFrame]]:
    """Fold one list_item block against the TOP of the incoming ``list_stack`` (never mutated --
    a local copy is built and returned) to decide: close deeper levels no longer present, close
    and reopen the container when the list type changes at the same depth, or open new deeper
    levels while leaving the parent <li> open so the child list nests inside it. The rendered
    <li> is deliberately left unclosed here; it closes when its sibling/parent eventually does,
    which is what makes real HTML list nesting (not per-item isolation) work."""
    html_parts: list[str] = []
    stack = list(list_stack)

    while len(stack) > block.level:
        html_parts.append(_close_list_frame_html(stack[-1]))
        stack = stack[:-1]

    if len(stack) == block.level and stack:
        top = stack[-1]
        html_parts.append("</li>")
        if top.ordered != block.ordered:
            html_parts.append("</ol>" if top.ordered else "</ul>")
            html_parts.append(_open_list_container_html(block.ordered))
            top = _ListFrame(ordered=block.ordered, index=0)
        stack = stack[:-1] + [top._replace(index=top.index + 1)]
    else:
        while len(stack) < block.level:
            html_parts.append(_open_list_container_html(block.ordered))
            stack = stack + [_ListFrame(ordered=block.ordered, index=1)]

    html_parts.append(f"<li>{_render_inline_html(block.text)}")
    return "".join(html_parts), stack


def _render_non_list_block(block: _Block) -> str:
    if block.kind == "heading":
        tag = f"h{block.level}"
        return f"<{tag}>{_render_inline_html(block.text)}</{tag}>"
    if block.kind == "code_block":
        # Fenced code content is literal Markdown -- never inline-substituted -- so it's HTML-
        # escaped directly rather than routed through _render_inline_html.
        return f"<pre><code>{_escape_html_text(block.text)}\n</code></pre>"
    if block.kind == "paragraph":
        content = "<br>".join(_render_inline_html(line) for line in block.text.split("\n"))
        return f"<p>{content}</p>"
    # kind == "blank": a pure block separator -- nothing to emit.
    return ""


def _render_block_html(block: _Block, list_stack: list[_ListFrame]) -> tuple[str, list[_ListFrame]]:
    """Render one block to HTML, folding list state against ``list_stack`` (never mutates the
    caller's list -- always returns a new one). A non-list block first closes every list still
    open on ``list_stack``, matching real HTML nesting rules (a list can't stay open across a
    heading/paragraph/code block)."""
    if block.kind == "list_item":
        return _render_list_item(block, list_stack)
    closing = _close_open_lists_html(list_stack)
    return closing + _render_non_list_block(block), []


def markdown_to_leankit_html(md: str) -> str:
    """Translate GitHub-flavored Markdown into AgilePlace's HTML subset. Raises TypeError if
    ``md`` isn't a str; otherwise never raises. Block structure (headings, fenced code, cross-
    block list nesting/numbering) is folded by _render_block_html; inline formatting within each
    block's text (bold/italic/strikethrough/links/code spans) is rendered by _render_inline_html.
    A delimiter with no matching close, or a link with a disallowed href scheme, degrades to plain
    (HTML-escaped) text rather than emitting an unbalanced or unsafe tag."""
    if not isinstance(md, str):
        raise TypeError(f"markdown_to_leankit_html: expected str, got {type(md).__name__}")
    blocks = _parse_blocks(md)
    html_parts: list[str] = []
    list_stack: list[_ListFrame] = []
    for block in blocks:
        rendered, list_stack = _render_block_html(block, list_stack)
        html_parts.append(rendered)
    html_parts.append(_close_open_lists_html(list_stack))
    return "".join(html_parts)
