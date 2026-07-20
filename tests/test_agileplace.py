"""Unit tests for agileplace.py's pure op-builders. No network or gh -- pins the JSON Patch shapes
the live sync depends on. Run: pytest -q
"""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agileplace import (  # noqa: E402
    _card_with_version,
    api,
    card_external_urls,
    card_block_reason,
    card_is_blocked,
    card_tags,
    connect_children,
    create_card,
    disconnect_children,
    get_card,
    list_cards,
    op_custom_id,
    op_tag,
    ops_blocked,
    ops_tag_remove,
    patch_card,
    resolve_lane_for_stage,
)

CFG = {"token": "t", "host": "h", "board_id": "b1"}


# --- api: transport failures become clean command errors -----------------

def test_api_converts_non_json_200_to_standard_truncated_system_exit():
    raw = b"<html>captive portal</html>" + (b"x" * 400)

    with patch("agileplace.urllib.request.urlopen") as urlopen_mock:
        urlopen_mock.return_value.__enter__.return_value.read.return_value = raw
        with pytest.raises(SystemExit) as exc_info:
            api(CFG, "GET", "card")

    message = str(exc_info.value)
    prefix = "AgilePlace GET /card failed: invalid JSON response "
    assert message == prefix + raw[:300].decode()


def test_api_converts_non_utf8_200_to_standard_truncated_system_exit():
    raw = b"\xff<html>invalid encoding</html>" + (b"x" * 400)

    with patch("agileplace.urllib.request.urlopen") as urlopen_mock:
        urlopen_mock.return_value.__enter__.return_value.read.return_value = raw
        with pytest.raises(SystemExit) as exc_info:
            api(CFG, "GET", "card")

    message = str(exc_info.value)
    prefix = "AgilePlace GET /card failed: invalid JSON response "
    assert message == prefix + raw.decode(errors="replace")[:300]


# --- list_cards: response-driven, bounded pagination (issue #16) ---------

def test_list_cards_honors_server_page_size_clamp_and_uses_contiguous_offsets():
    pages = [
        {"cards": [{"id": str(i)} for i in range(25)],
         "pageMeta": {"limit": 25, "totalRecords": 60}},
        {"cards": [{"id": str(i)} for i in range(25, 50)],
         "pageMeta": {"limit": 25, "totalRecords": 60}},
        {"cards": [{"id": str(i)} for i in range(50, 60)],
         "pageMeta": {"limit": 25, "totalRecords": 60}},
    ]

    with patch("agileplace.api", side_effect=pages) as api_mock:
        cards = list_cards(CFG)

    assert [card["id"] for card in cards] == [str(i) for i in range(60)]
    assert [call.kwargs["params"]["offset"] for call in api_mock.call_args_list] == [0, 25, 50]
    assert all(call.kwargs["params"]["limit"] == 200 for call in api_mock.call_args_list)


def test_list_cards_retains_clamped_limit_when_later_metadata_omits_it():
    pages = [
        {"cards": [{"id": str(i)} for i in range(25)],
         "pageMeta": {"limit": 25}},
        {"cards": [{"id": str(i)} for i in range(25, 50)], "pageMeta": {}},
        {"cards": [{"id": str(i)} for i in range(50, 60)], "pageMeta": {}},
    ]

    with patch("agileplace.api", side_effect=pages) as api_mock:
        cards = list_cards(CFG)

    assert [card["id"] for card in cards] == [str(i) for i in range(60)]
    assert [call.kwargs["params"]["offset"] for call in api_mock.call_args_list] == [0, 25, 50]


def test_list_cards_stops_at_sane_total_records_even_when_final_page_is_full():
    pages = [
        {"cards": [{"id": str(i)} for i in range(25)],
         "pageMeta": {"limit": 25, "totalRecords": 50}},
        {"cards": [{"id": str(i)} for i in range(25, 50)],
         "pageMeta": {"limit": 25, "totalRecords": 50}},
    ]

    with patch("agileplace.api", side_effect=pages) as api_mock:
        cards = list_cards(CFG)

    assert len(cards) == 50
    assert api_mock.call_count == 2


