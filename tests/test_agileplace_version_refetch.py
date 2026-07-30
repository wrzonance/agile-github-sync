"""Regression tests for version-less card refetches and state-safe PATCH aborts (issue #29)."""
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agilesync.board.agileplace import (  # noqa: E402
    _card_value_for_patch_path,
    _card_with_version,
    patch_card,
)

CFG = {"token": "t", "host": "h", "board_id": "b1"}


def test_card_with_version_refetches_and_returns_validated_fresh_snapshot():
    """A fresh version must stay attached to the fresh resource, never a stale card snapshot."""
    card = {"id": "42", "title": "stale", "laneId": "L1"}
    fresh_card = {"id": "42", "title": "fresh", "laneId": "L1", "version": 9}
    refetched = {"card": fresh_card}
    ops = [{"op": "replace", "path": "/laneId", "value": "L2"}]

    with patch("agilesync.board.agileplace.api", return_value=refetched) as api_mock:
        result = _card_with_version(CFG, True, card, ops)

    api_mock.assert_called_once_with(CFG, "GET", "card/42")
    assert result == fresh_card
    assert result is not card
    assert "version" not in card


@pytest.mark.parametrize(
    ("card_fields", "fresh_fields", "ops"),
    [
        (
            {"laneId": "L1"},
            {"laneId": "L2"},
            [{"op": "replace", "path": "/laneId", "value": "L3"}],
        ),
        (
            {"tags": ["a"]},
            {"tags": ["a", "b"]},
            [{"op": "add", "path": "/tags/-", "value": "new"}],
        ),
        (
            {"customId": "OLD"},
            {"customId": "CONCURRENT"},
            [{"op": "replace", "path": "/customId", "value": "NEW"}],
        ),
        (
            {"plannedStart": "2026-01-01"},
            {"plannedStart": "2026-01-02"},
            [{"op": "replace", "path": "/plannedStart", "value": "2026-01-03"}],
        ),
        (
            {"blockedStatus": {"isBlocked": False, "reason": ""}},
            {"blockedStatus": {"isBlocked": True, "reason": "concurrent"}},
            [
                {"op": "replace", "path": "/isBlocked", "value": True},
                {"op": "add", "path": "/blockReason", "value": "sync"},
            ],
        ),
    ],
)
def test_refetch_refuses_ops_when_touched_snapshot_changed(card_fields, fresh_fields, ops, capsys):
    """A refetch may supply a version only when every field targeted by stale ops is unchanged."""
    card = {"id": "7", **card_fields}
    refetched = {"card": {"id": "7", "version": 5, **fresh_fields}}

    with patch("agilesync.board.agileplace.api", return_value=refetched):
        result = _card_with_version(CFG, True, card, ops)

    assert result is None
    warning = capsys.readouterr().out
    assert "card 7" in warning
    assert "changed between snapshot and version refetch" in warning


@pytest.mark.parametrize(
    ("fresh_card", "ops", "warning"),
    [
        (
            {"id": "other", "version": 5, "laneId": "L1"},
            [{"op": "replace", "path": "/laneId", "value": "L2"}],
            "different card id",
        ),
        (
            {"id": "7", "version": 5, "laneId": "L1"},
            [{"op": "replace", "path": "/futureField", "value": "x"}],
            "stale ops",
        ),
    ],
)
def test_refetch_fails_closed_when_snapshot_cannot_be_validated(fresh_card, ops, warning, capsys):
    card = {"id": "7", "laneId": "L1"}

    with patch("agilesync.board.agileplace.api", return_value={"card": fresh_card}):
        result = _card_with_version(CFG, True, card, ops)

    assert result is None
    assert warning in capsys.readouterr().out


def test_patch_card_aborts_after_double_miss_without_sending_patch(capsys):
    """A skipped apply PATCH is a failed write and must abort before callers persist merge state."""
    card = {"id": "42"}
    ops = [{"op": "replace", "path": "/laneId", "value": "L"}]

    with patch("agilesync.board.agileplace.api", return_value={"card": {"id": "42"}}) as api_mock:
        with pytest.raises(SystemExit, match="card 42 PATCH"):
            patch_card(CFG, True, card, ops)

    api_mock.assert_called_once_with(CFG, "GET", "card/42")
    warn_lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("WARN")]
    assert len(warn_lines) == 1
    assert "42" in warn_lines[0]


# --- issue #72: one refetch-validate-retry on optimistic-concurrency conflict -----

import email.message  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402


class _Response:
    def __init__(self, payload: object):
        self._payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self._payload


def _http_error(url: str, code: int, body: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, "error", email.message.Message(),
                                  io.BytesIO(body.encode()))


