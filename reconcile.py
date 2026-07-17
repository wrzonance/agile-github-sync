"""Pure 3-way merge for bidirectional label<->tag sync. No I/O -- exhaustively unit-tested.

`base` is the last-synced set (the merge base persisted in .sync-state.json); `gh_now` and `ap_now` are
the current sets on each side. We compute what to add/remove on each side so both converge, and the new
base. Because we compare set MEMBERSHIP against a common base, adds (not in base) and removes (in base)
are disjoint, so the merge is deterministic and removals propagate both ways (unlike a naive union).

There is therefore no genuine per-item conflict at the membership level: an item cannot be both
"added since base" (absent from base) and "removed since base" (present in base). The GitHub-wins
tiebreak in the design only becomes relevant if we ever track value-level (not membership) changes;
that is out of scope here and documented as such.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Reconciled:
    gh_add: frozenset      # labels to add on GitHub
    gh_remove: frozenset   # labels to remove on GitHub
    ap_add: frozenset      # tags to add on AgilePlace
    ap_remove: frozenset   # tags to remove on AgilePlace
    new_base: frozenset    # converged set (next base)


def reconcile(base, gh_now, ap_now) -> Reconciled:
    base, gh_now, ap_now = set(base), set(gh_now), set(ap_now)
    added = (gh_now - base) | (ap_now - base)         # added on either side -> add everywhere
    removed = (base - gh_now) | (base - ap_now)       # removed on either side -> remove everywhere
    final = (base | added) - removed
    return Reconciled(
        gh_add=frozenset(final - gh_now),
        gh_remove=frozenset(gh_now - final),
        ap_add=frozenset(final - ap_now),
        ap_remove=frozenset(ap_now - final),
        new_base=frozenset(final),
    )


def reconcile_value(base, gh, ap):
    """3-way merge of a single OPTIONAL value (e.g. an issue's milestone, of which GitHub allows exactly
    one). Returns the resolved value; None means 'unset'. GitHub wins a genuine two-sided conflict --
    this is a real value-level conflict, unlike the membership merge above. The caller applies the
    result as a single set-operation on each side (no clear-then-set), avoiding data loss."""
    if gh == ap:
        return gh
    if gh != base and ap == base:
        return gh          # only GitHub changed
    if ap != base and gh == base:
        return ap          # only AgilePlace changed
    return gh              # both diverged from base -> GitHub wins
