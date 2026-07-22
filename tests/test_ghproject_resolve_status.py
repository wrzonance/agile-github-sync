"""Direct boundary tests for ghproject.resolve_project_v2_status -- the tri-state Projects v2 read
moved out of sync.py's former (private) _resolve_project_v2_status (issue #79). Pins the exact
tri-state contract main() depends on: a configured-but-FAILED read, and a technically-successful
read that yields zero recognized statuses despite the Project having issue-linked items, must both
resolve to project_read_failed=True / move_lanes=False so a bad read never mass-moves lanes
(issue #5). These tests patch ghproject.configured/items/field_meta/hydrate_item_dates directly
(the same module-qualified seam test_sync_main.py already patches), not sync.py -- pinning the
function at its own module boundary rather than only through main()'s integration tests."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ghproject  # noqa: E402


def _cfg():
    return {"gh_project": {"owner": "acme", "number": "7", "status_field": "Status",
                           "start_field": "Start", "target_field": "Target"}}


def _field_meta(start=True, target=True):
    return {"project_id": "PVT_1", "start_field_id": "SF_1" if start else None,
            "target_field_id": "TF_1" if target else None, "host": "github.com"}


def test_not_configured_short_circuits_to_empty_success(capsys):
    with patch("ghproject.configured", return_value=False):
        result = ghproject.resolve_project_v2_status(_cfg())

    assert result == ghproject.ProjectV2Status(project_items={}, project_status={},
                                                field_meta=None, project_read_failed=False,
                                                move_lanes=True)
    assert capsys.readouterr().out == ""


def test_failed_items_call_marks_read_failed_and_blocks_lane_moves(capsys):
    with patch("ghproject.configured", return_value=True), \
         patch("ghproject.items", return_value=None):
        result = ghproject.resolve_project_v2_status(_cfg())

    assert result.project_read_failed is True
    assert result.move_lanes is False
    assert result.project_items == {}
    assert result.project_status == {}
    assert "Projects v2 read FAILED" in capsys.readouterr().out


def test_zero_recognized_statuses_despite_items_is_treated_as_failed(capsys):
    """issue #5: a technically-successful call that yields zero recognized Status values despite
    the Project having issue-linked items (e.g. misspelled GH_PROJECT_STATUS_FIELD) must not
    silently fall back to a full mass-move -- it is the same failure mode reached a different way."""
    items = {"https://github.com/acme/widgets/issues/1": {"item_id": "I1"}}
    with patch("ghproject.configured", return_value=True), \
         patch("ghproject.items", return_value=items):
        result = ghproject.resolve_project_v2_status(_cfg())

    assert result.project_read_failed is True
    assert result.move_lanes is False
    assert result.project_status == {}
    out = capsys.readouterr().out
    assert "none carry a recognized" in out
    assert "GH_PROJECT_STATUS_FIELD" in out


def test_successful_read_without_date_fields_reports_status_only(capsys):
    url = "https://github.com/acme/widgets/issues/1"
    items = {url: {"item_id": "I1", "status": "In Progress"}}
    with patch("ghproject.configured", return_value=True), \
         patch("ghproject.items", return_value=items), \
         patch("ghproject.field_meta", return_value=_field_meta(start=False, target=False)):
        result = ghproject.resolve_project_v2_status(_cfg())

    assert result.project_read_failed is False
    assert result.move_lanes is True
    assert result.project_status == {url: "In Progress"}
    assert result.field_meta is None
    out = capsys.readouterr().out
    assert "1 items carry Status" in out
    assert "dates enabled" not in out


def test_successful_read_with_date_fields_hydrates_items_and_reports_dates_enabled(capsys):
    url = "https://github.com/acme/widgets/issues/1"
    items = {url: {"item_id": "I1", "status": "In Progress"}}
    hydrated = {url: {"item_id": "I1", "status": "In Progress", "start": "2026-01-01", "target": None}}
    meta = _field_meta()
    with patch("ghproject.configured", return_value=True), \
         patch("ghproject.items", return_value=items), \
         patch("ghproject.field_meta", return_value=meta), \
         patch("ghproject.hydrate_item_dates", return_value=hydrated) as hydrate_mock:
        result = ghproject.resolve_project_v2_status(_cfg())

    hydrate_mock.assert_called_once_with(_cfg(), items, meta)
    assert result.project_read_failed is False
    assert result.project_items == hydrated
    assert result.field_meta == meta
    assert "dates enabled" in capsys.readouterr().out


def test_date_hydration_failure_drops_field_meta_but_leaves_status_read_intact(capsys):
    """A failed date-field-value read must skip only date sync -- it must not be conflated with the
    Status read failing, so lane moves stay governed by project_read_failed alone."""
    url = "https://github.com/acme/widgets/issues/1"
    items = {url: {"item_id": "I1", "status": "In Progress"}}
    with patch("ghproject.configured", return_value=True), \
         patch("ghproject.items", return_value=items), \
         patch("ghproject.field_meta", return_value=_field_meta()), \
         patch("ghproject.hydrate_item_dates", return_value=None):
        result = ghproject.resolve_project_v2_status(_cfg())

    assert result.project_read_failed is False
    assert result.move_lanes is True
    assert result.field_meta is None
    assert result.project_items == items
    out = capsys.readouterr().out
    assert "1 items carry Status" in out
    assert "dates enabled" not in out
    assert "date field-value read FAILED" in out