class _SequencedTenant:
    """Scripted urlopen: each PATCH consumes the next entry of `patch_results`
    (an int = raise that HTTP error; a dict = respond with it); GETs serve `fresh`."""

    def __init__(self, fresh: dict, patch_results: list):
        self.fresh = fresh
        self.patch_results = list(patch_results)
        self.patch_headers: list = []

    def urlopen(self, req, timeout=None):
        if req.get_method() == "GET":
            return _Response(self.fresh)
        self.patch_headers.append({k.lower(): v for k, v in req.header_items()})
        result = self.patch_results.pop(0)
        if isinstance(result, int):
            raise _http_error(req.full_url, result, '{"message": "conflict"}')
        return _Response(result)


def _snapshot_card():
    return {"id": "C1", "version": "12", "laneId": "L_OLD",
            "plannedStart": None, "plannedFinish": None}


_LANE_OPS = [{"op": "replace", "path": "/laneId", "value": "L_NEW"}]


@pytest.mark.parametrize("conflict_status", [409, 428])
def test_unrelated_version_bump_retries_once_with_fresh_version(monkeypatch, capsys,
                                                                conflict_status):
    """The live 2026-07-21 failure mode: the card's version ticked mid-run but the targeted
    field (/laneId) is untouched on the server -- ONE retry with the fresh version succeeds.
    Both conflict statuses the server is known to emit (409 from the fake-tenant era, 428
    confirmed live) must take the retry branch."""
    fresh = {**_snapshot_card(), "version": "13"}          # laneId unchanged -> unrelated bump
    tenant = _SequencedTenant(fresh, [conflict_status, {"id": "C1", "version": "14"}])
    monkeypatch.setattr(urllib.request, "urlopen", tenant.urlopen)
    result = patch_card(CFG, True, _snapshot_card(), _LANE_OPS)
    assert result == {"id": "C1", "version": "14"}
    assert [h.get("x-lk-resource-version") for h in tenant.patch_headers] == ["12", "13"]
    assert "retrying the PATCH once" in capsys.readouterr().out


def test_targeted_field_changed_on_refetch_aborts_without_retry(monkeypatch):
    """A human really moved the card meanwhile: /laneId differs from the run's snapshot, so the
    conflict re-raises and NO second PATCH is attempted -- the #8 guard's authority is intact."""
    fresh = {**_snapshot_card(), "version": "13", "laneId": "L_HUMAN"}
    tenant = _SequencedTenant(fresh, [428])
    monkeypatch.setattr(urllib.request, "urlopen", tenant.urlopen)
    with pytest.raises(SystemExit):
        patch_card(CFG, True, _snapshot_card(), _LANE_OPS)
    assert len(tenant.patch_headers) == 1                  # no retry PATCH went out


def test_second_conflict_propagates_never_a_retry_loop(monkeypatch):
    fresh = {**_snapshot_card(), "version": "13"}
    tenant = _SequencedTenant(fresh, [428, 428])
    monkeypatch.setattr(urllib.request, "urlopen", tenant.urlopen)
    with pytest.raises(SystemExit):
        patch_card(CFG, True, _snapshot_card(), _LANE_OPS)
    assert len(tenant.patch_headers) == 2                  # exactly two attempts, then done


def test_non_conflict_failures_never_trigger_the_retry_path(monkeypatch):
    tenant = _SequencedTenant(_snapshot_card(), [422])
    monkeypatch.setattr(urllib.request, "urlopen", tenant.urlopen)
    with pytest.raises(SystemExit):
        patch_card(CFG, True, _snapshot_card(), _LANE_OPS)
    assert len(tenant.patch_headers) == 1                  # 422 = shape bug: fail loud, no retry


# --- issue #105: the guard must be able to VALIDATE every path the sync can queue -----
#
# The #72 retry above only ever gated on /laneId, so the gap this section pins went unnoticed: a
# path _card_value_for_patch_path doesn't know raises ValueError, which _changed_patch_paths counts
# as "changed" -- refusing the retry and re-raising the conflict. sync.main()'s flush queues
# /description on essentially every card whose text differs from its issue (description_sync), so
# that one missing case made the whole retry path dead for the ordinary sync, aborting real applies
# on HTTP 428 (live 2026-07-29).

def _flush_snapshot_card():
    """The shape sync.main()'s flush actually patches: a card whose observed description is part of
    the run's snapshot (board_reads.hydrate_run_reads hydrates `description` in place; a card
    created this run gets it from sync._created_card_snapshot)."""
    return {"id": "C1", "version": "1", "laneId": "L", "tags": [],
            "description": "<p>as the run observed it</p>", "externalLink": None}


