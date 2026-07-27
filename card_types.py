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
#
# These are only the DEFAULTS, used when .env sets no CARD_TYPE_MAP (see parse_card_type_map): they
# name the card types of the board this tool was first written against, and no board is obliged to
# use that vocabulary. A board whose types are e.g. Defect/Story/Task resolves none of them and
# skips every typeId write -- exactly what CARD_TYPE_MAP exists to fix. Run `python type_inventory.py`
# to list what each side actually offers.
CARD_TYPE_RULES: tuple[CardTypeRule, ...] = (
    CardTypeRule(kind="issue_type", key="Bug", target="Bug"),
    CardTypeRule(kind="issue_type", key="Feature", target="New Feature"),
    CardTypeRule(kind="label", key="documentation", target="Documentation"),
    CardTypeRule(kind="label", key="enhancement", target="Improvement"),
    CardTypeRule(kind="label", key="bug", target="Bug"),
)


# .env prefix -> CardTypeRule.kind. `type:` reads GitHub's NATIVE issue type (org-configured, listed
# by ghkit.org_issue_types); `label:` reads an ordinary issue label. Nothing else is accepted -- an
# unprefixed entry would have to be guessed as one or the other, and guessing which side of GitHub a
# name refers to is exactly the ambiguity this map exists to remove.
CARD_TYPE_MAP_KINDS: Mapping[str, str] = MappingProxyType({"type": "issue_type", "label": "label"})


def parse_card_type_map(raw: str) -> tuple[CardTypeRule, ...]:
    """Parse CARD_TYPE_MAP: ';'-separated `<kind>:<key>=<AgilePlace card type>` entries, e.g.
    ``type:Bug=Defect; label:enhancement=Improvement``.

    `<kind>` is `type` (a native GitHub issue type) or `label`; `<key>` is that type's/label's name
    on the GitHub side; the value is the card type TITLE as your AgilePlace board spells it. Entries
    are evaluated in the order written -- FIRST match wins for an issue matching several of them --
    so put the rule that should outrank the others first (the built-in defaults put native types
    ahead of labels for exactly that reason).

    Blank/unset returns () meaning "use CARD_TYPE_RULES", so an untouched .env keeps today's
    behavior. Split on the FIRST '=' and the FIRST ':', so a namespaced label keeps its own colon
    (`label:area:api=Improvement` parses as key `area:api`). A malformed or unknown-kind entry is
    skipped with one
    WARN naming it rather than silently ignored: a typo'd mapping that quietly does nothing is the
    failure mode this whole feature exists to make visible."""
    rules: list[CardTypeRule] = []
    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        left, sep, target = entry.partition("=")
        kind_raw, _, key = left.strip().partition(":")
        kind = CARD_TYPE_MAP_KINDS.get(kind_raw.strip().lower())
        if not sep or not kind or not key.strip() or not target.strip():
            print(f"WARN  CARD_TYPE_MAP entry {entry!r} is malformed -- expected "
                  f"'type:<issue type>=<card type>' or 'label:<label>=<card type>' -- skipping it")
            continue
        rules.append(CardTypeRule(kind=kind, key=key.strip(), target=target.strip()))
    return tuple(rules)


def active_rules(rules: tuple[CardTypeRule, ...] | None) -> tuple[CardTypeRule, ...]:
    """The derivation table actually in force: a configured CARD_TYPE_MAP when it has any entry,
    else the built-in CARD_TYPE_RULES. One helper so every consumer resolves "configured or default"
    identically -- a caller that forgot the fallback would silently derive no card type at all."""
    return tuple(rules) if rules else CARD_TYPE_RULES


def board_type_title(card_type: dict) -> str:
    """The display TITLE of one BOARD card-type entry, stripped; "" when it has none usable.

    Reads `title` and falls back to `name`, the same hedge board_layout.lane_title has always
    applied to lanes: AgilePlace's io v2 board schema is documented for `lanes[].title`, but the
    `cardTypes[]` entry shape is NOT pinned down by the public docs (see API-VALIDATION.md), and a
    payload keyed `name` would otherwise make every board card type unresolvable and silently skip
    every typeId write. Non-string/absent values on both keys degrade to "" -- never raises.

    Distinct from card_type_title() below, which reads a CARD's nested `type` object."""
    for key in ("title", "name"):
        value = card_type.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def derive_card_type_name(issue: dict, rules: tuple[CardTypeRule, ...] | None = None) -> str | None:
    """The derived card type NAME for one issue, or None when no rule matches (native `Task` alone,
    `type:epic` issues -- the board has no Epic card type, or any other unmapped combination). Pure
    and total: never raises regardless of `issue`'s `issue_type`/`labels` shape, and unmatched always
    means "no write, board default/manual choice stands" -- never a guess.

    `rules` is the configured CARD_TYPE_MAP (cfg["card_type_map"]); None/empty falls back to the
    built-in CARD_TYPE_RULES via active_rules.

    Reads issue.get("issue_type") (ghkit.list_issues's normalized name-or-None) and
    issue.get("labels", []) (read-only, never mutated)."""
    issue_type = issue.get("issue_type")
    labels = issue.get("labels", [])
    if not isinstance(labels, (list, tuple, set, frozenset)):
        labels = []
    label_set = {label for label in labels if isinstance(label, str)}
    for rule in active_rules(rules):
        if rule.kind == "issue_type" and issue_type == rule.key:
            return rule.target
        if rule.kind == "label" and rule.key in label_set:
            return rule.target
    return None


