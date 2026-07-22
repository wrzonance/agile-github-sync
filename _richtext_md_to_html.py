"""Markdown->HTML direction of richtext.py's translator. Block structure (headings, fenced code,
cross-block list nesting/numbering) is folded by ``_render_block_html``; inline formatting within
each block's text (bold/italic/strikethrough/links/code spans) is rendered by
``_render_inline_html``. No I/O; the public entry point never raises for a malformed document
shape, only for a non-str input.
"""
from __future__ import annotations

import re

from _richtext_shared import _UNESCAPABLE_CHARS, _LIST_INDENT_UNIT, _Block, _ListFrame, _sanitize_href

# Line-oriented block patterns. Only the documented supported subset is recognized as structure;
# anything else falls through to plain paragraph text.
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


def _unescape_markdown_text(text: str) -> str:
    """Inverse of the HTML->MD escaper: strip a backslash immediately preceding any character
    that escaper ever emits a backslash before (_UNESCAPABLE_CHARS), leaving that character
    literal. A backslash not followed by such a character, or at end of string, is left as-is --
    it was never something that escaper produced."""
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


def _flush_paragraph(lines: list[str], blocks: list[_Block]) -> list[str]:
    """Return a new (empty) paragraph-line buffer, appending the accumulated lines as one
    _Block to ``blocks`` first if there were any. ``blocks`` is appended to in place (it is the
    caller's own accumulator, not a shared/aliased input); ``lines`` itself is never mutated --
    the caller always receives a fresh list back."""
    if lines:
        blocks.append(_Block(kind="paragraph", level=0, ordered=False, text="\n".join(lines)))
    return []


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
        if blocks and blocks[-1].kind == "list_item" and not paragraph_lines:
            # Lazy continuation: a line right after a list item, with no blank-line separator and
            # no structural marker of its own, is a hard-line-break continuation of that same item
            # (mirroring how a bare '\n' inside a paragraph already becomes '<br>' -- see
            # _render_lines_with_br) -- fold it into the item's own text instead of starting a new
            # top-level paragraph, which would also prematurely close the still-open list.
            blocks[-1] = blocks[-1]._replace(text=f"{blocks[-1].text}\n{line}")
            i += 1
            continue
        paragraph_lines = paragraph_lines + [line]
        i += 1
    _flush_paragraph(paragraph_lines, blocks)
    return blocks


# Bold/italic/strikethrough markers tried in this fixed precedence order at each inline scan
# position -- "**"/"__" are checked before "*"/"_" so a bold span is never mis-split into two
# italic opens. The underscore spellings are GFM's alternate strong/em delimiters -- the HTML->MD
# walker emits them (instead of the star spelling) for a span that opens directly against another
# star-based marker with no separating text, precisely so this parser never has to disambiguate an
# adjacent run of literal '*' characters; recognizing "__"/"_" here is what makes that emitted
# Markdown parse back to the intended nesting.
_INLINE_FORMAT_DELIMITERS: tuple[tuple[str, str], ...] = (
    ("**", "strong"),
    ("__", "strong"),
    ("*", "em"),
    ("_", "em"),
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
    marker -- is left untouched rather than raising, whether its digit run is merely out of range
    or too long for int() to parse at all (see _replace): totality over arbitrary content is a hard
    invariant of this module's public functions."""
    def _replace(match: re.Match[str]) -> str:
        try:
            index = int(match.group(1))
        except ValueError:
            # A digit run longer than CPython's int-string-conversion limit (4300 digits by
            # default, 3.11+) can only come from adversarial input -- this module's own placeholder
            # indices are tiny -- so leave it untouched rather than raising, exactly as an
            # out-of-range index below does. Totality (never raises) is a hard invariant.
            return match.group(0)
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


def _unescape_href_text(href: str) -> str:
    """Strip a backslash immediately preceding ANY character in a raw link-href slice, leaving
    that character literal. This mirrors _find_balanced_close's escape handling for link syntax
    (used to find the href's closing paren): that scan treats a backslash before *any* character --
    not just _UNESCAPABLE_CHARS -- as a non-structural escape pair so a literal '(' or ')' can be
    embedded in a URL without prematurely closing the link. Applying the narrower
    _unescape_markdown_text here would leave such a backslash (e.g. before ')') in the emitted
    href, corrupting the link target -- so this generic inverse is used instead, scoped to hrefs
    only."""
    out: list[str] = []
    i = 0
    n = len(href)
    while i < n:
        if href[i] == "\\" and i + 1 < n:
            out.append(href[i + 1])
            i += 2
            continue
        out.append(href[i])
        i += 1
    return "".join(out)


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
    raw_href = text[close_bracket + 2:close_paren]
    href = _sanitize_href(_unescape_href_text(raw_href))
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
    literal form (the true inverse of the HTML->MD escaper), and finally splice the protected
    code-span content back in verbatim."""
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

    # Clamp the target depth to at most one level deeper than what's currently open. A block.level
    # more than one deeper than the stack (a Markdown indent that jumps several levels at once,
    # e.g. a first item that starts already indented, or a stray deep indent mid-list) would
    # otherwise make the "open new deeper levels" branch below open one container per skipped
    # level while only ever emitting a matching <li> for the final (deepest) one -- every
    # intermediate container's frame still gets a </li> from _close_list_frame_html when it later
    # closes, producing unbalanced HTML. Treating any such jump as "one level deeper than the
    # parent" keeps every opened container paired with exactly one <li>.
    target_level = min(block.level, len(stack) + 1)

    while len(stack) > target_level:
        html_parts.append(_close_list_frame_html(stack[-1]))
        stack = stack[:-1]

    if len(stack) == target_level and stack:
        top = stack[-1]
        html_parts.append("</li>")
        if top.ordered != block.ordered:
            html_parts.append("</ol>" if top.ordered else "</ul>")
            html_parts.append(_open_list_container_html(block.ordered))
            top = _ListFrame(ordered=block.ordered, index=0)
        stack = stack[:-1] + [top._replace(index=top.index + 1)]
    else:
        while len(stack) < target_level:
            html_parts.append(_open_list_container_html(block.ordered))
            stack = stack + [_ListFrame(ordered=block.ordered, index=1)]

    html_parts.append(f"<li>{_render_lines_with_br(block.text)}")
    return "".join(html_parts), stack


def _render_lines_with_br(text: str) -> str:
    """Render each '\\n'-joined physical line of ``text`` through the inline renderer and rejoin
    with '<br>' -- the block-internal hard-line-break encoding shared by paragraph and list-item
    blocks (a '\\n' that's a line break within the block, not a boundary between blocks)."""
    return "<br>".join(_render_inline_html(line) for line in text.split("\n"))


def _render_non_list_block(block: _Block) -> str:
    if block.kind == "heading":
        tag = f"h{block.level}"
        return f"<{tag}>{_render_inline_html(block.text)}</{tag}>"
    if block.kind == "code_block":
        # Fenced code content is literal Markdown -- never inline-substituted -- so it's HTML-
        # escaped directly rather than routed through _render_inline_html.
        return f"<pre><code>{_escape_html_text(block.text)}\n</code></pre>"
    if block.kind == "paragraph":
        return f"<p>{_render_lines_with_br(block.text)}</p>"
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
