"""GFM code-span fencing for richtext.py, both directions: rendering a <code> element's captured
text into a backtick-fenced Markdown span (used by _richtext_html_to_md.py) and locating/protecting
a backtick-run-delimited span while parsing Markdown back to HTML (used by _richtext_md_to_html.py).
Split out of those two modules as its own self-contained concern -- distinct doc comments, no
dependency on either module's walker/block/inline-format machinery beyond the stdlib ``re`` import
below. No I/O; never raises.
"""
from __future__ import annotations

import re

# Positional placeholder _extract_code_spans swaps in for protected code-span content. NUL is not
# a character this module's HTML/Markdown escaping ever produces, so a placeholder-shaped
# substring in the rendered HTML can only originate from a NUL byte already present in the
# *original* input, never from this module's own output.
_CODE_SPAN_PLACEHOLDER_RE = re.compile(r"\x00(\d+)\x00")

# How far past an opening backtick run _find_closing_backtick_run will look for the closer --
# the code-span mirror of _richtext_md_to_html's _MAX_DELIMITER_SCAN bound on every other inline
# delimiter. Without it, each unmatched run scanned the entire remaining text, making input with
# many distinct-length unmatched runs quadratic in document size (a few hundred KB of adversarial
# description text could stall a sync pass for seconds). A genuine closer farther away than this
# degrades the span to literal text -- the same accepted tradeoff as the other delimiters.
_MAX_CODE_SPAN_SCAN = 2000


def _longest_backtick_run(content: str) -> int:
    """Length of the longest run of consecutive backticks in ``content`` (0 if none). A fence one
    longer than this can never be confused with a run inside the content itself."""
    longest = 0
    current = 0
    for ch in content:
        if ch == "`":
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _pad_code_span_content(content: str) -> str:
    """Pad ``content`` with a symmetric leading/trailing space when either edge would otherwise
    merge with the fence on re-parsing: an edge backtick (read as part of the fence's own run), or
    both edges space with at least one non-space char (MD->HTML's GFM strip rule would otherwise
    misread that as unpadded whitespace and strip it for real). Padding is never applied to only
    one edge, since that strip rule only fires when BOTH ends are space. All-space ``content`` is
    left untouched -- GFM's strip rule excludes that case, so padding it would corrupt round-trip."""
    if not content:
        return content
    edge_is_backtick = content[0] == "`" or content[-1] == "`"
    edge_is_space = content[0] == " " and content[-1] == " " and content.strip(" ")
    if edge_is_backtick or edge_is_space:
        return f" {content} "
    return content


def _chunk_ends_in_live_backtick(chunk: str) -> bool:
    """True if ``chunk`` (the most recently emitted HTML->MD buffer entry) ends in a genuine,
    unescaped backtick -- one that would act as a live fence character if immediately followed by
    another backtick run, as opposed to a backtick _richtext_html_to_md.py's own
    ``_escape_markdown_text`` produced (always emitted as part of a self-contained ``\\``` two-char
    escape pair, which ``_extract_code_spans`` consumes as one unsplittable unit on the way back --
    see its docstring). The only two things that ever emit a live trailing backtick are a rendered
    code span's own closing fence (_render_code_span) and a ``<pre>`` block's closing fence; both
    route new code-span content through that module's _flush_code_span, so checking this one
    predicate there is sufficient to catch every case."""
    if not chunk or chunk[-1] != "`":
        return False
    count = 0
    i = len(chunk) - 2
    while i >= 0 and chunk[i] == "\\":
        count += 1
        i -= 1
    return count % 2 == 0


# Zero-width space: invisible wherever it renders, but present as real text between two adjacent
# fenced spans so a live trailing backtick from one never touches a live leading backtick from the
# next. Markdown has no escape syntax that can separate two touching backtick runs (a backslash is
# never interpreted as an escape immediately before/after a code-span fence -- see
# _find_closing_backtick_run's docstring below) -- inserting real, if invisible, content is the only
# way to break the adjacency rather than merging into one corrupted span.
_ADJACENT_FENCE_SEPARATOR = "\u200b"  # ZERO WIDTH SPACE (U+200B)


