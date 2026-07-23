# AgilePlace (Planview LeanKit) io v2: API validation

This file records how the API calls this tool makes were checked before running against a real
account. On 2026-07-17 each call was compared with the public Planview LeanKit io v2 docs and, where
it documents the same operation, the official LeanKit Node client. Most calls are confirmed. A few,
marked `[live-check]`, are not fully pinned down by public docs and should be smoke-tested once
against a disposable card. The last sections record what was verified live: GitHub shapes on
2026-07-18, and AgilePlace write paths on 2026-07-20 and 2026-07-21 (`smoke.py` runs).

## Confirmed against the cited sources

| Call (in `agileplace.py`) | Format used | Evidence |
|---|---|---|
| Update card | `PATCH /io/card/{id}` with an RFC-6902 JSON Patch array | io v2 "Update a card" docs; the Node client maps `card.update(id, ops)` to this PATCH and forwards the operation array unchanged |
| Add tag | `{op:"add", path:"/tags/-", value:<str>}` | Node client: "appends the tag... existing tags are preserved" |
| Move lane | `{op:"replace", path:"/laneId", value:<laneId>}` | io v2 "Update a card" docs (`/laneId` replace example) |
| Optimistic concurrency | `x-lk-resource-version` header (card `version`) | Core-concepts doc: version via the `x-lk-resource-version` header or a `/version` test op |
| List cards | `GET /io/card?limit&offset`, read `pageMeta.totalRecords` and `pageMeta.limit` | Docs: `pageMeta:{totalRecords,offset,limit,startRow,endRow}`; the code advances by the returned card count, honors a server-clamped limit, and fails closed at a defensive request ceiling |
| List child cards | `GET /io/card/{cardId}/connection/children?limit&offset`, read `cards[]` and `pageMeta` | io v2 "Get a list of child cards" documents the singular path, pagination parameters, and successful `{pageMeta,cards}` response; the code rejects malformed, inconsistent, or incomplete snapshots |
| Board layout | `GET /io/board/{id}` returns `lanes[]` with `id/title/cardStatus/parentLaneId/isDefaultDropLane` | io v2 board schema; `cardStatus` has only three values (`notStarted`, `started`, `finished`), so In progress and In review are told apart by lane title |
| Tags representation | array of plain strings | io v2 card/update schemas; the Node client's add-tag example appends a string |

