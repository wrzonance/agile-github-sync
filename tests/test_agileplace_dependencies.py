"""Unit tests for agileplace's dependency transport (issue #57, Phase 1).

Shapes live-confirmed 2026-07-21 (see API-VALIDATION.md "Dependencies API discovery"):
  read   GET    /io/card/{cardId}/dependency  -> {"dependencies": [{direction, cardId, timing, ...}]}
  create POST   /io/card/dependency  {"cardIds": [dep], "dependsOnCardIds": [...], "timing": "finishToStart"}
  delete DELETE /io/card/dependency  {"cardIds": [dep], "dependsOnCardIds": [...]}

The invariants: reads fail CLOSED (None = unknown, never "no dependencies"); writes send
exactly the confirmed bodies; empty id lists are no-ops that never touch the network.

Run: pytest -q
"""
import email.message
import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agileplace import (  # noqa: E402
    card_dependencies,
    create_dependencies,
    delete_dependencies,
    incoming_dependency_ids,
)

CFG = {"token": "t", "host": "h", "board_id": "b1"}


class _Response:
    def __init__(self, payload: object):
        self._payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self._payload


def _serve(monkeypatch, payload=None, *, error_code=None):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append((req.get_method(), req.full_url, json.loads(req.data) if req.data else None))
        if error_code is not None:
            raise urllib.error.HTTPError(req.full_url, error_code, "err",
                                         email.message.Message(), io.BytesIO(b'{"m":"x"}'))
        return _Response(payload)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


# --- card_dependencies (read, fail-closed) --------------------------------

def test_card_dependencies_returns_entries(monkeypatch):
    entries = [{"direction": "incoming", "cardId": "B1", "timing": "finishToStart"}]
    calls = _serve(monkeypatch, {"dependencies": entries})
    assert card_dependencies(CFG, "C1") == entries
    method, url, body = calls[0]
    assert (method, body) == ("GET", None)
    assert url.endswith("/io/card/C1/dependency")


def test_card_dependencies_http_failure_returns_none_with_warn(monkeypatch, capsys):
    _serve(monkeypatch, error_code=500)
    assert card_dependencies(CFG, "C1") is None
    assert "WARN" in capsys.readouterr().out


@pytest.mark.parametrize("payload", [
    {"dependencies": "nope"},          # not a list
    {"dependencies": [{"ok": 1}, 7]},  # non-dict entry
    {"unexpected": []},                # missing key
    [],                                # not even an object
    # gpt-5.6-sol review P2: an incomplete snapshot acted on authoritatively re-creates an
    # existing pair -> the live 409 aborts the run. Entries must carry a usable
    # direction/cardId, and a paginating response can no longer be assumed complete.
    {"dependencies": [{"cardId": "B1"}]},                          # no direction
    {"dependencies": [{"direction": "incoming"}]},                 # no cardId
    {"dependencies": [{"direction": "sideways", "cardId": "B1"}]}, # unknown direction
    {"dependencies": [], "pageMeta": {"totalRecords": 7}},         # server began paginating
])
def test_card_dependencies_malformed_shapes_return_none(monkeypatch, capsys, payload):
    _serve(monkeypatch, payload)
    assert card_dependencies(CFG, "C1") is None
    assert "WARN" in capsys.readouterr().out


def test_incoming_dependency_ids_filters_direction_and_missing_ids():
    entries = [
        {"direction": "incoming", "cardId": "B1", "timing": "finishToStart"},
        {"direction": "incoming", "cardId": 42},        # non-str id normalizes
        {"direction": "outgoing", "cardId": "X9"},      # not a blocker of this card
        {"direction": "incoming"},                      # no cardId -> ignored
        {"cardId": "Y2"},                               # no direction -> ignored
    ]
    assert incoming_dependency_ids(entries) == {"B1", "42"}


# --- create/delete (writes, confirmed bodies) -----------------------------

def test_create_dependencies_sends_confirmed_body(monkeypatch):
    calls = _serve(monkeypatch, {})
    create_dependencies(CFG, True, "C1", {"B2", "B1"})
    method, url, body = calls[0]
    assert method == "POST"
    assert url.endswith("/io/card/dependency")
    assert body == {"cardIds": ["C1"], "dependsOnCardIds": ["B1", "B2"],
                    "timing": "finishToStart"}


def test_delete_dependencies_sends_confirmed_body_without_timing(monkeypatch):
    calls = _serve(monkeypatch, {})
    delete_dependencies(CFG, True, "C1", {"B1"})
    method, url, body = calls[0]
    assert method == "DELETE"
    assert url.endswith("/io/card/dependency")
    assert body == {"cardIds": ["C1"], "dependsOnCardIds": ["B1"]}


def test_empty_or_falsy_ids_are_network_free_no_ops(monkeypatch):
    calls = _serve(monkeypatch, {})
    create_dependencies(CFG, True, "C1", set())
    create_dependencies(CFG, True, "C1", {"", None})
    delete_dependencies(CFG, True, "C1", [])
    assert calls == []


def test_dry_run_prints_instead_of_sending(monkeypatch, capsys):
    calls = _serve(monkeypatch, {})
    create_dependencies(CFG, False, "C1", {"B1"})
    delete_dependencies(CFG, False, "C1", {"B1"})
    out = capsys.readouterr().out
    assert calls == []
    assert "DRY   POST /io/card/dependency" in out
    assert "DRY   DELETE /io/card/dependency" in out
