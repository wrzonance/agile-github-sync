"""Print what BOTH sides actually offer for card-type mapping, then how the active mapping lands.

Run it whenever `sync.py` warns that a card type name did not resolve:

    python -m agilesync.tools.type_inventory

The sync derives an AgilePlace card type from each GitHub issue (native issue type, else label) and
writes it as the card's `/typeId`. That derivation is a NAME match against the board's own card
types, so a board using a different vocabulary than the built-in defaults resolves nothing and
silently skips every typeId write. Fixing it means writing a CARD_TYPE_MAP in .env -- which requires
knowing the exact names each side uses, and neither side's names are visible from the sync's output
alone. This script is that missing half: it lists GitHub's native issue types and labels, the
board's eligible card types, and prints a copy-pasteable CARD_TYPE_MAP skeleton.

Strictly READ-ONLY -- no card, issue, or board is modified, so it is safe to run at any time,
including against a production board mid-sync. Degrades rather than fails: any side that cannot be
read -- unconfigured, unreachable, bad token, gh not installed -- prints why and the rest of the
report still renders. That matters more here than anywhere else in the repo: this is the command an
operator runs BECAUSE the configuration is wrong, so it must never abort on the misconfiguration it
was invoked to diagnose.
"""
from __future__ import annotations

from agilesync.board import board_layout
from agilesync import card_types
from agilesync.gh import ghkit
from agilesync.config import env_config

_UNAVAILABLE = "<unavailable -- see the note above>"


def board_card_type_names(cfg: dict) -> tuple[list[str], list[str]] | None:
    """(eligible, task_only) board card type titles, each sorted, or **None when the board could not
    be read**. `eligible` are the only legal CARD_TYPE_MAP targets -- a task-only type (`isCardType`
    falsy, e.g. `Subtask`) can never be a card's type, which is exactly why resolve_card_type_ids
    refuses to match one.

    Tri-state for the same reason the GitHub-side readers are: an unreachable tenant, a bad token,
    or a 404 board raises SystemExit out of agileplace.api, and letting that escape would abort the
    whole report -- the one command an operator runs precisely BECAUSE something is misconfigured
    (adversarial-review finding: the module's "degrades rather than fails" promise was false)."""
    try:
        entries = board_layout.board_layout(cfg).card_types
    except SystemExit as exc:
        print(f"\nNOTE: could not read the AgilePlace board -- {exc}")
        return None
    eligible, task_only = [], []
    for entry in entries:
        title = card_types.board_type_title(entry)
        if not title:
            continue
        (eligible if entry.get("isCardType") else task_only).append(title)
    return sorted(eligible), sorted(task_only)


def _print_section(heading: str, values: list[str] | None, empty_note: str) -> None:
    print(f"\n{heading}")
    if values is None:
        print(f"  {_UNAVAILABLE}")
        return
    if not values:
        print(f"  {empty_note}")
        return
    for value in values:
        print(f"  {value}")


def _print_active_mapping(rules: tuple[card_types.CardTypeRule, ...], configured: bool,
                          eligible: list[str] | None) -> None:
    """Every rule in force, each marked with whether its target exists on the board -- the one view
    that answers "why did nothing happen?" without cross-referencing two lists by eye. An empty
    table is its own answer and says so: that is the fail-closed state a CARD_TYPE_MAP whose every
    entry was rejected lands in, and it must not read as "defaults apply"."""
    source = "CARD_TYPE_MAP (.env)" if configured else "built-in defaults (CARD_TYPE_MAP unset)"
    print(f"\nActive mapping -- {source}")
    if not rules:
        print("  <none: every CARD_TYPE_MAP entry was rejected (see the WARNs above) -- this run "
              "would write NO card types at all>")
        return
    kind_label = {"issue_type": "type", "label": "label"}
    for rule in rules:
        if eligible is None:
            status = "?"
        elif rule.target in eligible:
            status = "OK"
        else:
            status = "NOT ON BOARD"
        print(f"  {kind_label[rule.kind]}:{rule.key} -> {rule.target!r}  [{status}]")


def _print_skeleton(rules: tuple[card_types.CardTypeRule, ...], eligible: list[str] | None) -> None:
    """A copy-pasteable starting point with every unresolved target left as a placeholder, so the
    only editing needed is replacing each <...> with a name from the board list above."""
    kind_label = {"issue_type": "type", "label": "label"}
    entries = []
    for rule in rules:
        target = rule.target if eligible and rule.target in eligible else "<board card type>"
        entries.append(f"{kind_label[rule.kind]}:{rule.key}={target}")
    print("\nCARD_TYPE_MAP skeleton for .env (';'-separated, FIRST match wins):")
    print("  CARD_TYPE_MAP=" + "; ".join(entries))


def print_inventory(cfg: dict) -> None:
    """The whole report. Reads GitHub through `gh` and, when AgilePlace is configured, the board;
    an unconfigured or unreachable side is reported, never fatal."""
    configured = cfg.get("card_type_map")
    rules = card_types.active_rules(configured)

    if cfg["target_repo_path"] is None:
        print("NOTE: TARGET_REPO_PATH not set (.env) -- cannot read GitHub issue types or labels.")
        issue_types, labels = None, None
    else:
        org_types = ghkit.org_issue_types(cfg)
        issue_types = sorted(org_types) if org_types is not None else None
        labels = ghkit.list_label_names(cfg)

    _print_section("GitHub native issue types (org-level, used as `type:<name>`)", issue_types,
                   "<none enabled for this org>")
    _print_section("GitHub labels on the target repo (used as `label:<name>`)", labels,
                   "<this repo defines no labels>")
    if labels is not None and len(labels) == ghkit.LABEL_LIST_LIMIT:
        print(f"  NOTE: exactly {ghkit.LABEL_LIST_LIMIT} labels returned -- this list may be "
              f"truncated, so a label missing above may still exist.")

    if not (cfg["token"] and cfg["host"] and cfg["board_id"]):
        print("\nNOTE: AgilePlace is not fully configured (.env) -- cannot read the board's card types.")
        eligible, task_only = None, None
    else:
        board = board_card_type_names(cfg)
        eligible, task_only = board if board is not None else (None, None)

    _print_section("AgilePlace board card types (CARD_TYPE_MAP targets)", eligible,
                   "<this board defines no card types>")
    if task_only:
        _print_section("AgilePlace task-only types (NOT usable as a card type)", task_only,
                       "<none>")

    _print_active_mapping(rules, configured is not None, eligible)
    _print_skeleton(rules, eligible)
    print("\nA target that is NOT ON BOARD writes no typeId at all -- the card keeps whatever type "
          "the board gives it.")


def main() -> None:
    print_inventory(env_config())


if __name__ == "__main__":
    main()
