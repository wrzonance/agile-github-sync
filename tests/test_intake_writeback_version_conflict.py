"""Regression test for the critical writeback version-staleness bug (issue #62 post-review).

`intake._writeback` issues the customId and external-link writes as two SEPARATE
`agileplace.patch_card` calls against the same `card` object (see the ordering rationale in
`intake._writeback`'s own docstring and API-VALIDATION.md's "Reverse intake" section). The first
PATCH bumps the card's server-side resource version; reusing the same, now-stale `card.version`
for the second PATCH deterministically produces an HTTP 409/428 on every real apply=True writeback
against a card that came from `agileplace.list_cards()` with a usable version -- the ordinary case.
`agileplace._card_value_for_patch_path` has no case for `/externalLink`, so `_conflict_retry`
always treats it as "changed" and refuses the retry, and the original conflict propagates
uncaught out of `sync.main()`, aborting the entire run.

This test drives `intake._writeback` against a small stateful fake AgilePlace tenant (mirroring
`tests/test_agileplace_version_refetch.py`'s `_SequencedTenant`, but tracking the card's version
across GET/PATCH like a real optimistic-concurrency server) to prove the second write never sends
a version the server has already superseded.

Run: pytest -q tests/test_intake_writeback_version_conflict.py
"""
from __future__ import annotations

import email.message
import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import intake  # noqa: E402

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


def _http_error(url: str, code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, "error", email.message.Message(),
                                  io.BytesIO(b'{"message": "conflict"}'))


class _VersionedCardServer:
    """A minimal, STATEFUL fake AgilePlace tenant: tracks one card's fields and resource version
    across GET/PATCH calls, rejecting (428) any PATCH whose `x-lk-resource-version` header doesn't
    match the card's CURRENT server-side version -- mirroring real optimistic concurrency. Every
    accepted PATCH bumps the version, exactly like a real write would, so a second PATCH reusing
    the first write's pre-bump version is caught the same way production would catch it."""

    def __init__(self, card: dict):
        self.card = dict(card)
        self.patch_versions_sent: list[str | None] = []

    def urlopen(self, req, timeout=None):
        if req.get_method() == "GET":
            return _Response(dict(self.card))
        headers = {k.lower(): v for k, v in req.header_items()}
        sent_version = headers.get("x-lk-resource-version")
        self.patch_versions_sent.append(sent_version)
        if sent_version != str(self.card["version"]):
            raise _http_error(req.full_url, 428)
        ops = json.loads(req.data)
        for op in ops:
            if op["path"] == "/customId":
                self.card["customId"] = op["value"]
            elif op["path"] == "/externalLink":
                self.card["externalLink"] = op["value"]
        self.card["version"] = str(int(self.card["version"]) + 1)
        return _Response(dict(self.card))


def test_writeback_second_write_never_reuses_the_first_writes_stale_version(monkeypatch):
    """The exact failure scenario from the review finding: a typical list_cards()-shaped candidate
    (usable version, no externalLinks array) goes through a real customId-then-link writeback. Both
    PATCHes must succeed with the CORRECT, up-to-date version header each -- never a 409/428, and
    never a retry (the fix avoids the conflict outright rather than merely recovering from it)."""
    card = {"id": "C1", "version": "1", "laneId": "X", "title": "Foo"}
    issue = {"number": 42, "url": "https://github.com/o/r/issues/42"}
    server = _VersionedCardServer(card)
    monkeypatch.setattr(urllib.request, "urlopen", server.urlopen)

    intake._writeback(CFG, True, card, issue)

    assert server.patch_versions_sent == ["1", "2"]
    assert server.card["customId"] == "42"
    assert server.card["externalLink"] == {"label": "GitHub #42", "url": issue["url"]}


def test_writeback_never_mutates_the_original_card_object(monkeypatch):
    """Immutability: whatever mechanism forces the second write's version refresh must build a new
    object, never mutate the `card` dict the caller (intake.promote's candidate loop) still holds."""
    card = {"id": "C1", "version": "1", "laneId": "X", "title": "Foo"}
    card_before = dict(card)
    issue = {"number": 42, "url": "https://github.com/o/r/issues/42"}
    server = _VersionedCardServer(card)
    monkeypatch.setattr(urllib.request, "urlopen", server.urlopen)

    intake._writeback(CFG, True, card, issue)

    assert card == card_before
