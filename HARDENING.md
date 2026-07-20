# Hardening backlog

This file lists known gaps that are deliberately not fixed yet. They came out of a whole-repo
review by Codex (gpt-5.6-sol). The findings that risked losing data during `--apply` are already
fixed (see the `fix(model2): harden the write path` commit). What remains falls into two groups:
things that need live API access to get right, and scaling work that doesn't matter at this
project's current size of about 50 issues. Do the `[first-live-run]` items on a disposable board
and repo before pointing the tool at anything real. The `[scale]` items only start to bite well
past roughly 1,000 records.

## Implemented defensive contracts

- Blocked-by dependencies are qualified by repository before their issue numbers enter the sync.
  `ghkit.blocked_by_map` accepts either an embedded `repository` identity or the documented
  `repository_url`, retains only blockers from the configured target repository, and warns with the
  foreign `owner/repo#number` when it skips a cross-repo blocker. A missing or malformed identity
  makes the whole blocked-by snapshot incomplete so existing card blocked states remain untouched.
  This is defensive parsing; whether GitHub permits creating cross-repository dependency edges has
  not been verified against the live API.
- Card identity reconciliation derives one customId for creation and lookup, repairs customId drift
  in the card's single versioned PATCH, and fails before writes when URL and customId identify
  different cards. URL-owned cards also update the in-run customId index before fallback matching,
  so a renamed key cannot claim the old card; creation with that released key waits until the next
  run, after the rename PATCH has applied.

## [first-live-run]: confirm the exact API behavior (see also API-VALIDATION.md)

- Card connections. `connect_children` and `disconnect_children` POST/DELETE `card/connections`
  with `{cardIds:[parent], connections:{children:[...]}}`, and `card_child_ids` reads the
  `childCards` include. Confirm the endpoint, the body, and that `list_cards` actually returns
  `childCards`. If the read is unreliable, the add/remove reconciliation is unsafe and should be
  gated on a trustworthy read of existing children.
- Blocked state. The code reads `blockedStatus.{isBlocked,reason}` and writes flat `/isBlocked`
  (replace) + `/blockReason` (add). Confirm the write field paths and that a PATCH round-trips
  (block, unblock, change the reason).
- Tag removal and date clearing. The io v2 docs describe both index-based and value-based tag
  removal; the code intentionally sends deterministic RFC-6902 index removals
  (`{op:remove, path:/tags/{i}}`, without a `value`, in descending index order) plus
  `{op:replace, path:/plannedStart, value:null}`. Confirm that the chosen indexed form and null as
  "clear this date" are accepted by the target tenant.
- GitHub Project read shapes. `item-list` returns Status as a flattened top-level key (the parser is
  defensive about casing); `field-list` provides Status option ids and date field ids. Planned dates
  are read separately through paginated GraphQL `ProjectV2Item.fieldValues` and matched by field id;
  any incomplete or malformed date snapshot disables date sync for the run. Pin a minimum `gh`
  version and confirm the shapes on your board.
- Metadata that only exists on one side. A card tag with no matching GitHub label, or a milestone
  name GitHub doesn't have, makes `gh issue edit --add-label/--milestone` fail and halts the run.
  Decide whether to auto-create the label or milestone, skip it with a warning, or require it to
  be set up in advance.

## [scale] and robustness: beyond this project's current size

- Cross-repo sub-issues (a correctness bug). `ghkit.sub_issue_numbers` returns issue numbers only.
  A child issue that lives in another repo would be looked up as the same-numbered issue in the
  target repo and connect the wrong card. Fix: query the sub-issue URL and repository via GraphQL,
  key relationships by URL, and skip cross-repo children with a warning (or handle them
  explicitly).
- Pagination. `list_issues` (1,000), `open_pr_issue_numbers` (100 PRs, 20 closing refs each), and
  the Projects Status `item-list` (1,000) silently truncate above their caps. (The separate GraphQL
  planned-date read paginates Project items to exhaustion and fails closed if an item's first 100
  field values are insufficient.) An issue missing from the Status read falls back to the stage
  derived from labels, assignees, and open PRs, so truncation produces wrong results at scale, not
  just missing ones. Fix: paginate the remaining reads to exhaustion and validate totals.
- The blocked-by read spawns one `gh` process per issue (`ghkit.blocked_by_map`), each with a 60s
  timeout. At thousands of issues this can exceed the 10-minute scheduled-task window, in which
  case the writes happen but the state save does not. Fix: batch the blocked-by read (GraphQL or
  paginated REST) and enforce a whole-run deadline, with all reads done before any writes.
- Concurrent runs. Two overlapping invocations (say, manual plus cron) can both create a missing
  card and race the state file's read-modify-write. Atomic replace protects the file's syntax, not
  the transaction. Fix: a cross-platform lock (for example an exclusive lock file) held from the
  first read through the state save.
- Fuller state identity. State is currently scoped by target owner/repo, board id, and schema
  version. For multi-host or multi-project safety, also fold in the GitHub host, the AgilePlace
  host/tenant, and the Project owner and number, and refuse to reuse state when they don't match.
