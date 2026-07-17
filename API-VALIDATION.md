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

## GitHub side (standard, stable — noted for completeness)

- `gh issue list --json number,title,state,labels,milestone,assignees,url` — stable.
- Native sub-issues via `gh api graphql` `repository.issue.subIssues` (GitHub 2024+). **[live-check]** on
  the target host/GHES version; `sub_issue_numbers()` returns **None on failure** so `sync.py` warns and
  falls back to the `[KEY]` title convention rather than silently mis-associating.
- Open-PR "in review" signal via `pullRequests.closingIssuesReferences` — standard GraphQL.
