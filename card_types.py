"""Card-type derivation, resolution, and drift decision for issue #82. No I/O -- exhaustively
unit-tested, fully pure/zero-mock (mirrors card_coherence.py's and stages.py's posture).

Direction is GH->AP with drift warning: GitHub's native issue type + labels are authoritative for
the derived card type; a manual AgilePlace-side type change is never silently stomped -- it WARNs
and re-aligns only once the GitHub side changes again (see _decide's branch table). Reverse mapping
(AP card type -> new GitHub issue's native type/label) is intake-only -- the one path where the
inverse isn't ambiguous, because a freshly-promoted card has no prior GitHub state to conflict with.

card_type_title and op_type live HERE rather than in agileplace.py, even though both are plain
dict readers/builders with no agileplace-internal dependency: agileplace.py measured 805/800 lines
(over the repo's own 800-line hard cap) before this issue started, and has no enforced size-budget
test of its own (unlike sync.py's regression-budget test) to catch further growth. intake.py already
set this exact precedent for the same reason (card_created_by_name/op_external_link/card_web_url
were kept out of agileplace.py there too) -- see intake.py's module docstring.
"""
from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import NamedTuple


class CardTypeRule(NamedTuple):
    """One derivation-table row: match `kind` ("issue_type" or "label") against `key`, and when it
    matches, the derived card type NAME is `target`. Order matters -- CARD_TYPE_RULES is walked
    first-match-wins, so a rule earlier in the tuple always outranks a later one for the same
    issue."""
    kind: str
    key: str
    target: str


# First match wins. Native issue type is checked ahead of labels (rules 1-2 before 3-5) per the
# issue's own derivation table. Within the label rules, order is immaterial in practice (each label
# asserts a different target) but is pinned here anyway for determinism if an issue ever carries
# more than one of these labels at once.
CARD_TYPE_RULES: tuple[CardTypeRule, ...] = (
    CardTypeRule(kind="issue_type", key="Bug", target="Bug"),
    CardTypeRule(kind="issue_type", key="Feature", target="New Feature"),
    CardTypeRule(kind="label", key="documentation", target="Documentation"),
    CardTypeRule(kind="label", key="enhancement", target="Improvement"),
    CardTypeRule(kind="label", key="bug", target="Bug"),
)


def derive_card_type_name(issue: dict) -> str | None:
    """The derived card type NAME for one issue, or None when no rule matches (native `Task` alone,
    `type:epic` issues -- the board has no Epic card type, or any other unmapped combination). Pure
    and total: never raises regardless of `issue`'s `issue_type`/`labels` shape, and unmatched always
    means "no write, board default/manual choice stands" -- never a guess.

    Reads issue.get("issue_type") (ghkit.list_issues's normalized name-or-None) and
    issue.get("labels", []) (read-only, never mutated)."""
    issue_type = issue.get("issue_type")
    labels = issue.get("labels", [])
    if not isinstance(labels, (list, tuple, set, frozenset)):
        labels = []
    label_set = {label for label in labels if isinstance(label, str)}
    for rule in CARD_TYPE_RULES:
        if rule.kind == "issue_type" and issue_type == rule.key:
            return rule.target
        if rule.kind == "label" and rule.key in label_set:
            return rule.target
    return None


class ResolvedCardTypes(NamedTuple):
    """resolve_card_type_ids's return shape: `by_name` maps a derivation-table target NAME to its
    board typeId (only for names that resolved cleanly), `warnings` is one printable WARN line per
    unresolved/ineligible/ambiguous target name, in a stable (sorted-by-name) order."""
    by_name: Mapping[str, str]
    warnings: tuple[str, ...]


