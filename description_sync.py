"""Pure GH<->AgilePlace description merge for issue #65. No I/O -- exhaustively unit-tested.

Two canonicalization helpers put each side's raw text into a stable, comparable "canonical
Markdown" form. richtext's HTML<->Markdown translation absorbs cosmetic round-trip variance (e.g.
`*bold*` vs `_bold_`-equivalent AgilePlace HTML) so it never registers as a spurious edit --
echo prevention falls out of comparing canonicalized values, never raw ones. `resolve_description`
is the 3-way merge itself: `base`/`ap_written_base` are the last-synced canonical values persisted
in .sync-state.json (see sync.py's issues_state); `gh_canonical`/`ap_canonical` are this run's
freshly canonicalized GitHub issue body and AgilePlace card description.

Conflict policy is warn-and-skip (issue #65's brainstormed decision, deliberately breaking the
AgilePlace-wins-dates precedent set by sync_dates): when BOTH sides changed since their own
reference point AND landed on genuinely different values, neither side is overwritten -- prose
edits are too costly to silently destroy. When both sides changed but converged on the identical
value (e.g. two people independently typing the same fix), that is not a conflict: there is
nothing to warn about and nothing left to write.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import NamedTuple

import agileplace
import ghkit
import richtext
from stages import issue_custom_id

# Appended to a truncated AgilePlace description so a reader knows the full text lives on GitHub.
# Exact text pinned by the issue #65 design doc -- config.py's DEFAULT_AP_DESCRIPTION_MAX_LENGTH
# docstring references this same constant by name.
TRUNCATION_MARKER = "…[truncated by sync — full text on GitHub]"


class DescriptionResolution(NamedTuple):
    """Result of one resolve_description() call.

    Invariant: `conflict` is True iff `write_gh` and `write_ap` are both False AND `warning` is not
    None AND `merged == (base or "")`. A genuine conflict never touches either side and always
    reports the OLD agreed value, never a partial or guessed merge. (Steady state also has both
    write flags False and `merged == (base or "")` -- `warning is None` is what tells the two
    apart.)
    """
    merged: str
    write_gh: bool
    write_ap: bool
    conflict: bool
    warning: str | None


def resolve_description(base: str | None, ap_written_base: str | None, gh_canonical: str,
                        ap_canonical: str) -> DescriptionResolution:
    """3-way merge of a GitHub issue body against an AgilePlace card description, both already
    canonicalized (see _canonicalize_gh_body / _canonicalize_ap_description below). Pure, total,
    no I/O.

    `base` is the full agreed canonical Markdown as of the last successful sync (None on a
    never-synced issue). `ap_written_base` is the canonical of what was actually WRITTEN to the
    card last time -- the two differ only when a prior run truncated an oversized description, so
    the shorter, truncated text left on the card is compared against its own prior truncated form,
    never against the full untruncated base. Without that split, a card still carrying last run's
    truncated text would look AP-side-edited on every subsequent run, forever.

    Change is detected independently per side against its own reference point; `None` normalizes
    to "" on both sides. Four outcomes:
      - neither side changed -> no write; `merged` is the unchanged base (steady state, including
        the truncated-steady-state where the card still carries last run's truncated text).
      - exactly one side changed -> that side's value propagates to the other; `merged` is the
        changed side's canonical text.
      - both changed but landed on the SAME value -> no conflict, no write (independently
        converged); `merged` becomes that shared value so the base can advance to it.
      - both changed to DIFFERENT values -> conflict: neither side is written, `merged` stays the
        old base, and `warning` names the conflict for a human to reconcile by hand.
    """
    base_norm = base or ""
    ap_written_norm = ap_written_base or ""
    gh_changed = gh_canonical != base_norm
    ap_changed = ap_canonical != ap_written_norm

    if not gh_changed and not ap_changed:
        return DescriptionResolution(base_norm, False, False, False, None)
    if gh_changed and not ap_changed:
        return DescriptionResolution(gh_canonical, False, True, False, None)
    if ap_changed and not gh_changed:
        return DescriptionResolution(ap_canonical, True, False, False, None)
    if gh_canonical == ap_canonical:
        return DescriptionResolution(gh_canonical, False, False, False, None)
    warning = ("description conflict: both the GitHub issue body and the AgilePlace card "
               "description changed since the last sync and now disagree -- leaving both sides "
               "untouched until a human reconciles them by hand")
    return DescriptionResolution(base_norm, False, False, True, warning)


def _canonicalize_gh_body(body: str | None) -> str:
    """Canonical Markdown for a GitHub issue body: a genuine md->html->md round trip absorbs
    cosmetic Markdown variance (e.g. equivalent emphasis-marker choices) that would otherwise
    register as a spurious edit every run. None/missing normalizes to "".

    Self-composition-idempotent: canonical(canonical(x)) == canonical(x). A second round trip
    starts from output that is already a fixed point of richtext's md->html->md translation, so
    re-canonicalizing it reproduces the same text."""
    return richtext.leankit_html_to_markdown(richtext.markdown_to_leankit_html(body or ""))


def _canonicalize_ap_description(description: str | None) -> str:
    """Canonical Markdown for an AgilePlace card description, which AgilePlace stores as HTML: one
    html->md translation. None/missing normalizes to "".

    NOT self-composition-idempotent: calling this twice in a row would feed its own Markdown
    output back in as though it were HTML -- a type mismatch, not a no-op, since richtext's HTML->
    Markdown walker parses stray Markdown punctuation as literal text rather than recognizing it.
    The real invariant is a round trip THROUGH HTML:
    `_canonicalize_ap_description(html) ==
     _canonicalize_ap_description(markdown_to_leankit_html(_canonicalize_ap_description(html)))`
    -- re-rendering the canonical Markdown back to HTML and canonicalizing THAT reproduces the
    same canonical Markdown."""
    return richtext.leankit_html_to_markdown(description or "")


def _snap_to_whitespace_boundary(markdown: str, index: int) -> int:
    """Snap a candidate cut `index` down to the nearest whitespace boundary at or before it, so a
    truncation cut never splits a word. Clamped to [0, len(markdown)] first, so callers can pass
    any binary-search midpoint without pre-validating it. Returns 0 when no whitespace precedes
    `index` (e.g. one giant unbroken token) -- a zero-length prefix is a valid degenerate result,
    never a negative slice."""
    index = max(0, min(index, len(markdown)))
    while index > 0 and not markdown[index - 1].isspace():
        index -= 1
    return index


def _truncate_for_agileplace(markdown: str, max_length: int) -> tuple[str, bool]:
    """Render `markdown` to AgilePlace HTML, truncating at a clean word boundary with
    TRUNCATION_MARKER appended if the render exceeds `max_length` characters. Returns
    (html, was_truncated).

    Binary-searches the markdown cut-length against rendered-HTML length (each candidate cut
    snapped to its preceding whitespace boundary, re-rendered once, with TRUNCATION_MARKER's
    length folded into the same length check) instead of shrinking one word at a time -- O(log n)
    renders instead of O(n). That distinction is the difference between milliseconds and 50+
    seconds on a realistically large GH issue body (up to 65,536 chars): a prior per-word shrink
    loop re-rendered the whole prefix on every single word removed.

    Never negative-length slices (see _snap_to_whitespace_boundary). A max_length too small to fit
    even TRUNCATION_MARKER degrades to a marker-only result rather than looping forever or raising
    -- the search floor is always a zero-length markdown prefix, which still renders cleanly."""
    full_html = richtext.markdown_to_leankit_html(markdown)
    if len(full_html) <= max_length:
        return full_html, False

    lo, hi = 0, len(markdown)
    best_cut = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        cut = _snap_to_whitespace_boundary(markdown, mid)
        candidate_len = len(richtext.markdown_to_leankit_html(markdown[:cut])) + len(TRUNCATION_MARKER)
        if candidate_len <= max_length:
            best_cut = cut
            lo = mid + 1
        else:
            hi = mid - 1

    final_html = richtext.markdown_to_leankit_html(markdown[:best_cut]) + TRUNCATION_MARKER
    return final_html, True


def sync_description(cfg: dict, apply: bool, issue: dict, card: dict, issues_state: dict,
                     queue: Callable) -> None:
    """Wire resolve_description's pure merge into GitHub/AgilePlace writes, with a coupled
    base-advance gate mirroring sync_dates' (issue #6) merge-base contract exactly: `desc_base` and
    `desc_ap_written` only ever advance TOGETHER, and only when `apply` is True AND the GitHub-side
    write is confirmed (`gh_write_ok`). `gh_write_ok` defaults to True whenever no GitHub write was
    needed at all (write_gh is False) -- there is nothing to have failed, same as sync_dates.

    The AgilePlace-side queue write is unconditional, also like sync_dates: it fires whenever
    write_ap is True regardless of apply or gh_write_ok, and its eventual success/failure at PATCH-
    flush time is not fed back into this gate -- a poisoned card's flush skip already holds the
    whole run's state per sync.py's own contract (see sync.py's "poisoned card(s) this run" branch).
    A write that is skipped, a dry run, or a failed GitHub write must never advance either field --
    and the two fields never advance independently of one another.

    A dry-run-only planned card (``card["_planOnly"]``) exists only as a synthetic snapshot for
    continuing this one dry run -- it has no server-side identity yet, so it never carries a
    'description' key. agileplace.card_description()'s lazy get_card() fallback would otherwise
    issue a live GET for an id that was never created on the server. Same convention sync.py's
    dependency-sync loop already uses for plan-only cards ("a fresh card has no server-side
    dependencies; never read a plan-only id") -- a fresh card likewise has no server-side
    description yet, so the AgilePlace side is treated as "" without any network read.
    """
    url = issue["url"]
    prev = issues_state[url]
    key = issue_custom_id(issue)

    gh_canonical = _canonicalize_gh_body(issue.get("body"))
    ap_description = "" if card.get("_planOnly") else agileplace.card_description(cfg, card)
    ap_canonical = _canonicalize_ap_description(ap_description)
    result = resolve_description(prev.get("desc_base"), prev.get("desc_ap_written"),
                                 gh_canonical, ap_canonical)

    if result.conflict:
        print(f"WARN  [{key}] {result.warning}")
        return

    gh_write_ok = True
    if result.write_gh:
        gh_write_ok = ghkit.edit_issue_body(cfg, apply, issue["number"], result.merged)

    new_ap_written = ap_canonical
    if result.write_ap:
        html, _ = _truncate_for_agileplace(result.merged, cfg["ap_description_max_length"])
        queue(card, [agileplace.op_description(html)], "description")
        new_ap_written = _canonicalize_ap_description(html)

    if result.write_gh or result.write_ap:
        print(f"desc  [{key}] gh={result.write_gh} ap={result.write_ap}")

    if apply and gh_write_ok:
        prev["desc_base"] = result.merged
        prev["desc_ap_written"] = new_ap_written
