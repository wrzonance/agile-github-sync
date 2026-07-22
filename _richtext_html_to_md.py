"""HTML->Markdown direction of richtext.py's translator. Streams AgilePlace HTML through stdlib's
tolerant HTMLParser and accumulates the equivalent GitHub-flavored Markdown. No I/O; the public
entry point never raises for a bad document shape, only for a non-str input.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

from _richtext_code_spans import (
    _ADJACENT_FENCE_SEPARATOR,
    _chunk_ends_in_live_backtick,
    _render_code_span,
)
from _richtext_shared import (
    _INLINE_AMBIGUOUS_CHARS,
    _LIST_INDENT_UNIT,
    _STRUCTURAL_LINE_START_CHARS,
    _escape_href_for_markdown,
    _ListFrame,
    _sanitize_href,
)

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

# strong/em share the '*' character with each other ("**" vs "*"), so directly-adjacent nesting
# (no separating text -- e.g. <strong><em>x</em></strong>) would otherwise concatenate into an
# ambiguous run like "***x***" that a nearest-delimiter-match parser can mis-split. GFM's
# alternate underscore spelling ("__" for strong, "_" for em) is a distinct character from '*',
# so picking it for whichever tag opens directly against a '*'-ending buffer sidesteps the
# ambiguity entirely rather than requiring a full CommonMark delimiter-run algorithm. Keyed by
# tag; (default_marker, alt_marker).
_STAR_MARKER_VARIANTS: dict[str, tuple[str, str]] = {
    "strong": ("**", "__"),
    "em": ("*", "_"),
}

# Consecutive newlines beyond a single blank line collapse to the documented one-blank-line
# block-separator convention.
_BLANK_LINE_RUN = re.compile(r"\n{3,}")


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
        self.tag_marker_stack: dict[str, list[str]] = {}
        self.list_stack: list[_ListFrame] = []
        self.href_stack: list[str | None] = []
        # Accumulates a <code> span's text (outside <pre>) so the fence/padding -- which depend
        # on the whole span's content -- can be chosen at close time. See _flush_code_span.
        self.code_span_buffer: list[str] = []
        self.in_code = False
        self.in_pre = False
        # Counts nested (non-<pre>) <code> opens. Markdown has no nested code-span syntax, so a
        # directly-nested <code> (malformed input) is not a new span -- it just keeps adding to
        # the same one. Only a 0->1 transition resets code_span_buffer; see _open_code.
        self.code_depth = 0
        self.suppress_text = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "p":
            self._ensure_block_separator()
        elif tag == "br":
            self.buffer.append("\n")
        elif tag == "pre":
            if self.in_code and not self.in_pre:
                # Malformed: <pre> opening inside an active <code> span (a block element inside
                # an inline one). Same degrade as a directly-nested <code>: don't emit a fence
                # mid-span -- the <pre>'s content just keeps buffering into the enclosing span
                # (handle_data's in_code branch), and _close_pre ignores the matching </pre>.
                return
            self._ensure_block_separator()
            self.in_pre = True
            self.buffer.append("```\n")
        elif tag == "code":
            self._open_code()
        elif tag in _STAR_MARKER_VARIANTS:
            self._open_variable_format(tag)
        elif tag in _STRIKE_TAGS:
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
        elif tag in _STAR_MARKER_VARIANTS:
            self._close_variable_format(tag)
        elif tag in _STRIKE_TAGS:
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
        # never overwrite the buffer/code_span_buffer.
        if self.suppress_text or not data:
            return
        if self.in_pre:
            # <pre> content is literal Markdown (the fence itself doesn't reinterpret syntax
            # inside it), so it bypasses _escape_markdown_text entirely.
            self.buffer.append(data)
            return
        if self.in_code:
            # Buffered rather than appended directly -- the right-sized fence/padding
            # (_render_code_span) can only be chosen once the whole span's content is known, at
            # close time (_flush_code_span).
            self.code_span_buffer.append(data)
            return
        self.buffer.append(_escape_markdown_text(data, at_line_start=self._at_line_start()))

    def get_markdown(self) -> str:
        """Flush any still-open inline-format markers (an unclosed ``<strong>`` degrades to a
        balanced ``**word**`` instead of a dangling ``**word``), join the buffer, and collapse
        blank-line runs to the documented single-blank-line block-separator convention."""
        if self.in_code and not self.in_pre:
            # An unclosed <code> at EOF never reaches _close_code, so flush it here or its
            # buffered content is silently dropped. Runs BEFORE the format_stack loop below since
            # format_stack no longer carries a code marker -- for degenerate, never-closed nested
            # tag-soup (e.g. "<code><strong>x", neither closed) this changes the relative order
            # of the code fence vs. the outer format's closer in the output. A harmless behavior
            # change with no stated invariant covering it -- documented, not test-pinned.
            self._flush_code_span()
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

    def _select_star_marker(self, tag: str) -> str:
        """Pick strong/em's default marker, unless the buffer's most recently emitted chunk ends
        in '*' -- meaning this span is opening with zero separating text directly against another
        star-based marker (nested or sibling) -- in which case the alt (underscore) marker avoids
        concatenating into an ambiguous run of literal '*' characters."""
        default, alt = _STAR_MARKER_VARIANTS[tag]
        if self.buffer and self.buffer[-1].endswith("*"):
            return alt
        return default

    def _open_variable_format(self, tag: str) -> None:
        marker = self._select_star_marker(tag)
        self.tag_marker_stack.setdefault(tag, []).append(marker)
        self._open_format(marker)

    def _close_variable_format(self, tag: str) -> None:
        # Close with the exact marker this open used (tracked per tag, LIFO) -- never recomputed
        # from the current buffer state, which has since changed with the span's own content.
        stack = self.tag_marker_stack.get(tag)
        marker = stack.pop() if stack else _STAR_MARKER_VARIANTS[tag][0]
        self._close_format(marker)

    def _open_code(self) -> None:
        if self.in_pre:
            # Still inside <pre> -- content goes straight to self.buffer via handle_data,
            # unbuffered; code_depth is irrelevant here.
            self.in_code = True
            return
        if self.code_depth == 0:
            # Start collecting this span's content fresh -- the fence/padding it needs can only
            # be chosen once the whole span is known, at close time. A directly-nested <code>
            # (code_depth already > 0, malformed input -- Markdown has no nested code-span
            # syntax) skips this reset, so text already captured for the outer span survives
            # rather than being silently overwritten.
            self.code_span_buffer = []
        self.in_code = True
        self.code_depth += 1

    def _close_code(self) -> None:
        if self.in_pre:
            # Still inside <pre> -- the fence itself closes on </pre>, not here.
            self.in_code = False
            return
        self.code_depth = max(0, self.code_depth - 1)
        if self.code_depth == 0:
            self._flush_code_span()
        # else: this closed an inner nested <code> -- still buffering for the outer span, which
        # is still open.

    def _flush_code_span(self) -> None:
        """Render the buffered ``<code>`` span's content as a GFM code span (_render_code_span)
        and append it to the buffer, then reset code-span state. An empty span renders to ""
        (see _render_code_span's documented degrade) and is not appended -- it leaves no trace in
        the Markdown output rather than a bare, meaningless "``". If the buffer's last emitted
        chunk already ends in a live (unescaped) backtick -- another code span's closing fence, or
        a <pre> block's closing fence, immediately touching this one with no separating text --
        a zero-width separator is inserted first so the two fences can never merge into one longer
        run on reparse (see _chunk_ends_in_live_backtick)."""
        rendered = _render_code_span("".join(self.code_span_buffer))
        if rendered:
            if self.buffer and _chunk_ends_in_live_backtick(self.buffer[-1]):
                self.buffer.append(_ADJACENT_FENCE_SEPARATOR)
            self.buffer.append(rendered)
        self.code_span_buffer = []
        self.in_code = False
        self.code_depth = 0

    def _close_pre(self) -> None:
        if not self.in_pre:
            # Stray </pre> with no open <pre> -- either genuinely unmatched input, or the
            # closer of a <pre> that handle_starttag degraded inside an active code span.
            # Emitting a closing fence here would fabricate one that was never opened.
            return
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
            self.buffer.append(f"]({_escape_href_for_markdown(href)})")
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