def resolve_card_type_ids(card_types: list) -> ResolvedCardTypes:
    """Resolve every CARD_TYPE_RULES target name against the board's configured card types.

    `card_types` is agileplace.board_layout(cfg).card_types -- already structurally validated by
    agileplace._card_types_with_ids (every entry a dict with a usable id). Eligibility here is
    semantic, not structural: an entry counts only when its `isCardType` flag is truthy (excludes
    task-only types like `Subtask`) and its (stripped) `title` is non-empty. A name with zero
    eligible matches, or more than one (ambiguous -- two board types sharing a title), is left out
    of `by_name` and gets one WARN in `warnings` instead; a name is never silently dropped without
    an explanation.

    Pure and total over any list input (never raises); idempotent -- calling it twice on the same
    `card_types` list yields an equal result both times, since it depends on nothing but that
    input. Intended to be called once per run."""
    needed_names = sorted({rule.target for rule in CARD_TYPE_RULES})
    ids_by_title: dict[str, list] = {}
    for card_type in card_types:
        if not card_type.get("isCardType"):
            continue
        title = (card_type.get("title") or "").strip()
        if not title:
            continue
        ids_by_title.setdefault(title, []).append(card_type.get("id"))

    by_name: dict[str, str] = {}
    warnings: list[str] = []
    for name in needed_names:
        matches = ids_by_title.get(name, [])
        if not matches:
            warnings.append(
                f"WARN  no eligible board card type named {name!r} -- typeId writes for it are "
                f"skipped until the board defines one"
            )
        elif len(matches) > 1:
            warnings.append(
                f"WARN  board has {len(matches)} eligible card types named {name!r} -- ambiguous, "
                f"typeId writes for it are skipped"
            )
        else:
            by_name[name] = matches[0]
    return ResolvedCardTypes(by_name=MappingProxyType(by_name), warnings=tuple(warnings))


class CardTypeDecision(NamedTuple):
    """_decide's return shape: `op` is a JSON-Patch op to queue (or None), `warn` is a printable WARN
    line for the manual-edit-detected branch (or None), `update_base` is whether the caller should
    persist `new_base` as the issue's new last-synced type, and `new_base` is that value."""
    op: dict | None
    warn: str | None
    update_base: bool
    new_base: str | None


def _decide(base: str | None, derived: str | None, current: str | None,
            by_name: Mapping[str, str]) -> CardTypeDecision:
    """Pure function of its four inputs only -- exhaustively unit-testable, five branches:

    1. derived is None (no rule matched this issue) -> no write, no drift check, base untouched.
    2. current == derived (card already carries the derived type) -> nothing to queue, but the base
       advances to confirm the match.
    3. derived != base (GitHub side changed since last sync) and the derived name resolves via
       `by_name` -> queue the typeId patch; base advances to `derived` (confirmed only once the
       caller sees the write actually applied -- see sync_card_type).
    4. derived != base but the derived name does NOT resolve (unknown/ineligible/ambiguous board
       type) -> no write possible, base stays put so a later board fix can still catch up.
    5. else (derived == base, but the card's current type != derived) -> a manual AgilePlace-side
       edit happened after last sync; WARN and leave it alone rather than stomping a human choice.
    """
    if derived is None:
        return CardTypeDecision(op=None, warn=None, update_base=False, new_base=None)
    if current == derived:
        return CardTypeDecision(op=None, warn=None, update_base=True, new_base=derived)
    if derived != base:
        type_id = by_name.get(derived)
        if type_id:
            return CardTypeDecision(op=op_type(type_id), warn=None, update_base=True, new_base=derived)
        return CardTypeDecision(op=None, warn=None, update_base=False, new_base=None)
    warn = (
        f"WARN  card type {current!r} differs from derived {derived!r}, but the last-synced base "
        f"already matches derived -- manual board-side change detected, leaving it alone"
    )
    return CardTypeDecision(op=None, warn=warn, update_base=False, new_base=None)


def card_type_title(card: dict) -> str | None:
    """Best-effort card type NAME read from a card's nested `type` object (the shape both the real
    AgilePlace card payload and agileplace._planned_card_snapshot's dry-run snapshot carry).

    Defensive against malformed shapes the same way agileplace.custom_id_value is: a missing/None
    `type` is just "no type" (returns None, no WARN -- that's the ordinary untyped-card case); a
    present-but-non-dict `type`, or a non-string `.title`, WARNs once and returns None rather than
    raising. An empty/whitespace-only title also normalizes to None."""
    card_type = card.get("type")
    if card_type is None:
        return None
    if not isinstance(card_type, dict):
        print(f"WARN  card {card.get('id', '<unknown>')!r} has non-object type "
              f"({type(card_type).__name__}) -- ignoring")
        return None
    title = card_type.get("title")
    if title is None:
        return None
    if not isinstance(title, str):
        print(f"WARN  card {card.get('id', '<unknown>')!r} has non-string type.title "
              f"({type(title).__name__}) -- ignoring")
        return None
    return title.strip() or None


