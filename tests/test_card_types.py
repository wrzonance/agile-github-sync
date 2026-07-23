"""Unit tests for card_types.py's pure boundary invariants (issue #82).

Fully pure/zero-mock -- no network, no gh, no agileplace I/O. These pin:

  - derive_card_type_name is pure and total: never raises regardless of `issue`'s issue_type/labels
    shape, and returns a stable result for a given input.
  - CARD_TYPE_RULES precedence is stable and first-match-wins: an issue matching more than one rule
    always resolves to the earliest rule's target, never a later one.
  - resolve_card_type_ids is idempotent and total over any list input: never raises on malformed
    entries, and calling it twice on the same input yields an equal result both times.
  - _decide is a pure function of its four inputs only -- no hidden state, same inputs always
    produce the same CardTypeDecision.
  - Both "derived is None" (unmatched) and "by_name.get(derived) is None" (unresolved name) leave
    the caller's issues_state[url]["type"] completely untouched -- sync_card_type must not persist
    a new base in either case.
  - reverse_seed_for_card_type is total: every card type name (mapped, unmapped, or None) returns a
    ReverseSeed, never raises.

Run: pytest -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from card_types import (  # noqa: E402
    CARD_TYPE_RULES,
    CardTypeDecision,
    CardTypeRule,
    ResolvedCardTypes,
    ReverseSeed,
    _decide,
    card_type_title,
    derive_card_type_name,
    op_type,
    resolve_card_type_ids,
    reverse_seed_for_card_type,
    sync_card_type,
    validate_reverse_issue_type,
)


# --- derive_card_type_name: pure + total -------------------------------------------------------

def test_derive_native_bug_maps_to_bug():
    assert derive_card_type_name({"issue_type": "Bug", "labels": []}) == "Bug"


def test_derive_native_feature_maps_to_new_feature():
    assert derive_card_type_name({"issue_type": "Feature", "labels": []}) == "New Feature"


def test_derive_label_documentation_maps_to_documentation():
    assert derive_card_type_name({"issue_type": None, "labels": ["documentation"]}) == "Documentation"


def test_derive_label_enhancement_maps_to_improvement():
    assert derive_card_type_name({"issue_type": None, "labels": ["enhancement"]}) == "Improvement"


def test_derive_label_bug_maps_to_bug():
    assert derive_card_type_name({"issue_type": None, "labels": ["bug"]}) == "Bug"


def test_derive_native_task_alone_is_unmatched():
    assert derive_card_type_name({"issue_type": "Task", "labels": []}) is None


def test_derive_type_epic_label_is_unmatched():
    """The board has no Epic card type -- type:epic issues derive to None (no write)."""
    assert derive_card_type_name({"issue_type": None, "labels": ["type:epic"]}) is None


def test_derive_no_signal_at_all_is_unmatched():
    assert derive_card_type_name({}) is None


def test_derive_never_raises_on_malformed_labels_shape():
    """labels as a non-iterable-of-str value (None, a bare string, an int) must not raise -- it is
    treated as no labels rather than crashing derivation."""
    assert derive_card_type_name({"issue_type": None, "labels": None}) is None
    assert derive_card_type_name({"issue_type": None, "labels": 42}) is None
    assert derive_card_type_name({"issue_type": None, "labels": [None, 1, "bug"]}) == "Bug"


def test_derive_is_pure_same_input_same_output():
    issue = {"issue_type": "Bug", "labels": ["bug", "enhancement"]}
    assert derive_card_type_name(issue) == derive_card_type_name(dict(issue))


# --- CARD_TYPE_RULES precedence: stable, first-match-wins ------------------------------------

def test_rules_check_native_issue_type_before_labels():
    """An issue with BOTH a native Feature type and a `bug` label resolves via the earlier
    (issue_type) rule, not the later (label) rule."""
    issue = {"issue_type": "Feature", "labels": ["bug"]}
    assert derive_card_type_name(issue) == "New Feature"


def test_rules_first_matching_label_wins_over_a_later_one():
    """documentation is listed before bug in CARD_TYPE_RULES -- an issue carrying both labels
    resolves to the earlier rule's target."""
    issue = {"issue_type": None, "labels": ["bug", "documentation"]}
    assert derive_card_type_name(issue) == "Documentation"


