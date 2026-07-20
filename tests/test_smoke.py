"""Offline tests for smoke.py: live write-path validation with preview, confirm, and cleanup.

The fake AgilePlace tenant below enforces optimistic concurrency (409 on a stale
x-lk-resource-version) so the smoke script's expected-conflict probe can be tested both ways.
"""
from __future__ import annotations

import email.message
import io
import json
import sys
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import smoke  # noqa: E402


class _Response:
    def __init__(self, payload: object):
        self._payload = json.dumps(payload).encode() if payload is not None else b""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self._payload


def _http_error(url: str, code: int, body: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, "error", email.message.Message(),
                                  io.BytesIO(body.encode()))


class FakeTenant:
    """Minimal stateful AgilePlace io v2 double for the whole smoke sequence."""

    def __init__(self, *, accept_stale: bool = False, fail_child_create_body: str | None = None,
                 create_returns_version: bool = True):
        self.accept_stale = accept_stale
        self.fail_child_create_body = fail_child_create_body
        self.create_returns_version = create_returns_version
        self.writes: list[tuple[str, str]] = []
        self.cards: dict[str, dict] = {
            "P1": {"id": "P1", "title": "Existing card", "customId": "EX-1",
                   "laneId": "L1", "tags": [], "version": 3},
        }
        self.children: dict[str, list[str]] = {}
        self._next_id = 0

    def urlopen(self, req, timeout=None):
        method = req.get_method()
        parsed = urllib.parse.urlparse(req.full_url)
        path = parsed.path.removeprefix("/io/")
        body = json.loads(req.data) if req.data else None
        if method != "GET":
            self.writes.append((method, path))
        if method == "GET":
            return self._get(req.full_url, path)
        if method == "POST" and path == "card":
            return self._create(req.full_url, body)
        if method == "PATCH" and path.startswith("card/"):
            return self._patch(req, path.removeprefix("card/"), body)
        if path == "card/connections":
            return self._connections(method, body)
        if method == "DELETE" and path.startswith("card/"):
            del self.cards[path.removeprefix("card/")]
            return _Response(None)
        raise AssertionError(f"unexpected request: {method} {path}")

    def _get(self, url: str, path: str):
        if path == "board/42":
            return _Response({"id": "42", "title": "Smoke Test Board", "lanes": [
                {"id": "L1", "title": "Backlog", "cardStatus": "notStarted",
                 "isDefaultDropLane": True},
                {"id": "L2", "title": "Ready", "cardStatus": "notStarted"},
            ]})
        if path == "card":
            cards = list(self.cards.values())
            return _Response({"cards": cards,
                              "pageMeta": {"totalRecords": len(cards), "limit": 200}})
        if path.endswith("/connection/children"):
            child_ids = self.children.get(path.split("/")[1], [])
            return _Response({"cards": [{"id": cid} for cid in child_ids],
                              "pageMeta": {"offset": 0, "limit": 200,
                                           "totalRecords": len(child_ids)}})
        card_id = path.removeprefix("card/")
        if card_id not in self.cards:
            raise _http_error(url, 404, json.dumps({"message": "card not found"}))
        return _Response({"card": {**self.cards[card_id],
                                   "version": str(self.cards[card_id]["version"])}})

    def _create(self, url: str, body: dict):
        if self.fail_child_create_body and body.get("customId") == "SMOKE-C":
            raise _http_error(url, 422, self.fail_child_create_body)
        self._next_id += 1
        card_id = f"S{self._next_id}"
        card = {"id": card_id, "title": body["title"], "customId": body["customId"],
                "laneId": body.get("laneId"), "tags": [], "version": 1}
        if "externalLink" in body:
            card["externalLink"] = body["externalLink"]
        self.cards[card_id] = card
        response = {"id": card_id, **body}
        if self.create_returns_version:
            response["version"] = "1"
        return _Response(response)

    def _patch(self, req, card_id: str, ops: list):
        card = self.cards[card_id]
        headers = {key.lower(): value for key, value in req.header_items()}
        sent = headers.get("x-lk-resource-version")
        if not self.accept_stale and sent != str(card["version"]):
            raise _http_error(req.full_url, 409, json.dumps(
                {"message": f"version conflict: sent {sent}, current {card['version']}"}))
        for op in ops:
            if op["path"] == "/tags/-":
                card["tags"].append(op["value"])
            elif op["path"].startswith("/tags/"):
                card["tags"].pop(int(op["path"].removeprefix("/tags/")))
            elif op["path"] == "/externalLink":
                card["externalLink"] = op["value"]
        card["version"] += 1
        return _Response({"id": card_id, "version": str(card["version"])})

    def _connections(self, method: str, body: dict):
        parent = body["cardIds"][0]
        kids = body["connections"]["children"]
        if method == "POST":
            self.children.setdefault(parent, []).extend(kids)
        else:
            self.children[parent] = [c for c in self.children.get(parent, [])
                                     if c not in kids]
        return _Response({})


