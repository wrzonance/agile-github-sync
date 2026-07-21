# Sync GitHub blocked-by edges as native AgilePlace Dependencies

Design agreed 2026-07-21 (issue #57). Decisions made with the maintainer:
discovery via read-only probe script; the Blocked flag stays until the native
dependency visuals are judged on the live board (phased); GitHub is authoritative
for dependencies between sync-managed cards.

## Problem

GitHub blocked-by edges currently surface in AgilePlace only as the card-level
Blocked flag (`isBlocked` + "Blocked by #N" free text). That is loud but
structureless: no link to the blocking card, an empty Dependencies tab
(INCOMING/OUTGOING both 0), no health tracking, and the "#N" reference is a
GitHub issue number a board user cannot click. AgilePlace's native lexicon:
parent/child connections express hierarchy (already synced correctly);
Dependencies express sequencing -- the correct home for blocked-by.

## Gating constraint

The io v2 public docs do not document a dependencies endpoint (checked
2026-07-21: documented write surface is cards, connections (parent/child),
comments, attachments, custom fields, lanes; the "dependencies" doc hits are the
read-only Advanced Reporting export and UI guides). The UI's Dependencies tab
calls an undocumented endpoint. Nothing may be built against guessed shapes --
the planned-date HTTP 422 (issue #52) is this repo's standing proof of why.

## Phase 0 -- read-only discovery probe (this phase)

`probe_dependencies.py`, stdlib-only, reads `.env` exactly like `smoke.py`.
Strictly read-only: every request it issues is a GET; a test pins this
invariant. No confirmation prompt is needed because nothing mutates.

Behavior:

1. Env check: requires `AGILEPLACE_TOKEN`, `AGILEPLACE_HOST`,
   `AGILEPLACE_BOARD_ID` (same message pattern as smoke).
2. Pick a card: first card returned by `list_cards`, or `--card-id` override.
3. Baseline dump: GET the card and print its sorted top-level keys, flagging any
   key containing "depend" -- dependencies may already be embedded in card reads.
4. Positive control: GET `card/{id}/connection/children` (documented, known
   good). If the control fails, the probe's plumbing/credentials are broken and
   candidate results are meaningless; say so and stop.
5. Probe candidates, printing one line each (`FOUND HTTP 200` / `MISS HTTP 404`
   / `HTTP <other>`) plus a truncated body for anything that is not a 404:
   - `card/{id}/dependencies`
   - `card/{id}/dependency`
   - `dependencies` with `?cardId={id}`
   - `dependency` with `?cardId={id}`
   - `board/{boardId}/dependencies`
   - `card/{id}/connection/dependencies`
6. Summary: list FOUND endpoints; if none, print the fallback instruction
   (create one dependency in the UI with browser devtools open, capture the
   request as cURL, record it in API-VALIDATION.md).

The probe uses its own GET helper that returns `(status, body)` for every HTTP
outcome -- `agileplace.api` deliberately converts HTTP errors to `SystemExit`,
which is correct for the sync and wrong for a probe, where 404 is data.

Probe findings land in API-VALIDATION.md (a "dependencies discovery" entry) in
the same evidence-recording style as every other live check.

## Phase 1 -- sync integration (contingent on confirmed shapes)

Not built until Phase 0 (plus, if needed, a smoke-style write probe on throwaway
cards) confirms read and write shapes.

- `agileplace.py`: `card_dependencies(cfg, card_id)` (read),
  `create_dependencies(cfg, apply, card_id, depends_on_ids)`,
  `delete_dependencies(cfg, apply, card_id, depends_on_ids)` -- batch pair
  bodies, as confirmed live. Direction blocker -> blocked, timing
  `finishToStart` (GitHub blocked-by's one semantic is FS; the UI's other
  timings -- SS, FF, SF -- are never written by the sync).
- Timing ownership: reconciliation matches dependencies by card PAIR only. A
  human who refines a synced dependency's timing in the UI keeps that
  refinement; the sync neither inspects nor rewrites `timing` after creation.
- `sync.py`, after the connections step: build the desired edge set from GitHub
  blocked-by (already fetched for `blocked_reason`), read current dependencies
  for managed cards, diff, create missing, delete unmatched.
- Authority rule: reconciliation touches a dependency ONLY when both endpoint
  cards are sync-managed. Any dependency involving a non-managed card is
  invisible to the sync, in both directions.
- Edge semantics: dependencies mirror ALL GitHub blocked-by edges, including
  edges whose blocker is Done -- the edge is structural and AgilePlace's health
  display shows satisfaction. This deliberately differs from the Blocked flag,
  which continues to reflect incomplete blockers only.
- Failure semantics: a failed or malformed dependency read fails closed -- warn
  and skip dependency reconciliation for that run (mirrors children reads).
- Dry run: planned dependency ops print with the same planned-card-id boundary
  rules as connections; planned ids never escape the run.
- Tests: the desired-vs-current diff is a pure function, unit-tested directly;
  the offline fake tenant grows dependency endpoints mirroring the CONFIRMED
  shapes only.

## Phase 2 -- Blocked-flag retirement (separate, later decision)

After Phase 1 has run on the live board, the maintainer judges whether the
native dependency visuals give sufficient on-board blocked context. Only then
may `isBlocked`/`blockReason` writes be dropped, as its own change. Until that
decision, the sync writes both representations.

## Process

Issue #57 tracks the feature. Phase 0 ships on branch `feat/dependency-probe`
(no PR: Codex-reviewed locally, pushed for the maintainer to run against the
live tenant from another machine). Phase 1 branches after shapes are confirmed
and goes through the normal draft-PR review flow.
