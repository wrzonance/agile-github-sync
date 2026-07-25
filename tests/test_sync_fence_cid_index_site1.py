"""Integration tests for issue #95 Site 1 wiring: sync.main()'s board-wide customId index
(`all_card_by_cid`) is now built by card_coherence.fence_cid_index() instead of the old inline
last-wins loop.

card_coherence.fence_cid_index() itself is pure and exhaustively unit-tested in
test_card_coherence.py (including the retirement-index call site, fence_run_indices). These tests
instead exercise the REAL main() (every I/O boundary mocked: ghkit, ghproject, agileplace) to pin
that a customId collision on the AgilePlace side alone -- two cards whose customId both normalize
to the same header_match_key, independent of anything any GitHub issue does -- is fenced out of
`all_card_by_cid` at board-index-build time, the same way a `contested_cards()` collision fences
`all_card_by_url`/`all_card_by_cid` entries downstream. Before this wiring, the old inline loop's
`all_card_by_cid[cid] = card` silently let the last card in AgilePlace's own listing order win,
with no warning at all.

Run: pytest -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _sync_main_shared import _cid_only_card, _issue, _run_main  # noqa: E402


def test_colliding_customid_cards_are_excluded_from_the_board_index_not_last_wins(
        tmp_path, monkeypatch, capsys):
    """Two AgilePlace cards whose customId ('5' and 'GitHub Issue #5') both normalize via
    header_match_key to the same key '5', with no external link claiming either. Pre-fix, the old
    inline loop's `all_card_by_cid[cid] = card` would silently let whichever card iterated last win
    the "5" slot, with no warning -- the active issue #5 (customId-only match, no URL) would then
    silently adopt that card. Post-fix, fence_cid_index excludes BOTH from the index, so issue #5
    is treated as unmatched and a NEW card is created instead."""
    old_format = _cid_only_card("100", "5")
    header_format = _cid_only_card("200", "GitHub Issue #5")
    issue = _issue(5, "widget")  # no [KEY] prefix -> issue_custom_id() falls back to "5"

    create_card, patch_card = _run_main(
        tmp_path, monkeypatch, [issue], cards=[old_format, header_format])

    out = capsys.readouterr().out
    warn_lines = [line for line in out.splitlines()
                  if line.startswith("WARN  customId 5 claimed by")]
    assert len(warn_lines) == 1, "exactly one WARN line for the colliding customId key"
    assert "2 cards" in warn_lines[0]
    assert "100" in warn_lines[0] and "200" in warn_lines[0]

    # Neither colliding card was adopted -- the unmatched issue takes the create path instead.
    create_card.assert_called_once()
    patch_card.assert_not_called()


def test_fence_cid_index_warning_prints_before_contested_and_fence_run_indices_warnings(
        tmp_path, monkeypatch, capsys):
    """The board-index-build WARN must appear before any Layer 1 (contested_cards) WARN in run
    output, matching the design's stated print order. This fixture genuinely produces BOTH kinds
    of WARN in the same run -- a customId collision (via fence_cid_index at board-index-build time)
    AND a contested card (via contested_cards/fence_run_indices, one card claimed by two issues'
    external links) -- so the ordering assertion actually exercises the two lines interleaving,
    rather than passing vacuously because only one of them exists."""
    old_format = _cid_only_card("100", "5")
    header_format = _cid_only_card("200", "GitHub Issue #5")
    issue1 = _issue(1, "one")
    issue2 = _issue(2, "two")
    contested_card = {
        "id": "999",
        "customId": "unrelated",
        "externalLinks": [{"url": issue1["url"]}, {"url": issue2["url"]}],
    }

    _run_main(tmp_path, monkeypatch, [issue1, issue2],
              cards=[old_format, header_format, contested_card])

    out = capsys.readouterr().out
    lines = out.splitlines()
    cid_index = next(i for i, line in enumerate(lines) if line.startswith("WARN  customId 5"))
    contested_index = next(i for i, line in enumerate(lines) if line.startswith("WARN  card 999"))
    assert cid_index < contested_index, (
        "the board-index-build customId WARN must print before contested_cards()/"
        "fence_run_indices() are invoked"
    )


def test_noncolliding_customid_card_still_matches_via_fallback_drop_in_replacement(
        tmp_path, monkeypatch, capsys):
    """Drop-in-replacement invariant: a single, non-colliding customId-only card must still match
    its issue via the customId fallback exactly as the old inline loop did -- fence_cid_index must
    not regress the ordinary (no-collision) path."""
    card = _cid_only_card("100", "5")
    issue = _issue(5, "widget")

    create_card, patch_card = _run_main(tmp_path, monkeypatch, [issue], cards=[card])

    create_card.assert_not_called()
    patch_card.assert_called_once()
