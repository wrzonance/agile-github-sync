"""Client-level contracts smoke mode relies on: full HTTP error detail and card deletion."""
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

import agileplace  # noqa: E402

CFG = {"token": "t", "host": "tenant.test", "board_id": "42"}


def _http_error(code: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://tenant.test/io/card", code, "Unprocessable",
        email.message.Message(), io.BytesIO(body))


def test_api_system_exit_carries_full_http_error_body_and_status():
    body = json.dumps({"error": "detail " * 100}).encode()  # far beyond the 300-char message cap
    assert len(body) > 300

    with patch("agileplace.urllib.request.urlopen", side_effect=_http_error(422, body)), \
         pytest.raises(SystemExit) as raised:
        agileplace.api(CFG, "POST", "card", body={"title": "x"})

    exc = raised.value
    assert "HTTP 422" in str(exc)
    assert len(str(exc)) < len(body)  # printed message stays truncated
    assert exc.http_status == 422
    assert exc.http_body == body.decode()  # full server body preserved for verbose reporting


def test_api_system_exit_on_unreachable_has_no_http_attributes():
    err = urllib.error.URLError("connection refused")

    with patch("agileplace.urllib.request.urlopen", side_effect=err), \
         pytest.raises(SystemExit) as raised:
        agileplace.api(CFG, "GET", "board/42")

    assert not hasattr(raised.value, "http_status")


def test_delete_card_sends_delete_for_quoted_card_path():
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["method"] = req.get_method()
        captured["url"] = req.full_url
        return io.BytesIO(b"")

    with patch("agileplace.urllib.request.urlopen", fake_urlopen):
        agileplace.delete_card(CFG, True, "A/B")

    assert captured["method"] == "DELETE"
    assert captured["url"].endswith("/io/card/A%2FB")


def test_delete_card_dry_run_makes_no_network_call(capsys):
    with patch("agileplace.urllib.request.urlopen",
               side_effect=AssertionError("network call in dry run")):
        agileplace.delete_card(CFG, False, "123")

    assert "DELETE /io/card/123" in capsys.readouterr().out