def test_list_cards_ignores_total_records_smaller_than_cards_already_received():
    pages = [
        {"cards": [{"id": str(i)} for i in range(25)],
         "pageMeta": {"limit": 25, "totalRecords": 1}},
        {"cards": [], "pageMeta": {"limit": 25, "totalRecords": 1}},
    ]

    with patch("agileplace.api", side_effect=pages) as api_mock:
        cards = list_cards(CFG)

    assert len(cards) == 25
    assert [call.kwargs["params"]["offset"] for call in api_mock.call_args_list] == [0, 25]


def test_list_cards_invalidates_conflicting_total_instead_of_stopping_early():
    pages = [
        {"cards": [{"id": str(i)} for i in range(25)],
         "pageMeta": {"limit": 25, "totalRecords": 100}},
        {"cards": [{"id": str(i)} for i in range(25, 50)],
         "pageMeta": {"limit": 25, "totalRecords": 50}},
        {"cards": [{"id": str(i)} for i in range(50, 60)],
         "pageMeta": {"limit": 25}},
    ]

    with patch("agileplace.api", side_effect=pages) as api_mock:
        cards = list_cards(CFG)

    assert len(cards) == 60
    assert [call.kwargs["params"]["offset"] for call in api_mock.call_args_list] == [0, 25, 50]


def test_list_cards_invalidates_retained_total_once_received_cards_exceed_it():
    pages = [
        {"cards": [{"id": str(i)} for i in range(25)],
         "pageMeta": {"limit": 25, "totalRecords": 40}},
        {"cards": [{"id": str(i)} for i in range(25, 50)],
         "pageMeta": {"limit": 25}},
        {"cards": [{"id": str(i)} for i in range(50, 60)],
         "pageMeta": {"limit": 25}},
    ]

    with patch("agileplace.api", side_effect=pages) as api_mock:
        cards = list_cards(CFG)

    assert len(cards) == 60
    assert [call.kwargs["params"]["offset"] for call in api_mock.call_args_list] == [0, 25, 50]


def test_list_cards_fails_loud_on_empty_page_before_retained_total():
    pages = [
        {"cards": [{"id": str(i)} for i in range(25)],
         "pageMeta": {"limit": 25, "totalRecords": 100}},
        {"cards": [], "pageMeta": {"limit": 25, "totalRecords": 100}},
    ]

    with patch("agileplace.api", side_effect=pages):
        with pytest.raises(SystemExit, match=r"ended at 25 before totalRecords 100"):
            list_cards(CFG)


def test_list_cards_fails_loud_on_short_page_before_retained_total():
    pages = [
        {"cards": [{"id": str(i)} for i in range(25)],
         "pageMeta": {"limit": 25, "totalRecords": 100}},
        {"cards": [{"id": str(i)} for i in range(25, 35)],
         "pageMeta": {"limit": 25, "totalRecords": 100}},
    ]

    with patch("agileplace.api", side_effect=pages):
        with pytest.raises(SystemExit, match=r"ended at 35 before totalRecords 100"):
            list_cards(CFG)


def test_list_cards_fails_loud_when_hostile_page_meta_never_terminates():
    hostile_page = {
        "cards": [{"id": "same-card"}],
        "pageMeta": {"limit": 1, "totalRecords": 10**100},
    }

    with (
        patch("agileplace.MAX_CARD_PAGE_REQUESTS", 3),
        patch("agileplace.api", return_value=hostile_page) as api_mock,
        pytest.raises(SystemExit, match=r"pagination exceeded.*3 requests"),
    ):
        list_cards(CFG)

    assert api_mock.call_count == 3
    assert [call.kwargs["params"]["offset"] for call in api_mock.call_args_list] == [0, 1, 2]


def test_list_cards_ignores_truthy_non_dict_page_meta():
    pages = [
        {"cards": [{"id": "1"}], "pageMeta": "not-an-object"},
        {"cards": [], "pageMeta": "still-not-an-object"},
    ]

    with patch("agileplace.api", side_effect=pages) as api_mock:
        cards = list_cards(CFG)

    assert cards == [{"id": "1"}]
    assert [call.kwargs["params"]["offset"] for call in api_mock.call_args_list] == [0, 1]


def test_ops_blocked_block_with_reason():
    ops = ops_blocked(True, "waiting on design review")
    assert len(ops) == 2
    assert ops[0] == {"op": "replace", "path": "/isBlocked", "value": True}
    assert ops[1] == {"op": "add", "path": "/blockReason", "value": "waiting on design review"}
    # dict `==` doesn't distinguish bool from int (1 == True), so pin the type explicitly.
    assert ops[0]["value"] is True


