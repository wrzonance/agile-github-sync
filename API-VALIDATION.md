# AgilePlace (Planview LeanKit) io v2: API validation

This file records how the API calls this tool makes were checked before running against a real
account. On 2026-07-17 each call was compared with the public Planview LeanKit io v2 docs and the
official LeanKit Node client, the best available reference for request shapes. Most calls are
confirmed. A few, marked `[live-check]`, are not fully pinned down by public docs and should be
smoke-tested once against a disposable card. The last section records what was verified against
the live GitHub APIs on 2026-07-18.

## Confirmed against docs and the official client

| Call (in `agileplace.py`) | Format used | Evidence |
|---|---|---|
| Update card | `PATCH /io/card/{id}` with an RFC-6902 JSON Patch array | LeanKit Node client `client.card.update(id, [ops])` |
| Add tag | `{op:"add", path:"/tags/-", value:<str>}` | Node client: "appends the tag... existing tags are preserved" |
| Move lane | `{op:"replace", path:"/laneId", value:<laneId>}` | Node client (lane change via `card.update`) |
| Optimistic concurrency | `x-lk-resource-version` header (card `version`) | Core-concepts doc: version via the `x-lk-resource-version` header or a `/version` test op |
| List cards | `GET /io/card?limit&offset`, read `pageMeta.totalRecords` | Docs: `pageMeta:{totalRecords,offset,limit,startRow,endRow}`; the code paginates to exhaustion |
| Board layout | `GET /io/board/{id}` returns `lanes[]` with `id/title/cardStatus/parentLaneId/isDefaultDropLane` | io v2 board schema; `cardStatus` has only three values (`notStarted`, `started`, `finished`), so In progress and In review are told apart by lane title |
| Tags representation | array of plain strings | the add op's `value` is a string; `card_tags()` reads strings |

Sources: the LeanKit io v2 pages "Update a card", "Get a list of cards", "Get board", and "Core
concepts" (`success.planview.com/Planview_LeanKit/LeanKit_API/01_v2/...`), and the official client
at `github.com/LeanKit/leankit-node-client`.

## [live-check]: verify once with real keys, on a disposable card

1. Tag removal. The code sends `{op:"remove", path:"/tags", value:<str>}`, LeanKit's documented
   value-based removal (standard RFC-6902 remove is index-based). Confirm an add-then-remove
   round-trip clears the tag. If the call is rejected, the fallback is removal by index
   (`/tags/{i}`) computed from the card's current tags, applying removals in descending index
   order within one patch.
2. External link add (init `04`). `{op:"add", path:"/externalLink", value:{label,url}}` on a card
   that has no link yet. Confirm `add` succeeds on the absent property (the code uses `add`, not
   `replace`).
3. Version conflict. Edit a card out-of-band, then run `--apply`. Confirm the
   `x-lk-resource-version` header produces a clean conflict (HTTP 409 or 428) rather than a silent
   stale overwrite. The current code sends the version but does not retry on conflict, so a
   conflict surfaces as a failed run; add refetch-and-recompute if that proves noisy.

## Model 2 additions, also [live-check]

- `gh project` CLI shapes (`ghproject.py`, init `05`): `item-list --format json` (field values
  come back as top-level keys; the parser is defensive about casing), `project view` (project id),
  `field-list` (Status field id and option ids), `item-add --url`, and
  `item-edit --single-select-option-id`. These need the `project` token scope. Confirm the
  item-list JSON shape on your board.
- Card create (`POST /io/card`, `agileplace.create_card`): the same shape the init used
  successfully. Confirm `customId` and `externalLink` are accepted on a fresh card.
- Parent/child connections (`connect_children`): posts `card/connections` with
  `{cardIds:[parent], connections:{children:[...]}}`, matching `agileplace.py`. An earlier draft
  of this doc said `card/connect` with `{parentCardId, childCardIds}`, which is not what the code
  sends. Validate the exact endpoint and body against the Connections API
  ([create](https://success.planview.com/Planview_LeanKit/LeanKit_API/01_v2/connections/create) /
  connect-many) on a disposable card, and confirm how existing children read back
  (`card_child_ids`).
- Planned dates (Phase 4): `plannedStart`/`plannedFinish` via the card PATCH; the Project Start
  and Target date fields via `item-edit --date`.

## GitHub side (standard and stable, noted for completeness)

- `gh issue list --json number,title,state,stateReason,labels,milestone,assignees,url` is stable.
  Issues closed as `NOT_PLANNED` or `DUPLICATE` are filtered out in `list_issues`; they are not
  work and must not get cards. `stateReason` was confirmed a valid `--json` field on the installed
  gh (2026-07-18).
- Native sub-issues via `gh api graphql` and `repository.issue.subIssues` (on GitHub since 2024).
  `[live-check]` on the target host or GHES version. `sub_issue_numbers()` returns None on
  failure, so `sync.py` warns and falls back to the `[KEY]` title convention instead of silently
  mis-associating issues.
- The open-PR "in review" signal comes from `pullRequests.closingIssuesReferences`, standard
  GraphQL.
- `gh issue edit --remove-milestone` was confirmed present in the installed gh's help
  (2026-07-18).

## Validated live on 2026-07-18 (against People-Places-Solutions/cable-tool and board #158)

During the backlog stand-up, these formerly `[live-check]` GitHub shapes were exercised for real:

- Issue dependencies REST: `GET repos/{owner}/{repo}/issues/{n}/dependencies/blocked_by` returns
  an array of issue objects, and `.number` extraction works, so `blocked_by_map` is sound.
- `gh project item-list --format json`: items carry a top-level `id`, flattened lower-case field
  values (`status`), and `content{number,title,url,state}`, which is exactly what `parse_items`
  expects.
- `gh project view --format json` (`.id`) and `field-list --format json`
  (`fields[].id/name/options[].id/name`): shapes as coded. Board #158's Status options are the
  standard five (Backlog / Ready / In progress / In review / Done). Note that field-list's default
  limit is 30; the code already passes `--limit 200`.
- `gh project item-add` and `item-edit --single-select-option-id` were used successfully by init
  `05`.

Two gh behaviors learned the same day, recorded so this repo never has to rediscover them:

1. `gh issue create --type <TYPE>` is not atomic. With a type the org lacks, the issue is created
   first and the command then fails, so a blind retry creates a duplicate. This sync never creates
   issues, but any future issue-writing code must probe `orgs/{org}/issue-types` first.
2. `gh issue edit --add-blocked-by` is not idempotent. Re-adding an existing edge fails with
   "Target issue has already been taken", which for the caller's intent means the edge is already
   there.
