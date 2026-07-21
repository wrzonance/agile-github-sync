"""Offline tests for probe_dependencies.py (issue #57 Phase 0).

The invariant that matters most: the probe is READ-ONLY -- every request it ever
issues is a GET. A discovery tool that writes to a production tenant is the bug
class this file exists to make impossible. The rest pins the reporting contract:
one line per candidate, body excerpts for non-404s, and the devtools fallback
instruction when nothing is found.

Run: pytest -q
"""
import email.message
import io
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import probe_dependencies  # noqa: E402


class _Response:
    def __init__(self, payload: object, status: int = 200):
        self.status = status
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
    """Read-side io v2 double: serves the card list, the card, and the control
    endpoint; candidate endpoints 404 unless listed in ``found``."""

    def __init__(self, *, control_status: int = 200, found: dict | None = None,
                 card: dict | None = None, troubles: dict | None = None):
        self.methods: list[str] = []
        self.paths: list[str] = []
        self.card = card or {"id": "C1", "title": "Card", "customId": "X1", "version": 2}
        self.found = found or {}
        self.troubles = troubles or {}  # path -> HTTP status int, or an exception to raise
        self.control_status = control_status

    def urlopen(self, req, timeout=None):
        self.methods.append(req.get_method())
        path = urllib.parse.urlparse(req.full_url).path.removeprefix("/io/")
        self.paths.append(path)
        cid = self.card["id"]
        if path == "card":
            return _Response({"pageMeta": {"totalRecords": 1, "offset": 0, "limit": 200},
                              "cards": [self.card]})
        if path == f"card/{cid}":
            return _Response(self.card)
        if path == f"card/{cid}/connection/children":
            if self.control_status != 200:
                raise _http_error(req.full_url, self.control_status, '{"message": "boom"}')
            return _Response({"pageMeta": {"totalRecords": 0, "offset": 0, "limit": 1},
                              "cards": []})
        if path in self.troubles:
            trouble = self.troubles[path]
            if isinstance(trouble, Exception):
                raise trouble
            if trouble == 204:
                return _Response(None, status=204)
            raise _http_error(req.full_url, trouble, '{"message": "trouble"}')
        if path in self.found:
            return _Response(self.found[path])
        raise _http_error(req.full_url, 404, '{"message": "not found"}')


@pytest.fixture
def tenant_env(monkeypatch):
    def _install(tenant: FakeTenant):
        monkeypatch.setenv("AGILEPLACE_TOKEN", "t")
        monkeypatch.setenv("AGILEPLACE_HOST", "tenant.test")
        monkeypatch.setenv("AGILEPLACE_BOARD_ID", "42")
        monkeypatch.setattr(urllib.request, "urlopen", tenant.urlopen)
    return _install


def _run(monkeypatch, capsys, argv=("--card-id", "C1")) -> str:
    monkeypatch.setattr(sys, "argv", ["probe_dependencies.py", *argv])
    probe_dependencies.main()
    return capsys.readouterr().out


def test_probe_never_issues_non_get_requests(tenant_env, monkeypatch, capsys):
    tenant = FakeTenant(found={"card/C1/dependencies": {"dependencies": []}})
    tenant_env(tenant)
    _run(monkeypatch, capsys)
    assert tenant.methods and set(tenant.methods) == {"GET"}


def test_all_candidates_missing_prints_miss_lines_and_devtools_fallback(
        tenant_env, monkeypatch, capsys):
    tenant = FakeTenant()
    tenant_env(tenant)
    out = _run(monkeypatch, capsys)
    for path, params in probe_dependencies.candidate_probes("C1", "42"):
        shown = path + ("?" + urllib.parse.urlencode(params) if params else "")
        assert f"MISS  {shown} -> HTTP 404" in out
    assert "no readable dependency endpoint" in out
    assert "devtools" in out


def test_found_candidate_is_reported_with_body_excerpt(tenant_env, monkeypatch, capsys):
    tenant = FakeTenant(found={"card/C1/dependencies": {"dependencies": [{"id": "D1"}]}})
    tenant_env(tenant)
    out = _run(monkeypatch, capsys)
    assert "FOUND card/C1/dependencies -> HTTP 200" in out
    assert '"D1"' in out
    assert "readable dependency endpoint(s) found" in out


def test_card_key_dump_flags_dependency_ish_keys(tenant_env, monkeypatch, capsys):
    card = {"id": "C1", "title": "Card", "version": 2, "dependencyCounts": {"incoming": 0}}
    tenant = FakeTenant(card=card)
    tenant_env(tenant)
    out = _run(monkeypatch, capsys)
    assert "dependencyCounts" in out
    assert "dependency-ish keys: dependencyCounts" in out


def test_no_dependency_ish_keys_is_stated_explicitly(tenant_env, monkeypatch, capsys):
    tenant = FakeTenant()
    tenant_env(tenant)
    out = _run(monkeypatch, capsys)
    assert "dependency-ish keys: none" in out


def test_control_failure_aborts_before_probing_candidates(tenant_env, monkeypatch, capsys):
    tenant = FakeTenant(control_status=500)
    tenant_env(tenant)
    monkeypatch.setattr(sys, "argv", ["probe_dependencies.py", "--card-id", "C1"])
    with pytest.raises(SystemExit, match="control"):
        probe_dependencies.main()
    candidate_paths = {p for p, _ in probe_dependencies.candidate_probes("C1", "42")}
    assert not candidate_paths & set(tenant.paths)


def test_auto_picks_first_board_card_when_no_card_id(tenant_env, monkeypatch, capsys):
    tenant = FakeTenant()
    tenant_env(tenant)
    out = _run(monkeypatch, capsys, argv=())
    assert "card C1" in out
    assert "card" in tenant.paths  # the list read happened


def test_missing_env_refuses_to_run(monkeypatch, tmp_path):
    # env_config() re-reads .env on every call ("real environment variables win over
    # it"), so a configured checkout would repopulate the deleted vars and pass
    # _require_env(). Point ENV_FILE somewhere empty so this test is deterministic
    # everywhere -- including the machine the probe is actually run from.
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / "no-such.env")
    for var in ("AGILEPLACE_TOKEN", "AGILEPLACE_HOST", "AGILEPLACE_BOARD_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(sys, "argv", ["probe_dependencies.py"])
    with pytest.raises(SystemExit, match="AGILEPLACE_TOKEN"):
        probe_dependencies.main()


def test_204_counts_as_found_route_not_a_miss(tenant_env, monkeypatch, capsys):
    tenant = FakeTenant(troubles={"card/C1/dependencies": 204})
    tenant_env(tenant)
    out = _run(monkeypatch, capsys)
    assert "FOUND card/C1/dependencies -> HTTP 204" in out
    assert "(empty body)" in out
    assert "readable dependency endpoint(s) found" in out


def test_transport_failure_is_inconclusive_not_a_miss_and_no_traceback(
        tenant_env, monkeypatch, capsys):
    tenant = FakeTenant(troubles={
        "card/C1/dependencies": urllib.error.URLError("dns exploded"),
        "card/C1/dependency": 503,
    })
    tenant_env(tenant)
    out = _run(monkeypatch, capsys)  # completing at all proves no traceback escaped
    assert "?     card/C1/dependencies -> no HTTP response" in out
    assert "transport failure" in out
    assert "?     card/C1/dependency -> HTTP 503" in out
    assert "INCONCLUSIVE" in out
    assert "re-run the probe" in out
    assert "devtools" not in out  # the definitive all-404 fallback must NOT fire
