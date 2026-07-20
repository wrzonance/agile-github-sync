# AgilePlace (Planview LeanKit) io v2: API validation

This file records how the API calls this tool makes were checked before running against a real
account. On 2026-07-17 each call was compared with the public Planview LeanKit io v2 docs and, where
it documents the same operation, the official LeanKit Node client. Most calls are confirmed. A few,
marked `[live-check]`, are not fully pinned down by public docs and should be smoke-tested once
against a disposable card. The last section records what was verified against the live GitHub APIs
on 2026-07-18.

## Confirmed against the cited sources

| Call (in `agileplace.py`) | Format used | Evidence |
|---|---|---|
| Update card | `PATCH /io/card/{id}` with an RFC-6902 JSON Patch array | io v2 "Update a card" docs; the Node client maps `card.update(id, ops)` to this PATCH and forwards the operation array unchanged |
| Add tag | `{op:"add", path:"/tags/-", value:<str>}` | Node client: "appends the tag... existing tags are preserved" |
| Move lane | `{op:"replace", path:"/laneId", value:<laneId>}` | io v2 "Update a card" docs (`/laneId` replace example) |
| Optimistic concurrency | `x-lk-resource-version` header (card `version`) | Core-concepts doc: version via the `x-lk-resource-version` header or a `/version` test op |
| List cards | `GET /io/card?limit&offset`, read `pageMeta.totalRecords` and `pageMeta.limit` | Docs: `pageMeta:{totalRecords,offset,limit,startRow,endRow}`; the code advances by the returned card count, honors a server-clamped limit, and fails closed at a defensive request ceiling |
| Board layout | `GET /io/board/{id}` returns `lanes[]` with `id/title/cardStatus/parentLaneId/isDefaultDropLane` | io v2 board schema; `cardStatus` has only three values (`notStarted`, `started`, `finished`), so In progress and In review are told apart by lane title |
| Tags representation | array of plain strings | io v2 card/update schemas; the Node client's add-tag example appends a string |

Sources: the LeanKit io v2 pages "Update a card", "Get a list of cards", "Get board", and "Core
concepts" (`success.planview.com/Planview_AgilePlace/AgilePlace_API/01_v2/...`), and the official client
at `github.com/LeanKit/leankit-node-client`.

The Node client is evidence only for the rows that name it: it forwards update operations without
interpreting their paths, and it does not add `x-lk-resource-version`. The lane operation and
optimistic-concurrency claims therefore rely on the io v2 update-card and core-concepts docs,
respectively.

## [live-check]: verify once with real keys, on a disposable card

1. Tag removal. The code now sends standard RFC-6902 index-based removal --
   `{op:"remove", path:"/tags/{i}"}`, no `value` member -- with indices computed from the card's
   current tags and removals applied in descending index order within one patch (`agileplace.py`'s
   `ops_tag_remove`), so an earlier removal in the batch never shifts a later op's target index.
   This replaces the previously used value-based form (`{op:"remove", path:"/tags", value:<str>}`).
   The current io v2 "Update a card" docs describe both forms: `/tags/{i}` removes by position,
   while `/tags` plus `value` removes by value. This implementation intentionally uses the
   deterministic index form: it follows RFC 6902's standard `remove` shape and ties each removal to
   the versioned tag snapshot used to build the batch. The Node client README documents tag *add*
   only and is not evidence for either removal form. Still needs a human-run live check: confirm an
   add-then-remove round-trip on a disposable card actually clears the tag (not attempted here -- no
   live AgilePlace credentials available/authorized for this task).
2. External link add (init `04`). `{op:"add", path:"/externalLink", value:{label,url}}` on a card
   that has no link yet. Confirm `add` succeeds on the absent property (the code uses `add`, not
   `replace`).
3. Version conflict. Edit a card out-of-band, then run `--apply`. Confirm the
   `x-lk-resource-version` header produces a clean conflict (HTTP 409 or 428) rather than a silent
   stale overwrite. The current code sends the version but does not retry on conflict, so a
   conflict surfaces as a failed run; add refetch-and-recompute if that proves noisy.
4. Single-card GET response shape (`agileplace.get_card`, `GET /io/card/{id}`, issue #8). Docs
   don't confirm whether this wraps the payload as `{"card": {...}}` (like `list_cards`' `cards`
   array) or returns the card fields flat. `get_card` defensively unwraps either shape
   (`data.get("card", data)`), so both possibilities are handled without a human needing to pick
   one first. Confirm on a live card and, if it's always one shape, the unwrap can be simplified.
