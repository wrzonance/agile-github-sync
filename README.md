# agile-github-sync

Keeps an AgilePlace (LeanKit/PlanView) planning board in step with a GitHub repo's issues — **ongoing,
agnostic, one command**. It reads live GitHub + the board and needs no project-specific manifest.

It does two things each run:

1. **Card movement.** Each epic's card advances lane-for-lane as its tasks progress:
   **Backlog → Ready → In progress → In review → Done**. An issue's stage is derived from GitHub
   (closed → Done; open with an open linked PR or `agent:in-review` → In review;
   `agent:in-progress` / an assignee → In progress; `agent:ready` → Ready; else Backlog), and the
   epic's card is the rollup of its tasks. Lanes are matched by **title** (LeanKit lane status has only
   three values, so In progress and In review are distinguished by name).
2. **Bidirectional metadata.** GitHub **labels** and **milestone** on an epic issue ↔ its card's
   **tags**, reconciled by a 3-way merge against `.sync-state.json` so **removals propagate both ways**
   (tag it a bug on either side and it appears on the other; untag it and it clears on both).

The board is a projection of GitHub's execution truth; this tool is the only thing that moves cards.

## Point it at a repo

```
cp .env.example .env      # set TARGET_REPO_PATH to the local clone; fill AGILEPLACE_*
```

Every `gh` call runs with its working directory set to `TARGET_REPO_PATH`, so `gh` resolves the repo
from that clone's remote — no hardcoded owner/name, and this tool can live anywhere. Stdlib-only Python
(3.10+); the same `python sync.py` runs in PowerShell, cmd, and bash.

**Custom board columns?** The tool reads your board layout via the API and maps stages (Backlog / Ready
/ In progress / In review / Done) to lanes by title, falling back to the three `cardStatus` tiers and
**failing closed** (leaving the card put) when a stage is ambiguous — it never guesses a wrong lane. If
your Not Started / Started / Finished tiers split into custom sub-lanes, set `STAGE_LANE_MAP` in `.env`
to pin them; multiple lanes per stage are allowed (first = where a card is moved, all = "already in
that stage", so cards you shuffle between equivalent lanes are left alone). See `.env.example`.

## Run

```
python sync.py            # verbose DRY RUN (no writes)
python sync.py --apply    # move cards + sync tags for real (needs a full .env)
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

Run-as prereqs either way: `python` + `gh` on PATH, `gh auth login` done for that user, `.env` filled.

## Layout

- `sync.py` — orchestration (the two jobs above).
- `stages.py` — pure stage derivation, epic rollup, lane/title matching (unit-tested).
- `reconcile.py` — pure 3-way label↔tag merge (unit-tested).
- `ghkit.py` — GitHub via `gh` (issues, native sub-issues, open-PR signal, label writes).
- `agileplace.py` — AgilePlace io v2 (board, cards, lane resolution, tag writes).
- `config.py` — `.env` config; `tests/` — pytest (`pytest -q`).

The **initial** backlog stand-up (labels, milestones, first issues + cards) is a separate, throwaway
step that lives **outside** this repo — this tool only maintains the ongoing sync.
