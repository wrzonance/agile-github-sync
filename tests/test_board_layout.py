"""Unit tests for board_layout.py (issue #84 -- split out of agileplace.py).

Pins three invariants for this pure code-motion:
  1. Behavior is byte-for-byte identical to the pre-move functions (same inputs -> same
     outputs/WARNs, including the one live I/O call composed via agileplace.api).
  2. agileplace.py must never import from board_layout.py (no reverse dependency / no cycle).
  3. agileplace.py's remaining public API is untouched -- the 24 symbols it keeps stay present
     with the same callables, and the 11 moved symbols are gone from its namespace (no re-export).

Run: pytest -q
"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agileplace  # noqa: E402
import board_layout  # noqa: E402
from board_layout import (  # noqa: E402
    BoardLayout,
    _ancestor_titles,
    _card_types_with_ids,
    _lanes_with_ids,
    _leaf_lanes,
    _mapped_lanes,
    _release_lane,
    board_layout as fetch_board_layout,
    lane_title,
    resolve_lane_for_stage,
    stage_for_lane,
)

CFG = {"token": "t", "host": "h", "board_id": "b1"}


# --- invariant: agileplace.py never imports board_layout.py (no reverse dependency) --------

def test_agileplace_source_never_imports_board_layout():
    """The dependency runs one way only: board_layout.py -> agileplace.py. If agileplace.py ever
    grew an `import board_layout` (or `from board_layout import ...`), that would be a reverse
    dependency / cycle risk this split is designed to avoid."""
    source = Path(agileplace.__file__).read_text()
    for line in source.splitlines():
        stripped = line.strip()
        assert not stripped.startswith("import board_layout"), line
        assert not stripped.startswith("from board_layout"), line


def test_board_layout_module_imports_agileplace_for_its_one_io_call():
    assert board_layout.agileplace is agileplace


# --- invariant: agileplace.py's remaining public API is untouched --------------------------

_AGILEPLACE_RETAINED_NAMES = (
    "api", "mutate", "list_cards", "get_card", "card_external_urls", "custom_id_value",
    "card_tags", "card_is_blocked", "card_block_reason", "card_child_ids",
    "op_custom_id", "op_lane", "op_tag", "ops_tag_remove", "op_planned_date", "ops_blocked",
    "patch_card", "create_card", "delete_card", "connect_children", "disconnect_children",
    "card_dependencies", "incoming_dependency_ids", "create_dependencies", "delete_dependencies",
)

_MOVED_NAMES = (
    "lane_title", "_lanes_with_ids", "_card_types_with_ids", "BoardLayout", "board_layout",
    "_ancestor_titles", "_leaf_lanes", "_release_lane", "_mapped_lanes",
    "resolve_lane_for_stage", "stage_for_lane",
)


def test_agileplace_retains_every_name_in_its_public_api():
    for name in _AGILEPLACE_RETAINED_NAMES:
        assert callable(getattr(agileplace, name)), f"agileplace.{name} missing or not callable"


def test_agileplace_no_longer_defines_or_reexports_moved_names():
    for name in _MOVED_NAMES:
        assert not hasattr(agileplace, name), f"agileplace.{name} should have moved to board_layout.py"


def test_board_layout_module_defines_every_moved_name():
    for name in _MOVED_NAMES:
        assert hasattr(board_layout, name), f"board_layout.{name} missing"


# --- behavior: byte-for-byte identical to the pre-move functions --------------------------

def test_lane_title_prefers_title_then_name_then_blank():
    assert lane_title({"title": "Ready"}) == "Ready"
    assert lane_title({"name": "Ready"}) == "Ready"
    assert lane_title({}) == ""
    assert lane_title({"title": "  Ready  "}) == "Ready"


def test_lanes_with_ids_skips_malformed_entries_with_one_warn_each(capsys):
    lanes = [
        {"id": "valid", "title": "Ready"},
        {"id": "bad-title", "title": {"text": "Ready"}},
        {"id": ["bad-id"], "title": "Ready"},
        "not-a-dict",
        {"title": "no-id"},
    ]

    valid = _lanes_with_ids(lanes)

    assert valid == [{"id": "valid", "title": "Ready"}]
    warnings = [line for line in capsys.readouterr().out.splitlines() if line.startswith("WARN")]
    assert len(warnings) == 4


def test_card_types_with_ids_skips_malformed_card_types(capsys):
    card_types = [
        {"id": "valid", "title": "Bug"},
        {"id": "bad-title", "title": {"text": "Bug"}},
        {"id": ["bad-id"], "title": "Feature"},
        {"title": "no-id"},
        "not-a-dict",
    ]

    valid = _card_types_with_ids(card_types)

    assert valid == [{"id": "valid", "title": "Bug"}]
    warnings = [line for line in capsys.readouterr().out.splitlines() if line.startswith("WARN")]
    assert len(warnings) == 4
    assert any("bad-title" in line and "non-string" in line for line in warnings)
    assert any("unhashable" in line for line in warnings)
    assert any("no id" in line for line in warnings)
    assert any("is not an object" in line for line in warnings)


def test_board_layout_returns_lanes_and_card_types_as_a_boardlayout():
    """board_layout composes its one I/O call via agileplace.api -- patching agileplace.api (not a
    board_layout-local name) proves board_layout.py calls through the shared client, unchanged."""
    response = {
        "lanes": [{"id": "L1", "title": "Ready"}],
        "cardTypes": [{"id": "T1", "title": "Bug", "isCardType": True}],
    }
    with patch("agileplace.api", return_value=response) as api_mock:
        layout = fetch_board_layout(CFG)
    api_mock.assert_called_once_with(CFG, "GET", "board/b1")
    assert layout == BoardLayout(
        lanes=[{"id": "L1", "title": "Ready"}],
        card_types=[{"id": "T1", "title": "Bug", "isCardType": True}],
    )


def test_board_layout_defaults_missing_cardtypes_to_empty_list():
    with patch("agileplace.api", return_value={"lanes": []}):
        layout = fetch_board_layout(CFG)
    assert layout == BoardLayout(lanes=[], card_types=[])


def test_ancestor_titles_walks_parent_chain_to_the_root():
    by_id = {
        "root": {"id": "root", "title": "Release 1"},
        "mid": {"id": "mid", "title": "Team A", "parentLaneId": "root"},
    }
    lane = {"id": "leaf", "title": "Ready", "parentLaneId": "mid"}

    assert _ancestor_titles(lane, by_id) == ["Team A", "Release 1"]


def test_leaf_lanes_excludes_parent_container_lanes():
    lanes = [
        {"id": "root", "title": "Release 1"},
        {"id": "leaf", "title": "Ready", "parentLaneId": "root"},
    ]

    assert [lane["id"] for lane in _leaf_lanes(lanes)] == ["leaf"]


def test_release_lane_disambiguates_duplicate_titles_by_release_ancestor():
    by_id = {
        "r1": {"id": "r1", "title": "Release 1"},
        "r2": {"id": "r2", "title": "Release 2"},
    }
    candidates = [
        {"id": "l1", "title": "Ready", "parentLaneId": "r1"},
        {"id": "l2", "title": "Ready", "parentLaneId": "r2"},
    ]

    assert _release_lane(candidates, "Release 2", by_id) == candidates[1]
    assert _release_lane(candidates, "", by_id) is None
    assert _release_lane(candidates, "Release 3", by_id) is None


def test_mapped_lanes_resolves_stage_map_titles_in_order():
    leaves = [{"id": "a", "title": "Doing"}, {"id": "b", "title": "In Review"}]

    ordered = _mapped_lanes(leaves, ["In Review", "Doing"], "", {})

    assert [lane["id"] for lane in ordered] == ["b", "a"]


def test_mapped_lanes_returns_none_on_unresolved_duplicate_title():
    leaves = [{"id": "a", "title": "Ready"}, {"id": "b", "title": "Ready"}]

    assert _mapped_lanes(leaves, ["Ready"], "", {}) is None


def test_resolve_lane_for_stage_skips_non_string_titles_and_unhashable_ids(capsys):
    lanes = [
        {"id": "valid", "title": "Ready"},
        {"id": "bad-title", "title": {"text": "Ready"}},
        {"id": ["bad-id"], "title": "Ready"},
    ]

    lane, acceptable = resolve_lane_for_stage(lanes, "Ready", "")

    assert lane == {"id": "valid", "title": "Ready"}
    assert acceptable == {"valid"}
    warnings = [line for line in capsys.readouterr().out.splitlines() if line.startswith("WARN")]
    assert len(warnings) == 2


def test_resolve_lane_for_stage_stage_map_wins_over_title_inference():
    lanes = [{"id": "a", "title": "Doing"}, {"id": "b", "title": "In progress"}]

    lane, acceptable = resolve_lane_for_stage(
        lanes, "In progress", "", stage_map={"In progress": ["Doing"]}
    )

    assert lane == {"id": "a", "title": "Doing"}
    assert acceptable == {"a"}


def test_resolve_lane_for_stage_quiet_suppresses_misconfiguration_warn(capsys):
    lanes = [{"id": "a", "title": "In progress"}]

    resolve_lane_for_stage(lanes, "In progress", "", stage_map={"In progress": ["Nonexistent"]},
                            quiet=True)

    warnings = [line for line in capsys.readouterr().out.splitlines() if line.startswith("WARN")]
    assert warnings == []


def test_stage_for_lane_reverses_the_stage_map_lookup():
    lanes = [{"id": "42", "title": "Doing"}]

    assert stage_for_lane("42", {"In progress": ["Doing"]}, lanes) == "In progress"


def test_stage_for_lane_coerces_int_and_str_lane_ids():
    lanes = [{"id": 42, "title": "Doing"}]

    assert stage_for_lane("42", {"In progress": ["Doing"]}, lanes) == "In progress"


def test_stage_for_lane_returns_none_on_ambiguous_or_unmapped_lane():
    lanes = [{"id": "1", "title": "Ready"}]

    assert stage_for_lane("1", None, lanes) is None
    assert stage_for_lane("unknown", {"Ready": ["Ready"]}, lanes) is None
    assert stage_for_lane("1", {"A": ["Ready"], "B": ["Ready"]}, lanes) is None
