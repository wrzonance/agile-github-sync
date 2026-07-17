# Design — Model 2: full per-issue AgilePlace mirror

Status: Draft for review · Date: 2026-07-17 · Supersedes the sync portions of the bifurcation spec
(`…/backlog-cross-platform-sync-design.md`); init/scrub parts of that spec still stand.

## Why this evolves the built tool

What's built (**Model 1**): one AgilePlace card per *epic*; each epic's lane is a rollup of its tasks'
stages, where a stage is *derived* from issue state + `agent:*` labels + open PRs; bidirectional
labels/milestone ↔ tags; `STAGE_LANE_MAP`. Tasks have no card; no hierarchy, dependencies, or dates on
AgilePlace.

Research confirmed AgilePlace's io v2 supports **parent/child card connections** (with native rollup of
child progress + dates to the parent), **planned dates** (`plannedStart`/`plannedFinish`), and
**Enhanced Dependency Management** (card-to-card, Finish-to-Start). GitHub already carries the matching
structure (sub-issues, blocked-by) and — via **Projects v2** — a canonical **Status** field and **date**
fields. So the board can be a *faithful mirror of the GitHub work graph* instead of a lossy rollup.

## Decisions (all confirmed with the user)

1. **Model 2 — one card per GitHub issue** (epics *and* tasks). GitHub **sub-issues → AgilePlace
   parent/child connections**; LeanKit then rolls child progress/dates up to the parent natively (this
   *removes* the hand-written `epic_rollup`).
2. **Projects v2 is the Status source of truth ("Both").** Onboarding **adds each issue to the Project
   and sets its Status**; the sync **reads each issue's Project Status** for its stage (replacing the
   label/PR derivation). Needs `GH_PROJECT_OWNER` + `GH_PROJECT_NUMBER` and the **`project` token scope**.
3. **Lane = the issue's Project Status → `STAGE_LANE_MAP` → lane** (leaf-only, fail-closed on ambiguity).
   Unchanged mechanism; new source.
4. **Dependencies** — GitHub **blocked-by → AgilePlace Finish-to-Start** dependency. ("Can't start until
   X finishes.")
5. **Dates — bidirectional, AgilePlace-wins**, via the existing 3-way merge (`.sync-state.json` base,
   no per-field timestamps needed — AgilePlace lacks them; GitHub Projects v2 has `updatedAt` but we
   don't rely on it). GitHub Project **Start/Target date fields ↔ `plannedStart`/`plannedFinish`**.
6. **Bidirectional metadata** (labels/milestone ↔ tags) and **`STAGE_LANE_MAP`**: unchanged, carry over.
7. **Cleaner init/sync split:** the **init kit no longer touches AgilePlace at all** (drop `04`, its
   `agileplace.py`, and the AgilePlace token) — it does GitHub setup only: labels, milestones, issues,
   sub-issues, blocked-by, add-to-Project, initial Status. The **sync becomes the sole owner of the
   AgilePlace mirror**: it *creates* the per-issue cards and maintains their lanes, connections,
   dependencies, dates, and tags. One authority, no duplicated board logic.

## Data model & card identity

- One card per issue, matched by **external-link URL == issue URL** (fallback `customId == [KEY]`).
- Stage ← the issue's **Projects v2 Status** (canonical Backlog/Ready/In progress/In review/Done).
- Hierarchy ← GitHub **sub-issues** → connections. Dependencies ← **blocked-by** → F-to-S.
- Dates ↔ Projects v2 date fields; tags ↔ labels+milestone.
- `.sync-state.json` (target-scoped, URL-keyed, atomic) holds the per-issue **base** for tag-set and
  each date (the merge base for bidirectional reconcile).

## Sync algorithm (revised)

1. **Read GitHub:** all issues; Projects v2 items (Status + date fields); the sub-issue graph and the
   blocked-by graph (GraphQL).
2. **Read AgilePlace:** all cards (paginated) + their connections, dependencies, tags, dates.
3. **Ensure a card per issue** (create missing, with external link + customId).
4. **Per card:** set lane from Status (`STAGE_LANE_MAP`); reconcile **tags** (labels/milestone, 3-way);
   reconcile **dates** (bidirectional 3-way, AgilePlace-wins) ↔ Project date fields; ensure **parent/
   child connections** equal the sub-issue graph; ensure **F-to-S dependencies** equal blocked-by.
5. **Write-back to GitHub** only for the bidirectional fields (dates, and the labels/milestone already
   built) via `gh` + the Projects v2 GraphQL mutations. Lanes/connections/deps are GitHub→AgilePlace
   (derived), one-way.
6. Advance `.sync-state.json` only on `--apply`.

## New config

```
GH_PROJECT_OWNER=<user-or-org>      # e.g. @me or your-org
GH_PROJECT_NUMBER=<n>               # the Projects v2 board number
GH_PROJECT_STATUS_FIELD=Status      # single-select field name (default Status)
GH_PROJECT_START_FIELD=Start        # date field <-> plannedStart
GH_PROJECT_TARGET_FIELD=Target      # date field <-> plannedFinish
```
Plus the `project` scope: `gh auth refresh -s project`.

## API surface to validate live (first-run, on a disposable board/card)

- **Projects v2 (GraphQL):** `addProjectV2ItemById`; read item Status + date field values;
  `updateProjectV2ItemFieldValue` for Status and dates. (Well-documented; standard.)
- **AgilePlace connections:** `POST /io/connections` / `connect-many`, list, delete. Confirmed feature;
  verify exact request/response.
- **AgilePlace dependencies:** **VERIFY io v2 coverage** — Enhanced Dependency Management is newer and
  its API surface is not yet confirmed. If there is no write API, fall back to representing "blocked-by"
  via the card **Blocked** state + a note, and revisit when the API lands.
- **AgilePlace dates:** `plannedStart`/`plannedFinish` via the card PATCH (JSON Patch), read on card GET.
- **Tag add/remove** (`/tags/-`, value-remove) — already flagged in `API-VALIDATION.md`.

## Phasing (build once, in reviewable slices; each keeps the tool green)

- **Phase 0 (kept):** current tool — bidirectional tags, `STAGE_LANE_MAP`, lane movement. Done.
- **Phase 1 — Projects v2 as Status source ("Both"):** onboarding adds issues to the Project + sets
  Status; sync reads Status (replaces label/PR derivation). Config + `project` scope.
- **Phase 2 — per-issue cards + parent/child connections:** sync creates a card per issue and mirrors
  the sub-issue hierarchy; drop epic-only rollup (LeanKit rolls up natively); retire init `04`.
- **Phase 3 — dependencies:** blocked-by → Finish-to-Start (pending the dependency-API check).
- **Phase 4 — bidirectional dates:** Project date fields ↔ `plannedStart`/`plannedFinish`, AgilePlace-wins.

## Risks / open

- **Dependency API coverage** (Phase 3) is the one genuine unknown — verify before committing to it.
- **Volume:** per-issue cards on large repos — pagination is already handled; connection/date calls are
  O(issues), fine at this scale.
- **Parent (epic) card lane:** LeanKit rolls up child *stats/dates* to the parent but not its *lane*, so
  the epic card's lane still comes from the epic issue's own Project Status (consistent with Model 2).
- This supersedes the "one card per epic" line in the original mvp breakdown (which now lives in the
  init kit, not either repo).
