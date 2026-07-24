# Issue #93 — Card header carries the GitHub issue number

**Date:** 2026-07-24
**Issue:** [#93](https://github.com/wrzonance/agile-github-sync/issues/93)

## Problem

An AgilePlace card's header (its `customId`) shows only the sync key (`0C1`), so a board user
can't tell which GitHub issue a card mirrors without opening the external link. The header should
read `0C1 (GitHub Issue #5)`.

The wrinkle: `customId` plays two roles today. It is the **display string** written to cards, and
the **match key** used by `_matching_card`'s customId fallback, the #70/#75 coherence fence,
`_reconciled_custom_id_index`, and intake's managed-set disqualification. A naive format change
breaks matching for every existing card until it happens to be rewritten.

## Decision: key/header split

Chosen over (a) changing `issue_custom_id()`'s format globally — the long string leaks into every
`[0C1]` log label and coherence key, and still needs card-side normalization for the transition —
and (b) a one-shot migration script — unnecessary, because sync.py's existing customId drift
reconciliation (step 2 of the per-issue loop) already rewrites any card whose stored customId
differs from the desired value, which *is* the migration.

`stages.issue_custom_id()` stays untouched as the internal match key. Two new pure functions in
`stages.py` split the roles:

### Format contract

`issue_card_header(issue) -> str` — the string **written** to a card's customId:

| Issue shape | Header |
|---|---|
| Keyed task (`[0C1] …`, number 5) | `0C1 (GitHub Issue #5)` |
| Epic (`[EP-0C] …`, number 12) | `EP-0C (GitHub Issue #12)` |
| Unkeyed (no `[KEY]`, number 5) | `GitHub Issue #5` (no redundant `5 (…#5)`) |

Keyed-ness is decided by `title_key(issue["title"])` (truthy → keyed), not by comparing the key to
the number string.

`header_match_key(value) -> str` — the inverse, applied to card-side reads:

1. `"<key> (GitHub Issue #<digits>)"` → `"<key>"` (anchored at the END; only the final suffix is
   stripped, so a pathological `"A (GitHub Issue #5) (GitHub Issue #6)"` yields
   `"A (GitHub Issue #5)"`).
2. Bare `"GitHub Issue #<digits>"` → `"<digits>"` (the unkeyed fallback's key).
3. Anything else → returned unchanged (old-format cards, human-authored cards, smoke cards).

It operates on `agileplace.custom_id_value(...)` output (already stripped, `""` for missing).

**Round-trip invariant** (the core test):
`header_match_key(issue_card_header(issue)) == issue_custom_id(issue)` for every issue shape.

## Architecture — touch points

All new logic is pure and lives in `stages.py`. sync.py is over the 800-line budget, so its diff
is net-minimal wiring only.

**Write sites (switch from key to `issue_card_header`):**

- `sync.py _ensure_cards_for_syncable_issues` — the `create_card` custom_id argument becomes the
  header, with `link_label=f"GitHub {key}"` keeping the link label short (see below). The
  freshly-created card still registers in `card_by_cid` under its **key** (match space).
- `sync.py` per-issue loop (~line 676) — the drift check becomes
  `custom_id_value(card) != issue_card_header(issue)`, writing the header via `op_custom_id`.
  This raw (un-normalized) comparison is deliberately what upgrades every old-format card on the
  next `--apply` run. Log labels keep printing the short `[{key}]`.
- `intake.py _writeback_key` — the card→new-issue writeback (the reverse-intake path issue #93
  names explicitly) returns the header computed from the card's own title + new issue number.
  Writeback ordering/failure semantics are untouched.

**Read sites (normalize through `header_match_key`; comparisons stay in key space):**

- `sync.py` board index build (~line 604): `all_card_by_cid` keys become
  `header_match_key(custom_id_value(card))`.
- `sync.py _reconciled_custom_id_index` (~line 312): `current_custom_id` normalized; the
  release/rename semantics are unchanged because both sides are then in key space.
- `card_coherence.py` retired-card index (~line 208): keys normalized. No other coherence change —
  the fence already compares `issue_custom_id` against the (now normalized) indexes.
- `intake.py _is_candidate` (~line 145): the card side normalized before the
  `managed_custom_ids` membership test, so a managed card in either format is never mistaken for
  an intake candidate.

**External-link label stays short:** `agileplace.create_card` today derives the link label from
the customId it receives (`f"GitHub {custom_id}"`), which would render the redundant
`GitHub 0C1 (GitHub Issue #5)`. It grows a keyword parameter `link_label: str | None = None` —
when provided, used verbatim as the label; when `None`, the existing `f"GitHub {custom_id}"`
fallback keeps every current caller (smoke.py included) byte-identical. The sync's create site
passes `link_label=f"GitHub {key}"`, so new cards' links keep reading `GitHub 0C1`. The dry-run
`_planned_card_snapshot` mirrors the same label so plan output matches what `--apply` writes.
Intake's own label convention (`GitHub #5`) is a different write path and is untouched.

## Transition

No migration script (per the no-cruft rule). On the first run after deploy:

1. Every existing card still matches — by URL as today, and by customId because old-format values
   pass through `header_match_key` unchanged into the same key space.
2. The per-issue drift check sees `"0C1" != "0C1 (GitHub Issue #5)"` and queues the rewrite; one
   `--apply` run revises the whole board.
3. Mixed-format boards (mid-transition, or a crashed run) stay coherent: both formats normalize to
   one key, so duplicate claims still land in the contested fence rather than slipping past it.

## Testing

Unit (pure, no I/O — the #70 pattern):

- `stages`: formatter table above; parser cases — new format, bare unkeyed form, old format
  passthrough, empty string, suffix-not-at-end, double-suffix, non-digit "number"; the round-trip
  invariant across keyed/epic/unkeyed shapes.
- `sync`: old-format card queues the header rewrite; new-format card queues nothing; customId
  fallback matching finds a card in either format; `_reconciled_custom_id_index` rename/release
  behavior with mixed formats.
- `agileplace`: `create_card` with `link_label` uses it verbatim; without it, the label falls back
  to `f"GitHub {custom_id}"` unchanged (both real-body and dry-run snapshot shapes).
- `intake`: writeback writes the header; a managed card in old OR new format is disqualified as a
  candidate.
- `card_coherence`: retired-index and contested detection with mixed-format cards.

**Smoke (project rule: every new feature adds a smoke step):** a new `_check_*` step PATCHes the
throwaway parent card's customId to `f"{parent_custom_id} (GitHub Issue #999999)"` and
refetch-verifies a byte-exact round-trip — proving the live API accepts and preserves parens, `#`,
and spaces in customId (the one thing unit tests cannot). Uses the existing per-run-suffixed
parent id so `header_match_key` still normalizes it to a smoke-unique key; smoke must NEVER write
a bare `GitHub Issue #N` customId (it would normalize to a real unkeyed issue's key if leaked).
Record the confirmed behavior in API-VALIDATION.md alongside the other `[live-check]` items.

## Out of scope

- Card **title** format, external-link label wording, and comment/description sync — untouched.
- The `SystemExit` ambiguous-match contract and #75 fence semantics — unchanged, only fed
  normalized keys.
