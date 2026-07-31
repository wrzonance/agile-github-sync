"""Rate-limit backoff and retry reporting for the AgilePlace client.

429 is the one status this client retries -- every other failure, 5xx included, surfaces
immediately, because the write gate sends non-idempotent POSTs a blind retry could duplicate.
Waits grow exponentially under MAX_RETRY_SLEEP, a server-sent Retry-After overrides them, and every
retry announces itself on stderr so the console shows why a run stalled.

Run: pytest -q
"""
from __future__ import annotations

import email.message
import io
import json
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agilesync.board import agileplace  # noqa: E402

CFG = {"token": "t", "host": "tenant.test", "board_id": "42"}


def _http_error(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://tenant.test/io/card", code, "Too Many Requests",
                                  headers, io.BytesIO(b'{"error":"rate limited"}'))


@pytest.fixture
def no_jitter():
    """Pins the randomized spread to zero so a schedule can be asserted exactly."""
    with patch("agilesync.board.agileplace.random.uniform", return_value=0.0):
        yield


def _rate_limited(times: int, then=None):
    """urlopen stub: `times` consecutive 429s, then `then` (a payload) or another 429."""
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] <= times:
            raise _http_error(429)
        if then is None:
            raise _http_error(429)
        return io.BytesIO(json.dumps(then).encode())

    fake_urlopen.calls = calls
    return fake_urlopen


def test_every_request_takes_a_slot_from_the_shared_limiter(monkeypatch):
    """Pacing has to sit at the one place a request is issued, or the read pool's threads bypass
    it entirely."""
    taken = []
    monkeypatch.setattr(agileplace, "_LIMITER",
                        SimpleNamespace(acquire=lambda: taken.append("slot"), penalize=lambda: 0))

    with patch("agilesync.board.agileplace.urllib.request.urlopen",
               lambda req, timeout=None: io.BytesIO(b"{}")):
        agileplace.api(CFG, "GET", "card/1")
        agileplace.api(CFG, "GET", "card/2")

    assert taken == ["slot", "slot"]


def test_a_rate_limited_response_slows_the_limiter_down(monkeypatch):
    penalties = []
    monkeypatch.setattr(agileplace, "_LIMITER",
                        SimpleNamespace(acquire=lambda: None, total_requests=0,
                                        recent_requests=lambda: 0,
                                        penalize=lambda: penalties.append("halved")))

    with patch("agilesync.board.agileplace.urllib.request.urlopen", _rate_limited(times=2,
                                                                                  then={})), \
         patch("agilesync.board.agileplace.time.sleep"):
        agileplace.api(CFG, "GET", "card/1")

    assert penalties == ["halved", "halved"]  # once per push-back, not once per run


def test_the_retry_wait_is_jittered_so_the_pool_does_not_wake_in_lockstep():
    """Eight workers rate-limited by the same response would otherwise sleep the identical
    Retry-After and collide again on the next window."""
    waits = set()

    for _ in range(20):
        with patch("agilesync.board.agileplace.urllib.request.urlopen",
                   lambda req, timeout=None: (_ for _ in ()).throw(_http_error(429, "10"))), \
             patch("agilesync.board.agileplace.time.sleep") as sleep, \
             pytest.raises(SystemExit):
            agileplace.api(CFG, "GET", "card/1")
        waits.update(call.args[0] for call in sleep.call_args_list)

    assert len(waits) > 1, "every retry waited exactly the same time"
    assert all(10.0 <= wait <= 10.0 * (1 + agileplace.JITTER_FRACTION) for wait in waits), waits


def test_a_retry_after_near_the_cap_still_gets_a_real_spread():
    """The board's own Retry-After is ~57s, a hair under MAX_RETRY_SLEEP. Clamping the JITTERED
    value to the cap would collapse most draws back onto an identical 60.0s -- reintroducing the
    lockstep wake-up that jitter exists to break, in the one case that actually happens."""
    waits = set()

    for _ in range(20):
        with patch("agilesync.board.agileplace.urllib.request.urlopen",
                   lambda req, timeout=None: (_ for _ in ()).throw(_http_error(429, "57"))), \
             patch("agilesync.board.agileplace.time.sleep") as sleep, \
             pytest.raises(SystemExit):
            agileplace.api(CFG, "GET", "card/1")
        waits.update(call.args[0] for call in sleep.call_args_list)

    at_the_cap = [w for w in waits if w == agileplace.MAX_RETRY_SLEEP]
    assert not at_the_cap, f"jitter collapsed onto the cap: {sorted(waits)}"
    assert len(waits) > 1
    assert all(57.0 <= w <= 57.0 * (1 + agileplace.JITTER_FRACTION) for w in waits), sorted(waits)