class ResolvedCardTypes(NamedTuple):
    """resolve_card_type_ids's return shape: `by_name` maps a derivation-table target NAME to its
    board typeId (only for names that resolved cleanly), `warnings` is one printable WARN line per
    unresolved/ineligible/ambiguous target name, in a stable (sorted-by-name) order, followed (only
    when there is at least one such line) by a single trailing hint line."""
    by_name: Mapping[str, str]
    warnings: tuple[str, ...]


def resolve_card_type_ids(card_types: list,
                          rules: tuple[CardTypeRule, ...] | None = None) -> ResolvedCardTypes:
    """Resolve every target name of the active derivation table (`rules`, else CARD_TYPE_RULES)
    against the board's configured card types.

    `card_types` is board_layout.board_layout(cfg).card_types -- already structurally validated by
    board_layout._card_types_with_ids (every entry a dict with a usable id). Eligibility here is
    semantic, not structural: an entry counts only when its `isCardType` flag is truthy (excludes
    task-only types like `Subtask`) and board_type_title reads a non-empty title from it. A name
    with zero eligible matches, or more than one (ambiguous -- two board types sharing a title), is
    left out of `by_name` and gets one WARN in `warnings` instead; a name is never silently dropped
    without an explanation. Any unresolved name also appends one trailing hint line naming the
    titles the board DOES offer (see _unresolved_hint).

    Pure and total over any list input (never raises); idempotent -- calling it twice on the same
    `card_types` list yields an equal result both times, since it depends on nothing but that
    input. Intended to be called once per run."""
    needed_names = sorted({rule.target for rule in active_rules(rules)})
    ids_by_title: dict[str, list] = {}
    for card_type in card_types:
        if not isinstance(card_type, dict):
            continue
        if not card_type.get("isCardType"):
            continue
        title = board_type_title(card_type)
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
                f"skipped until the board defines one, or CARD_TYPE_MAP maps it to one it has"
            )
        elif len(matches) > 1:
            warnings.append(
                f"WARN  board has {len(matches)} eligible card types named {name!r} -- ambiguous, "
                f"typeId writes for it are skipped"
            )
        else:
            by_name[name] = matches[0]
    if warnings:
        warnings.append(_unresolved_hint(sorted(ids_by_title)))
    return ResolvedCardTypes(by_name=MappingProxyType(by_name), warnings=tuple(warnings))


def _unresolved_hint(board_titles: list[str]) -> str:
    """One trailing line appended whenever ANY name failed to resolve, naming the titles the board
    actually offers and how to fix the mismatch. Without it the WARNs above tell a reader what is
    missing but never what is available -- the exact information needed to write a CARD_TYPE_MAP,
    and the reason four identical-looking WARNs a run were easy to dismiss as noise."""
    offered = ", ".join(repr(title) for title in board_titles) if board_titles else "<none>"
    return (f"WARN  board's eligible card types are: {offered} -- map GitHub issue types/labels "
            f"onto them with CARD_TYPE_MAP in .env (run `python type_inventory.py` for both sides)")


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
    mutates `issue`, `card`, or `by_name`; `cfg` is read only for the configured CARD_TYPE_MAP."""
    prev = issues_state[issue["url"]]
    derived = derive_card_type_name(issue, cfg.get("card_type_map"))
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


def reverse_seed_for_card_type(name: str | None,
                               rules: tuple[CardTypeRule, ...] | None = None) -> ReverseSeed:
    """The reverse-intake seed for one card type NAME (e.g. from card_type_title on the promoted
    card). Pure and total: an unmapped name or a bare None input both return _NO_SEED -- never
    raises.

    A configured CARD_TYPE_MAP is INVERTED to build the seed, so the two directions can never drift
    apart: whatever GitHub type/label maps a card type IN is what a promoted card of that type gets
    seeded with going OUT. The FIRST rule naming a target wins, matching derive_card_type_name's own
    first-match-wins precedence. Card types the map does not mention fall back to
    REVERSE_SEED_BY_CARD_TYPE, which is also the whole answer when no map is configured -- that
    table additionally covers types no forward rule produces (`Other Work` -> a native `Task`)."""
    if name is None:
        return _NO_SEED
    for rule in rules or ():
        if rule.target == name:
            return ReverseSeed(issue_type=rule.key if rule.kind == "issue_type" else None,
                               label=rule.key if rule.kind == "label" else None)
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