def test_ops_blocked_unblock_clears_both():
    ops = ops_blocked(False, None)
    assert len(ops) == 2
    assert ops[0] == {"op": "replace", "path": "/isBlocked", "value": False}
    assert ops[1] == {"op": "add", "path": "/blockReason", "value": ""}
    # dict `==` doesn't distinguish bool from int (0 == False), so pin the type explicitly.
    assert ops[0]["value"] is False


def test_ops_blocked_true_with_no_reason_coerces_empty_string():
    """stages.blocked_reason() can return blocked=True with no reason text -- blockReason must
    still be a str, never None."""
    ops = ops_blocked(True, None)
    assert ops[1]["value"] == ""
    assert isinstance(ops[1]["value"], str)


def test_ops_blocked_op_verbs():
    ops = ops_blocked(True, "x")
    assert ops[0]["op"] == "replace"
    assert ops[1]["op"] == "add"


def test_ops_blocked_never_uses_nested_blockedstatus_path():
    """Guards against reintroducing the bug this test file exists to catch: the nested path is the
    read shape only (card_is_blocked/card_block_reason), never a write path."""
    for ops in (ops_blocked(True, "reason"), ops_blocked(False, None)):
        for op in ops:
            assert "blockedStatus" not in op["path"]


def test_ops_blocked_unblock_forces_empty_reason():
    """An unblocked card carries no reason: even when a truthy reason is passed, unblocking must
    write "" to /blockReason -- never the self-contradictory isBlocked=False + non-empty reason."""
    ops = ops_blocked(False, "some reason")
    assert ops[1] == {"op": "add", "path": "/blockReason", "value": ""}


# --- op_custom_id ---------------------------------------------------------

def test_op_custom_id_replaces_the_card_custom_id():
    assert op_custom_id("XYZ") == {"op": "replace", "path": "/customId", "value": "XYZ"}


# --- op_tag / ops_tag_remove: index-based tag removal (issue #3) ----------

def test_op_tag_returns_append_op_unchanged():
    """Tag add is a non-goal for issue #3 -- op_tag must still return the same /tags/- append op."""
    assert op_tag("foo") == {"op": "add", "path": "/tags/-", "value": "foo"}


def test_op_tag_rejects_add_kwarg():
    """Pins the invariant that op_tag has no `add` kwarg -- a regression that reintroduces
    `def op_tag(tag, add=True)` would default every existing call site (sync.py's two call sites,
    and test_op_tag_returns_append_op_unchanged above) to add=True and stay green, silently
    resurrecting a path back to the undocumented value-based remove op issue #3 removed."""
    with pytest.raises(TypeError):
        op_tag("foo", add=False)


def test_ops_tag_remove_two_of_four_tags_produces_descending_index_ops():
    """The issue's explicit acceptance scenario: removing 2 tags from a 4-tag card produces the
    correct index-based remove ops, in descending index order within the single batch."""
    current_tags = ["alpha", "beta", "gamma", "delta"]
    ops = ops_tag_remove(current_tags, {"beta", "delta"})
    assert ops == [
        {"op": "remove", "path": "/tags/3"},
        {"op": "remove", "path": "/tags/1"},
    ]


def test_ops_tag_remove_raises_on_tag_not_present():
    """A name in tags_to_remove that isn't in current_tags signals a real upstream bug -- fail loud
    rather than silently no-op (which would let sync.py persist state claiming the tag is gone)."""
    with pytest.raises(ValueError, match="missing-tag"):
        ops_tag_remove(["alpha", "beta"], {"missing-tag"})


def test_ops_tag_remove_duplicate_tag_value_removes_every_occurrence():
    """A tag value appearing at multiple indices yields one remove op per occurrence, all still
    sorted descending together -- not just the first match."""
    current_tags = ["dup", "alpha", "dup", "beta"]
    ops = ops_tag_remove(current_tags, {"dup"})
    assert ops == [
        {"op": "remove", "path": "/tags/2"},
        {"op": "remove", "path": "/tags/0"},
    ]


def test_ops_tag_remove_empty_set_returns_empty_list():
    assert ops_tag_remove(["alpha", "beta"], set()) == []


