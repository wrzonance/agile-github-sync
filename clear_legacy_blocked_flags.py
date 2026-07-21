"""One-shot cleanup: clear the sync-authored Blocked flags left on AgilePlace cards.

The sync used to mirror GitHub blocked-by edges as the card Blocked flag with a
"Blocked by #N" reason. Native dependencies replaced that (issue #57 Phase 2) and
sync.py no longer writes /isBlocked or /blockReason at all, so this script retires
the flags the old behavior left behind -- and ONLY those: a flag is cleared only when
its reason matches the old sync's exact signature ("Blocked by #N[, #M...]"), so
human-authored flags survive untouched. Idempotent; DELETE THIS FILE once the board
is clean.

Run: python clear_legacy_blocked_flags.py            (dry run, read-only)
     python clear_legacy_blocked_flags.py --apply
"""
from __future__ import annotations

import argparse
import re
import sys

import agileplace
from config import env_config

SYNC_REASON = re.compile(r"Blocked by #\d+(, #\d+)*")


def is_sync_authored(card: dict) -> bool:
    """Only the exact reason text the old sync wrote counts -- anything else is human."""
    return (agileplace.card_is_blocked(card)
            and bool(SYNC_REASON.fullmatch(agileplace.card_block_reason(card) or "")))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Clear sync-authored Blocked flags (one-shot, issue #57 Phase 2)")
    parser.add_argument("--apply", action="store_true", help="actually clear (default: dry run)")
    args = parser.parse_args()
    cfg = env_config()
    missing = [env for env, key in (("AGILEPLACE_TOKEN", "token"), ("AGILEPLACE_HOST", "host"),
                                    ("AGILEPLACE_BOARD_ID", "board_id")) if not cfg.get(key)]
    if missing:
        raise SystemExit(f"flag cleanup needs {', '.join(missing)} set (.env) -- refusing to run")

    cards = agileplace.list_cards(cfg)
    targets = [card for card in cards if is_sync_authored(card)]
    kept = [card for card in cards
            if agileplace.card_is_blocked(card) and not is_sync_authored(card)]
    for card in kept:
        print(f"keep  {card['id']} [{agileplace.custom_id_value(card) or '-'}] -- "
              f"human-authored: {agileplace.card_block_reason(card)!r}")
    for card in targets:
        agileplace.patch_card(cfg, args.apply, card, agileplace.ops_blocked(False, None),
                              "clear legacy sync-authored Blocked flag")
        print(f"{'clear' if args.apply else 'DRY  '} {card['id']} "
              f"[{agileplace.custom_id_value(card) or '-'}] -- "
              f"was {agileplace.card_block_reason(card)!r}")
    print(f"\n{len(targets)} sync-authored flag(s) "
          f"{'cleared' if args.apply else 'would be cleared'}; "
          f"{len(kept)} human flag(s) left untouched")
    if not args.apply and targets:
        print("re-run with --apply to write")
    return 0


if __name__ == "__main__":
    sys.exit(main())
