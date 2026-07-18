# agile-github-sync

Mirrors a GitHub repo's work graph onto an AgilePlace (LeanKit/PlanView) board — **ongoing, agnostic,
one command**. It reads live GitHub (issues, sub-issues, blocked-by) plus the repo's **GitHub Projects
v2** board and reflects all of it onto AgilePlace. No project-specific manifest; it derives everything
from live state.

## What a run does

With `--apply`, each run:

1. **Creates a card per issue** (epics *and* tasks), matched to the issue by external-link URL (customId
   fallback).
2. **Moves each card to the lane for its stage.** Stage is the issue's **GitHub Projects v2 Status**
   (Backlog / Ready / In progress / In review / Done) — the source of truth — falling back to a
   label/PR-derived stage only for issues not on the Project. Lanes resolve by title among leaf lanes,
   **failing closed** (leaving the card put) when ambiguous. If the Project read *fails*, lanes are left
   untouched for that run rather than mass-moved by the fallback.
3. **Mirrors sub-issues as parent/child card connections** (add *and* remove, so the hierarchy equals
   the GitHub graph); LeanKit rolls child progress/dates up to the parent natively.
4. **Mirrors blocked-by as the card's Blocked state** — a card is blocked while any blocker isn't Done.
5. **Bidirectionally reconciles metadata and dates** via a 3-way merge against `.sync-state.json`:
   GitHub **labels + milestone ↔ card tags** (removals propagate both ways); Project **Start/Target date
   fields ↔ card `plannedStart`/`plannedFinish`** (AgilePlace wins a genuine two-sided conflict).

Every mutation to one card is sent as a **single versioned PATCH** (optimistic concurrency). **DRY RUN
is the default** — `python sync.py` prints every planned action and writes nothing.

> **First live run:** the AgilePlace write shapes (connections, blocked, tags, dates) and the
> `gh project` reads are validated against the live APIs — see **`API-VALIDATION.md`**. Known deferred
> hardening (pagination beyond ~1k, cross-repo sub-issues, run locking) is tracked in **`HARDENING.md`**.
> Start with a disposable board/repo.

## Point it at a repo

```
cp .env.example .env
```

Fill `TARGET_REPO_PATH` (the local clone), `AGILEPLACE_*`, and `GH_PROJECT_*`; grant the Projects scope
with `gh auth refresh -s project`. Every `gh` call runs with its working directory set to
`TARGET_REPO_PATH`, so `gh` resolves the repo from that clone's remote — no hardcoded owner/name.
Stdlib-only Python (3.10+); the same `python sync.py` runs in PowerShell, cmd, and bash.

**Custom board columns?** Set `STAGE_LANE_MAP` to pin stages to your lanes (multiple lanes per stage —
first = move target, all = "already in that stage"). See `.env.example`. Without it, stages resolve by
lane title / cardStatus, failing closed when ambiguous.

## Run

```
python sync.py            # verbose DRY RUN (no writes)
python sync.py --apply    # create/move/connect cards, sync metadata + dates (needs a full .env + scopes)
```

Idempotent and safe at any frequency. With no token it runs offline/read-only; if the target repo has
no reachable remote it prints a notice and does nothing.

## Keep it always in sync

- **Windows (Task Scheduler):** `powershell -ExecutionPolicy Bypass -File .\Register-BacklogSync.ps1`
  registers a task that runs `sync.py --apply` **every 30 min, weekdays 07:00–19:00, only while you are
  logged on** (no stored password); logs to `sync.log`. Remove with `-Unregister`.
- **Linux/macOS (cron):**
  ```cron
  */30 7-18 * * 1-5  cd ~/github/agile-github-sync && python sync.py --apply >> sync.log 2>&1
  ```

Run-as prereqs: `python` + `gh` on PATH, `gh auth login` + `gh auth refresh -s project` done, `.env` filled.

## Layout

- `sync.py` — orchestration (create/move/connect/block cards; reconcile tags + dates; one PATCH per card).
- `stages.py` — pure stage derivation, blocked-reason, lane/title matching (unit-tested).
- `reconcile.py` — pure 3-way set + single-value merges (unit-tested).
- `ghkit.py` — GitHub via `gh` (issues, sub-issues, open-PR + blocked-by reads, label/milestone writes).
- `ghproject.py` — GitHub Projects v2 via `gh project` (Status + date reads/writes).
- `agileplace.py` — AgilePlace io v2 (board, cards, lanes, tags, dates, connections, Blocked).
- `config.py` — `.env` config; `tests/` — pytest (`pytest -q`).

The **initial** stand-up (labels, milestones, first issues, add-to-Project) is a separate, throwaway
step that lives **outside** this repo — this tool only maintains the ongoing mirror.
