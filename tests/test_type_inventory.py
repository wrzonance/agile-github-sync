"""Unit tests for type_inventory.py -- the read-only card-type mapping inventory.

Fully mocked at the two I/O seams (ghkit's gh calls and board_layout's one board GET); no network,
no gh, no AgilePlace. These pin:

  - the report NEVER writes: no agileplace mutate/patch and no gh write ever leaves this script.
  - every side degrades independently: an unconfigured or failed read prints a note and the rest of
    the report still renders (the whole point is to be runnable while things are broken).
  - eligibility is reported the same way resolve_card_type_ids decides it, so the listed targets
    are exactly the ones a CARD_TYPE_MAP could actually resolve.
  - the active mapping marks each rule's target OK vs NOT ON BOARD -- the line that answers "why
    did nothing happen?".

Run: pytest -q
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import board_layout  # noqa: E402
import ghkit  # noqa: E402
import type_inventory  # noqa: E402
from card_types import parse_card_type_map  # noqa: E402


def _cfg(**overrides):
    cfg = {
        "token": "t", "host": "h.leankit.com", "board_id": "42",
        "target_repo_path": Path("/tmp/repo"),
        "card_type_map": None,
    }
    return {**cfg, **overrides}


def _layout(card_types):
    return board_layout.BoardLayout(lanes=[], card_types=card_types)


BOARD = [
    {"id": "t-def", "title": "Defect", "isCardType": True},
    {"id": "t-story", "title": "Story", "isCardType": True},
    {"id": "t-sub", "title": "Subtask", "isCardType": False},
]


def _run(cfg, *, layout=None, org_types=frozenset({"Bug", "Feature", "Task"}),
         labels=("bug", "documentation", "enhancement")):
    with patch("type_inventory.board_layout.board_layout",
               return_value=_layout(BOARD if layout is None else layout)), \
         patch("type_inventory.ghkit.org_issue_types", return_value=org_types), \
         patch("type_inventory.ghkit.list_label_names",
               return_value=None if labels is None else list(labels)):
        type_inventory.print_inventory(cfg)


def test_lists_both_sides_and_marks_unresolvable_targets(capsys):
    _run(_cfg())
    out = capsys.readouterr().out
    assert "Bug" in out and "Feature" in out          # GitHub native issue types
    assert "enhancement" in out                        # GitHub labels
    assert "Defect" in out and "Story" in out          # board card types
    assert out.count("[NOT ON BOARD]") == 5            # defaults resolve nothing on this board
    assert "CARD_TYPE_MAP=" in out                     # copy-pasteable skeleton


def test_task_only_board_types_are_listed_separately_and_never_as_targets(capsys):
    _run(_cfg())
    out = capsys.readouterr().out
    targets_section = out.split("AgilePlace task-only types")[0]
    assert "Subtask" not in targets_section
    assert "Subtask" in out


def test_a_configured_map_whose_targets_exist_reports_ok(capsys):
    _run(_cfg(card_type_map=parse_card_type_map("type:Bug=Defect; label:enhancement=Story")))
    out = capsys.readouterr().out
    assert "CARD_TYPE_MAP (.env)" in out
    assert "[NOT ON BOARD]" not in out
    assert out.count("[OK]") == 2


def test_a_configured_map_is_echoed_back_verbatim_in_the_skeleton(capsys):
    _run(_cfg(card_type_map=parse_card_type_map("type:Bug=Defect")))
    out = capsys.readouterr().out
    assert "CARD_TYPE_MAP=type:Bug=Defect" in out


def test_unconfigured_agileplace_still_reports_the_github_side(capsys):
    cfg = _cfg(token=None)
    with patch("type_inventory.board_layout.board_layout") as board_mock, \
         patch("type_inventory.ghkit.org_issue_types", return_value=frozenset({"Bug"})), \
         patch("type_inventory.ghkit.list_label_names", return_value=["bug"]):
        type_inventory.print_inventory(cfg)
    board_mock.assert_not_called()
    out = capsys.readouterr().out
    assert "AgilePlace is not fully configured" in out
    assert "Bug" in out


def test_unset_target_repo_path_still_reports_the_agileplace_side(capsys):
    cfg = _cfg(target_repo_path=None)
    with patch("type_inventory.board_layout.board_layout", return_value=_layout(BOARD)), \
         patch("type_inventory.ghkit.org_issue_types") as types_mock, \
         patch("type_inventory.ghkit.list_label_names") as labels_mock:
        type_inventory.print_inventory(cfg)
    types_mock.assert_not_called()
    labels_mock.assert_not_called()
    out = capsys.readouterr().out
    assert "TARGET_REPO_PATH not set" in out
    assert "Defect" in out


def test_failed_github_reads_are_reported_as_unavailable_not_as_empty(capsys):
    _run(_cfg(), org_types=None, labels=None)
    out = capsys.readouterr().out
    assert out.count("<unavailable") == 2
    assert "Defect" in out


def test_a_board_with_no_card_types_says_so(capsys):
    _run(_cfg(), layout=[])
    out = capsys.readouterr().out
    assert "<this board defines no card types>" in out


def test_board_card_type_names_reads_a_name_keyed_entry():
    """Same io v2 shape hedge card_types.board_type_title documents: a `name`-keyed payload must be
    reported, not silently dropped from the inventory the user is told to map against."""
    cfg = _cfg()
    with patch("type_inventory.board_layout.board_layout",
               return_value=_layout([{"id": "t1", "name": "Defect", "isCardType": True}])):
        eligible, task_only = type_inventory.board_card_type_names(cfg)
    assert eligible == ["Defect"]
    assert task_only == []


def test_the_report_performs_no_writes_at_all():
    """The script's core promise. agileplace.mutate is the single choke point every AgilePlace write
    passes through, and ghkit's write helpers are the GitHub equivalents."""
    with patch("agileplace.mutate") as mutate_mock, \
         patch("ghkit.edit_issue_body") as body_mock, \
         patch("ghkit.edit_label") as label_mock, \
         patch("ghkit.create_issue") as create_mock:
        _run(_cfg())
    mutate_mock.assert_not_called()
    body_mock.assert_not_called()
    label_mock.assert_not_called()
    create_mock.assert_not_called()


