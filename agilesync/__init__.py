"""GitHub -> AgilePlace sync. Stdlib only.

`agilesync.sync` is the orchestrator (the main loop); everything else is a module it composes:

- `agilesync.board`   -- AgilePlace io v2 client and board topology.
- `agilesync.gh`      -- GitHub reads/writes via the `gh` CLI and Projects v2.
- `agilesync.markup`  -- Markdown <-> AgilePlace rich-text translation and comment rendering.
- `agilesync.syncers` -- the per-concern sync passes the main loop drives (comments, description,
  metadata, intake, card coherence, vetting latch).

Shared vocabulary (`stages`, `reconcile`, `card_types`, `config`) sits at the package root because
every layer above depends on it.
"""
