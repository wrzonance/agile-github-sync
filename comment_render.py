"""Comment provenance headers + body rendering (issue #66). Pure, no I/O.

Extracted from comment_sync.py (which co-locates the timestamp helper, planning core, and wiring
layer) once the draft-phase Codex fixes pushed that module over the repo's 800-line file cap. This
module owns the two mechanical, side-effect-free concerns the planner leans on:

- **Provenance prefixes** -- the exact ``comment by <author> on <platform>`` header every mirror
  carries, its pure builder, and the tolerant anchored parser that recovers it.
- **Rendering** -- translating a body across the GH-markdown / AgilePlace-HTML divide, prepending
  (or, for a reverse-edit, stripping) the provenance header, so the invariant "a provenance prefix
  appears exactly once, only on the mirror" holds in both sync directions.

comment_sync imports what it calls from here; nothing here imports comment_sync back (no cycle).
"""
from __future__ import annotations

from re import compile as _re_compile
from typing import Literal, NamedTuple

import richtext

# =================================================================================================
# Provenance prefixes -- exact wording from the issue/design doc, pure build + tolerant parse
# =================================================================================================

_PROVENANCE_TEMPLATES = {
    "gh": "comment by {author} on GitHub",
    "ap": "comment by {author} on Agile Place",
}
_PROVENANCE_LEAD = "comment by "
# Order matters: "on GitHub" is not a substring of "on Agile Place" or vice versa, so a first-match
# scan is unambiguous regardless of dict order -- pinned by test_comment_sync's parser suite.
_PROVENANCE_SUFFIXES = (
    (" on GitHub", "gh"),
    (" on Agile Place", "ap"),
)
# Strips leading whitespace and HTML wrapper tags (e.g. AP's "<p>") so an anchored match still finds
# the prefix even though AP renders comment bodies as HTML. Only the *leading* wrapper is stripped --
# trailing markup after the matched suffix is irrelevant since parsing stops at the first suffix hit.
_LEADING_WRAPPER_RE = _re_compile(r"^(?:\s|<[^>]+>)+")


class ProvenanceHeader(NamedTuple):
    """A parsed `build_provenance_prefix` header: which side the comment ORIGINATED on, and the
    origin author's display label (name/email/id -- whatever `_author_label` extracted at mirror
    time)."""
    origin_side: Literal["gh", "ap"]
    author_label: str


def build_provenance_prefix(origin_side: str, author_label: str) -> str:
    """The exact leading text every mirrored comment carries, naming the platform the ORIGINAL
    comment was written on -- not the platform this rendered text is about to be posted to. Pure,
    total for any origin_side in {"gh", "ap"}; an unrecognized origin_side is a caller bug and
    raises rather than emitting a header a parser could never round-trip."""
    template = _PROVENANCE_TEMPLATES.get(origin_side)
    if template is None:
        raise ValueError(f"build_provenance_prefix: origin_side must be 'gh' or 'ap', got {origin_side!r}")
    return template.format(author=author_label)


def parse_provenance_prefix(text: str) -> ProvenanceHeader | None:
    """Recovers the ProvenanceHeader from a rendered comment body, or None when `text` doesn't open
    with a recognizable prefix (a genuine human comment, or garbage). Anchored at the start (after
    stripping leading whitespace/HTML wrapper tags) rather than searched anywhere in the body, so a
    human comment that merely *mentions* "comment by X on GitHub" mid-sentence never false-positives
    as a mirror.

    Author-label extraction is a first-occurrence-of-suffix-literal split: everything between the
    "comment by " lead and the first matching " on GitHub"/" on Agile Place" suffix is the author
    label, taken verbatim (not itself parsed further). This assumes an author label never contains
    either suffix literal as a substring -- a near-zero-probability real-world case, accepted as-is
    rather than hardened with a different delimiter scheme (design doc finding #4)."""
    if not isinstance(text, str):
        return None
    stripped = _LEADING_WRAPPER_RE.sub("", text, count=1)
    if not stripped.startswith(_PROVENANCE_LEAD):
        return None
    remainder = stripped[len(_PROVENANCE_LEAD):]
    for suffix, origin_side in _PROVENANCE_SUFFIXES:
        idx = remainder.find(suffix)
        if idx == -1:
            continue
        author_label = remainder[:idx].strip()
        if not author_label:
            return None
        return ProvenanceHeader(origin_side=origin_side, author_label=author_label)
    return None