def test_an_unreachable_board_degrades_instead_of_aborting_the_report(capsys):
    """Adversarial-review finding: `agileplace.api` raises SystemExit for an unreachable tenant or a
    bad token, and nothing caught it -- so the one command an operator runs BECAUSE the config is
    broken died on that exact breakage, printing nothing about the GitHub side either."""
    with patch("type_inventory.board_layout.board_layout",
               side_effect=SystemExit("AgilePlace GET board/42 failed: HTTP 401")), \
         patch("type_inventory.ghkit.org_issue_types", return_value=frozenset({"Bug"})), \
         patch("type_inventory.ghkit.list_label_names", return_value=["bug"]):
        type_inventory.print_inventory(_cfg())

    out = capsys.readouterr().out
    assert "could not read the AgilePlace board" in out
    assert "HTTP 401" in out
    assert "Bug" in out          # the GitHub side still rendered
    assert "CARD_TYPE_MAP=" in out  # and so did the skeleton


def test_a_label_read_that_lands_exactly_on_the_limit_is_flagged_as_possibly_truncated(capsys):
    """`gh label list` takes one page: a result the same size as the limit may be clipped, and an
    inventory that reads as authoritative would tell an operator a label does not exist when it
    does."""
    _run(_cfg(), labels=[f"label-{n}" for n in range(ghkit.LABEL_LIST_LIMIT)])
    assert "may be truncated" in capsys.readouterr().out


def test_a_normal_label_read_carries_no_truncation_caveat(capsys):
    _run(_cfg(), labels=["bug", "enhancement"])
    assert "may be truncated" not in capsys.readouterr().out


def test_a_fully_rejected_card_type_map_is_reported_as_writing_nothing(capsys):
    """The fail-closed state must not read as "defaults apply" -- () is configured-but-unusable."""
    _run(_cfg(card_type_map=parse_card_type_map("typo:Bug=Defect")))
    out = capsys.readouterr().out
    assert "CARD_TYPE_MAP (.env)" in out
    assert "would write NO card types at all" in out
