"""Unit tests for ghkit's label-name safety guard, and for sync.py's reconcile-boundary filter that
keeps CSV-unsafe label names from ever reaching ghkit.edit_label.

gh's --add-label/--remove-label flag is a pflag StringSlice: it CSV-splits its value using Go's
encoding/csv Reader in its default (LazyQuotes=false) mode, so a label name containing a comma
anywhere, or a '"' anywhere (not just a leading one -- a bare quote inside an unquoted CSV field is
itself a parse error in that mode), would arrive at gh as multiple/garbled labels or fail to parse
rather than the one name the caller intended. These tests pin edit_label's guard clause and
is_gh_label_safe's pure/total contract at the module boundary -- no network, no gh CLI -- plus
sync._filter_gh_safe_labels and sync_metadata's persisted-merge-base arithmetic that must never
record a label as GitHub-side-applied when it was not actually written. Run: pytest -q
"""
import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ghkit import edit_label, is_gh_label_safe  # noqa: E402
from reconcile import Reconciled  # noqa: E402
from metadata_sync import _filter_gh_safe_labels, sync_metadata  # noqa: E402


# --- is_gh_label_safe: pure, total predicate ------------------------------

def test_safe_plain_label():
    assert is_gh_label_safe("bug") is True


def test_unsafe_label_with_comma():
    assert is_gh_label_safe("bug,feature") is False


def test_unsafe_label_leading_quote():
    assert is_gh_label_safe('"quoted') is False


def test_unsafe_label_with_internal_quote():
    # Go's encoding/csv Reader (LazyQuotes=false) rejects a bare '"' anywhere inside an unquoted
    # field, not just at position 0 -- a non-leading quote is just as CSV-parse-significant.
    assert is_gh_label_safe('quo"ted') is False


def test_safe_empty_string():
    assert is_gh_label_safe("") is True


def test_is_gh_label_safe_returns_bool_for_arbitrary_input():
    for name in ["", ",", '"', "a,b,c", '"""', "milestone:1.0", "a" * 200]:
        result = is_gh_label_safe(name)
        assert isinstance(result, bool)


# --- edit_label: guard clause raises before any gh call or DRY print -----

