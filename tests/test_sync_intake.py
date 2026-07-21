"""resolve_issue_stage's "Intake" branch (Task 3/8, issue #63).

An issue only ever resolves to "Intake" when ALL of: it has no explicit Project Status, its
issue_stage() fallback is the bare-else "Backlog" (no work signal at all), the board's stage_map
actually declares an "Intake" lane mapping, AND the issue is not already a member of the Project
(project_items). Board membership and any work signal both veto it unconditionally. Run: pytest -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stages import normalize_status  # noqa: E402
from sync import resolve_issue_stage  # noqa: E402


def test_status_literally_named_intake_never_resolves_intake():
    """PR #68 review (Major): "Intake" is not part of the Project-Status vocabulary. A board
    Status literally named "Intake" normalizes to None and falls back like any unrecognized
    value -- flag off AND flag on. Board membership means vetted; only the off-board bare
    fallback may ever produce the Intake stage."""
    assert normalize_status("Intake") is None
    assert normalize_status("intake") is None
    issue = {"url": "u1", "state": "OPEN", "labels": []}
    for stage_map in ({}, {"Intake": ["New Requests"]}):            # flag off and flag on
        stage = resolve_issue_stage(issue, {"u1": "Intake"},
                                    {"u1": {"item_id": "PVTI_1"}}, stage_map)
        assert stage == "Backlog"                                   # classic fallback, never Intake


def test_resolve_issue_stage_flag_off_is_byte_identical_to_pre_issue_63_behavior():
    # Whenever "Intake" is absent from stage_map (the default/legacy config), resolve_issue_stage
    # must behave exactly as it did before issue #63 existed -- new project_items/stage_map
    # params must be fully inert. Covers explicit Status, closed, and every fallback branch.
    open_issue = {"url": "u1", "state": "OPEN", "labels": ["agent:in-progress"]}
    backlog_issue = {"url": "u2", "state": "OPEN", "labels": []}
    closed_issue = {"url": "u3", "state": "CLOSED", "labels": []}

    for stage_map in (None, {}, {"Backlog": ["Some Lane"]}):
        assert resolve_issue_stage(open_issue, {"u1": "In review"}, {}, stage_map) == "In review"
        assert resolve_issue_stage(open_issue, {}, {}, stage_map) == "In progress"
        assert resolve_issue_stage(backlog_issue, {}, {}, stage_map) == "Backlog"
        assert resolve_issue_stage(closed_issue, {"u3": "In progress"}, {}, stage_map) == "Done"
        # Presence of project_items must not matter either, while the flag itself is off.
        assert resolve_issue_stage(
            backlog_issue, {}, {"u2": {"item_id": "PVTI_1"}}, stage_map) == "Backlog"


def test_resolve_issue_stage_board_membership_vetoes_intake_regardless_of_status():
    # An issue already present in project_items (added to the Project) must never be sent back to
    # Intake, no matter what its Status value is -- board membership beats the Intake branch.
    stage_map = {"Intake": ["New Requests"]}
    issue = {"url": "u1", "state": "OPEN", "labels": []}

    for project_items, project_status in (
        ({"u1": {"item_id": "PVTI_1"}}, {}),                       # on board, no Status
        ({"u1": {"item_id": "PVTI_1"}}, {"u1": "Backlog"}),        # on board, explicit Backlog
        ({"u1": {}}, {}),                                          # on board, empty item value
    ):
        assert resolve_issue_stage(issue, project_status, project_items, stage_map) == "Backlog"


def test_resolve_issue_stage_work_signal_vetoes_intake():
    # Any issue whose issue_stage() fallback carries a work signal (open PR, assignee, or an
    # agent:* label putting it past bare "Backlog") must never resolve to "Intake" -- the branch
    # only fires on the true no-signal bare-else fallback.
    stage_map = {"Intake": ["New Requests"]}
    with_open_pr = {"url": "u1", "state": "OPEN", "labels": [], "has_open_pr": True}
    with_assignee = {"url": "u2", "state": "OPEN", "labels": [], "assignees": ["octocat"]}
    with_agent_label = {"url": "u3", "state": "OPEN", "labels": ["agent:ready"]}

    assert resolve_issue_stage(with_open_pr, {}, {}, stage_map) == "In review"
    assert resolve_issue_stage(with_assignee, {}, {}, stage_map) == "In progress"
    assert resolve_issue_stage(with_agent_label, {}, {}, stage_map) == "Ready"


def test_resolve_issue_stage_intake_requires_flag_off_board_and_no_work_signal():
    # The positive case: flag on, off-board, no work signal -- and only then -- resolves "Intake".
    stage_map = {"Intake": ["New Requests"]}
    issue = {"url": "u1", "state": "OPEN", "labels": []}
    assert resolve_issue_stage(issue, {}, {}, stage_map) == "Intake"