Sources: the LeanKit io v2 pages "Update a card", "Get a list of cards",
["Get a list of child cards"](https://success.planview.com/Planview_AgilePlace/AgilePlace_API/01_v2/connections/children),
"Get board", and "Core concepts" (`success.planview.com/Planview_AgilePlace/AgilePlace_API/01_v2/...`),
and the official client at `github.com/LeanKit/leankit-node-client`.

The Node client is evidence only for the rows that name it: it forwards update operations without
interpreting their paths, and it does not add `x-lk-resource-version`. The lane operation and
optimistic-concurrency claims therefore rely on the io v2 update-card and core-concepts docs,
respectively.

## [live-check]: verify once with real keys, on a disposable card

> `python smoke.py` automates these checks (plus the Connections round-trip below): it previews the
> configured board, asks for confirmation, then exercises every write shape on two throwaway cards
> and reports PASS/FAIL per item with the server's full response body on any rejection.
>
> **2026-07-20: a smoke.py run against the production tenant confirmed every numbered item below**
> -- see "Validated live on 2026-07-20 (smoke.py run)" at the end of this file for the outcomes.
> Each entry keeps its original rationale and carries its confirmed outcome inline.

1. Tag removal. The code now sends standard RFC-6902 index-based removal --
   `{op:"remove", path:"/tags/{i}"}`, no `value` member -- with indices computed from the card's
   current tags and removals applied in descending index order within one patch (`agileplace.py`'s
   `ops_tag_remove`), so an earlier removal in the batch never shifts a later op's target index.
   This replaces the previously used value-based form (`{op:"remove", path:"/tags", value:<str>}`).
   The current io v2 "Update a card" docs describe both forms: `/tags/{i}` removes by position,
   while `/tags` plus `value` removes by value. This implementation intentionally uses the
   deterministic index form: it follows RFC 6902's standard `remove` shape and ties each removal to
   the versioned tag snapshot used to build the batch. The Node client README documents tag *add*
   only and is not evidence for either removal form. Confirmed live 2026-07-20: an add-then-remove
   round-trip on a disposable card cleared the tag (outcome 1 below).
2. External link add (init `04`). `{op:"add", path:"/externalLink", value:{label,url}}` on a card
   that has no link yet. Confirmed live 2026-07-20: `add` succeeds on the absent property (outcome 2
   below; the code uses `add`, not `replace`).
3. Version conflict. Edit a card out-of-band, then run `--apply`. Confirmed live 2026-07-20: a
   stale `x-lk-resource-version` header produces a clean HTTP 428 rejection, not a silent stale
   overwrite (outcome 3 below). The current code sends the version but does not retry on conflict,
   so a conflict surfaces as a failed run; add refetch-and-recompute if that proves noisy.
4. Single-card GET response shape (`agileplace.get_card`, `GET /io/card/{id}`, issue #8). Docs
   don't confirm whether this wraps the payload as `{"card": {...}}` (like `list_cards`' `cards`
   array) or returns the card fields flat. `get_card` defensively unwraps either shape
   (`data.get("card", data)`), so both possibilities are handled without a human needing to pick
   one first. Confirmed live 2026-07-20: the response is flat (outcome 4 below); the defensive
   unwrap stays as harmless cover for both shapes.
5. Card create response `version` field presence (`agileplace.create_card`, `POST /io/card`, issue
   #8). Docs don't confirm whether the create response includes a resource `version` for the new
   card. If it does, a card created earlier in the same run could skip `_card_with_version`'s
   refetch and PATCH immediately with the version from the create response. Until confirmed,
   `patch_card` treats every version-less card the same way regardless of origin: refetch via
   `get_card`, then PATCH only when the refetch has a usable version and every queued field still
   matches the original snapshot. A failed validation warns and aborts before sync state is saved
   (see `[live-check]` item 4 and `_card_with_version` in `agileplace.py`). Confirmed live
   2026-07-20: the create response carries no version (outcome 5 below), so the refetch is required
   behavior and the contemplated pass-through follow-up is moot.

Together, items 4 and 5 are what closes the fail-open optimistic-concurrency gap from issue #8
(`patch_card` no longer ever sends an unversioned PATCH). Both were confirmed live on 2026-07-20;
the code still fails closed (refetch, validate, or abort) rather than assuming either shape.

## Model 2 additions, also [live-check]

- `gh project` CLI shapes (`ghproject.py`, init `05`): `item-list --format json` (Status comes back
  as a top-level key; the parser is defensive about casing), `project view` (project id),
  `field-list` (Status field id and option ids), `item-add --url`, and
  `item-edit --single-select-option-id`. These need the `project` token scope. Confirmed live
  2026-07-18 on board #158 (see "Validated live on 2026-07-18" below).
- Card create (`POST /io/card`, `agileplace.create_card`): the same shape the init used
  successfully. Confirmed live 2026-07-20: `customId` and `externalLink` were accepted on a fresh
  card, both with and without an external link.
- Parent/child connections: `card_child_ids` reads each epic through the documented singular
  `GET card/{cardId}/connection/children` endpoint and paginates only while `pageMeta` remains
  complete and internally consistent. A failed or malformed read returns a distinct unavailable
  result, warns, and makes that epic's reconciliation add-only; a successful empty `cards` array is
  still authoritative. `connect_children`/`disconnect_children` write `card/connections` with
  `{cardIds:[parent], connections:{children:[...]}}`. Confirmed live 2026-07-20: a disposable-card
  read/connect/disconnect round-trip matched the documented read response and the Connections API
  write shape
  ([create](https://success.planview.com/Planview_AgilePlace/AgilePlace_API/01_v2/connections/create) /
  connect-many) in the production tenant.
- Planned dates (Phase 4): `plannedStart`/`plannedFinish` via the card PATCH; Project Start and
  Target values read from paginated GraphQL `ProjectV2Item.fieldValues` by field id; writes via
  `item-edit --date`. A successful GraphQL snapshot with no matching values means the field is
  cleared project-wide; a failed, malformed, or incomplete snapshot skips every date write that run.
  The GraphQL date-read shape was exercised live on 2026-07-20 (see below). The card PATCH was
  live-run on 2026-07-21: setting both dates as strings is confirmed, but clearing them with a null
  `replace` was rejected (HTTP 422 "Invalid value: must be string"), so clears now use an RFC-6902
  `remove` op (issue #52). A second run the same day, after the fix merged, confirmed the
  remove-based clear and the blocked-state clear live -- see "Validated live on 2026-07-21" below.

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

## Validated live on 2026-07-20 (smoke.py run, production tenant board)

A confirmed `python smoke.py` run (create -> mutate -> connect -> stale-probe -> delete on two
throwaway cards) retired every AgilePlace `[live-check]` item above except the planned-date card
PATCH -- smoke steps 5-6 (blocked state + planned dates) were added after this run; their first
live outcomes are recorded in the 2026-07-21 section below:

1. **Tag removal (item 1): confirmed.** Index-based `{op:"remove", path:"/tags/{i}"}` cleared the
   tag in an add-then-remove round-trip (`tags` ended empty).
2. **External link add (item 2): confirmed.** `{op:"add", path:"/externalLink", value:{label,url}}`
   succeeded on a card created with no link.
3. **Version conflict (item 3): confirmed.** A PATCH with a stale `x-lk-resource-version` was
   rejected with **HTTP 428 Precondition Required**; the body shows the server running a JSON-Patch
   `test` op on `/version` built from the header (`error: "Test operation failed"`, with
   `actualValue` reporting the current version). Optimistic concurrency is real; no silent stale
   overwrite. A conflict surfaces as a failed run, as coded.
4. **Single-card GET shape (item 4): confirmed FLAT.** `GET /io/card/{id}` returns the card fields
   at the top level, not wrapped in `{"card": ...}`. `get_card`'s defensive unwrap stays (it is
   harmless and covers both shapes), but the wrapped branch is now known to be unused live.
5. **Card create response `version` (item 5): confirmed ABSENT.** The create response carries no
   resource version, so `patch_card`'s refetch-before-PATCH path for version-less cards is
   *required* behavior, not an optimization opportunity -- the contemplated follow-up (reusing the
   create response's version to skip the refetch) is moot.

Model 2 additions confirmed by the same run: card create with `customId`+`externalLink` and without
an external link both accepted; `card/connections` connect/disconnect round-trip works and the
children read reflects it immediately (including the authoritative empty read after disconnect).
`DELETE /io/card/{id}` (smoke cleanup only) deletes for real -- a follow-up GET returns 404.

## Validated live on 2026-07-21 (smoke.py runs, production tenant board)

Two runs: the first exercised steps 5-6 (blocked state + planned dates, added after the 2026-07-20
run) and surfaced the date-clear rejection; the second, after the issue #52 fix merged, passed the
full sequence:

1. **Blocked-state set: confirmed.** `/isBlocked` true + `/blockReason` string round-trips
   (`isBlocked=True`, reason read back).
2. **Planned-date set: confirmed.** String `replace` on `/plannedStart` and `/plannedFinish`
   round-trips (`2026-01-01`/`2026-01-02` read back exactly).
3. **Planned-date clear via null replace: REJECTED -- and the rejection taught two facts.**
   `{op:"replace", path:"/plannedStart", value:null}` fails HTTP 422 "Invalid value: must be
   string": the server type-validates `replace` values on these paths. And PATCH validation is
   atomic: the 422 listed only the two date ops as invalid, yet no op in the batch (including the
   valid blocked-clear ops) was applied. `op_planned_date` now emits `{op:"remove", path:"/{field}"}`
   for a clear, and the offline smoke double mirrors the 422 (issue #52).
4. **Remove-based date clear + blocked-state clear: confirmed.** The second (post-fix) run's step 6
   read back `isBlocked=False`, `plannedStart=None`, `plannedFinish=None`: the RFC-6902 `remove`
   clears both dates, and the blocked-clear ops apply once no invalid op poisons the batch.

Both runs also re-confirmed create (`customId`+`externalLink`), the version-less create response,
flat single-card GET, both tag round-trips, the connections round-trip, the stale-version HTTP 428
rejection, and `DELETE` + 404 cleanup.

The same day's first live migration (53 cards, issue #55) established one more create-response
fact: it echoes neither `customId` nor `laneId` -- for sync purposes the response is the new card
id and nothing else. `sync.py` therefore refetches each just-created card once and indexes the
fresh card as its snapshot, and the offline sync-run double mirrors the id-only response.

With that, every AgilePlace `[live-check]` item in this file is retired -- each one now has a
recorded live outcome.

## Dependencies API discovery (2026-07-21, issue #57)

The io v2 public docs do not document the Dependencies feature's endpoints, so
`probe_dependencies.py` (strictly read-only; the GET-only invariant is test-pinned) probed the
production tenant:

- **Read endpoint confirmed: `GET /io/card/{cardId}/dependency` -> HTTP 200 `{"dependencies":[]}`.**
  Singular path, matching the documented `connection/children` singular pattern.
- All other candidates returned a clean 404: `card/{cardId}/dependencies`,
  `dependencies?cardId=`, `dependency?cardId=`, `board/{boardId}/dependencies`,
  `card/{cardId}/connection/dependencies`. The documented `connection/children` control endpoint
  answered 200 in the same run, so those 404s are real misses, not plumbing failures.
- The single-card GET embeds no dependency data: none of its 49 top-level keys is dependency-ish
  (it does embed `parentCards` and `connectedCardStats` for the connections feature).

**Populated entry shape: confirmed live 2026-07-21.** After a UI-created dependency
(EP-3A -> JPOWER1, mirroring the real GitHub blocked-by edge #31 -> #68), the blocked card's read
returned:

```json
{"dependencies": [{"direction": "incoming", "cardId": "2490185684",
                   "timing": "finishToStart", "createdOn": "2026-07-21T16:19:24.147Z"}]}
```

- `direction` is relative to the card being read (`incoming` = the other card must progress first);
  `cardId` is the OTHER end; `timing` uses camelCase enum values (`finishToStart` observed).
- **Entries carry no dependency id.** A dependency is evidently identified by its
  (card, direction, other-card, timing) tuple, which implies deletion is addressed by card pair,
  not by id -- the delete capture below must confirm how.

**Delete shape: confirmed live 2026-07-21** (devtools capture of the UI removing the EP-3A ->
JPOWER1 dependency):

```http
DELETE /io/card/dependency
{"cardIds": ["2490186236"], "dependsOnCardIds": ["2490185684"]}
```

Pair-addressed, as the id-less read entries implied: `cardIds` is the dependent (blocked) side,
`dependsOnCardIds` the blocker side, both plural batch arrays in the same style as
`card/connections`. No card id in the path; the UI's own session auth rode a cookie, but the
endpoint lives under the same `/io/` surface as everything the token-authenticated client uses.

**Create shape: confirmed live 2026-07-21** (devtools capture of the UI recreating the same
dependency):

```http
POST /io/card/dependency
{"cardIds": ["2490186236"], "dependsOnCardIds": ["2490185684"], "timing": "finishToStart"}
```

Same pair body as delete plus an explicit `timing` member (camelCase enum, `finishToStart`
observed). The POST *response* returns the card's updated `dependencies` list where each entry
additionally carries a `face` projection of the other card -- including a `dependencyStats` object
(`incoming/outgoing/total` x `Count/ResolvedCount/ExceptionCount/UnresolvedBlockedCount`), which is
the native health tracking. Plain reads (`GET card/{cardId}/dependency`) omit `face`; the sync needs
only `direction`/`cardId`/`timing`.

**Timing types.** The UI offers four: FS (Finish->Start), SS (Start->Start), FF (Finish->Finish),
SF (Start->Finish). Only `finishToStart`'s wire value has been observed; the other three
presumably follow the same camelCase pattern but are unconfirmed and unused -- GitHub blocked-by
has exactly one semantic ("the blocker must finish first"), which is FS. The sync reconciles
dependencies by card PAIR and never inspects or rewrites `timing` after creation: a human refining
a synced dependency's timing in the UI keeps their refinement.

**Round-trip and duplicate-create: confirmed live 2026-07-21** (`smoke.py` steps 11-12, run
suffix 10a01e, production tenant): create -> incoming read (right card, `finishToStart`) ->
delete -> empty read all behaved as coded. Duplicate create is REJECTED -- **HTTP 409 Conflict
`{"message": "Dependency already exists", "data": {"dependsOnCardId", "cardId"}}`** -- not
idempotent. The sync's diff-before-write never re-creates an existing pair, and its fail-closed
skip on unknown reads is what keeps a blind re-create (and its 409 SystemExit) impossible. The
offline doubles mirror the 409. No dependency `[live-check]` remains open.

## Vetting-latch gh writes (issue #63) -- `[live-check]` pending

The Intake latch introduces the sync's first Project writes: `gh project item-add --url` and
`item-edit --single-select-option-id` (both shapes used successfully by init `05` on 2026-07-18;
`set_item_status` resolves field/option ids through the same `field-list` reads the date sync
already trusts). Two behaviors remain `[live-check]` pending:

1. **`item-add` idempotency on an already-present issue.** The code treats "already present" as
   success either way, so this is fact-finding. A live probe was deliberately NOT run during
   implementation -- it would have written to the production Project board without explicit
   authorization (the implementation agent's attempt was correctly refused on exactly those
   grounds). The latch's first real promotion confirms the behavior; record the outcome here.
2. **Status write on a just-added item** (add -> immediate `item-edit` in one run). The failure
   path was hardened by issue #69 after adversarial review disproved the original "boarded
   Status-less is harmless" assumption: the write is now preflighted before the add
   (`can_set_status`), and a member left Status-less anyway becomes a pending latch (card held,
   Status retried from its lane) -- all test-pinned. The happy path awaits the same first real
   promotion.

## Reverse intake (issue #62) -- spike findings, `[live-check]` pending

Reverse intake (`intake.py`) writes an `/externalLink` back onto the AgilePlace card that a new
GitHub issue was created from, so the marker-resume scan can find it again next run. A design
spike (before implementation) probed four open questions that could not be settled from docs
alone, since this worktree has no `.env` and no live board to probe against:

1. **`/externalLink` singular-`add` is the confirmed-safe write shape.** The existing
   `[live-check]`-confirmed init `04` result above (`{op:"add", path:"/externalLink",
   value:{label,url}}` succeeds on a card with no link yet) covers exactly the case reverse
   intake needs -- an Intake-lane card that has never been linked to an issue. `_writeback` in
   `intake.py` uses this confirmed shape and nothing else.
2. **The plural `/externalLinks` array shape is UNCONFIRMED and intentionally not attempted.**
   Cards *can* carry more than one external link in the AgilePlace UI, which raises the
   possibility of a separate array-typed patch path for adding a second link without clobbering
   the first. No doc or prior `[live-check]` run in this file confirms that shape's op/path/value,
   and no live probe was possible here. `intake.py`'s `op_external_link` ships only the singular
   `add`; a card that already has a different external link is treated as the array-shaped case
   (see finding 3) rather than guessed at.
3. **The externalLink writeback has NO conflict-retry support -- this is intentional, not an
   oversight.** `agileplace.py`'s `_card_value_for_patch_path` (the table the 409/428
   conflict-retry path in `agileplace.patch_card` uses to recompute a stale op's value before
   retrying) does not recognize `/externalLink` at all -- and, per `agileplace.py`'s own file-budget
   note (805/800 lines before this feature existed; see `intake.py`'s module docstring), extending
   it to do so isn't free. So unlike the `customId` writeback (which does retry once on a version
   conflict), a 409/428 hit on the link write is uncaught and propagates as a failed run.
   `_writeback` in `intake.py` issues the `customId` write and the link write as two SEPARATE
   `patch_card` calls, `customId` FIRST (fixed post-review, issue #62 follow-up): writing the
   more-reliable, retry-supported join-key write first means any failure partway through leaves the
   card either fully unwritten (still a full candidate; the next run's marker-resume scan retries
   the whole writeback) or customId-written-but-link-missing (still fully tracked -- matched and
   reconciled by the ordinary sync via its customId -- just missing the informational external-link
   decoration, which is never independently retried). The original link-first ordering could
   instead strand a card permanently: a link write that succeeded followed by a customId write that
   failed left a card matching a known target URL (disqualifying it from `_is_candidate`) whose join
   key was never established, with no further retry path at all.
   **CRITICAL bug fixed post-review (issue #62 follow-up):** this two-separate-`patch_card`-calls
   design has a sharper failure mode than "no retry on a genuine concurrent edit" -- the customId
   write ITSELF bumps the card's server-side version, so the link write, issued against the SAME,
   now-stale `card` snapshot, deterministically conflicted on every real apply=True writeback
   against a card with a usable version (the ordinary `agileplace.list_cards()` case) -- and because
   `_card_value_for_patch_path` doesn't recognize `/externalLink`, the conflict-retry path always
   refused to recover (unsupported path -> always treated as "changed"), re-raising and aborting the
   entire sync run. `_writeback` now routes the link write's card through `_card_for_link_write`,
   which explicitly refetches the card via `agileplace.get_card` (apply=True only; a dry run reuses
   `card` unchanged, zero network calls) before the second PATCH -- avoiding the self-inflicted
   conflict outright rather than depending on a retry path that structurally can't recover from it.
   A genuine concurrent edit landing in the narrow window between that refetch and the PATCH itself
   still 409/428s and still propagates uncaught, unchanged from the original design intent above.
4. **`card_web_url`'s host is an UNCONFIRMED best guess.** The issue body written for a promoted
   card links back to the card in AgilePlace's web app. No separate "web host" config key exists
   in `config.py`/`.env.example` distinct from `AGILEPLACE_HOST` (the API host), and no live board
   was reachable to confirm whether the API host and the web-app host are the same value for a
   Planview/LeanKit tenant. `card_web_url` falls back to `cfg.get('host')` with an inline comment
   flagging this as unconfirmed. If a tenant's web UI is served from a different host than its API,
   the generated link will be wrong until this is confirmed live and corrected.

All four remain `[live-check]` pending -- the first real reverse-intake promotion against a
production board should confirm or correct them and update this section.

## Comment sync (issue #66) -- `[live-check]` pending

`agileplace_comments.py` adds list/create/update/delete for AgilePlace card comments, following the
same defensive-normalization style as the rest of this file. `smoke.py` steps 15-18 (added alongside
this module) exercise the whole CRUD surface offline via the fake tenant in `test_smoke.py`, but
**no live run against a real tenant has been done from this worktree** (no `.env`/credentials are
available here -- see the module's own docstring). Three things remain genuinely unconfirmed:

1. **List endpoint shape.** `GET /io/card/{cardId}/comment` is assumed to return either a bare array
   or an object carrying a `comments` array (mirroring `list_cards`' `{"cards": [...]}` convention);
   `list_comments` falls back once to the single-card GET's top-level `comments` field on ANY shape
   surprise. Neither the primary endpoint nor the fallback field name has been checked against a
   real tenant -- the whole thing is inferred from `list_cards`/`get_card`'s already-confirmed
   conventions, not from public docs (the web UI's comment feature has no documented io v2 page).
2. **Create/update field names.** `POST`/`PUT` send `{"text": <html>}`; the read side tries
   `createdBy` (nested `fullName`/`emailAddress`/`id`) and `createdOn` for the author/created
   timestamp, with `lastModified`/`modifiedOn`/`updatedOn`/`editedOn` tried in that order for the
   edited timestamp. None of these key names are confirmed -- `_normalize_ap_comment` degrades every
   field but `id` to `None` on a miss rather than raising, so a wrong guess here would silently
   report `author_name=None`/`edited=None` forever instead of failing loud. `smoke.py` step 16 prints
   the resolved shape as an INFO line specifically so a live run surfaces this.
3. **DELETE shape is speculative.** `DELETE /io/card/{cardId}/comment/{commentId}` was never
   confirmed against docs, the Node client, or a devtools capture (unlike the Dependencies delete
   shape above, which WAS captured live) -- the web UI has never exposed deleting a comment.
   `smoke.py` step 18 pins this as a hard PASS/FAIL (readback-confirms-gone, not just a 2xx), the
   same pattern used for the externalLink-add and tag-add checks.

Run `python smoke.py` against a disposable card on a real tenant to confirm or correct all three,
then move this section to a "Validated live" one the way the reverse-intake findings above will be.

### Live run 2026-07-23 (partial) -- `POST` id is a JSON string of digits

A real-tenant `python smoke.py` run got through steps 1-14 and reached STEP 15 (create comment).
The **`POST /io/card/{cardId}/comment` response serializes the new comment `id` as a STRING of
digits** (observed: `'2491550223'`), not a JSON number. The original `_normalize_ap_comment`
required `isinstance(id, int)` and so rejected a valid live response with a (misleading)
"non-numeric id" error. Fixed: `_coerce_comment_id` now accepts an int (never a bool) OR an
all-ASCII-digit string and coerces to `int`, preserving the ledger's `gh_id`/`ap_id` `int|None`
contract at the I/O boundary; the list read path funnels through the same normalizer, so it is
covered too. **Confirmed live:** item 1 (list-shape) reached only far enough to create; items 2
(author/timestamp field names) and 3 (speculative `DELETE`) plus the `PUT` edit shape remain
**pending-live-validation** -- the run raised before steps 16-18 executed, so re-run `smoke.py`
to exercise list-readback / edit / delete now that create succeeds.