def test_card_type_rules_is_an_ordered_tuple_of_card_type_rule():
    assert isinstance(CARD_TYPE_RULES, tuple)
    assert all(isinstance(rule, CardTypeRule) for rule in CARD_TYPE_RULES)
    assert len(CARD_TYPE_RULES) >= 5


# --- resolve_card_type_ids: idempotent + total over any list input -----------------------------

def test_resolve_maps_eligible_types_by_title():
    card_types = [
        {"id": "t-bug", "title": "Bug", "isCardType": True},
        {"id": "t-doc", "title": "Documentation", "isCardType": True},
        {"id": "t-imp", "title": "Improvement", "isCardType": True},
        {"id": "t-feat", "title": "New Feature", "isCardType": True},
        {"id": "t-sub", "title": "Subtask", "isCardType": False},
    ]
    resolved = resolve_card_type_ids(card_types)
    assert isinstance(resolved, ResolvedCardTypes)
    assert dict(resolved.by_name) == {
        "Bug": "t-bug",
        "Documentation": "t-doc",
        "Improvement": "t-imp",
        "New Feature": "t-feat",
    }
    assert resolved.warnings == ()


def test_resolve_excludes_ineligible_non_card_types_and_warns():
    """A type-only-for-tasks entry (isCardType falsy) is never an eligible match, even if its title
    matches a needed name exactly -- it warns instead of silently resolving."""
    card_types = [{"id": "t-bug", "title": "Bug", "isCardType": False}]
    resolved = resolve_card_type_ids(card_types)
    assert resolved.by_name == {}
    assert any("Bug" in w for w in resolved.warnings)


def test_resolve_warns_once_per_unresolved_name_and_skips_it():
    resolved = resolve_card_type_ids([])
    needed = {"Bug", "New Feature", "Documentation", "Improvement"}
    assert resolved.by_name == {}
    assert len(resolved.warnings) == len(needed)


def test_resolve_warns_on_ambiguous_duplicate_titles():
    card_types = [
        {"id": "t-bug-1", "title": "Bug", "isCardType": True},
        {"id": "t-bug-2", "title": "Bug", "isCardType": True},
    ]
    resolved = resolve_card_type_ids(card_types)
    assert "Bug" not in resolved.by_name
    assert any("Bug" in w and "ambiguous" in w for w in resolved.warnings)


def test_resolve_never_raises_on_malformed_entries():
    card_types = [
        {"id": None, "title": "Bug", "isCardType": True},
        {"title": None, "isCardType": True},
        {"id": "t-x", "title": "", "isCardType": True},
    ]
    resolved = resolve_card_type_ids(card_types)
    assert isinstance(resolved, ResolvedCardTypes)


def test_resolve_never_raises_on_non_dict_list_elements():
    """The docstring promises totality over 'any list input' -- a non-dict element (a bare string,
    None, an int) must be skipped like any other ineligible entry, never raise AttributeError."""
    resolved = resolve_card_type_ids(["not-a-dict", None, 123, {"id": "t-bug", "title": "Bug",
                                                                  "isCardType": True}])
    assert isinstance(resolved, ResolvedCardTypes)
    assert resolved.by_name["Bug"] == "t-bug"


def test_resolve_is_idempotent_over_the_same_input():
    card_types = [{"id": "t-bug", "title": "Bug", "isCardType": True}]
    first = resolve_card_type_ids(card_types)
    second = resolve_card_type_ids(card_types)
    assert first == second