def op_type(type_id: str) -> dict:
    """RFC-6902 op replacing a card's typeId -- same shape/homing rationale as agileplace's sibling
    op_custom_id, but this one has no agileplace-internal dependency so it lives here instead."""
    return {"op": "replace", "path": "/typeId", "value": type_id}


def sync_card_type(cfg: dict, apply: bool, issue: dict, card: dict, by_name: Mapping[str, str],
                    issues_state: dict, queue) -> None:
    """Per-issue card-type sync step, matching metadata_sync.sync_metadata/sync_dates's
    (cfg, apply, issue, card, ..., issues_state, queue) call shape so sync.py's per-issue loop can
    call all three uniformly. Computes derived/current, delegates the actual decision to _decide,
    then carries out its side effects: queues `decision.op` (if any) through the existing
    queue/patch_card path (409/428 conflict-retry and dry-run gating come free from there), prints
    `decision.warn` (if any), and -- ONLY when `apply` is True and `decision.update_base` says the
    match is confirmed -- persists `issues_state[issue["url"]]["type"] = decision.new_base`. Never
    mutates `issue`, `card`, or `by_name`; `cfg` is accepted for call-shape parity but unused here."""
    prev = issues_state[issue["url"]]
    derived = derive_card_type_name(issue)
    current = card_type_title(card)
    decision = _decide(prev.get("type"), derived, current, by_name)
    if decision.op:
        queue(card, [decision.op], f"type->{derived}")
    if decision.warn:
        print(decision.warn)
    if apply and decision.update_base:
        prev["type"] = decision.new_base


class ReverseSeed(NamedTuple):
    """reverse_seed_for_card_type's return shape: the native GitHub issue TYPE name to request at
    creation (or None), and/or the label to apply after creation (or None). At most one of the two
    is ever non-None for any card type in REVERSE_SEED_BY_CARD_TYPE today, but callers must not
    assume that stays true -- both fields are independent."""
    issue_type: str | None
    label: str | None


# Shared sentinel for "no reverse seed" -- returned for both an unmapped card type NAME and a bare
# None input, so callers get one uniform falsy-ish shape regardless of why nothing seeded.
_NO_SEED = ReverseSeed(issue_type=None, label=None)

# Card type NAME -> reverse seed, from the issue's own reverse-mapping table. `Risk / Issue` and
# `Subtask` (and any other card type not listed here) intentionally have no entry -- they fall
# through to _NO_SEED via .get()'s default.
REVERSE_SEED_BY_CARD_TYPE: Mapping[str, ReverseSeed] = MappingProxyType({
    "Bug": ReverseSeed(issue_type="Bug", label=None),
    "New Feature": ReverseSeed(issue_type="Feature", label=None),
    "Improvement": ReverseSeed(issue_type=None, label="enhancement"),
    "Documentation": ReverseSeed(issue_type=None, label="documentation"),
    "Other Work": ReverseSeed(issue_type="Task", label=None),
})


def reverse_seed_for_card_type(name: str | None) -> ReverseSeed:
    """The reverse-intake seed for one card type NAME (e.g. from card_type_title on the promoted
    card). Pure and total: an unmapped name or a bare None input both return _NO_SEED -- never
    raises."""
    if name is None:
        return _NO_SEED
    return REVERSE_SEED_BY_CARD_TYPE.get(name, _NO_SEED)


def validate_reverse_issue_type(issue_type: str | None, org_types: frozenset[str] | None) -> str | None:
    """Gate a reverse-seeded native issue TYPE against the org's actually-enabled issue types before
    it ever reaches ghkit.create_issue -- gh issue create --type is non-atomic (a bad type creates
    the issue, then fails the command; a blind retry duplicates it -- see API-VALIDATION.md), so this
    must never let an unconfirmed type through.

    Returns `issue_type` only when `org_types` is not None AND `issue_type` is a member of it.
    `org_types is None` covers BOTH "the probe failed or was skipped" and "dry run never fetched it"
    -- deliberately the same fail-closed signal as "type not enabled", so callers get one fallback
    path (create typeless) regardless of which of those two actually happened."""
    if org_types is None or issue_type is None:
        return None
    return issue_type if issue_type in org_types else None
