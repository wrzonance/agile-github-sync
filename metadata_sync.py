"""Metadata (labels/milestone) and planned-date sync for issue #79.

sync.py was already at 908 lines (over the repo's 800-line hard cap) before this change -- issue
#79's actual ask, as opposed to #80's constant-raise re-anchor, is an honest extraction: pull the
genuinely-cohesive label/milestone/date reconciliation logic out into its own pure-ish module
rather than let sync.py keep absorbing it.

One cohesive domain lives here: computing the 3-way (base/GitHub/AgilePlace) reconcile deltas for
labels, milestone, and planned start/target dates, and queuing the resulting card mutations plus
the matching GitHub-side writes. Public surface: `sync_metadata`, `sync_dates`. Everything else
(`_label_set`, `_filter_gh_safe_labels`, `_card_milestones`, `_stale_milestone_tags`) is a private
helper shared only between those two entry points and stays module-private.

Moved verbatim (docstrings and bodies unchanged) from sync.py's prior lines 494-647; see issue #79.
"""
from __future__ import annotations

import agileplace
import ghkit
import ghproject
from reconcile import reconcile, reconcile_value
from stages import issue_custom_id

MS_PREFIX = "milestone:"


def _label_set(labels, ignore: frozenset) -> set[str]:
    return {l for l in labels if l not in ignore and not l.startswith(MS_PREFIX)}


def _filter_gh_safe_labels(names: frozenset[str], *, key: str, action: str) -> frozenset[str]:
    """Subset of names safe to pass to gh's --add-label/--remove-label; prints one WARN per rejected
    name (comma, or a '"' anywhere -- gh CSV-splits the flag value) naming the offender and side."""
    safe = frozenset(n for n in names if ghkit.is_gh_label_safe(n))
    for bad in sorted(names - safe):
        print(f"WARN  [{key}] label {bad!r} contains a comma or a double quote -- gh CSV-splits "
              f"--add-label/--remove-label values; skipping {action} on GitHub")
    return safe


def _card_milestones(card: dict, base: str | None, gh: str | None) -> tuple[str | None, set[str]]:
    """(selected current milestone value, all raw milestone: tags incl. empty-suffix ones for cleanup).

    Selection over the card's non-empty milestone: suffixes is by PROVENANCE, not sort order:
      - zero suffixes       -> None
      - `base` among them    -> base (nothing changed AP-side this pass; a coexisting extra tag is
                                cleanup fodder, never a same-pass override -- closes the
                                milestone:0.0.0 downgrade abuse vector from issue #7's 'Why')
      - else `gh` among them -> gh (same rationale, GitHub-side anchor)
      - else                 -> sorted(suffixes)[0] -- tie-break used ONLY among tags matching
                                 NEITHER anchor, i.e. genuinely new/fully-unanchored AP-side values;
                                 never used to arbitrate an anchored tag against an unanchored one.
    Pure function of its three inputs; no I/O. Determinism is a property of the base/gh-anchor rule
    (and, only in the fully-unanchored case, the sort tie-break) -- NOT, as the prior docstring
    claimed, a virtue of sorting itself; sorting alone was the actual bug (issue #7).
    """
    tags = {t for t in agileplace.card_tags(card) if t.startswith(MS_PREFIX)}
    suffixes = {t[len(MS_PREFIX):] for t in tags if t[len(MS_PREFIX):]}
    if not suffixes:
        return None, tags
    if base is not None and base in suffixes:
        return base, tags
    if gh is not None and gh in suffixes:
        return gh, tags
    return sorted(suffixes)[0], tags


def _stale_milestone_tags(ms_tags: set[str], old_base: str | None, new_ms: str | None) -> frozenset[str]:
    """Subset of ms_tags (the 2nd _card_milestones return) safe to remove via ops_tag_remove this
    pass. Postcondition: result <= ms_tags always -- never proposes removing a tag that was never on
    the card. Included:
      - new_ms is None (reconcile resolved the milestone to UNSET this pass -- GitHub cleared it, or
        it was never set): EVERY milestone: tag is stale. With no current milestone there is nothing
        legitimate for any tag to represent, and leaving one behind lets it resurrect the cleared
        value on a later pass -- once the base is persisted as None the leftover looks like a fresh,
        unanchored AgilePlace value and gets pushed straight back onto GitHub, silently undoing the
        user's deletion (the cross-run resurrection Codex flagged). A tag cannot be a genuine pending
        upgrade here: if it were, reconcile_value would have resolved new_ms TO that value, not None.
      - otherwise (new_ms is a real value), the conservative set:
          - every empty-suffix tag ("milestone:" alone) -- always stale, carries no value
          - f"{MS_PREFIX}{old_base}" iff ALL THREE hold: old_base is not None, old_base != new_ms (the
            base has been confirmed superseded THIS pass), AND that literal tag is a member of ms_tags
            (it may legitimately not be, e.g. the base was never re-tagged onto this card)
        and deliberately EXCLUDES any other non-empty-suffix tag (one matching neither the old base
        nor the new value): while a real milestone still stands it cannot be told apart from a pending,
        ambiguous human edit by value alone, so it is preserved rather than destroyed -- risking the
        deletion of a genuine, not-yet-reconciled upgrade (issue #7).
    """
    if new_ms is None:
        return frozenset(ms_tags)
    stale = {t for t in ms_tags if t == MS_PREFIX}
    old_tag = f"{MS_PREFIX}{old_base}" if old_base is not None else None
    if old_tag is not None and old_base != new_ms and old_tag in ms_tags:
        stale.add(old_tag)
    return frozenset(stale)