def test_resolve_does_not_mutate_its_input():
    card_types = [{"id": "t-bug", "title": "Bug", "isCardType": True}]
    snapshot = [dict(ct) for ct in card_types]
    resolve_card_type_ids(card_types)
    assert card_types == snapshot


# --- _decide: pure function of its four inputs only --------------------------------------------

def test_decide_derived_none_is_a_pure_no_op():
    decision = _decide("Bug", None, "Bug", {"Bug": "t-bug"})
    assert decision == CardTypeDecision(op=None, warn=None, update_base=False, new_base=None)


def test_decide_current_matches_derived_advances_base_with_no_op():
    decision = _decide(None, "Bug", "Bug", {"Bug": "t-bug"})
    assert decision == CardTypeDecision(op=None, warn=None, update_base=True, new_base="Bug")


def test_decide_derived_changed_and_resolves_queues_op_and_advances_base():
    decision = _decide("Documentation", "Bug", "Documentation", {"Bug": "t-bug"})
    assert decision.op == op_type("t-bug")
    assert decision.warn is None
    assert decision.update_base is True
    assert decision.new_base == "Bug"


def test_decide_derived_changed_but_unresolved_name_does_nothing():
    decision = _decide("Documentation", "Bug", "Documentation", {})
    assert decision == CardTypeDecision(op=None, warn=None, update_base=False, new_base=None)


def test_decide_manual_edit_detected_warns_without_writing():
    decision = _decide("Bug", "Bug", "Documentation", {"Bug": "t-bug"})
    assert decision.op is None
    assert decision.update_base is False
    assert decision.new_base is None
    assert decision.warn is not None
    assert "Bug" in decision.warn and "Documentation" in decision.warn


def test_decide_is_pure_same_four_inputs_same_output():
    args = ("Bug", "Documentation", "Bug", {"Documentation": "t-doc"})
    assert _decide(*args) == _decide(*args)


# --- unmatched/unresolved leave issues_state[url]["type"] untouched -----------------------------

def _noop_queue(card, ops, note):
    raise AssertionError("queue should not be called when no op is decided")


def test_sync_card_type_leaves_base_untouched_when_unmatched():
    issue = {"url": "https://github.com/o/r/issues/1", "issue_type": None, "labels": []}
    card = {"type": {"id": "t-bug", "title": "Bug"}}
    issues_state = {issue["url"]: {"type": "Bug"}}

    sync_card_type("cfg", True, issue, card, {}, issues_state, _noop_queue)

    assert issues_state[issue["url"]]["type"] == "Bug"


def test_sync_card_type_leaves_base_untouched_when_name_unresolved():
    issue = {"url": "https://github.com/o/r/issues/2", "issue_type": "Bug", "labels": []}
    card = {"type": {"id": "t-doc", "title": "Documentation"}}
    issues_state = {issue["url"]: {"type": "Documentation"}}

    sync_card_type("cfg", True, issue, card, {}, issues_state, _noop_queue)

    assert issues_state[issue["url"]]["type"] == "Documentation"


def test_sync_card_type_never_mutates_issue_card_or_by_name():
    issue = {"url": "https://github.com/o/r/issues/3", "issue_type": "Bug", "labels": []}
    card = {"type": {"id": "t-doc", "title": "Documentation"}}
    by_name = {"Bug": "t-bug"}
    issues_state = {issue["url"]: {"type": "Documentation"}}
    issue_snapshot, card_snapshot, by_name_snapshot = dict(issue), dict(card), dict(by_name)
    queued = []

    sync_card_type("cfg", True, issue, card, by_name, issues_state,
                    lambda c, ops, note: queued.append((c, ops, note)))

    assert issue == issue_snapshot
    assert card == card_snapshot
    assert by_name == by_name_snapshot
    assert queued == [(card, [op_type("t-bug")], "type->Bug")]
    assert issues_state[issue["url"]]["type"] == "Bug"


