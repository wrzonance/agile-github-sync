# Hardening backlog

Tracks the findings from the Codex `gpt-5.6-sol` whole-repo review that are **deferred** — either they
need live-API access to get right, or they're scaling/robustness work beyond this project's current
size (~50 issues). The confident data-loss / `--apply`-safety findings are already fixed (see the
`fix(model2): harden the write path…` commit). Do the **[first-live-run]** items on a disposable
board/repo before pointing it at anything real; the **[scale]** items only bite well beyond ~1k records.

## [first-live-run] — confirm the exact API behavior (see also API-VALIDATION.md)

- **Card connections** — `connect_children`/`disconnect_children` POST/DELETE `card/connections` with
  `{cardIds:[parent], connections:{children:[...]}}`, and `card_child_ids` reads the `childCards`
  include. Confirm the endpoint, body, and that `list_cards` actually returns `childCards`. If the read
  is unreliable, the add/remove reconciliation is unsafe — gate it on a trustworthy existing-children read.
- **Blocked state** — reads/writes `blockedStatus.{isBlocked,reason}`. Confirm the field path and that a
  PATCH round-trips (block → unblock → reason change).
- **Tag remove / date clear** — `{op:remove,path:/tags,value}` and `{op:replace,path:/plannedStart,
  value:null}`. Confirm value-based tag removal and null-to-clear are accepted.
- **`gh project` JSON shapes** — `item-list` field values arrive as flattened top-level keys (parse is
  defensive across casing); `field-list` gives Status option ids + date field ids. Pin a minimum `gh`
  version and confirm the shapes on your board.
- **Metadata that doesn't exist on the other side** — a card tag with no matching GitHub label (or a
  milestone name GitHub doesn't have) will make `gh issue edit --add-label/--milestone` fail and halt
  the run. Decide: auto-create the label/milestone, skip-with-warning, or require pre-provisioning.

## [scale] / robustness — beyond this project's current size

- **Cross-repo sub-issues (correctness).** `ghkit.sub_issue_numbers` returns only issue *numbers*; a
  child in another repo would be looked up as the target repo's same-numbered issue and connect the
  wrong card. Fix: query sub-issue URL + repository via GraphQL, key relationships by URL, and skip (or
  explicitly handle) cross-repo children with a warning.
- **Pagination.** `list_issues` (1000), `open_pr_issue_numbers` (100 PRs / 20 closing refs), and the
  Projects `item-list` (1000) silently truncate above their caps; a missing Project item then falls to
  the label-derived stage, so truncation is a *wrong-result* bug at scale. Fix: paginate to exhaustion
  and validate totals.
- **Blocked-by read is one `gh` process per issue** (`ghkit.blocked_by_map`), each with a 60s timeout —
  at thousands of issues this can exceed the 10-minute scheduled-task window (writes happen, state save
  doesn't). Fix: batch the blocked-by read (GraphQL / paginated) and enforce a whole-run deadline with
  all reads before any writes.
- **Concurrent-run safety.** Two overlapping invocations (manual + cron) can both create a missing card
  and race the state read-modify-write; atomic replace protects file syntax, not the transaction. Fix:
  a cross-platform lock (e.g. an exclusive lock file) held across read → writes → state save.
- **Fuller state identity.** State is scoped by `target owner/repo` + board id + schema version. For
  multi-host / multi-project safety also fold in the GitHub host, AgilePlace host/tenant, and Project
  owner/number, and refuse to reuse state across a mismatch.
- **Duplicate/ambiguous card match.** URL-then-customId matching resolves the common cases; if both a
  URL match and a *different* customId match exist, fail loudly rather than picking one.