def test_ops_tag_remove_handles_unhashable_malformed_raw_tag_elements():
    current_tags = ["alpha", {"name": "bad"}, "beta"]

    assert ops_tag_remove(current_tags, {"beta"}) == [{"op": "remove", "path": "/tags/2"}]


def test_ops_tag_remove_interleaved_with_op_tag_add_stays_consistent():
    """Covers the issue's 'interleaved add+remove in one patch stays consistent' criterion: adds
    and removes combined into one ops list, with the remove ops still strictly descending among
    themselves and never carrying a `value` member."""
    current_tags = ["alpha", "beta", "gamma", "delta"]
    tag_ops = [op_tag("new-tag")] + ops_tag_remove(current_tags, {"beta", "delta"})
    assert tag_ops[0] == {"op": "add", "path": "/tags/-", "value": "new-tag"}
    remove_ops = tag_ops[1:]
    assert all(op["op"] == "remove" and "value" not in op for op in remove_ops)
    indices = [int(op["path"].rsplit("/", 1)[1]) for op in remove_ops]
    assert indices == sorted(indices, reverse=True)


def test_card_is_blocked_reads_nested_blockedstatus_isblocked():
    """card_is_blocked is the READ-side counterpart to ops_blocked's write-side flat shape -- it
    must keep reading the nested blockedStatus.isBlocked field the AgilePlace API actually returns,
    never the flat /isBlocked write path."""
    assert card_is_blocked({"blockedStatus": {"isBlocked": True, "reason": "x"}}) is True
    assert card_is_blocked({"blockedStatus": {"isBlocked": False, "reason": ""}}) is False
    assert card_is_blocked({}) is False
    assert card_is_blocked({"isBlocked": True}) is False  # flat write shape must not be read


def test_card_tags_skips_non_string_elements_and_warns_with_card_id(capsys):
    card = {"id": "card-7", "tags": ["alpha", {"name": "bad"}, 42, ""]}

    assert card_tags(card) == {"alpha"}
    warnings = [line for line in capsys.readouterr().out.splitlines() if line.startswith("WARN")]
    assert len(warnings) == 2
    assert all("card-7" in line for line in warnings)


def test_card_tags_rejects_supplied_falsy_non_array_and_warns_with_card_id(capsys):
    card = {"id": "card-7-falsy", "tags": ""}

    assert card_tags(card) == set()
    warnings = [line for line in capsys.readouterr().out.splitlines() if line.startswith("WARN")]
    assert len(warnings) == 1
    assert "card-7-falsy" in warnings[0]


def test_card_external_urls_skips_non_dict_links_and_warns_with_card_id(capsys):
    card = {
        "id": "card-8",
        "externalLinks": [{"url": "https://example.test/issues/8"}, "bad-link", 8],
    }

    assert card_external_urls(card) == ["https://example.test/issues/8"]
    warnings = [line for line in capsys.readouterr().out.splitlines() if line.startswith("WARN")]
    assert len(warnings) == 2
    assert all("card-8" in line for line in warnings)


def test_card_external_urls_skips_non_string_urls_and_warns_with_card_id(capsys):
    card = {
        "id": "card-8-url",
        "externalLinks": [
            {"url": "https://example.test/issues/8"},
            {"url": {"host": "example.test"}},
        ],
    }

    assert card_external_urls(card) == ["https://example.test/issues/8"]
    warnings = [line for line in capsys.readouterr().out.splitlines() if line.startswith("WARN")]
    assert len(warnings) == 1
    assert "card-8-url" in warnings[0]


def test_card_external_urls_rejects_supplied_falsy_non_array_before_legacy_fallback(capsys):
    card = {
        "id": "card-8-falsy",
        "externalLinks": {},
        "externalLink": {"url": "https://example.test/issues/8"},
    }

    assert card_external_urls(card) == []
    warnings = [line for line in capsys.readouterr().out.splitlines() if line.startswith("WARN")]
    assert len(warnings) == 1
    assert "card-8-falsy" in warnings[0]


def test_blocked_readers_ignore_string_status_and_warn_with_card_id(capsys):
    card = {"id": "card-9", "blockedStatus": "blocked"}

    assert card_is_blocked(card) is False
    assert card_block_reason(card) == ""
    warnings = [line for line in capsys.readouterr().out.splitlines() if line.startswith("WARN")]
    assert len(warnings) == 2
    assert all("card-9" in line for line in warnings)


