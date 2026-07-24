"""Combined-document, fixed-point, and full adversarial-battery tests spanning both richtext.py
translation directions together (leankit_html_to_markdown and markdown_to_leankit_html). Individual
vocabulary items round-trip in isolation (see test_richtext_html_to_md.py /
test_richtext_md_to_html.py); this file pins that they still do once combined into one realistic
document, that adjacent nested inline spans don't collide into an ambiguous delimiter run, and
closes every remaining cell of the module docstring's degradation table with a systematic
(not just ad hoc-substring) whitelist-closure and content-conservation sweep. No I/O.

Run: pytest -q
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from richtext import _sanitize_href, leankit_html_to_markdown, markdown_to_leankit_html  # noqa: E402

# =====================================================================================
# Round-trip fixed point -- combined document (heading + nested list + link + code +
# bold/italic/strike) in both directions. This is the spike's highest-risk item: individual
# vocabulary items round-trip in isolation, but nothing so far has pinned that they still do once
# combined into one realistic document, or that adjacent (not separated-by-text) nested inline
# spans don't collide into an ambiguous run of delimiter characters.
# =====================================================================================

_COMBINED_HTML_DOCUMENT = (
    "<h1>Title</h1>"
    "<p>Intro <strong>bold</strong> and <em>italic</em> and <s>strike</s> text with "
    '<code>code()</code> and a <a href="https://example.com">link</a>.</p>'
    "<ul><li>parent<ul><li>child <strong>bold child</strong></li></ul></li>"
    "<li>second top item</li></ul>"
    "<pre><code>def f():\n    return 1\n</code></pre>"
    "<p>Outro paragraph.</p>"
)


def test_combined_document_html_to_markdown_to_html_round_trip_is_the_identity():
    md = leankit_html_to_markdown(_COMBINED_HTML_DOCUMENT)
    assert markdown_to_leankit_html(md) == _COMBINED_HTML_DOCUMENT


def test_combined_document_markdown_round_trip_reaches_a_fixed_point_after_one_pass():
    # A second HTML->MD->HTML pass over the already-round-tripped Markdown must reproduce the
    # exact same Markdown -- the fixed point the spike's design doc calls out by name.
    md1 = leankit_html_to_markdown(_COMBINED_HTML_DOCUMENT)
    html2 = markdown_to_leankit_html(md1)
    md2 = leankit_html_to_markdown(html2)
    assert md1 == md2


@pytest.mark.parametrize(
    "html",
    [
        # strong directly wrapping em with zero separating text -- opening markers "**" + "*"
        # collapse into one run of three literal '*' characters ("***"), and closing markers
        # "*" + "**" collapse the same way. A naive nearest-delimiter-match parser mis-splits
        # this run and drops content or leaves a stray asterisk outside the tag.
        "<p><strong><em>bold italic</em></strong></p>",
        # three levels stacked directly adjacent: strong(em(s(text))).
        "<p><strong><em><s>all three</s></em></strong></p>",
        # the same triple nesting with a link immediately following, no separating text.
        (
            "<p><strong><em><s>all three</s></em></strong> and "
            '<a href="https://example.com"><strong><em>bold italic link</em></strong></a></p>'
        ),
    ],
)
def test_directly_adjacent_nested_inline_formats_round_trip_without_delimiter_collision(html):
    md = leankit_html_to_markdown(html)
    assert markdown_to_leankit_html(md) == html


@pytest.mark.parametrize(
    "html, code_texts",
    [
        # two directly-adjacent <code> spans, no separating text -- the first span's closing
        # fence and the second's opening fence would otherwise concatenate into one longer run of
        # live backtick characters on reparse, merging both spans' content into one and losing the
        # element boundary between them entirely.
        ("<p><code>a</code><code>b</code></p>", ["a", "b"]),
        # same collision with a leading word first, so the pair never lands at true line start --
        # isolates the adjacent-span collision under test here from the separate (now fixed)
        # fenced-code-BLOCK collision a 3+ backtick run used to hit at true line start; see
        # test_code_span_needing_a_three_plus_backtick_fence_round_trips_even_at_true_line_start.
        ("<p>Inline <code>a</code><code>b</code> spans.</p>", ["a", "b"]),
        # three-way chain of adjacent spans -- the collision must not just be fixed pairwise.
        ("<p><code>a</code><code>b</code><code>c</code></p>", ["a", "b", "c"]),
    ],
)
def test_directly_adjacent_code_spans_preserve_distinct_boundaries_and_content(html, code_texts):
    # Markdown has no escape syntax that can separate two touching backtick runs (see
    # _chunk_ends_in_live_backtick's docstring), so a perfectly byte-identical round trip is not
    # achievable here -- the documented degrade is an invisible zero-width-space inserted between
    # the two spans (never inside either one). Each span's own content is conserved exactly and
    # the element boundary survives; that invisible separator is the one unavoidable difference,
    # analogous to this module's other documented, honest degrades (e.g. the empty-<code> case).
    md1 = leankit_html_to_markdown(html)
    html2 = markdown_to_leankit_html(md1)
    for text in code_texts:
        assert f"<code>{text}</code>" in html2
    # Idempotent: re-translating the already-degraded HTML must not add another layer of
    # separators or otherwise keep drifting.
    md2 = leankit_html_to_markdown(html2)
    assert md2 == md1


@pytest.mark.parametrize(
    "html, markdown",
    [
        # an ordered sublist nested inside a bullet item's <li> -- and the inverse (a bullet
        # sublist inside a numbered item). Exact-value pins for both directions, not just a
        # fixed-point self-consistency check: a regression that consistently mis-renders one of
        # these (e.g. always emitting the wrong list-type tag, or silently flattening the nested
        # list) would satisfy a same-output-twice check while still being wrong.
        (
            "<ul><li>parent<ol><li>child</li></ol></li></ul>",
            "- parent\n  1. child",
        ),
        (
            "<ol><li>parent<ul><li>child</li></ul></li></ol>",
            "1. parent\n  - child",
        ),
    ],
)
def test_mixed_type_nested_list_round_trips_to_an_exact_known_value_both_directions(html, markdown):
    assert leankit_html_to_markdown(html) == markdown
    assert markdown_to_leankit_html(markdown) == html


@pytest.mark.parametrize(
    "html",
    [
        # An href with an unmatched literal '(' would otherwise make MD->HTML's balanced-paren
        # scan for the link's closing paren run past the intended end of the href (or fail to
        # find one at all within the scan window), corrupting or dropping the link on the way
        # back to HTML. Wrapped in <p> -- matching the module's existing "bare inline content at
        # document root round-trips wrapped in a paragraph" convention (see the directly-adjacent-
        # nested-format tests above) -- so this pins the link-escaping fix, not that pre-existing,
        # unrelated wrapping asymmetry.
        '<p><a href="https://example.com/(unclosed">text</a></p>',
        '<p><a href="https://example.com/a(b)c(d">text</a></p>',
        '<p><a href="https://example.com/)stray">text</a></p>',
        '<p><a href="https://example.com/back\\slash">text</a></p>',
        # A backslash preceding an _UNESCAPABLE_CHARS character ('.'/'#') -- unlike the
        # backslash-before-'s' case above -- is exactly what the trailing unescape pass in
        # _render_inline_html would strip if the finalized href were not protected from it.
        '<p><a href="https://example.com/a\\.b">text</a></p>',
        '<p><a href="https://example.com/a\\#b">text</a></p>',
    ],
)
def test_href_with_unbalanced_paren_or_backslash_round_trips_through_markdown_link_syntax(html):
    md = leankit_html_to_markdown(html)
    assert markdown_to_leankit_html(md) == html


@pytest.mark.parametrize(
    "html",
    [
        "<ul><li>line one<br>line two</li></ul>",
        "<ol><li>line one<br>line two</li><li>next item</li></ol>",
        "<ul><li>parent<br>continued<ul><li>child</li></ul></li></ul>",
    ],
)
def test_br_inside_list_item_round_trips_through_markdown_and_reaches_a_fixed_point(html):
    md1 = leankit_html_to_markdown(html)
    assert markdown_to_leankit_html(md1) == html
    # Second pass must reach the same fixed point -- the continuation line must not detach into
    # a sibling block on a repeated translation.
    md2 = leankit_html_to_markdown(markdown_to_leankit_html(md1))
    assert md1 == md2


@pytest.mark.parametrize(
    "markdown",
    [
        # irregular 3-space list continuation indent under a numbered marker, and headings glued
        # directly to the following block with no blank line -- both normalize on the first pass
        # (canonical 2-space indent, an inserted blank-line separator) rather than round-tripping
        # byte-for-byte, but the *normalized* form must itself be a genuine fixed point.
        "# Title\n\nIntro **bold** and *italic* and ~~strike~~ text with `code()` and a "
        "[link](https://example.com).\n\n- parent\n  - child **bold child**\n"
        "- second top item\n\n1. first\n2. second\n   - nested bullet inside ordered\n3. third"
        "\n\n```\ndef f():\n    return 1\n```\n\nOutro paragraph.",
        "## Sub\n### SubSub\n\n- a\n- b\n  1. nested ordered\n  2. two\n- c",
        "# H1\n## H2\n- item1\n- item2",
    ],
)
def test_markdown_authored_document_stabilizes_after_one_normalization_pass(markdown):
    html1 = markdown_to_leankit_html(markdown)
    md_normalized = leankit_html_to_markdown(html1)
    html2 = markdown_to_leankit_html(md_normalized)
    md_refixed = leankit_html_to_markdown(html2)
    assert md_normalized == md_refixed
    assert html1 == html2


# =====================================================================================
# Issue #78 gate -- fixed-point round trip covering the two fixes together (variable-length
# code-span backtick fencing + symmetric '<'/'>' escaping), combined with tag-looking prose and a
# link in the SAME document. Individual vocabulary items are pinned in isolation by
# test_richtext_html_to_md.py / test_richtext_md_to_html.py; this closes the spike's own
# highest-risk gap those files call out -- that raw '<'/'>' escaping and the new run-length-N
# backtick fencing don't destabilize each other, or the link-escaping/list/heading machinery
# already pinned above, once all combined into one document. Code spans here still use the
# established "leading word" convention (see
# test_code_span_needing_a_three_plus_backtick_fence_round_trips_when_not_at_true_line_start) to
# keep this gate scoped to the angle-bracket/fencing/link interaction under test -- the separate
# true-line-start fenced-code-BLOCK collision a 3+ backtick run used to hit is fixed and pinned on
# its own by test_code_span_needing_a_three_plus_backtick_fence_round_trips_even_at_true_line_start.
# =====================================================================================

_ANGLE_AND_CODE_SPAN_COMBINED_HTML = (
    "<p>Compare a &lt; b and b &gt; a while reading fake tags like &lt;div&gt; and "
    '&lt;/div&gt;. See <a href="https://example.com/a(b)c">the docs</a> for inline '
    "<code>a``b</code> and <code>x```y</code> examples, plus a "
    "<code>has a ` single backtick</code> case.</p>"
)


def test_angle_brackets_and_variable_length_code_span_fences_round_trip_together():
    md = leankit_html_to_markdown(_ANGLE_AND_CODE_SPAN_COMBINED_HTML)
    assert markdown_to_leankit_html(md) == _ANGLE_AND_CODE_SPAN_COMBINED_HTML


def test_angle_brackets_and_variable_length_code_span_fences_reach_a_fixed_point_after_one_pass():
    # A second HTML->MD->HTML pass over the already-round-tripped Markdown must reproduce the
    # exact same Markdown -- neither the new escape chars nor the new fence-length rule may drift
    # once combined with each other, a link, and tag-looking prose in the same document.
    md1 = leankit_html_to_markdown(_ANGLE_AND_CODE_SPAN_COMBINED_HTML)
    html2 = markdown_to_leankit_html(md1)
    md2 = leankit_html_to_markdown(html2)
    assert md1 == md2


_ANGLE_AND_CODE_SPAN_COMBINED_MARKDOWN = (
    "Compare a \\< b and b \\> a while reading fake tags like \\<div\\> and \\</div\\>. "
    "See [the docs](https://example.com/a\\(b\\)c) for inline ```a``b``` and "
    "````x```y```` examples, plus a ``has a ` single backtick`` case."
)


def test_markdown_authored_angle_brackets_and_code_spans_stabilize_after_one_normalization_pass():
    # Mirrors test_markdown_authored_document_stabilizes_after_one_normalization_pass but for
    # Markdown authored directly with the new backslash-escaped angle brackets and variable-length
    # backtick fences, rather than Markdown produced by this module's own HTML->MD direction.
    html1 = markdown_to_leankit_html(_ANGLE_AND_CODE_SPAN_COMBINED_MARKDOWN)
    md_normalized = leankit_html_to_markdown(html1)
    html2 = markdown_to_leankit_html(md_normalized)
    md_refixed = leankit_html_to_markdown(html2)
    assert md_normalized == md_refixed
    assert html1 == html2


@pytest.mark.parametrize(
    "malformed",
    [
        # unclosed backtick span colliding with unescaped-looking tag soup
        "a < b and b > a with `unclosed backtick and <tag> soup",
        # mismatched backtick-run lengths that can never find a valid closing fence
        "``` broken fence `` mismatched `` more",
        # angle brackets inside an otherwise well-formed link target
        "[link text with < and > inside](https://example.com/<>)",
        # unterminated <code> (HTML input) whose buffered content itself contains angle brackets
        "<code>``unterminated code span with < and >",
        # pre-escaped angle brackets adjacent to a run of empty-ish backtick spans
        "\\<div\\> plus `` `` `` many empty-ish spans",
    ],
)
def test_neither_direction_raises_over_malformed_angle_bracket_and_code_span_input(malformed):
    md_result = leankit_html_to_markdown(malformed)
    html_result = markdown_to_leankit_html(malformed)
    assert isinstance(md_result, str)
    assert isinstance(html_result, str)


# =====================================================================================
# Task 7/7 -- full safety/degradation matrix sweep, closing every remaining cell of the
# richtext.py module docstring's degradation table with a parametrized pin, plus systematic
# (not just ad hoc-substring) whitelist-closure checks over a large adversarial battery.
# Pins exactly: total over content | whitelist closure (HTML output) | whitelist closure
# (Markdown output) | content conservation.
# =====================================================================================

# --- invariant: whitelist closure (HTML output) -- systematic tag-name extraction ----------------

# The exact set of tag names markdown_to_leankit_html is ever allowed to emit. Any other tag name
# appearing anywhere in its output -- regardless of which specific adversarial input produced it --
# is a whitelist-closure violation.
_SUPPORTED_HTML_TAG_NAMES = frozenset(
    {"p", "br", "strong", "em", "code", "pre", "a", "li", "ul", "ol", "s",
     "h1", "h2", "h3", "h4", "h5", "h6"}
)
_HTML_OPEN_TAG_NAME_RE = re.compile(r"<\s*/?\s*([a-zA-Z0-9]+)")


def _assert_html_output_uses_only_whitelisted_tags(html: str) -> None:
    tag_names = {match.group(1).lower() for match in _HTML_OPEN_TAG_NAME_RE.finditer(html)}
    assert tag_names <= _SUPPORTED_HTML_TAG_NAMES, f"leaked non-whitelisted tag(s): {tag_names - _SUPPORTED_HTML_TAG_NAMES}"


# Every ``href="..."`` attribute value that ever reaches markdown_to_leankit_html's HTML output
# must be one _sanitize_href itself would accept unchanged -- <a> is a legitimately whitelisted
# tag, so tag-name closure alone (above) can't catch a regression in _sanitize_href's scheme
# allowlist/comparison (e.g. a scheme accidentally added to _ALLOWED_HREF_SCHEMES, or the check
# becoming case-sensitive) that lets a live "javascript:"-style href slip through untouched.
_HREF_ATTR_RE = re.compile(r'href="([^"]*)"')


def _assert_html_output_hrefs_are_all_sanitizer_accepted(html: str) -> None:
    for href in _HREF_ATTR_RE.findall(html):
        assert _sanitize_href(href) == href, f"href attribute not accepted by _sanitize_href: {href!r}"


_DANGEROUS_MARKDOWN_HTML_BATTERY = [
    "<ScRiPt>alert(1)</sCriPt>",  # mixed-case tag name
    "<svg onload=alert(1)>",
    "<math><mtext></mtext></math>",
    "<form action=evil><input></form>",
    "<object data=evil></object>",
    "<embed src=evil>",
    "<base href=evil>",
    "<meta http-equiv=refresh content=0;url=evil>",
    "<!-- comment injection -->",
    "<![CDATA[weird]]>",
    '<iframe srcdoc="<script>alert(1)</script>"></iframe>',
    "<style>body{background:url(javascript:alert(1))}</style>",
    "<!DOCTYPE html><html><body>x</body></html>",
    "plain <b>bold-ish</b> text",
    "[x](JAVASCRIPT:alert(1))",
    "[x](  javascript:alert(1)  )",
    "[x](java\tscript:alert(1))",
    "[x](vbscript:msgbox(1))",
    '[x](https://evil.com"><script>alert(1)</script>)',
]


@pytest.mark.parametrize("markdown", _DANGEROUS_MARKDOWN_HTML_BATTERY)
def test_html_output_whitelist_closure_over_adversarial_markdown_battery(markdown):
    html = markdown_to_leankit_html(markdown)
    _assert_html_output_uses_only_whitelisted_tags(html)
    _assert_html_output_hrefs_are_all_sanitizer_accepted(html)


# --- invariant: whitelist closure (Markdown output) -- systematic raw-'<' absence ------------------

# Markdown output is never allowed to contain a live '<' immediately followed by a letter or '!' --
# that shape is exactly what a re-rendering Markdown viewer would reinterpret as an HTML tag/
# doctype/comment open, regardless of which supported or unsupported HTML tag produced it.
_RAW_TAG_OPEN_RE = re.compile(r"<[a-zA-Z!]")

_DANGEROUS_HTML_BATTERY = [
    "<ScRiPt>alert(1)</sCriPt>",
    "<svg onload=alert(1)>",
    "<math><mtext></mtext></math>",
    "<form action=evil><input></form>",
    "<object data=evil></object>",
    "<embed src=evil>",
    "<base href=evil>",
    "<meta http-equiv=refresh content=0;url=evil>",
    "<!-- comment injection -->",
    "<![CDATA[weird]]>",
    '<iframe srcdoc="<script>alert(1)</script>"></iframe>',
    "<style>body{background:url(javascript:alert(1))}</style>",
    '<a href="data:text/html,<script>alert(1)</script>">click</a>',
    "<!DOCTYPE html><html><body>x</body></html>",
]


@pytest.mark.parametrize("html", _DANGEROUS_HTML_BATTERY)
def test_markdown_output_whitelist_closure_over_adversarial_html_battery(html):
    result = leankit_html_to_markdown(html)
    assert not _RAW_TAG_OPEN_RE.search(result), f"raw tag-open shape leaked into Markdown: {result!r}"


# --- invariant: degradation matrix -- every unclosed inline delimiter, not just bold ---------------

@pytest.mark.parametrize(
    "markdown, must_not_contain",
    [
        ("*unclosed italic", "<em>"),
        ("~~unclosed strike", "<s>"),
        ("`unclosed code", "<code>"),
        ("[unclosed link text with no bracket close", "<a "),
        ("[unclosed](no closing paren", "<a "),
    ],
)
def test_every_unclosed_inline_delimiter_degrades_to_literal_text_not_a_dangling_tag(markdown, must_not_contain):
    result = markdown_to_leankit_html(markdown)
    assert must_not_contain not in result
    # The delimiter's literal characters themselves survive as escaped text, not as live syntax --
    # pinned as an exact value (not just "starts with <p>") so a regression that drops the
    # delimiter, or the surrounding prose, while still avoiding must_not_contain can't silently
    # pass. None of these inputs contain an HTML-special character, so the whole input is expected
    # back verbatim inside the paragraph wrapper.
    assert result == f"<p>{markdown}</p>"


# --- invariant: content conservation -- broad vocabulary, unicode, and structural-char battery -----

@pytest.mark.parametrize(
    "html, expected_substring",
    [
        ("<p>héllo wörld</p>", "héllo wörld"),
        ("<p>emoji sandwich \U0001F600 here</p>", "\U0001F600"),
        ("<p>costs $5.00 exactly</p>", "costs $5.00 exactly"),
        ("<h2>Numbers 1, 2, 3</h2>", "Numbers 1, 2, 3"),
        ("<ul><li>a &amp; b</li></ul>", "a & b"),
    ],
)
def test_content_conservation_over_broad_vocabulary_and_unicode_battery(html, expected_substring):
    md = leankit_html_to_markdown(html)
    html_back = markdown_to_leankit_html(md)
    # The readable text survives the full round trip even though delimiter/entity spelling may
    # be re-escaped along the way. Pinned on the far side of the round trip (md_back) only -- an
    # `or expected_substring in md` disjunct here would make this assertion pass even if
    # markdown_to_leankit_html were completely broken, since md is produced independently of
    # html_back and is already known (by construction) to contain the substring.
    md_back = leankit_html_to_markdown(html_back)
    assert expected_substring in md_back


# --- invariant: total over content -- broad safety sweep across both public functions --------------

_TOTALITY_BATTERY = [
    "\x00\x01\x02 control chars",
    "a" * 100_000,
    "\U0001D518\U0001D52B\U0001D526\U0001D520\U0001D52C\U0001D521\U0001D522",  # astral-plane unicode
    "\U0001F600 emoji sandwich \U0001F389",
    "mixed \r\n line \r endings \n here",
    "<" * 5_000,
    ">" * 5_000,
    "&" * 5_000,
]


@pytest.mark.parametrize(
    "content", _TOTALITY_BATTERY,
    # Short ids: several payloads are 5k-100k chars and would overflow Windows'
    # 32,767-char PYTEST_CURRENT_TEST env var (issue #90).
    ids=["control-chars", "a-100k", "astral-unicode", "emoji-sandwich", "mixed-line-endings",
         "lt-5k", "gt-5k", "amp-5k"],
)
def test_both_public_functions_never_raise_over_broad_safety_battery(content):
    md_result = leankit_html_to_markdown(content)
    html_result = markdown_to_leankit_html(content)
    assert isinstance(md_result, str)
    assert isinstance(html_result, str)


# --- boundary: TypeError for None/bytes/int is enforced identically on both public functions -------

@pytest.mark.parametrize("bad_input", [None, 0, 42, b"", b"bytes"])
def test_both_public_functions_reject_none_int_and_bytes_identically(bad_input):
    with pytest.raises(TypeError, match="expected str, got"):
        leankit_html_to_markdown(bad_input)
    with pytest.raises(TypeError, match="expected str, got"):
        markdown_to_leankit_html(bad_input)


# --- file-size budget: hard-cap regression guard, soft-target overage explicitly flagged -----------

# House style: 200-400 lines typical, 800 hard cap -- split before exceeding it, never add to a
# file already over budget. richtext.py's translation logic is split across four modules (shared
# structs/sanitizers, HTML->MD, MD->HTML, and code-span fencing/matching -- pulled into its own
# _richtext_code_spans.py since it's a self-contained concern used by both directions) so no
# single file need approach the hard cap; this test is the regression guard for that split staying
# intact. _richtext_md_to_html.py sits at 476 lines -- over the 400 soft target but well under the
# 800 hard cap. It is deliberately NOT split further: the block-folding renderer
# (_render_block_html) and the inline renderer (_render_inline_html) are two halves of one cohesive
# concern (a block's raw text always flows through the inline renderer before it's HTML-safe), and
# the two share fixed-precedence/protected-region invariants that would either duplicate across
# files or need re-threading through a new shared-state parameter if separated -- the classic
# "abstraction needs a flag to cover its callers" smell this repo's DRY rule warns against. This is
# recorded here as an explicit, reviewed flag rather than a silent budget overrun: if a NEW file
# crosses the soft target without a matching entry in _KNOWN_SOFT_TARGET_OVERAGES, the test below
# fails until that decision is made explicitly too.
_RICHTEXT_MODULE_HARD_CAP = 800
_RICHTEXT_MODULE_SOFT_TARGET = 400
_KNOWN_SOFT_TARGET_OVERAGES = frozenset({"_richtext_md_to_html.py"})
_RICHTEXT_MODULE_FILENAMES = (
    "richtext.py",
    "_richtext_shared.py",
    "_richtext_code_spans.py",
    "_richtext_html_to_md.py",
    "_richtext_md_to_html.py",
)


def test_richtext_modules_stay_within_hard_cap_and_soft_target_overage_is_explicitly_flagged():
    repo_root = Path(__file__).resolve().parent.parent
    for filename in _RICHTEXT_MODULE_FILENAMES:
        line_count = len((repo_root / filename).read_text(encoding="utf-8").splitlines())
        assert line_count <= _RICHTEXT_MODULE_HARD_CAP, (
            f"{filename} is {line_count} lines -- exceeds the {_RICHTEXT_MODULE_HARD_CAP}-line hard "
            "cap and must be split."
        )
        if line_count > _RICHTEXT_MODULE_SOFT_TARGET:
            assert filename in _KNOWN_SOFT_TARGET_OVERAGES, (
                f"{filename} is {line_count} lines -- over the {_RICHTEXT_MODULE_SOFT_TARGET}-line "
                "soft target with no recorded rationale in _KNOWN_SOFT_TARGET_OVERAGES. Either split "
                "it or add an explicit, reviewed entry explaining why not."
            )