def test_rate_limited_request_is_attempted_at_most_max_attempts_times():
    urlopen = _rate_limited(times=99)

    with patch("agilesync.board.agileplace.urllib.request.urlopen", urlopen), \
         patch("agilesync.board.agileplace.time.sleep"), \
         pytest.raises(SystemExit) as raised:
        agileplace.api(CFG, "POST", "card", body={"title": "x"})

    assert urlopen.calls["n"] == agileplace.MAX_ATTEMPTS
    assert "HTTP 429" in str(raised.value)


def test_rate_limited_request_succeeds_once_the_limit_clears():
    urlopen = _rate_limited(times=2, then={"id": "7"})

    with patch("agilesync.board.agileplace.urllib.request.urlopen", urlopen), \
         patch("agilesync.board.agileplace.time.sleep"):
        result = agileplace.api(CFG, "POST", "card", body={"title": "x"})

    assert result == {"id": "7"}
    assert urlopen.calls["n"] == 3  # stops retrying the moment it succeeds


def test_waits_between_rate_limited_attempts_grow_exponentially(no_jitter):
    slept = []

    with patch("agilesync.board.agileplace.urllib.request.urlopen", _rate_limited(times=99)), \
         patch("agilesync.board.agileplace.time.sleep", slept.append), \
         pytest.raises(SystemExit):
        agileplace.api(CFG, "GET", "card/1")

    assert slept == [1.0, 2.0, 4.0, 8.0]  # one wait per retry, doubling each time


def test_backoff_wait_is_capped_so_a_run_cannot_stall_for_hours():
    """MAX_RETRY_SLEEP caps the BASE wait; jitter then rides on top, so the real bound is
    cap x (1 + JITTER_FRACTION). Capping the jittered sum instead would collapse near-cap waits
    back onto an identical value -- the lockstep this file's near-cap test pins against."""
    slept = []

    with patch("agilesync.board.agileplace.urllib.request.urlopen", _rate_limited(times=99)), \
         patch("agilesync.board.agileplace.time.sleep", slept.append), \
         patch.object(agileplace, "MAX_RETRY_SLEEP", 3), \
         pytest.raises(SystemExit):
        agileplace.api(CFG, "GET", "card/1")

    assert max(slept) <= 3 * (1 + agileplace.JITTER_FRACTION)


def test_server_sent_retry_after_overrides_the_computed_backoff(no_jitter):
    slept = []

    def fake_urlopen(req, timeout=None):
        raise _http_error(429, retry_after="30")

    with patch("agilesync.board.agileplace.urllib.request.urlopen", fake_urlopen), \
         patch("agilesync.board.agileplace.time.sleep", slept.append), \
         pytest.raises(SystemExit):
        agileplace.api(CFG, "GET", "card/1")

    assert slept == [30.0, 30.0, 30.0, 30.0]  # the server's instruction wins over 1/2/4/8


def test_every_rate_limited_retry_is_reported_on_stderr(no_jitter, capsys):
    with patch("agilesync.board.agileplace.urllib.request.urlopen", _rate_limited(times=1,
                                                                                  then={})), \
         patch("agilesync.board.agileplace.time.sleep"):
        agileplace.api(CFG, "POST", "card/9/comment", body={"text": "x"})

    err = capsys.readouterr().err
    assert "WARN" in err
    assert "429" in err
    assert "POST /io/card/9/comment" in err  # which request stalled
    assert "attempt 1" in err and str(agileplace.MAX_ATTEMPTS) in err  # how far into the budget
    assert "1.0s" in err  # how long it waited
    assert "in 60s" in err  # and how many requests bought the 429 -- the quota is undocumented


def test_a_non_rate_limit_http_error_is_never_retried():
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        raise _http_error(503)

    with patch("agilesync.board.agileplace.urllib.request.urlopen", fake_urlopen), \
         patch("agilesync.board.agileplace.time.sleep",
               side_effect=AssertionError("slept on a non-429")), \
         pytest.raises(SystemExit) as raised:
        agileplace.api(CFG, "POST", "card", body={"title": "x"})

    assert calls["n"] == 1  # a POST is not idempotent -- never blind-retried
    assert raised.value.http_status == 503
