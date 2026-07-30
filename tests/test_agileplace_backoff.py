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


def test_waits_between_rate_limited_attempts_grow_exponentially():
    slept = []

    with patch("agilesync.board.agileplace.urllib.request.urlopen", _rate_limited(times=99)), \
         patch("agilesync.board.agileplace.time.sleep", slept.append), \
         pytest.raises(SystemExit):
        agileplace.api(CFG, "GET", "card/1")

    assert slept == [1.0, 2.0, 4.0, 8.0]  # one wait per retry, doubling each time


def test_backoff_wait_is_capped_so_a_run_cannot_stall_for_hours():
    slept = []

    with patch("agilesync.board.agileplace.urllib.request.urlopen", _rate_limited(times=99)), \
         patch("agilesync.board.agileplace.time.sleep", slept.append), \
         patch.object(agileplace, "MAX_RETRY_SLEEP", 3), \
         pytest.raises(SystemExit):
        agileplace.api(CFG, "GET", "card/1")

    assert max(slept) <= 3


def test_server_sent_retry_after_overrides_the_computed_backoff():
    slept = []

    def fake_urlopen(req, timeout=None):
        raise _http_error(429, retry_after="30")

    with patch("agilesync.board.agileplace.urllib.request.urlopen", fake_urlopen), \
         patch("agilesync.board.agileplace.time.sleep", slept.append), \
         pytest.raises(SystemExit):
        agileplace.api(CFG, "GET", "card/1")

    assert slept == [30.0, 30.0, 30.0, 30.0]  # the server's instruction wins over 1/2/4/8


def test_every_rate_limited_retry_is_reported_on_stderr(capsys):
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