def test_blocked_reader_rejects_supplied_falsy_non_object_and_warns_with_card_id(capsys):
    card = {"id": "card-9-falsy", "blockedStatus": 0}

    assert card_is_blocked(card) is False
    warnings = [line for line in capsys.readouterr().out.splitlines() if line.startswith("WARN")]
    assert len(warnings) == 1
    assert "card-9-falsy" in warnings[0]


def test_lane_resolution_skips_non_string_titles_and_unhashable_ids(capsys):
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
    assert any("bad-title" in line for line in warnings)
    assert any("Ready" in line and "unhashable" in line for line in warnings)


def test_card_block_reason_reads_nested_blockedstatus_reason():
    """card_block_reason is the READ-side counterpart to ops_blocked's write-side flat shape -- it
    must keep reading the nested blockedStatus.reason field, never the flat /blockReason write path,
    and must coerce a missing/falsy reason to ''."""
    assert card_block_reason({"blockedStatus": {"isBlocked": True, "reason": "waiting"}}) == "waiting"
    assert card_block_reason({"blockedStatus": {"isBlocked": False, "reason": None}}) == ""
    assert card_block_reason({}) == ""
    assert card_block_reason({"blockReason": "waiting"}) == ""  # flat write shape must not be read


def test_get_card_unwraps_wrapped_card_response():
    """The live single-card GET may wrap the card in {"card": {...}} (VALIDATE LIVE) -- get_card
    must hand callers the flat card dict either way."""
    wrapped = {"card": {"id": "123", "version": 5, "title": "Do the thing"}}
    with patch("agileplace.api", return_value=wrapped) as api_mock:
        card = get_card({"token": "t", "host": "h"}, "123")
    api_mock.assert_called_once_with({"token": "t", "host": "h"}, "GET", "card/123")
    assert card == {"id": "123", "version": 5, "title": "Do the thing"}


def test_get_card_returns_already_flat_response_as_is():
    """If the live API instead returns the card fields at the top level (no "card" wrapper),
    get_card must not misinterpret that shape -- it should hand it back unchanged."""
    flat = {"id": "456", "version": 2, "title": "Another thing"}
    with patch("agileplace.api", return_value=flat):
        card = get_card({"token": "t", "host": "h"}, "456")
    assert card == flat


@pytest.mark.parametrize(
    ("card_id", "encoded_path"),
    [
        ("1/../board/x", "card/1%2F..%2Fboard%2Fx"),
        ("1?x=1", "card/1%3Fx%3D1"),
    ],
)
def test_get_card_quotes_hostile_id_as_one_path_segment(card_id, encoded_path):
    with patch("agileplace.api", return_value={"id": card_id}) as api_mock:
        assert get_card(CFG, card_id) == {"id": card_id}

    api_mock.assert_called_once_with(CFG, "GET", encoded_path)


def test_get_card_fails_loud_when_card_is_null():
    """A live 200 response of {"card": null} must not silently become a bare None return -- that
    would crash callers like _card_with_version with an opaque AttributeError. get_card must
    instead fail loud with a message naming the card id (see issue #8 review finding)."""
    with patch("agileplace.api", return_value={"card": None}):
        with pytest.raises(SystemExit, match="789"):
            get_card({"token": "t", "host": "h"}, "789")


def test_get_card_fails_loud_when_response_is_top_level_null():
    """A bare top-level `null` body decodes to None from api() -- get_card must fail loud rather
    than call .get() on None and raise an opaque AttributeError (see issue #8 review finding)."""
    with patch("agileplace.api", return_value=None):
        with pytest.raises(SystemExit, match="790"):
            get_card({"token": "t", "host": "h"}, "790")


def test_get_card_fails_loud_when_response_is_a_bare_list():
    """The single-card GET's exact shape is unconfirmed (VALIDATE LIVE) -- a non-dict, non-null
    JSON body (e.g. a bare list) must not reach `.get()` and raise an opaque AttributeError.
    get_card must fail loud with a message naming the card id (issue #3 review finding)."""
    with patch("agileplace.api", return_value=[]):
        with pytest.raises(SystemExit, match="791"):
            get_card({"token": "t", "host": "h"}, "791")


def test_get_card_fails_loud_when_response_is_a_string():
    """Same as the bare-list case, for a scalar (string) top-level JSON body."""
    with patch("agileplace.api", return_value="oops"):
        with pytest.raises(SystemExit, match="792"):
            get_card({"token": "t", "host": "h"}, "792")