@pytest.fixture
def tenant_env(monkeypatch):
    def install(world: FakeTenant, answer: str | None = "smoke"):
        monkeypatch.setattr("config.ENV_FILE", Path("/nonexistent/.env"))
        for name, value in (("AGILEPLACE_TOKEN", "test-token"),
                            ("AGILEPLACE_HOST", "tenant.test"),
                            ("AGILEPLACE_BOARD_ID", "42")):
            monkeypatch.setenv(name, value)
        monkeypatch.setattr("urllib.request.urlopen", world.urlopen)
        if answer is None:
            def refuse(_prompt=""):
                raise AssertionError("input() must not be called with --yes")
            monkeypatch.setattr("builtins.input", refuse)
        else:
            monkeypatch.setattr("builtins.input", lambda _prompt="": answer)
        return world
    return install


def test_declining_confirmation_previews_board_but_writes_nothing(tenant_env, capsys):
    world = tenant_env(FakeTenant(), answer="nope")

    assert smoke.main([]) == 0

    out = capsys.readouterr().out
    assert "Smoke Test Board" in out
    assert "Existing card" in out
    assert "aborted" in out.lower()
    assert world.writes == []


def test_confirmed_run_executes_whole_sequence_and_cleans_up(tenant_env, capsys):
    world = tenant_env(FakeTenant())

    assert smoke.main([]) == 0

    assert world.writes == [
        ("POST", "card"),                 # parent (customId + externalLink)
        ("PATCH", "card/S1"),             # tag add
        ("PATCH", "card/S1"),             # tag remove (index-based)
        ("POST", "card"),                 # child, no external link
        ("PATCH", "card/S2"),             # externalLink add on bare card
        ("POST", "card/connections"),     # connect child
        ("DELETE", "card/connections"),   # disconnect child
        ("PATCH", "card/S1"),             # deliberate stale-version probe
        ("DELETE", "card/S2"),            # cleanup child
        ("DELETE", "card/S1"),            # cleanup parent
    ]
    assert set(world.cards) == {"P1"}  # only the pre-existing card survives
    out = capsys.readouterr().out
    assert "FAIL" not in out
    assert "wrapped" in out            # single-card GET shape reported
    assert "HTTP 409" in out           # stale probe rejection surfaced verbatim
    assert "404" in out                # post-delete GET confirms the cards are gone


def test_versionless_create_response_is_informational_not_a_failure(tenant_env, capsys):
    """Live tenants return no version on create (confirmed 2026-07-20); the sync's
    refetch-before-PATCH path handles that, so smoke must report the fact without failing."""
    tenant_env(FakeTenant(create_returns_version=False))

    assert smoke.main([]) == 0

    out = capsys.readouterr().out
    assert "FAIL" not in out
    assert "INFO" in out
    assert "refetch" in out  # the report says which patch path the sync will take


def test_yes_flag_skips_the_confirmation_prompt(tenant_env):
    tenant_env(FakeTenant(), answer=None)

    assert smoke.main(["--yes"]) == 0


def test_write_failure_prints_full_server_body_and_cleans_up(tenant_env, capsys):
    long_body = json.dumps({"message": "bad shape", "detail": "x" * 400,
                            "marker": "END-OF-BODY-SENTINEL"})
    assert len(long_body) > 300
    world = tenant_env(FakeTenant(fail_child_create_body=long_body))

    assert smoke.main([]) == 1

    out = capsys.readouterr().out
    assert "HTTP 422" in out
    assert "END-OF-BODY-SENTINEL" in out  # beyond api()'s 300-char message cap
    assert set(world.cards) == {"P1"}     # parent still cleaned up after the failure


def test_accepted_stale_write_is_reported_as_failed_concurrency_check(tenant_env, capsys):
    tenant_env(FakeTenant(accept_stale=True))

    assert smoke.main([]) == 1

    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "stale" in out.lower()


def test_missing_configuration_fails_loud(monkeypatch, tmp_path):
    for name in ("AGILEPLACE_TOKEN", "AGILEPLACE_HOST", "AGILEPLACE_BOARD_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("config.ENV_FILE", tmp_path / "no-such.env")

    with pytest.raises(SystemExit, match="AGILEPLACE"):
        smoke.main([])