def test_sync_card_type_does_not_persist_base_when_apply_is_false():
    """Even when the decision would advance the base, a dry run (apply=False) must never persist
    it -- mirrors sync_metadata/sync_dates's `if apply:` gating."""
    issue = {"url": "https://github.com/o/r/issues/4", "issue_type": "Bug", "labels": []}
    card = {"type": {"id": "t-bug", "title": "Bug"}}
    issues_state = {issue["url"]: {"type": None}}

    sync_card_type("cfg", False, issue, card, {"Bug": "t-bug"}, issues_state, _noop_queue)

    assert issues_state[issue["url"]]["type"] is None


# --- card_type_title / op_type (relocated per finding #7) ---------------------------------------

def test_card_type_title_reads_nested_title():
    assert card_type_title({"type": {"id": "t1", "title": "Bug"}}) == "Bug"


def test_card_type_title_missing_type_is_none_without_warning(capsys):
    assert card_type_title({}) is None
    assert capsys.readouterr().out == ""


def test_card_type_title_non_dict_type_warns_and_returns_none(capsys):
    assert card_type_title({"id": "c1", "type": "Bug"}) is None
    assert "WARN" in capsys.readouterr().out


def test_card_type_title_non_string_title_warns_and_returns_none(capsys):
    assert card_type_title({"id": "c1", "type": {"title": 5}}) is None
    assert "WARN" in capsys.readouterr().out


def test_card_type_title_blank_title_normalizes_to_none():
    assert card_type_title({"type": {"title": "   "}}) is None


def test_op_type_shape():
    assert op_type("t-bug") == {"op": "replace", "path": "/typeId", "value": "t-bug"}


# --- reverse_seed_for_card_type: total -----------------------------------------------------------

def test_reverse_seed_bug_maps_to_native_bug():
    assert reverse_seed_for_card_type("Bug") == ReverseSeed(issue_type="Bug", label=None)


def test_reverse_seed_new_feature_maps_to_native_feature():
    assert reverse_seed_for_card_type("New Feature") == ReverseSeed(issue_type="Feature", label=None)


def test_reverse_seed_improvement_maps_to_enhancement_label():
    assert reverse_seed_for_card_type("Improvement") == ReverseSeed(issue_type=None, label="enhancement")


def test_reverse_seed_documentation_maps_to_documentation_label():
    assert reverse_seed_for_card_type("Documentation") == ReverseSeed(issue_type=None, label="documentation")


def test_reverse_seed_other_work_maps_to_native_task():
    assert reverse_seed_for_card_type("Other Work") == ReverseSeed(issue_type="Task", label=None)


def test_reverse_seed_unmapped_name_returns_no_seed():
    assert reverse_seed_for_card_type("Risk / Issue") == ReverseSeed(issue_type=None, label=None)
    assert reverse_seed_for_card_type("Subtask") == ReverseSeed(issue_type=None, label=None)
    assert reverse_seed_for_card_type("Nonsense") == ReverseSeed(issue_type=None, label=None)


def test_reverse_seed_none_input_returns_no_seed():
    assert reverse_seed_for_card_type(None) == ReverseSeed(issue_type=None, label=None)


# --- validate_reverse_issue_type ------------------------------------------------------------------

def test_validate_reverse_issue_type_passes_through_when_enabled():
    assert validate_reverse_issue_type("Bug", frozenset({"Task", "Bug", "Feature"})) == "Bug"


def test_validate_reverse_issue_type_blocks_when_not_enabled():
    assert validate_reverse_issue_type("Bug", frozenset({"Task"})) is None


def test_validate_reverse_issue_type_blocks_when_probe_failed():
    """org_types is None -- probe failed or was skipped (e.g. dry run) -- fails closed same as
    'type not enabled', not a separate code path."""
    assert validate_reverse_issue_type("Bug", None) is None


def test_validate_reverse_issue_type_none_issue_type_is_a_no_op():
    assert validate_reverse_issue_type(None, frozenset({"Bug"})) is None