def test_get_card_fails_loud_when_response_is_a_bool():
    """Same as the bare-list case, for a bool top-level JSON body -- bool is a subclass of int in
    Python but must still be treated as "not a dict" here, never silently accepted."""
    with patch("agileplace.api", return_value=True):
        with pytest.raises(SystemExit, match="793"):
            get_card({"token": "t", "host": "h"}, "793")


def test_get_card_fails_loud_when_wrapped_card_value_is_not_a_dict():
    """A {"card": ...} wrapper whose value is itself a non-dict, non-null JSON type (e.g. a list)
    must also fail loud rather than handing callers a non-dict "card" that crashes downstream."""
    with patch("agileplace.api", return_value={"card": []}):
        with pytest.raises(SystemExit, match="794"):
            get_card({"token": "t", "host": "h"}, "794")


# --- _card_with_version / patch_card: no unversioned PATCH (issue #8) ------

def test_card_with_version_returns_card_unchanged_when_version_present():
    """Version already present -> zero network calls, same dict handed back."""
    card = {"id": "1", "version": 7}
    with patch("agileplace.api") as api_mock:
        result = _card_with_version(CFG, True, card)
    api_mock.assert_not_called()
    assert result == card


def test_card_with_version_skips_refetch_when_apply_is_false():
    """Dry run must never make a refetch network call, even if version is missing."""
    card = {"id": "1"}
    with patch("agileplace.api") as api_mock:
        result = _card_with_version(CFG, False, card)
    api_mock.assert_not_called()
    assert result == card


def test_card_with_version_returns_none_when_refetch_also_has_no_version(capsys):
    """apply=True, missing version, refetch also version-less -> None sentinel + one WARN naming the id."""
    card = {"id": "42", "title": "x"}
    refetched = {"card": {"id": "42", "title": "x"}}
    with patch("agileplace.api", return_value=refetched):
        result = _card_with_version(CFG, True, card)
    assert result is None
    out = capsys.readouterr().out
    warn_lines = [line for line in out.splitlines() if line.startswith("WARN")]
    assert len(warn_lines) == 1
    assert "42" in warn_lines[0]


def test_card_with_version_treats_empty_string_version_as_missing():
    """An empty-string `version` is not a usable resource version -- it must be treated the same as
    a missing one and trigger the refetch, never be handed straight to patch_card's headers."""
    card = {"id": "1", "version": ""}
    refetched = {"card": {"id": "1", "version": 9}}
    with patch("agileplace.api", return_value=refetched) as api_mock:
        result = _card_with_version(CFG, True, card)
    api_mock.assert_called_once_with(CFG, "GET", "card/1")
    assert result == {"id": "1", "version": 9}


def test_card_with_version_returns_none_when_version_is_empty_string_and_refetch_also_empty(capsys):
    """apply=True, version="" (falsy-but-not-None), refetch also comes back version-less/empty ->
    None sentinel + one WARN, same as the missing-version case."""
    card = {"id": "1", "version": ""}
    refetched = {"card": {"id": "1", "version": ""}}
    with patch("agileplace.api", return_value=refetched):
        result = _card_with_version(CFG, True, card)
    assert result is None
    out = capsys.readouterr().out
    warn_lines = [line for line in out.splitlines() if line.startswith("WARN")]
    assert len(warn_lines) == 1
    assert "1" in warn_lines[0]


def test_card_with_version_never_mutates_input_card():
    """The input dict must be unchanged after the call, whatever the outcome (same ref/new dict/None)."""
    versioned_card = {"id": "1", "version": 7}
    before = dict(versioned_card)
    _card_with_version(CFG, True, versioned_card)
    assert versioned_card == before

    dry_card = {"id": "2"}
    before = dict(dry_card)
    _card_with_version(CFG, False, dry_card)
    assert dry_card == before

    success_card = {"id": "42"}
    before = dict(success_card)
    with patch("agileplace.api", return_value={"card": {"id": "42", "version": 3}}):
        _card_with_version(CFG, True, success_card)
    assert success_card == before

    miss_card = {"id": "42"}
    before = dict(miss_card)
    with patch("agileplace.api", return_value={"card": {"id": "42"}}):
        _card_with_version(CFG, True, miss_card)
    assert miss_card == before


