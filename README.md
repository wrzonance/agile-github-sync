# agile-github-sync

This tool keeps an AgilePlace (LeanKit / Planview) board up to date with what is happening in a
GitHub repository. You run one command, by hand or on a schedule. It reads the repo's issues,
sub-issues, blocking relationships, and GitHub Projects v2 board, then creates and updates
AgilePlace cards to match. There is no per-project manifest; everything is derived from the live
GitHub state.

## What a run does

With `--apply`, each run:

1. Creates a card for every active issue, epics and tasks alike. Cards are matched to issues by the
   card's external-link URL, with the card's customId as a fallback. Issues closed as
   `NOT_PLANNED`/`DUPLICATE` never get new cards; if one already has a URL-matched card, the run
   retires it to Done and clears its stale blocked state without syncing its metadata back to GitHub.
2. Moves each card to the lane for its stage. The stage is the issue's Status on the GitHub
   Projects v2 board (Backlog / Ready / In progress / In review / Done), which is the source of
   truth. Issues that are not on the Project fall back to a stage derived from labels, assignees,
   and open PRs.
   Lanes are matched by title among leaf lanes; if a match is ambiguous, the card stays where it
   is. If reading the Project fails outright -- or technically succeeds but yields zero recognized
   statuses for a Project that does have issue-linked items (e.g. a misconfigured
   `GH_PROJECT_STATUS_FIELD`) -- no active-issue lanes are changed that run, so the fallback cannot
   mass-move the board. Authoritative `NOT_PLANNED`/`DUPLICATE` retirement still moves an existing
   card to Done. New cards created during such a run are left laneless rather than fallback-laned; a
   later run lanes them normally once the read succeeds.
3. Mirrors sub-issues as parent/child card connections, adding and removing links so the card
   hierarchy matches the GitHub graph. LeanKit then rolls child progress up to the parent on its
   own.
4. Marks a card blocked while any issue blocking it is not Done, and clears the blocked state
   otherwise. A `NOT_PLANNED`/`DUPLICATE` blocker is known Done, so it cannot block dependents
   forever; a blocker missing from the complete issue snapshot remains incomplete (fail-closed).
5. Reconciles metadata and dates in both directions using a three-way merge against
   `.sync-state.json`. GitHub labels and milestone sync with card tags, and removals carry over in
   both directions. The Project's Start and Target date fields sync with the card's `plannedStart`
   and `plannedFinish`; if both sides changed the same date, the AgilePlace value wins.

Field updates queued for an existing card -- lane, tags, dates, and blocked state -- are combined
into at most one versioned PATCH per run, so a stale write fails instead of silently overwriting
someone else's edit. Card creation and hierarchy connections use separate POST/DELETE requests,
and GitHub-side writes are issued separately. Dry run is the default: `python sync.py` prints every
planned action and writes nothing.

> Before the first live run: `API-VALIDATION.md` records which API shapes are confirmed and which
> still need a one-time check against the live APIs (the AgilePlace connection, blocked-state, tag,
> and date writes, and the `gh project` reads). Known deferred work (pagination beyond about 1,000
> records, cross-repo sub-issues, run locking) is tracked in `HARDENING.md`. Start with a
> disposable board and repo.

## Point it at a repo

```
cp .env.example .env
```

Fill in `TARGET_REPO_PATH` (the local clone), the `AGILEPLACE_*` values, and the `GH_PROJECT_*`
values, then grant the Projects scope with `gh auth refresh -s project`. Every `gh` call runs with
its working directory set to `TARGET_REPO_PATH`, so `gh` finds the repo through that clone's
remote; no owner or repo name is hardcoded here.

The script is plain Python (3.10+, standard library only) and the same `python sync.py` works in
PowerShell, cmd, and bash.

If your board columns don't match the standard five stages, set `STAGE_LANE_MAP` to pin stages to
your lanes. You can list several lanes per stage: the first is where cards get moved, and any of
them counts as "already in that stage". See `.env.example`. Without the map, lanes are matched by
title and card status, and an ambiguous match leaves the card alone.

## Run

```
python sync.py            # dry run: prints what it would do, writes nothing
python sync.py --apply    # create/move/connect cards and sync metadata and dates (needs a full .env and token scopes)
```

Runs are idempotent, so any schedule frequency is fine. With no AgilePlace token it still reads
GitHub through `gh`, but forces dry-run mode and makes no writes. If the target repo has no reachable
remote, it prints a notice and does nothing.

## Keep it always in sync

- Windows (Task Scheduler): `powershell -ExecutionPolicy Bypass -File .\Register-BacklogSync.ps1`
  registers a task that runs `sync.py --apply` every 30 minutes, weekdays 07:00 to 19:00, only
  while you are logged on (no stored password). Output goes to `sync.log`. Remove it with
  `-Unregister`.
- Linux/macOS (cron):
  ```cron
  */30 7-18 * * 1-5  cd ~/github/agile-github-sync && python sync.py --apply >> sync.log 2>&1
  ```

The account running the schedule needs `python` and `gh` on PATH, `gh auth login` plus
`gh auth refresh -s project` completed, and a filled-in `.env`.

## Layout

- `sync.py`: orchestration. Creates, moves, connects, and blocks cards; reconciles tags and dates;
  sends at most one field-update PATCH per existing card.
- `stages.py`: pure stage derivation, blocked-reason text, and lane/title matching (unit-tested).
- `reconcile.py`: pure three-way set and single-value merges (unit-tested).
- `ghkit.py`: GitHub via the `gh` CLI (issues, sub-issues, open-PR and blocked-by reads,
  label/milestone writes).
- `ghproject.py`: GitHub Projects v2 via `gh project` (Status/date reads and date writes).
- `agileplace.py`: AgilePlace io v2 (board, cards, lanes, tags, dates, connections, blocked
  state).
- `config.py`: `.env` config. `tests/`: pytest (`pytest -q`).

The one-time initial stand-up (labels, milestones, first issues, adding issues to the Project) is
a separate throwaway step that lives outside this repo. This tool only maintains the ongoing
mirror.