def _render_code_span(content: str) -> str:
    """Render ``content`` as a GFM code span: a fence one backtick longer than its longest
    internal run (_longest_backtick_run), wrapping edge-padded content (_pad_code_span_content).
    Empty ``content`` renders as "" -- the documented ``<code></code>`` degrade: even the
    shortest fence pair would produce a bare "``" that MD->HTML reads back as literal text, not
    an empty code element, so emitting nothing is the more honest choice."""
    if not content:
        return ""
    fence = "`" * (_longest_backtick_run(content) + 1)
    return f"{fence}{_pad_code_span_content(content)}{fence}"


def _backtick_run_length(text: str, i: int) -> int:
    """Length of the maximal run of consecutive backtick characters in ``text`` starting at
    index ``i`` (``text[i]`` is assumed to already be a backtick)."""
    n = len(text)
    j = i
    while j < n and text[j] == "`":
        j += 1
    return j - i


def _find_closing_backtick_run(text: str, start: int, run_len: int) -> int:
    """Index of the next run of EXACTLY ``run_len`` consecutive backticks at or after ``start``,
    or -1 if none exists within the bounded window (_MAX_CODE_SPAN_SCAN chars past ``start``).
    Per GFM, a code-span delimiter is matched by run length alone -- a backslash immediately
    preceding a candidate run has no bearing on whether it closes the span, unlike every other
    delimiter in richtext.py's Markdown->HTML direction (bold/italic/strike/links), which exempts
    a backslash-escaped candidate via that module's own _is_backslash_escaped. A run whose length
    doesn't match is skipped in one jump (not re-scanned backtick-by-backtick), so this never
    revisits a character once it's been measured. A candidate run may begin inside the window and
    extend past it -- it is still measured in full and can match."""
    n = min(len(text), start + _MAX_CODE_SPAN_SCAN)
    i = start
    while i < n:
        if text[i] == "`":
            run = _backtick_run_length(text, i)
            if run == run_len:
                return i
            i += run
        else:
            i += 1
    return -1


def _strip_code_span_padding(content: str) -> str:
    """Per GFM: if a code span's content both begins and ends with a space character, but doesn't
    consist entirely of space characters, exactly one leading and one trailing space is stripped.
    This is what lets a code span's content itself start or end with a backtick (`` `` ` `` ``) or
    be all-whitespace (`` `` `` ``, kept intact) without either merging into the delimiter run."""
    if len(content) >= 2 and content[0] == " " and content[-1] == " " and content.strip(" "):
        return content[1:-1]
    return content


def _extract_code_spans(text: str) -> tuple[str, list[str]]:
    """Replace each backtick-run-delimited code span in already-HTML-escaped ``text`` with a
    positional placeholder, returning (placeholder_text, protected_contents) so the span's content
    bypasses both delimiter substitution and the closing unescape pass -- mirroring in_code/in_pre's
    literal handling on the HTML->MD side. Per GFM, a span opens on a run of N consecutive
    backticks and closes on the next run of EXACTLY N backticks (not just any backtick) -- this is
    what lets a shorter/longer backtick run appear literally inside a span's content without
    prematurely closing it. A backslash-escaped backtick (``\\```) outside any span is not treated
    as an opener, matching richtext.py's general backslash-escape handling; once a run has opened,
    though, closing-run matching ignores backslashes entirely (see _find_closing_backtick_run). A
    run with no matching close is emitted as literal text in full -- advancing past the whole run,
    not just its first character -- so it is never re-split into shorter literal runs."""
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
            run_len = _backtick_run_length(text, i)
            close = _find_closing_backtick_run(text, i + run_len, run_len)
            if close != -1:
                content = _strip_code_span_padding(text[i + run_len:close])
                protected.append(content)
                out.append(f"\x00{len(protected) - 1}\x00")
                i = close + run_len
                continue
            out.append(text[i:i + run_len])
            i += run_len
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
    invariant of richtext.py's public functions."""
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