# --- refetch must not pair stale ops with a fresh version -----------------


def test_card_with_version_refetch_allows_index_removes_when_tags_unchanged():
    """Version-less card + index tag-remove ops: if the refetched tags MATCH the snapshot, the
    indices are still valid -> proceed with the fresh version."""
    card = {"id": "7", "tags": ["a", "b", "c"]}
    ops = [{"op": "remove", "path": "/tags/1"}]
    refetched = {"card": {"id": "7", "tags": ["a", "b", "c"], "version": 5}}
    with patch("agileplace.api", return_value=refetched):
        result = _card_with_version(CFG, True, card, ops)
    assert result == {"id": "7", "tags": ["a", "b", "c"], "version": 5}


def test_card_with_version_refetch_refuses_index_removes_when_tags_shifted(capsys):
    """Version-less card + index tag-remove ops: if the tags array shifted since the snapshot, the
    fresh version would let a stale /tags/{i} delete the WRONG tag -> fail closed with a WARN."""
    card = {"id": "7", "tags": ["a", "b", "c"]}
    ops = [{"op": "remove", "path": "/tags/2"}]  # index 2 was "c" in the snapshot
    refetched = {"card": {"id": "7", "tags": ["z", "a", "b", "c"], "version": 5}}  # inserted at front
    with patch("agileplace.api", return_value=refetched):
        result = _card_with_version(CFG, True, card, ops)
    assert result is None
    warn_lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("WARN")]
    assert len(warn_lines) == 1
    assert "7" in warn_lines[0]


def test_patch_card_version_present_is_byte_identical_to_pre_fix_behavior():
    """Version already present -> exactly one PATCH call, zero refetch calls."""
    card = {"id": "1", "version": 7}
    ops = [{"op": "replace", "path": "/laneId", "value": "L"}]
    with patch("agileplace.api", return_value={}) as api_mock:
        patch_card(CFG, True, card, ops)
    api_mock.assert_called_once_with(CFG, "PATCH", "card/1", body=ops, headers={"x-lk-resource-version": "7"})


@pytest.mark.parametrize(
    ("card_id", "encoded_path"),
    [
        ("1/../board/x", "card/1%2F..%2Fboard%2Fx"),
        ("1?x=1", "card/1%3Fx%3D1"),
    ],
)
def test_patch_card_quotes_hostile_id_as_one_path_segment(card_id, encoded_path):
    card = {"id": card_id, "version": 7}
    ops = [{"op": "replace", "path": "/laneId", "value": "L"}]

    with patch("agileplace.api", return_value={}) as api_mock:
        patch_card(CFG, True, card, ops)

    api_mock.assert_called_once_with(
        CFG,
        "PATCH",
        encoded_path,
        body=ops,
        headers={"x-lk-resource-version": "7"},
    )


def test_patch_card_dry_run_makes_no_network_call_regardless_of_version():
    """apply=False -> patch_card never calls the network, version present or not."""
    ops = [{"op": "replace", "path": "/laneId", "value": "L"}]
    with patch("agileplace.api") as api_mock:
        patch_card(CFG, False, {"id": "1", "version": 7}, ops)
        patch_card(CFG, False, {"id": "2"}, ops)
    api_mock.assert_not_called()


def test_patch_card_refetches_and_never_sends_empty_string_version_header():
    """A card arriving with version="" must never reach patch_card's PATCH with that empty string
    in the x-lk-resource-version header -- it must refetch first, same as a truly missing version."""
    card = {"id": "42", "version": ""}
    ops = [{"op": "replace", "path": "/laneId", "value": "L"}]

    def fake_api(cfg, method, path, body=None, params=None, headers=None, _attempt=0):
        if method == "GET":
            return {"card": {"id": "42", "version": 11}}
        assert method == "PATCH"
        assert headers == {"x-lk-resource-version": "11"}
        return {"ok": True}

    with patch("agileplace.api", side_effect=fake_api) as api_mock:
        result = patch_card(CFG, True, card, ops)
    assert result == {"ok": True}
    assert api_mock.call_count == 2  # one refetch, one PATCH -- never a PATCH with version=""


