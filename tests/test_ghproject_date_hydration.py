"""Offline boundary tests for authoritative Projects v2 date reads (issue #25)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ghproject  # noqa: E402


URL = "https://github.com/acme/widgets/issues/1"


def _items(start="stale", target="stale"):
    return {URL: {
        "item_id": "PVTI_1", "number": 1, "status": "Ready",
        "start": start, "target": target,
    }}


def _meta():
    return {
        "project_id": "PVT_1", "host": "github.com",
        "start_field_id": "SF_1", "target_field_id": "TF_1",
    }


def _pages(*nodes):
    return [_page(nodes, has_next_page=False)]


def _page(nodes, *, has_next_page):
    return {"data": {"node": {"items": {
        "pageInfo": {"hasNextPage": has_next_page, "endCursor": "cursor"},
        "nodes": list(nodes),
    }}}}


def _item_node(*values, item_id="PVTI_1", field_values_has_next_page=False):
    return {
        "id": item_id,
        "fieldValues": {
            "pageInfo": {"hasNextPage": field_values_has_next_page},
            "nodes": list(values),
        },
    }


def _date_value(field_id, date):
    return {"date": date, "field": {"id": field_id}}


def test_hydrate_item_dates_treats_successful_empty_snapshot_as_all_cleared():
    """A successful field-ID read with no date values is authoritative, not a read failure."""
    original = _items()
    response = _pages(_item_node())

    with patch("ghproject.ghkit.run", return_value=Mock(stdout=json.dumps(response))) as run_mock:
        hydrated = ghproject.hydrate_item_dates({}, original, _meta())

    assert hydrated[URL]["start"] is None
    assert hydrated[URL]["target"] is None
    assert original[URL]["start"] == "stale"  # input remains immutable
    args = run_mock.call_args.args[1]
    assert args[:2] == ["api", "graphql"]
    assert "--paginate" in args and "--slurp" in args
    assert args[args.index("--hostname") + 1] == "github.com"


def test_hydrate_item_dates_maps_values_by_field_id_not_flattened_name():
    response = _pages(_item_node(
        _date_value("TF_1", "2026-02-02"),
        _date_value("unrelated", "2026-03-03"),
        _date_value("SF_1", "2026-01-01"),
    ))

    with patch("ghproject.ghkit.run", return_value=Mock(stdout=json.dumps(response))):
        hydrated = ghproject.hydrate_item_dates({}, _items(None, None), _meta())

    assert hydrated[URL]["start"] == "2026-01-01"
    assert hydrated[URL]["target"] == "2026-02-02"


def test_hydrate_item_dates_accepts_complete_outer_pagination():
    paged_items = {
        URL: _items(None, None)[URL],
        "https://github.com/acme/widgets/issues/2": {
            "item_id": "PVTI_2", "number": 2, "status": "Ready", "start": None, "target": None,
        },
    }
    response = [
        _page([_item_node(_date_value("SF_1", "2026-01-01"))], has_next_page=True),
        _page([_item_node(_date_value("TF_1", "2026-02-02"), item_id="PVTI_2")],
              has_next_page=False),
    ]

    with patch("ghproject.ghkit.run", return_value=Mock(stdout=json.dumps(response))):
        hydrated = ghproject.hydrate_item_dates({}, paged_items, _meta())

    assert hydrated[URL]["start"] == "2026-01-01"
    assert hydrated["https://github.com/acme/widgets/issues/2"]["target"] == "2026-02-02"


def test_hydrate_item_dates_fails_closed_on_graphql_failure():
    failure = subprocess.CalledProcessError(1, ["gh", "api", "graphql"])
    with patch("ghproject.ghkit.run", side_effect=failure):
        assert ghproject.hydrate_item_dates({}, _items(), _meta()) is None


def test_hydrate_item_dates_fails_closed_on_partial_graphql_errors():
    response = _pages(_item_node())
    response[0]["errors"] = [{"message": "field values unavailable"}]
    with patch("ghproject.ghkit.run", return_value=Mock(stdout=json.dumps(response))):
        assert ghproject.hydrate_item_dates({}, _items(), _meta()) is None


def test_hydrate_item_dates_fails_closed_on_malformed_field_metadata():
    meta = {**_meta(), "start_field_id": {"not": "an id"}}
    with patch("ghproject.ghkit.run") as run_mock:
        assert ghproject.hydrate_item_dates({}, _items(), meta) is None
    run_mock.assert_not_called()


def test_hydrate_item_dates_fails_closed_when_nested_field_values_are_truncated():
    response = _pages(_item_node(field_values_has_next_page=True))
    with patch("ghproject.ghkit.run", return_value=Mock(stdout=json.dumps(response))):
        assert ghproject.hydrate_item_dates({}, _items(), _meta()) is None


def test_hydrate_item_dates_fails_closed_when_item_list_and_graphql_snapshots_disagree():
    response = _pages(_item_node(item_id="PVTI_OTHER"))
    with patch("ghproject.ghkit.run", return_value=Mock(stdout=json.dumps(response))):
        assert ghproject.hydrate_item_dates({}, _items(), _meta()) is None