_DESCRIPTION_OPS = [{"op": "replace", "path": "/description", "value": "<p>merged</p>"},
                    {"op": "add", "path": "/tags/-", "value": "type:bug"}]


def test_version_bump_retries_for_a_card_carrying_a_description_op(monkeypatch, capsys):
    """The live failure: the card's version ticked between snapshot and flush (a dependency write
    earlier in the same run), but nothing the queued ops target changed -- so ONE retry with the
    fresh version must go out, exactly as it already does for a /laneId-only card."""
    fresh = {**_flush_snapshot_card(), "version": "2"}      # description untouched on the server
    tenant = _SequencedTenant(fresh, [428, {"id": "C1", "version": "3"}])
    monkeypatch.setattr(urllib.request, "urlopen", tenant.urlopen)
    result = patch_card(CFG, True, _flush_snapshot_card(), _DESCRIPTION_OPS)
    assert result == {"id": "C1", "version": "3"}
    assert [h.get("x-lk-resource-version") for h in tenant.patch_headers] == ["1", "2"]
    assert "retrying the PATCH once" in capsys.readouterr().out


def test_concurrently_edited_description_still_refuses_the_retry(monkeypatch):
    """The flip side: a human really did edit the description meanwhile, so the run's merged text is
    computed against a superseded base -- the conflict re-raises and no second PATCH is attempted."""
    fresh = {**_flush_snapshot_card(), "version": "2", "description": "<p>a human typed this</p>"}
    tenant = _SequencedTenant(fresh, [428])
    monkeypatch.setattr(urllib.request, "urlopen", tenant.urlopen)
    with pytest.raises(SystemExit):
        patch_card(CFG, True, _flush_snapshot_card(), _DESCRIPTION_OPS)
    assert len(tenant.patch_headers) == 1                  # no retry PATCH went out


def test_version_bump_retries_for_a_card_carrying_an_external_link_op(monkeypatch):
    """Same gap, same fix, for intake's /externalLink writeback path."""
    link_ops = [{"op": "add", "path": "/externalLink",
                 "value": {"label": "GitHub #7", "url": "https://example.test/7"}}]
    fresh = {**_flush_snapshot_card(), "version": "2"}
    tenant = _SequencedTenant(fresh, [428, {"id": "C1", "version": "3"}])
    monkeypatch.setattr(urllib.request, "urlopen", tenant.urlopen)
    assert patch_card(CFG, True, _flush_snapshot_card(), link_ops) == {"id": "C1", "version": "3"}
    assert [h.get("x-lk-resource-version") for h in tenant.patch_headers] == ["1", "2"]


def test_concurrently_added_external_link_still_refuses_the_retry(monkeypatch):
    link_ops = [{"op": "add", "path": "/externalLink",
                 "value": {"label": "GitHub #7", "url": "https://example.test/7"}}]
    fresh = {**_flush_snapshot_card(), "version": "2",
             "externalLink": {"label": "Jira", "url": "https://jira.test/X-1"}}
    tenant = _SequencedTenant(fresh, [428])
    monkeypatch.setattr(urllib.request, "urlopen", tenant.urlopen)
    with pytest.raises(SystemExit):
        patch_card(CFG, True, _flush_snapshot_card(), link_ops)
    assert len(tenant.patch_headers) == 1                  # a foreign link must never be clobbered


_OP_PATH_LITERAL = re.compile(r'"path":\s*"(/[^"]*)"')

# Paths whose op-builder computes them (so the source scan below cannot see the literal):
# agileplace.ops_dates' f"/{field}". Listed explicitly rather than left uncovered.
_COMPUTED_OP_PATHS = ("/plannedStart", "/plannedFinish")


def _op_paths_in_production_source() -> set[str]:
    """Every literal JSON-Patch path any module under agilesync/ can emit."""
    package = Path(__file__).resolve().parent.parent / "agilesync"
    return {path
            for source in package.rglob("*.py")
            for path in _OP_PATH_LITERAL.findall(source.read_text(encoding="utf-8"))}


def test_every_op_path_the_package_emits_is_resolvable_by_the_guard():
    """Completeness pin, not a shape test: _changed_patch_paths turns an unresolvable path into a
    refused retry, so a NEW op-builder whose path the guard doesn't know silently re-breaks issue
    #105's failure mode. If this fails, add a case to _card_value_for_patch_path for the reported
    path (mapping it to the read-side field whose snapshot the op depends on) -- do NOT relax the
    guard's fail-closed behavior for genuinely unknown paths."""
    for path in sorted(_op_paths_in_production_source() | set(_COMPUTED_OP_PATHS)):
        _card_value_for_patch_path({}, path)   # raises ValueError if the guard can't resolve it