def test_patch_card_sends_one_patch_with_refetched_version_on_successful_refetch():
    """apply=True, version-less card whose refetch succeeds -> exactly one PATCH using the refetched version."""
    card = {"id": "42"}
    ops = [{"op": "replace", "path": "/laneId", "value": "L"}]

    def fake_api(cfg, method, path, body=None, params=None, headers=None, _attempt=0):
        if method == "GET":
            return {"card": {"id": "42", "version": 11}}
        assert method == "PATCH"
        assert headers == {"x-lk-resource-version": "11"}
        return {"ok": True}

    with patch("agileplace.api", side_effect=fake_api) as api_mock:
        result = patch_card(CFG, True, card, ops)
    assert result == {"ok": True}
    assert api_mock.call_count == 2  # one refetch, one PATCH


def test_patch_card_never_sends_patch_with_missing_or_empty_version_header():
    """Cross-cutting invariant: whenever patch_card actually issues a PATCH, its
    x-lk-resource-version header is present and non-whitespace -- across every apply/version combo.
    Version 0 is a legitimate value and must still produce the header "0"."""
    ops = [{"op": "replace", "path": "/laneId", "value": "L"}]
    # (apply, card, refetch_response, expected_header)
    scenarios = [
        (True, {"id": "1", "version": 7}, {}, "7"),                                       # version already present
        (True, {"id": "2"}, {"card": {"id": "2", "version": 3}}, "3"),                    # version-less, refetch succeeds
        (True, {"id": "5", "version": ""}, {"card": {"id": "5", "version": 13}}, "13"),   # empty-string, refetch succeeds
        (True, {"id": "7", "version": 0}, {}, "0"),                                       # version 0 is legitimate -> "0"
        (True, {"id": "8", "version": "   "}, {"card": {"id": "8", "version": 9}}, "9"),  # whitespace, refetch succeeds
    ]
    for apply, card, refetch_response, expected in scenarios:
        def fake_api(cfg, method, path, body=None, params=None, headers=None, _attempt=0,
                     _refetch=refetch_response, _expected=expected):
            if method == "GET":
                return _refetch
            header = headers.get("x-lk-resource-version")
            assert header is not None and header.strip()  # present and non-whitespace
            assert header == _expected
            return {}
        with patch("agileplace.api", side_effect=fake_api):
            patch_card(CFG, apply, card, ops)

    # Version-less + apply False sends zero PATCH -> vacuously satisfies the invariant.
    no_patch_scenarios = [
        (False, {"id": "3"}, None),
    ]
    for apply, card, refetch_response in no_patch_scenarios:
        def fake_api_no_patch(cfg, method, path, body=None, params=None, headers=None, _attempt=0,
                              _refetch=refetch_response):
            assert method != "PATCH"
            return _refetch or {}
        with patch("agileplace.api", side_effect=fake_api_no_patch):
            patch_card(CFG, apply, card, ops)

    # Apply-mode double misses also send zero PATCH, then fail the run so state cannot advance.
    double_miss_scenarios = [
        ({"id": "4"}, {"card": {"id": "4"}}),
        ({"id": "6", "version": ""}, {"card": {"id": "6", "version": ""}}),
        ({"id": "9", "version": "  "}, {"card": {"id": "9", "version": "  "}}),
    ]
    for card, refetch_response in double_miss_scenarios:
        def fake_api_double_miss(cfg, method, path, body=None, params=None, headers=None, _attempt=0,
                                 _refetch=refetch_response):
            assert method != "PATCH"
            return _refetch
        with patch("agileplace.api", side_effect=fake_api_double_miss), pytest.raises(SystemExit):
            patch_card(CFG, True, card, ops)


def test_connect_children_disconnect_children_create_card_have_no_version_header_logic():
    """Non-goal preserved: no version header logic is added to any non-card-PATCH endpoint."""
    with patch("agileplace.api", return_value={}) as api_mock:
        connect_children(CFG, True, "parent", ["c1", "c2"])
    _, kwargs = api_mock.call_args
    assert kwargs.get("headers") is None

    with patch("agileplace.api", return_value={}) as api_mock:
        disconnect_children(CFG, True, "parent", ["c1", "c2"])
    _, kwargs = api_mock.call_args
    assert kwargs.get("headers") is None

    with patch("agileplace.api", return_value={"id": "new"}) as api_mock:
        create_card(CFG, True, "Title", "CID-1", "https://example.com", None)
    _, kwargs = api_mock.call_args
    assert kwargs.get("headers") is None