def test_edit_label_raises_on_comma_when_applying(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr("ghkit.run", lambda *a, **k: calls.append((a, k)))
    with pytest.raises(ValueError):
        edit_label({}, True, 5, "bug,feature", add=True)
    assert calls == []                       # never shelled out
    assert capsys.readouterr().out == ""      # never printed a gh/DRY line


def test_edit_label_raises_on_leading_quote_when_dry_run(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr("ghkit.run", lambda *a, **k: calls.append((a, k)))
    with pytest.raises(ValueError):
        edit_label({}, False, 5, '"bug', add=False)
    assert calls == []
    assert capsys.readouterr().out == ""      # never printed the DRY line either


def test_edit_label_still_works_for_safe_labels(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr("ghkit.run", lambda *a, **k: calls.append((a, k)))
    result = edit_label({}, True, 5, "bug", add=True)
    assert result is None
    assert len(calls) == 1
    assert capsys.readouterr().out.startswith("gh    issue 5 add-label bug")


def test_edit_label_dry_run_still_works_for_safe_labels(monkeypatch, capsys):
    """Regression pin: apply=False + a safe label is unchanged by the guard -- prints the DRY line,
    never shells out."""
    calls = []
    monkeypatch.setattr("ghkit.run", lambda *a, **k: calls.append((a, k)))
    result = edit_label({}, False, 5, "bug", add=False)
    assert result is None
    assert calls == []
    assert capsys.readouterr().out.startswith("DRY   gh issue edit 5 --remove-label 'bug'")


# --- _filter_gh_safe_labels: pure subset + one WARN per rejected name -----

def test_filter_gh_safe_labels_keeps_all_safe_names(capsys):
    names = frozenset({"bug", "feature"})
    result = _filter_gh_safe_labels(names, key="42", action="add")
    assert result == names
    assert capsys.readouterr().out == ""


def test_filter_gh_safe_labels_drops_unsafe_and_warns(capsys):
    names = frozenset({"bug", "a,b"})
    result = _filter_gh_safe_labels(names, key="42", action="add")
    assert result == frozenset({"bug"})
    out = capsys.readouterr().out
    assert "WARN" in out
    assert "42" in out
    assert "a,b" in out or "'a,b'" in out
    # NOTE: every WARN line mentions both "--add-label" and "--remove-label" (the flag names quoted
    # verbatim in the message), so a bare `"add" in out` check would pass regardless of which action
    # was actually threaded through. Anchor on the full action phrase instead.
    assert "skipping add on GitHub" in out


def test_filter_gh_safe_labels_one_warn_per_rejected_name(capsys):
    names = frozenset({"a,1", "b,2", "ok"})
    result = _filter_gh_safe_labels(names, key="k", action="remove")
    assert result == frozenset({"ok"})
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("WARN")]
    assert len(lines) == 2  # exactly one per rejected name, not per retry


# --- new_base arithmetic: pure, pinned directly against a hand-built -------
# --- Reconciled -- no sync_metadata/AgilePlace harness needed --------------

def test_new_base_arithmetic_excludes_skipped_add_keeps_skipped_remove():
    """Pins sync.py's `new_base = (r.new_base - (r.gh_add - gh_add_safe)) | (r.gh_remove -
    gh_remove_safe)` formula directly against a hand-built Reconciled: 'a,b' is a reconciled add that
    got filtered out (never written to GitHub) so it must NOT survive into new_base even though
    reconcile said so; 'x,y' is a reconciled remove that got filtered out (still on GitHub) so it
    must be added BACK into new_base even though reconcile dropped it."""
    r = Reconciled(
        gh_add=frozenset({"a,b", "ok-add"}),
        gh_remove=frozenset({"x,y", "ok-remove"}),
        ap_add=frozenset(),
        ap_remove=frozenset(),
        new_base=frozenset({"ok-add", "kept"}),  # reconcile's aspirational base: has ok-add, lacks x,y
    )
    gh_add_safe = frozenset({"ok-add"})       # "a,b" filtered out
    gh_remove_safe = frozenset({"ok-remove"})  # "x,y" filtered out

    new_base = (r.new_base - (r.gh_add - gh_add_safe)) | (r.gh_remove - gh_remove_safe)

    assert "a,b" not in new_base    # skipped add never landed on GitHub -> excluded
    assert "x,y" in new_base        # skipped remove is still actually on GitHub -> included
    assert new_base == frozenset({"ok-add", "kept", "x,y"})
    # r itself is never mutated by computing new_base
    assert r.new_base == frozenset({"ok-add", "kept"})
    assert r.gh_add == frozenset({"a,b", "ok-add"})
    assert r.gh_remove == frozenset({"x,y", "ok-remove"})


def test_new_base_arithmetic_noop_when_nothing_filtered(capsys):
    """When every reconciled label is gh-safe, the real _filter_gh_safe_labels returns each batch
    untouched, so the correction terms in sync.py's new_base formula collapse to empty and new_base
    equals r.new_base exactly -- the fix is a no-op for the common (all-safe) case.

    Unlike computing `r.gh_add - r.gh_add` (a set-algebra identity that is empty no matter what
    gh_add_safe should have been, so it can never catch a regression), this drives the actual filter
    function: a bug that dropped a safe name from gh_add_safe/gh_remove_safe would make gh_add_safe
    != r.gh_add and this assertion would fail."""
    r = Reconciled(
        gh_add=frozenset({"bug"}),
        gh_remove=frozenset({"stale"}),
        ap_add=frozenset(),
        ap_remove=frozenset(),
        new_base=frozenset({"bug", "kept"}),
    )
    gh_add_safe = _filter_gh_safe_labels(r.gh_add, key="k", action="add")
    gh_remove_safe = _filter_gh_safe_labels(r.gh_remove, key="k", action="remove")
    assert capsys.readouterr().out == ""  # nothing unsafe -> no WARN

    new_base = (r.new_base - (r.gh_add - gh_add_safe)) | (r.gh_remove - gh_remove_safe)
    assert new_base == r.new_base


# --- sync_metadata: unsafe labels never reach edit_label; merge base is ----
# --- corrected to reflect what actually happened on GitHub -----------------

def _issue(number=42, labels=None, milestone=None):
    return {
        "number": number,
        "title": f"[T{number}] Issue {number}",
        "labels": labels or [],
        "milestone": milestone,
        "url": f"https://github.com/o/r/issues/{number}",
    }


def _card(tags=None):
    return {"id": "c1", "tags": tags or []}


def _removed_tag_names(card_tags, ops):
    """Map index-based /tags/{i} remove ops (no `value` member -- issue #3) back to the tag names
    they targeted, using the card's original (pre-removal) tags list for the index lookup."""
    return {card_tags[int(op["path"].rsplit("/", 1)[1])]
            for op in ops if op.get("op") == "remove" and op["path"].startswith("/tags/")}


def test_sync_metadata_skips_unsafe_labels_and_fixes_merge_base(monkeypatch, capsys):
    """base has an unsafe label GitHub still carries ('x,y'); AgilePlace introduces a new unsafe
    label ('a,b'). Reconcile would want to add 'a,b' on GitHub and remove 'x,y' from GitHub -- both
    unsafe, so both must be skipped, edit_label must never be called, and the persisted merge base
    must reflect reality: 'a,b' never landed on GitHub (must NOT be in the new base) and 'x,y' was
    never actually removed from GitHub (must STILL be in the new base)."""
    issue = _issue(labels=["x,y"])
    card = _card(tags=["a,b"])
    issues_state = {issue["url"]: {"labels": ["x,y"], "milestone": None}}

    calls = []
    monkeypatch.setattr("ghkit.edit_label", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr("ghkit.set_milestone", lambda *a, **k: calls.append(("set_milestone", a, k)))

    queued = []
    sync_metadata({}, True, issue, card, frozenset(), issues_state,
                  lambda c, ops, note: queued.append((c, ops, note)))

    assert calls == []  # unsafe names never reached ghkit
    prev = issues_state[issue["url"]]
    assert "a,b" not in prev["labels"]   # skipped add -> never actually on GitHub -> not in base
    assert "x,y" in prev["labels"]       # skipped remove -> still actually on GitHub -> stays in base

    out = capsys.readouterr().out
    warn_lines = [line for line in out.splitlines() if line.startswith("WARN")]
    assert len(warn_lines) == 2
    add_warn = next(line for line in warn_lines if "a,b" in line)      # skipped add -> must say "skipping add"
    remove_warn = next(line for line in warn_lines if "x,y" in line)   # skipped remove -> must say "skipping remove"
    # NOTE: every WARN line mentions both "--add-label" and "--remove-label" (the flag names quoted
    # verbatim in the message), so a bare `"add" in line` / `"remove" in line` check would pass no
    # matter which batch a name came from. Anchor on the actual action phrase instead.
    assert "skipping add on GitHub" in add_warn
    assert "skipping remove on GitHub" in remove_warn


def test_sync_metadata_mixed_safe_and_unsafe_labels_in_same_batch(monkeypatch, capsys):
    """Reconcile can legitimately put a safe AND an unsafe name in the SAME add batch and the SAME
    remove batch in one run (AgilePlace both drops an unsafe tag and keeps a safe one is not needed
    here -- it's enough that GitHub simultaneously carries a stale unsafe label and a stale safe
    label while AgilePlace introduces a new unsafe tag and a new safe tag). This drives the real
    reconcile -> _filter_gh_safe_labels -> ghkit.edit_label wiring end-to-end and pins that the safe/
    unsafe names actually passed to edit_label are EXACTLY {n : is_gh_label_safe(n)} within each
    batch -- not the whole batch (which would still write the unsafe name) and not empty (which would
    also drop the safe one)."""
    issue = _issue(labels=["x,y", "stale-safe"])
    card = _card(tags=["a,b", "new-safe"])
    issues_state = {issue["url"]: {"labels": ["x,y", "stale-safe"], "milestone": None}}

    calls = []
    monkeypatch.setattr("ghkit.edit_label", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr("ghkit.set_milestone", lambda *a, **k: None)

    sync_metadata({}, True, issue, card, frozenset(), issues_state, lambda c, ops, note: None)

    added = {a[3] for a, k in calls if k["add"]}
    removed = {a[3] for a, k in calls if not k["add"]}
    assert added == {"new-safe"}       # "a,b" (unsafe) skipped out of the add batch
    assert removed == {"stale-safe"}   # "x,y" (unsafe) skipped out of the remove batch

    prev = issues_state[issue["url"]]
    # stale-safe was actually removed on GitHub -> not in the new base; a,b never landed -> not in
    # the new base either; x,y is still actually on GitHub (skipped removal) -> stays in the base.
    assert set(prev["labels"]) == {"new-safe", "x,y"}

    out = capsys.readouterr().out
    warn_lines = [line for line in out.splitlines() if line.startswith("WARN")]
    assert len(warn_lines) == 2
    add_warn = next(line for line in warn_lines if "a,b" in line)
    remove_warn = next(line for line in warn_lines if "x,y" in line)
    assert "skipping add on GitHub" in add_warn
    assert "skipping remove on GitHub" in remove_warn


def test_sync_metadata_dry_run_never_mutates_state(monkeypatch, capsys):
    issue = _issue(labels=["x,y"])
    card = _card(tags=["a,b"])
    issues_state = {issue["url"]: {"labels": ["x,y"], "milestone": None}}
    before = dict(issues_state[issue["url"]])

    monkeypatch.setattr("ghkit.edit_label", lambda *a, **k: None)
    monkeypatch.setattr("ghkit.set_milestone", lambda *a, **k: None)

    sync_metadata({}, False, issue, card, frozenset(), issues_state, lambda c, ops, note: None)

    assert issues_state[issue["url"]] == before  # apply=False -> no state mutation


def test_sync_metadata_no_gh_rewrite_on_verified_repro_stale_leftover(monkeypatch, capsys):
    """Verified-repro acceptance criterion (issue #7): base=gh=ap='0.2.0', but the card also carries a
    stale leftover 'milestone:0.1.0' tag alongside the current 'milestone:0.2.0' tag. Because gh_ms
    already equals the reconciled new_ms, ghkit.set_milestone must NEVER be called -- this is the
    'no longer rewrites GitHub' bug this issue exists to fix. The stale 0.1.0 tag is also NOT queued
    for removal this pass (old_base == new_ms -> nothing superseded yet; see _stale_milestone_tags'
    documented never-destroy tradeoff) -- pinning that no spurious tag_ops for milestone are queued
    either."""
    issue = _issue(milestone="0.2.0")
    card = _card(tags=["milestone:0.2.0", "milestone:0.1.0"])
    issues_state = {issue["url"]: {"labels": [], "milestone": "0.2.0"}}

    ms_calls = []
    monkeypatch.setattr("ghkit.edit_label", lambda *a, **k: None)
    monkeypatch.setattr("ghkit.set_milestone", lambda *a, **k: ms_calls.append((a, k)))

    queued = []
    sync_metadata({}, True, issue, card, frozenset(), issues_state,
                  lambda c, ops, note: queued.append((c, ops, note)))

    assert ms_calls == []  # gh_ms already correct -> no GitHub rewrite
    card_tags = card["tags"]
    milestone_removes = {name for entry in queued for name in _removed_tag_names(card_tags, entry[1])}
    assert "milestone:0.1.0" not in milestone_removes  # stale leftover preserved this pass
    assert issues_state[issue["url"]]["milestone"] == "0.2.0"


def test_sync_metadata_no_gh_rewrite_on_coexisting_ambiguous_upgrade_tag(monkeypatch, capsys):
    """Coexisting-ambiguous worked example (issue #7): base=gh=ap='0.2.0', but the card ALSO carries an
    unrelated 'milestone:9.9' tag that matches neither anchor. Same shape as the verified-repro case
    (old_base == new_ms -> nothing superseded this pass) but with the 'other' tag looking like a
    genuine future upgrade rather than an old leftover -- the point being _stale_milestone_tags cannot
    (and must not) tell the two apart by value alone, so both must be preserved identically: no
    GitHub rewrite, and '9.9' is never queued for removal."""
    issue = _issue(milestone="0.2.0")
    card = _card(tags=["milestone:0.2.0", "milestone:9.9"])
    issues_state = {issue["url"]: {"labels": [], "milestone": "0.2.0"}}

    ms_calls = []
    monkeypatch.setattr("ghkit.edit_label", lambda *a, **k: None)
    monkeypatch.setattr("ghkit.set_milestone", lambda *a, **k: ms_calls.append((a, k)))

    queued = []
    sync_metadata({}, True, issue, card, frozenset(), issues_state,
                  lambda c, ops, note: queued.append((c, ops, note)))

    assert ms_calls == []  # gh_ms already correct -> no GitHub rewrite
    card_tags = card["tags"]
    milestone_removes = {name for entry in queued for name in _removed_tag_names(card_tags, entry[1])}
    assert "milestone:9.9" not in milestone_removes  # ambiguous 'other' tag preserved, not destroyed
    assert issues_state[issue["url"]]["milestone"] == "0.2.0"


def test_sync_metadata_calls_set_milestone_on_fully_unanchored_upgrade(monkeypatch, capsys):
    """Fully-unanchored-upgrade worked example (issue #7): base=gh='0.2.0', but the card's ONLY
    milestone: tag is 'milestone:9.9' -- neither anchor is present on the card at all. There is
    nothing ambiguous to preserve here: '9.9' is the sole candidate, _card_milestones selects it via
    the fully-unanchored tie-break, reconcile_value should carry it through as the new value, and
    since it differs from gh_ms ('0.2.0') ghkit.set_milestone MUST be called with '9.9' -- this is the
    genuine-upgrade side of the ambiguity that the never-destroy design must still let through."""
    issue = _issue(milestone="0.2.0")
    card = _card(tags=["milestone:9.9"])
    issues_state = {issue["url"]: {"labels": [], "milestone": "0.2.0"}}

    ms_calls = []
    monkeypatch.setattr("ghkit.edit_label", lambda *a, **k: None)
    monkeypatch.setattr("ghkit.set_milestone", lambda *a, **k: ms_calls.append((a, k)))

    sync_metadata({}, True, issue, card, frozenset(), issues_state, lambda c, ops, note: None)

    assert len(ms_calls) == 1
    args, _ = ms_calls[0]
    assert args[3] == "9.9"  # set_milestone(cfg, apply, number, title) -> title is the 4th arg
    assert issues_state[issue["url"]]["milestone"] == "9.9"


def test_sync_metadata_cleared_milestone_not_resurrected_next_pass(monkeypatch, capsys):
    """Cross-run deletion resurrection (Codex-flagged, issue #7 class): base=gh=ap='0.2.0' with a stale
    'milestone:0.1.0' leftover also on the card. The user then CLEARS the GitHub milestone. Pass 1 must
    honor the clear (new_ms=None, base persists None) AND remove every milestone: tag from the card --
    not just the old-base tag -- so pass 2 finds nothing to push back. Without wiping the leftover, pass
    2 sees '0.1.0' as the sole unanchored AgilePlace value and calls set_milestone('0.1.0'), silently
    undoing the deletion. This pins that pass 2 makes NO set_milestone call."""
    issue = _issue(milestone=None)  # user cleared it on GitHub
    card_tags = ["milestone:0.2.0", "milestone:0.1.0"]
    card = _card(tags=list(card_tags))
    issues_state = {issue["url"]: {"labels": [], "milestone": "0.2.0"}}

    ms_calls = []
    monkeypatch.setattr("ghkit.edit_label", lambda *a, **k: None)
    monkeypatch.setattr("ghkit.set_milestone", lambda *a, **k: ms_calls.append((a, k)))

    # PASS 1 — capture queued tag ops so we can apply the AgilePlace-side removals to the card.
    queued = []
    sync_metadata({}, True, issue, card, frozenset(), issues_state,
                  lambda c, ops, note: queued.append(ops))
    assert ms_calls == []  # gh already cleared -> no rewrite this pass either
    assert issues_state[issue["url"]]["milestone"] is None  # clear persisted as the new base
    removed = {name for ops in queued for name in _removed_tag_names(card_tags, ops)}
    # BOTH milestone tags must be queued for removal -- the leftover is not spared once new_ms is None
    assert removed == set(card_tags)
    card_after = {"id": card["id"], "tags": [t for t in card_tags if t not in removed]}

    # PASS 2 — nothing changed on GitHub (still cleared); the cleaned card must not resurrect anything.
    sync_metadata({}, True, issue, card_after, frozenset(), issues_state, lambda c, ops, note: None)
    assert ms_calls == []  # <-- resurrection would show up here as set_milestone('0.1.0')
    assert issues_state[issue["url"]]["milestone"] is None


def test_sync_metadata_dry_run_never_mutates_state_with_milestone_tags(monkeypatch, capsys):
    """Regression pin for the milestone-block rewrite: apply=False must still never mutate
    issues_state, even when the card carries multiple milestone: tags that now flow through
    _card_milestones/_stale_milestone_tags instead of the old blunt removal set."""
    issue = _issue(milestone="9.9")
    card = _card(tags=["milestone:0.2.0", "milestone:"])
    issues_state = {issue["url"]: {"labels": [], "milestone": "0.2.0"}}
    before = dict(issues_state[issue["url"]])

    monkeypatch.setattr("ghkit.edit_label", lambda *a, **k: None)
    monkeypatch.setattr("ghkit.set_milestone", lambda *a, **k: None)

    sync_metadata({}, False, issue, card, frozenset(), issues_state, lambda c, ops, note: None)

    assert issues_state[issue["url"]] == before  # apply=False -> no state mutation


# --- sync_metadata boundary invariants (issue #3, Task 2/4 wiring): no input --
# --- mutation, tags_to_remove is a fresh local accumulator each call, and -----
# --- combined add+remove tag ops are queued as exactly one PATCH per card ----

def test_sync_metadata_never_mutates_card_input(monkeypatch, capsys):
    """sync_metadata must never mutate the `card` dict it's handed -- callers (sync.py's card_by_url/
    card_by_cid caches, and the pending-ops accumulator keyed by the same object) rely on the exact
    same card surviving unchanged for the rest of the run. Both a tag add (ap_add) and a tag remove
    (ap_remove) fire in this scenario, exercising both op-builders' read paths without ever writing
    back into `card`."""
    issue = _issue(labels=["new-label"])
    card = _card(tags=["stale-tag"])
    before = copy.deepcopy(card)
    issues_state = {issue["url"]: {"labels": ["stale-tag"], "milestone": None}}

    monkeypatch.setattr("ghkit.edit_label", lambda *a, **k: None)
    monkeypatch.setattr("ghkit.set_milestone", lambda *a, **k: None)

    sync_metadata({}, True, issue, card, frozenset(), issues_state, lambda c, ops, note: None)

    assert card == before             # no key added/changed/removed
    assert card["tags"] == ["stale-tag"]  # the raw tags array itself is untouched, not just re-equal


def test_sync_metadata_tags_to_remove_is_fresh_each_call(monkeypatch, capsys):
    """tags_to_remove is accumulated into a `set()` local to sync_metadata's own call frame -- never a
    shared/mutable default that could leak removals computed for one issue's card into the next call.
    Call 1 (issue A) genuinely removes a stale tag; call 2 (issue B, an unrelated card that needs no
    removes at all) must queue zero remove ops -- proving call 1's accumulator did not survive into
    call 2."""
    monkeypatch.setattr("ghkit.edit_label", lambda *a, **k: None)
    monkeypatch.setattr("ghkit.set_milestone", lambda *a, **k: None)

    issue_a = _issue(number=1, labels=["new-label"])
    card_a = _card(tags=["stale-tag"])
    issues_state = {issue_a["url"]: {"labels": ["stale-tag"], "milestone": None}}

    queued_a = []
    sync_metadata({}, True, issue_a, card_a, frozenset(), issues_state,
                  lambda c, ops, note: queued_a.append(ops))
    assert any(op.get("op") == "remove" for ops in queued_a for op in ops)  # call 1 really removed

    issue_b = _issue(number=2, labels=["keep"])
    card_b = _card(tags=["keep"])
    issues_state[issue_b["url"]] = {"labels": ["keep"], "milestone": None}

    queued_b = []
    sync_metadata({}, True, issue_b, card_b, frozenset(), issues_state,
                  lambda c, ops, note: queued_b.append(ops))

    assert queued_b == []  # nothing to add/remove for issue B -> no leakage from call 1's accumulator


def test_sync_metadata_queues_combined_add_and_remove_as_one_patch(monkeypatch, capsys):
    """When the same pass needs both a tag add (ap_add) and a tag remove (ap_remove), sync_metadata
    must combine them into ONE tag_ops list and call queue() exactly once for tags/milestone -- never
    split across an add-queue call and a separate remove-queue call -- so the batch collapses into a
    single versioned PATCH per card (agileplace.patch_card) rather than fragmenting ops that a
    fragmented caller could accidentally interleave with other cards' queue() calls."""
    issue = _issue(labels=["new-label"])
    card = _card(tags=["stale-tag"])
    issues_state = {issue["url"]: {"labels": ["stale-tag"], "milestone": None}}

    monkeypatch.setattr("ghkit.edit_label", lambda *a, **k: None)
    monkeypatch.setattr("ghkit.set_milestone", lambda *a, **k: None)

    queue_calls = []
    sync_metadata({}, True, issue, card, frozenset(), issues_state,
                  lambda c, ops, note: queue_calls.append((c, ops, note)))

    tag_calls = [call for call in queue_calls if call[2] == "tags/milestone"]
    assert len(tag_calls) == 1  # exactly one queue() call carries the tag ops for this card
    _, ops, _ = tag_calls[0]
    assert {op["op"] for op in ops} == {"add", "remove"}         # both kinds combined in that one call
    assert sum(1 for op in ops if op["op"] == "add") == 1
    assert sum(1 for op in ops if op["op"] == "remove") == 1


def test_sync_metadata_backward_compatible_on_safe_labels(monkeypatch, capsys):
    """No comma-or-leading-quote names anywhere -> identical behavior to before this fix: edit_label
    called for the genuinely reconciled adds/removes, base updated to the full reconciled set."""
    issue = _issue(labels=["bug"])
    card = _card(tags=["feature"])
    issues_state = {issue["url"]: {"labels": [], "milestone": None}}

    calls = []
    monkeypatch.setattr("ghkit.edit_label", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr("ghkit.set_milestone", lambda *a, **k: None)

    sync_metadata({}, True, issue, card, frozenset(), issues_state, lambda c, ops, note: None)

    assert len(calls) == 1  # only "feature" needs adding on GitHub ("bug" is already there)
    prev = issues_state[issue["url"]]
    assert set(prev["labels"]) == {"bug", "feature"}
    assert capsys.readouterr().out.count("WARN") == 0