5. Card create response `version` field presence (`agileplace.create_card`, `POST /io/card`, issue
   #8). Docs don't confirm whether the create response includes a resource `version` for the new
   card. If it does, a card created earlier in the same run could skip `_card_with_version`'s
   refetch and PATCH immediately with the version from the create response. Until confirmed,
   `patch_card` treats every version-less card the same way regardless of origin: refetch via
   `get_card`, then PATCH only when the refetch has a usable version and every queued field still
   matches the original snapshot. A failed validation warns and aborts before sync state is saved
   (see `[live-check]` item 4 and `_card_with_version` in `agileplace.py`). Confirming this field
   would let a follow-up pass the create response's version straight through instead of paying for
   an extra refetch.

Together, items 4 and 5 are what closes the fail-open optimistic-concurrency gap from issue #8
(`patch_card` no longer ever sends an unversioned PATCH) pending those two live confirmations --
today the code fails closed (refetch, validate, or abort) rather than assuming either shape.

## Model 2 additions, also [live-check]

- `gh project` CLI shapes (`ghproject.py`, init `05`): `item-list --format json` (Status comes back
  as a top-level key; the parser is defensive about casing), `project view` (project id),
  `field-list` (Status field id and option ids), `item-add --url`, and
  `item-edit --single-select-option-id`. These need the `project` token scope. Confirm the
  item-list JSON shape on your board.
- Card create (`POST /io/card`, `agileplace.create_card`): the same shape the init used
  successfully. Confirm `customId` and `externalLink` are accepted on a fresh card.
- Parent/child connections (`connect_children`): posts `card/connections` with
  `{cardIds:[parent], connections:{children:[...]}}`, matching `agileplace.py`. An earlier draft
  of this doc said `card/connect` with `{parentCardId, childCardIds}`, which is not what the code
  sends. Validate the exact endpoint and body against the Connections API
  ([create](https://success.planview.com/Planview_AgilePlace/AgilePlace_API/01_v2/connections/create) /
  connect-many) on a disposable card, and confirm how existing children read back
  (`card_child_ids`).
- Planned dates (Phase 4): `plannedStart`/`plannedFinish` via the card PATCH; Project Start and
  Target values read from paginated GraphQL `ProjectV2Item.fieldValues` by field id; writes via
  `item-edit --date`. A successful GraphQL snapshot with no matching values means the field is
  cleared project-wide; a failed, malformed, or incomplete snapshot skips every date write that run.

## GitHub side (standard and stable, noted for completeness)

- `gh issue list --json number,title,state,stateReason,labels,milestone,assignees,url` is stable.
  Issues closed as `NOT_PLANNED` or `DUPLICATE` stay in `list_issues` as normalized retirement facts:
  they count as Done blockers, never enter card creation, and retire any existing URL-matched card
  to Done. `stateReason` was confirmed a valid `--json` field on the installed gh (2026-07-18).
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
  values (`status`), and `content{type,body,title,number,repository,url}` (no `state`) in gh 2.96.0.
  `parse_items` consumes the `type`, `number`, and `url` members.
- `gh project view --format json` (`.id`) and `field-list --format json`
  (`fields[].id/name/options[].id/name`): shapes as coded. Board #158's Status options are the
  standard five (Backlog / Ready / In progress / In review / Done). Note that field-list's default
  limit is 30; the code already passes `--limit 200`.
- `gh project item-add` and `item-edit --single-select-option-id` were used successfully by init
  `05`.

On 2026-07-20, the paginated GraphQL date-read shape was also exercised against wrzonance Project
`#4` (192 items across two pages). `ProjectV2Item.fieldValues` returned complete nested pagination
metadata, and the successful zero-date result remained distinguishable from a query failure.

Two gh behaviors learned the same day, recorded so this repo never has to rediscover them:

1. `gh issue create --type <TYPE>` is not atomic. With a type the org lacks, the issue is created
   first and the command then fails, so a blind retry creates a duplicate. This sync never creates
   issues, but any future issue-writing code must probe `orgs/{org}/issue-types` first.
2. `gh issue edit --add-blocked-by` is not idempotent. Re-adding an existing edge fails with
   "Target issue has already been taken", which for the caller's intent means the edge is already
   there.
