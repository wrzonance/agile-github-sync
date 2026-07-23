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

from typing import NamedTuple

import richtext


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