def sync_metadata(cfg, apply, issue, card, ignore, issues_state, queue) -> None:
    url = issue["url"]
    prev = issues_state[url]

    gh_labels = _label_set(issue["labels"], ignore)
    ap_label_tags = _label_set((t for t in agileplace.card_tags(card) if not t.startswith(MS_PREFIX)), ignore)
    base_labels = _label_set(prev.get("labels", []), ignore)
    r = reconcile(base_labels, gh_labels, ap_label_tags)
    key = issue_custom_id(issue)

    gh_add_safe = _filter_gh_safe_labels(r.gh_add, key=key, action="add")
    gh_remove_safe = _filter_gh_safe_labels(r.gh_remove, key=key, action="remove")
    for item in sorted(gh_add_safe):
        ghkit.edit_label(cfg, apply, issue["number"], item, add=True)
    for item in sorted(gh_remove_safe):
        ghkit.edit_label(cfg, apply, issue["number"], item, add=False)
    # A name skipped from an add was never actually written to GitHub -> pull it back out of the new
    # base; a name skipped from a remove is still actually on GitHub -> keep it in the new base. The
    # two terms never overlap: gh_add/gh_remove are disjoint set-differences of the same final/gh_now
    # pair (reconcile.py), so a name can't be skipped from both an add and a remove in the same run.
    new_base = (r.new_base - (r.gh_add - gh_add_safe)) | (r.gh_remove - gh_remove_safe)
    tags_to_remove: set[str] = set(r.ap_remove)
    tag_ops = [agileplace.op_tag(t) for t in sorted(r.ap_add)]

    gh_ms = issue.get("milestone")
    ap_ms, ms_tags = _card_milestones(card, prev.get("milestone"), gh_ms)
    new_ms = reconcile_value(prev.get("milestone"), gh_ms, ap_ms)
    if new_ms != gh_ms:
        ghkit.set_milestone(cfg, apply, issue["number"], new_ms)
    desired_ms_tag = f"{MS_PREFIX}{new_ms}" if new_ms else None
    stale = _stale_milestone_tags(ms_tags, prev.get("milestone"), new_ms) - ({desired_ms_tag} - {None})
    if stale or (desired_ms_tag and desired_ms_tag not in ms_tags):
        tags_to_remove |= stale
        if desired_ms_tag and desired_ms_tag not in ms_tags:
            tag_ops.append(agileplace.op_tag(desired_ms_tag))

    tag_ops += agileplace.ops_tag_remove(card.get("tags") or [], tags_to_remove)
    if tag_ops:
        queue(card, tag_ops, "tags/milestone")
    if gh_add_safe or gh_remove_safe or r.ap_add or r.ap_remove or new_ms != gh_ms or tag_ops:
        print(f"meta  [{key}] labels gh+{len(gh_add_safe)}/-{len(gh_remove_safe)}"
              f" ap+{len(r.ap_add)}/-{len(r.ap_remove)} milestone={new_ms}")
    if apply:
        prev.update({"labels": sorted(new_base), "milestone": new_ms})


def sync_dates(cfg, apply, issue, card, pitem, field_meta, issues_state, queue) -> None:
    """Bidirectional planned dates (AgilePlace-wins) from an authoritative field-ID snapshot.

    Only a date whose Project field id is known is synced. main() skips this function entirely when
    the GraphQL date snapshot failed, so a read failure cannot be mistaken for a project-wide clear.

    Merge-base gating: the GH-side merge base (prev[kind]) only advances when the GitHub value is
    already correct (new == gh_date, nothing to write) or the write is confirmed to have happened
    (ghproject.set_project_date returned True). A silently-skipped write (e.g. item_id/field_id
    missing) must never advance the base -- doing so would mask the mismatch forever, since the next
    run would compare the base against a GitHub value it never actually reached. The AgilePlace-side
    queue write is unaffected by this gating -- it always fires when the AgilePlace value needs to
    change."""
    if not pitem:
        return
    prev = issues_state[issue["url"]]
    key = issue_custom_id(issue)
    item_id = pitem.get("item_id")
    for kind, field_id, ap_field in (("start", field_meta.get("start_field_id"), "plannedStart"),
                                     ("target", field_meta.get("target_field_id"), "plannedFinish")):
        if not field_id:
            continue
        gh_date = pitem.get(kind)
        ap_date = card.get(ap_field)
        new = reconcile_value(prev.get(kind), gh_date, ap_date, prefer="ap")
        gh_write_ok = True
        if new != gh_date:
            gh_write_ok = ghproject.set_project_date(cfg, apply, field_meta["project_id"], item_id,
                                                     field_id, new, field_meta.get("host"))
        if new != ap_date:
            queue(card, [agileplace.op_planned_date(ap_field, new)], f"{ap_field}={new}")
        if new != gh_date or new != ap_date:
            print(f"date  [{key}] {kind} -> {new or 'unset'}")
        if apply and gh_write_ok:
            prev[kind] = new
