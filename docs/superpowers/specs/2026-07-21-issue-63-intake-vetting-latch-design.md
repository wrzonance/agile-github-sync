# Two-state backlog: Intake stage + vetting latch (issue #63)

Design approved by the maintainer 2026-07-21. Decisions: work signals beat Intake
(an off-board issue with an open PR/assignees/agent labels keeps its fallback
stage); ANY managed non-Intake lane triggers promotion, writing the Status that
matches that lane's stage; the feature is inert unless `STAGE_LANE_MAP` maps
`Intake`.

## Problem

The sync collapses "unvetted intake" and "vetted backlog" into one stage: an
open issue that is not on the Project board falls back to stage `Backlog`, so
the lane map cannot express the maintainer's triage model -- New Requests =
off-board intake, Approved = on-board Backlog. The vetting transition must be
promotable from either side and never auto-demoted.

| State | GitHub | AgilePlace lane (maintainer's board) |
|---|---|---|
| Intake (unvetted) | issue exists, NOT on the Project board | New Requests |
| Backlog (vetted) | on the board, Status = Backlog | Approved |
| Ready / In progress / ... | Status as today | as today |

## Stage model

`resolve_issue_stage` gains exactly one branch: an OPEN issue with no explicit
Project Status whose `issue_stage` fallback reaches the bare else (no open PR,
no assignees, no agent labels) resolves to the new stage `Intake` instead of
`Backlog`. Everything with work signals keeps today's stages -- active work is
never hidden in the intake lane. Closed issues resolve Done, as today.

**Feature flag by config:** when `STAGE_LANE_MAP` has no `Intake=` entry, the
new branch is inert and every code path behaves byte-for-byte as today. A
regression test pins this.

## Lanes

Maintainer's map becomes:

```
STAGE_LANE_MAP=Intake=New Requests; Backlog=Approved|Not Started - Future Work; Ready=Ready to Start; In progress=Doing Now; In review=Under Review; Done=Recently Finished|Finished As Planned|Finished - Ready to Archive
```

- Card creation for an Intake-stage issue targets the Intake stage's first lane.
- The lane-mover treats Intake like any other stage (its lanes form the
  acceptable set).
- **Demotion-trap rule (independent of the latch):** the lane-mover never moves
  a card OUT of a managed non-Intake lane because its issue's stage is Intake.
  That situation is a latch trigger, not a lane violation. This rule alone
  guarantees a failed or skipped promotion can never drag a human-moved card
  back to New Requests.

## The vetting latch (new step; runs BEFORE lane moves)

For each syncable issue whose stage is `Intake` and whose card sits in a managed
NON-Intake lane:

1. Reverse-map the card's lane to a stage via `stage_for_lane(lane_id,
   stage_map, lanes)` -- a pure function. A lane listed under multiple stages,
   or under none, is ambiguous: WARN and skip (fail closed).
2. Promote: `gh project item-add --url <issue url>` then set the Project Status
   to the reverse-mapped stage (Approved -> Backlog, Doing Now -> In progress,
   etc.).
3. Skip that issue's lane-move for the rest of this run -- the next run reads
   the new Status and both sides agree.

The latch NEVER demotes, never removes board membership, and a failed promotion
leaves the card where the human put it. Un-vetting is a manual act on both
sides. The GitHub->AgilePlace direction needs no new code: once the issue
carries a Status, the existing lane logic moves the card.

## New GitHub write surface (the sync's first Status writes)

- `ghproject.add_item(cfg, apply, issue_url) -> item_id | None` wrapping
  `gh project item-add`.
- `ghproject.set_item_status(cfg, apply, item_id, stage) -> bool` wrapping
  `item-edit --single-select-option-id`, resolving field/option ids through the
  same field-list reads the project code already performs.
- Both dry-run-gated: dry runs PRINT the planned gh commands (`DRY   gh ...`)
  and write nothing, exactly like existing gh writes.
- Verify during implementation whether `item-add` on an already-added issue is
  idempotent; treat "already present" as success. Remember this repo's recorded
  gh lessons: `gh issue create` is non-atomic and `--add-blocked-by` is
  non-idempotent -- probe, don't assume.

## Failure semantics

- Projects read failed -> skip the latch entirely for the run (mirrors the
  existing lanes-untouched guard).
- `add_item` fails -> WARN, skip Status write, card untouched.
- Status write fails after a successful add: the original design called this
  acceptable ("the next run re-attempts nothing destructive") -- the PR #68
  adversarial review DISPROVED that (issue #69): membership vetoes Intake, the
  status-less member falls back to a signal-derived stage, and the ordinary
  mover would demote the human-placed card. Amended semantics (implemented):
  (1) the Status write is PREFLIGHTED before `add_item` (`can_set_status`), so
  a doomed write can never create the half-state; (2) if the half-state exists
  anyway (a human half-vets, or gh fails between the calls), a Project member
  with no recognized Status is a *pending latch*: its card is never lane-moved,
  and the Status write is retried from the card's current lane when it
  reverse-maps to a non-Intake stage -- otherwise WARN and hold. Both paths are
  test-pinned, including the two-member regression that keeps the global
  zero-recognized-statuses guard from masking the demotion.

## Testing

- Pure: the `resolve_issue_stage` Intake branch (with/without signals, with/
  without board membership, flag on/off); `stage_for_lane` (unique, ambiguous,
  unmapped).
- Harness (offline, gh + AgilePlace boundaries mocked as in test_run.py /
  test_sync_main.py): Intake card creation targets New Requests; the latch
  fires `item-add` + status write for a card in Approved (and for a card in
  Doing Now, writing In progress); the demotion-trap regression (Intake stage,
  card in Doing Now, promotion path disabled -> NO lane move issued); flag-off
  pin (no Intake mapping -> writes identical to today's).
- Dry/apply parity: planned gh writes match executed ones (test_run pattern).

## Docs

- `.env.example`: the new Intake mapping line with a comment.
- README: the two-state triage flow, and the prerequisite that the Project's
  auto-add workflow stays OFF (auto-add would instantly "vet" every new issue).
- API-VALIDATION.md: record the `item-add`/`item-edit` behaviors observed
  during implementation (idempotency outcome).

## Relationship to other work

- #62 (reverse intake) builds on this state model: cards promoted to issues
  start off-board = Intake. It is deliberately sequenced AFTER this issue.
- The dependency feature's spec
  (docs/superpowers/specs/2026-07-21-blocked-by-dependencies-design.md)
  documents the one-way-structure contract this design preserves: the latch is
  a one-time promotion, not ongoing bidirectional Status sync.
