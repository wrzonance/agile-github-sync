# AgilePlace (Planview LeanKit) io v2 — API validation

Validated the calls this tool makes against the **public** Planview LeanKit io v2 docs and the official
**LeanKit Node client** (source of truth for request shapes), 2026-07-17. Goal: maximize the odds this
works unchanged on a machine that has real API keys. Items marked **[live-check]** are the few that
public docs don't pin down 100% — smoke-test them once against a disposable card.

## Confirmed against docs / official client

| Call (in `agileplace.py`) | Format used | Evidence |
|---|---|---|
| Update card | `PATCH /io/card/{id}` with RFC-6902 JSON Patch array | LeanKit Node client `client.card.update(id, [ops])` |
| **Add tag** | `{op:"add", path:"/tags/-", value:<str>}` | Node client: *"appends the tag… existing tags are preserved"* |
| Move lane | `{op:"replace", path:"/laneId", value:<laneId>}` | Node client (lane change via `card.update`) |
| Optimistic concurrency | `x-lk-resource-version` header (card `version`) | Core-concepts: version via `x-lk-resource-version` header **or** a `/version` test op |
| List cards | `GET /io/card?limit&offset`, read `pageMeta.totalRecords` | Docs: `pageMeta:{totalRecords,offset,limit,startRow,endRow}` — we paginate to exhaustion |
| Board layout | `GET /io/board/{id}` → `lanes[]` with `id/title/cardStatus/parentLaneId/isDefaultDropLane` | io v2 board schema; `cardStatus ∈ {notStarted, started, finished}` (only 3 → we disambiguate In progress / In review by lane title) |
| Tags representation | array of plain strings | add `value` is a string; `card_tags()` reads strings |

Sources: LeanKit io v2 — Update a card, Get a list of cards, Get board, Core concepts
(`success.planview.com/Planview_LeanKit/LeanKit_API/01_v2/...`); official client
`github.com/LeanKit/leankit-node-client`.

## [live-check] — verify once with real keys (a disposable card)

1. **Tag remove.** We send `{op:"remove", path:"/tags", value:<str>}` (LeanKit's documented value-based
   removal; RFC-6902 remove is normally index-based). Confirm an add→remove round-trip clears the tag.
   Fallback if rejected: remove by index (`/tags/{i}`) computed from the card's current tags, applying
   removals in descending index order within one patch.
2. **External link add** (init `04`): `{op:"add", path:"/externalLink", value:{label,url}}` on a card
   that has no link. Confirm `add` succeeds on the absent property (we chose `add` over `replace`).
3. **Version conflict**: edit a card out-of-band, then run `--apply`; confirm the `x-lk-resource-version`
   header produces a clean conflict (HTTP 409/428) rather than a silent stale overwrite. (Current code
   sends the version but does not yet retry-on-conflict — a conflict surfaces as a failed run; add
   refetch-and-recompute if that proves noisy.)

## Model 2 additions — [live-check]

- **`gh project` CLI shapes** (`ghproject.py`, init `05`): `item-list --format json` (field values come
  back as top-level keys — the parse is defensive across casing), `project view` (project id),
  `field-list` (Status field id + option ids), `item-add --url`, `item-edit --single-select-option-id`.
  Needs the `project` token scope. Confirm the item-list JSON shape on your board.
- **Card create** (`POST /io/card`, `agileplace.create_card`): same shape the init used successfully;
  confirm `customId` + `externalLink` are accepted for a fresh card.
- **Parent/child connections** (`connect_children`): posts `card/connections` with
  `{cardIds:[parent], connections:{children:[...]}}` (matching `agileplace.py`; an earlier draft of
  this doc said `card/connect` with `{parentCardId, childCardIds}`, which is NOT what the code
  sends) — **VALIDATE** the exact endpoint/body against the Connections API
  ([create](https://success.planview.com/Planview_LeanKit/LeanKit_API/01_v2/connections/create) /
  connect-many) on a disposable card, and confirm how existing children read back (`card_child_ids`).
- **Planned dates** (Phase 4): `plannedStart`/`plannedFinish` via the card PATCH; Project Start/Target
  date fields via `item-edit --date`.

## GitHub side (standard, stable — noted for completeness)

- `gh issue list --json number,title,state,stateReason,labels,milestone,assignees,url` — stable.
  Issues closed as `NOT_PLANNED`/`DUPLICATE` are filtered out in `list_issues` (not work; must not
  get cards). `stateReason` confirmed a valid `--json` field on the installed gh (2026-07-18).
- Native sub-issues via `gh api graphql` `repository.issue.subIssues` (GitHub 2024+). **[live-check]** on
  the target host/GHES version; `sub_issue_numbers()` returns **None on failure** so `sync.py` warns and
  falls back to the `[KEY]` title convention rather than silently mis-associating.
- Open-PR "in review" signal via `pullRequests.closingIssuesReferences` — standard GraphQL.
- `gh issue edit --remove-milestone` — confirmed present in the installed gh's help (2026-07-18).

## Validated live 2026-07-18 (against People-Places-Solutions/cable-tool + board #158)

During the backlog stand-up these formerly-[live-check] GitHub shapes were exercised for real:

- **Issue dependencies REST**: `GET repos/{owner}/{repo}/issues/{n}/dependencies/blocked_by`
  returns an array of issue objects; `.number` extraction works (`blocked_by_map` is sound).
- **`gh project item-list --format json`**: items carry top-level `id`, flattened lower-case field
  values (`status`), and `content{number,title,url,state}` — exactly what `parse_items` expects.
- **`gh project view --format json`** (`.id`) and **`field-list --format json`**
  (`fields[].id/name/options[].id/name`) — shapes as coded; board #158's Status options are the
  canonical five (Backlog/Ready/In progress/In review/Done). Note field-list's default limit is 30
  (the code already passes `--limit 200`).
- **`gh project item-add`/`item-edit --single-select-option-id`** — used successfully by init `05`.

Two hard-won gh behaviors from the same day, recorded so this repo never relearns them:

1. `gh issue create --type <TYPE>` is NOT atomic: with a type the org lacks, the issue is created
   FIRST and the command then fails -- a blind retry mints a duplicate. (This sync never creates
   issues, but any future issue-writing code must probe `orgs/{org}/issue-types` first.)
2. `gh issue edit --add-blocked-by` is NOT idempotent: re-adding an existing edge fails with
   "Target issue has already been taken", which is success for the caller's intent.