# =================================================================================================
# Body rendering -- translate across the markup divide, add/strip the provenance header
# =================================================================================================

def _translate_body(target_side: str, source_body: str) -> str:
    """Translate a comment body into `target_side`'s markup: GH markdown -> AgilePlace HTML for
    target 'ap', AgilePlace HTML -> GH markdown for target 'gh'. Source is always the opposite
    platform's format (every call site translates across the divide)."""
    if target_side == "ap":
        return richtext.markdown_to_leankit_html(source_body)
    return richtext.leankit_html_to_markdown(source_body)


# Inverse of _render_mirror_body's prefix placement, per mirror format (AP leads with
# `<p>comment by ... on ...</p>`, GH with `comment by ... on ...\n\n`). Reverse-syncing a human's
# edit of a mirror back to the prefix-less origin must strip this sync-authored header first, or the
# origin accumulates provenance decoration (issue #66 Codex P1 #2). Anchored at the start.
_PROVENANCE_HEADER = r"comment by .*? on (?:GitHub|Agile Place)"
_STRIP_AP_PREFIX_RE = _re_compile(r"^\s*<p>\s*" + _PROVENANCE_HEADER + r"\s*</p>")
_STRIP_GH_PREFIX_RE = _re_compile(r"^\s*" + _PROVENANCE_HEADER + r"[^\S\n]*\n+")


def _strip_provenance_prefix(mirror_side: str, body: str) -> str:
    """The `mirror_side` mirror body with its leading provenance header removed. Total: unchanged
    (empty for a non-string) when no recognizable header leads it."""
    if not isinstance(body, str):
        return ""
    pattern = _STRIP_AP_PREFIX_RE if mirror_side == "ap" else _STRIP_GH_PREFIX_RE
    return pattern.sub("", body, count=1)


def render_mirror_body(target_side: str, origin_side: str, author_label: str, source_body: str) -> str:
    """Translate `source_body` (living, right now, on the platform opposite `target_side`) into
    `target_side`'s format and prepend the provenance prefix naming `origin_side`/`author_label`.
    Translation direction is fully determined by `target_side` alone -- source and target are always
    opposite platforms at every call site (mirror_new, restore_mirror, and origin-drifted edits)."""
    prefix = build_provenance_prefix(origin_side, author_label)
    if target_side == "ap":
        return f"<p>{prefix}</p>{_translate_body('ap', source_body)}"
    return f"{prefix}\n\n{_translate_body('gh', source_body)}"


def render_drift_edit(origin_side: str, canonical_side: str, target_side: str, author_label: str,
                      canonical_body: str) -> str:
    """The body to write when propagating a drifted edit, preserving the invariant "a provenance
    prefix appears exactly once, only on the mirror":
    - target is the MIRROR (origin drifted): canonical is the prefix-less origin -> render a fresh
      mirror body (prefix + translation), like mirror_new.
    - target is the ORIGIN (mirror drifted): canonical is the mirror, whose body already carries the
      header -> STRIP it, translate back, write the origin prefix-less. Re-prefixing here
      double-stamped the header onto the human origin (issue #66 Codex P1 #2)."""
    if target_side == origin_side:
        mirror_side = canonical_side  # == _other_side(origin_side)
        return _translate_body(target_side, _strip_provenance_prefix(mirror_side, canonical_body))
    return render_mirror_body(target_side, origin_side, author_label, canonical_body)
