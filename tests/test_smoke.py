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
                 ignore_external_link: bool = False, ignore_tag_add: bool = False,
                 duplicate_status: int = 409, ignore_comment_delete: bool = False):
        self.accept_stale = accept_stale
        self.fail_child_create_body = fail_child_create_body
        self.ignore_external_link = ignore_external_link
        self.ignore_tag_add = ignore_tag_add
        self.duplicate_status = duplicate_status  # live contract is 409; override to model outages
        self.ignore_comment_delete = ignore_comment_delete  # models the speculative DELETE shape missing
        self.created_custom_ids: list[str] = []
        self.writes: list[tuple[str, str]] = []
        self.cards: dict[str, dict] = {
            "P1": {"id": "P1", "title": "Existing card", "customId": "EX-1",
                   "laneId": "L1", "tags": [], "version": 3, "description": ""},
        }
        self.children: dict[str, list[str]] = {}
        self.dependencies: dict[str, list[dict]] = {}
        self.comments: dict[str, list[dict]] = {}
        self._next_id = 0
        self._next_comment_id = 0

    def urlopen(self, req, timeout=None):
        method = req.get_method()
        parsed = urllib.parse.urlparse(req.full_url)
        path = parsed.path.removeprefix("/io/")
        body = json.loads(req.data) if req.data else None
        if method != "GET":
            self.writes.append((method, path))
        is_comment_collection = path.startswith("card/") and path.endswith("/comment")
        is_comment_item = path.startswith("card/") and "/comment/" in path
        if method == "GET" and is_comment_collection:
            return self._get_comments(path)
        if method == "GET":
            return self._get(req.full_url, path)
        if method == "POST" and is_comment_collection:
            return self._create_comment(path, body)
        if method == "POST" and path == "card":
            return self._create(req.full_url, body)
        if method == "PUT" and is_comment_item:
            return self._update_comment(path, body)
        if method == "PATCH" and path.startswith("card/"):
            return self._patch(req, path.removeprefix("card/"), body)
        if path == "card/connections":
            return self._connections(method, body)
        if path == "card/dependency":
            return self._dependency(req, method, body)
        if method == "DELETE" and is_comment_item:
            return self._delete_comment(path)
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
        if path.endswith("/dependency"):
            return _Response({"dependencies": self.dependencies.get(path.split("/")[1], [])})
        card_id = path.removeprefix("card/")
        if card_id not in self.cards:
            raise _http_error(url, 404, json.dumps({"message": "card not found"}))
        return _Response({"card": {**self.cards[card_id],
                                   "version": str(self.cards[card_id]["version"])}})

    def _get_comments(self, path: str):
        card_id = path.split("/")[1]
        return _Response({"comments": self.comments.get(card_id, [])})

    def _create_comment(self, path: str, body: dict):
        card_id = path.split("/")[1]
        self._next_comment_id += 1
        comment = {
            "id": self._next_comment_id, "text": body["text"],
            "createdBy": {"fullName": "Smoke Bot", "emailAddress": "smoke-bot@example.invalid",
                          "id": "U1"},
            "createdOn": "2026-07-23T00:00:00Z",
        }
        self.comments.setdefault(card_id, []).append(comment)
        return _Response(comment)

    def _update_comment(self, path: str, body: dict):
        card_id, comment_id = path.split("/")[1], int(path.split("/")[3])
        for comment in self.comments.get(card_id, []):
            if comment["id"] == comment_id:
                comment["text"] = body["text"]
                comment["lastModified"] = "2026-07-23T00:05:00Z"
        return _Response({})

    def _delete_comment(self, path: str):
        if self.ignore_comment_delete:
            return _Response(None)
        card_id, comment_id = path.split("/")[1], int(path.split("/")[3])
        self.comments[card_id] = [c for c in self.comments.get(card_id, []) if c["id"] != comment_id]
        return _Response(None)

    def _create(self, url: str, body: dict):
        if self.fail_child_create_body and body.get("customId", "").startswith("SMOKE-C"):
            raise _http_error(url, 422, self.fail_child_create_body)
        self._next_id += 1
        card_id = f"S{self._next_id}"
        self.created_custom_ids.append(body["customId"])
        card = {"id": card_id, "title": body["title"], "customId": body["customId"],
                "laneId": body.get("laneId"), "tags": [], "version": 1,
                "plannedStart": None, "plannedFinish": None, "description": "",
                "blockedStatus": {"isBlocked": False, "reason": ""}}
        if "externalLink" in body:
            card["externalLink"] = body["externalLink"]
        self.cards[card_id] = card
        # Live create responses are SPARSE: the new card id only -- no version and no
        # customId/laneId echo (validated live 2026-07-21, issue #55). The tenant still
        # persists the full card, which the single-card GET serves back.
        return _Response({"id": card_id})

    def _patch(self, req, card_id: str, ops: list):
        card = self.cards[card_id]
        headers = {key.lower(): value for key, value in req.header_items()}
        sent = headers.get("x-lk-resource-version")
        if not self.accept_stale and sent != str(card["version"]):
            raise _http_error(req.full_url, 409, json.dumps(
                {"message": f"version conflict: sent {sent}, current {card['version']}"}))
        # Mirror the live server's atomic validation (observed 2026-07-21, issue #52): a replace on
        # a planned-date path must carry a string value; the whole batch is rejected before any op
        # is applied.
        invalid = [{**op, "error": "Invalid value: must be string"}
                   for op in ops
                   if op["path"] in ("/plannedStart", "/plannedFinish")
                   and op["op"] == "replace" and not isinstance(op.get("value"), str)]
        if invalid:
            raise _http_error(req.full_url, 422, json.dumps(
                {"statusCode": 422, "error": "Unprocessable Entity",
                 "message": "Invalid patch operations", "data": {"operations": invalid}}))
        for op in ops:
            if op["path"] == "/tags/-":
                if not self.ignore_tag_add:
                    card["tags"].append(op["value"])
            elif op["path"].startswith("/tags/"):
                card["tags"].pop(int(op["path"].removeprefix("/tags/")))
            elif op["path"] == "/externalLink":
                if not self.ignore_external_link:
                    card["externalLink"] = op["value"]
            elif op["path"] == "/description":
                card["description"] = op["value"]
            elif op["path"] == "/isBlocked":
                card["blockedStatus"]["isBlocked"] = op["value"]
            elif op["path"] == "/blockReason":
                card["blockedStatus"]["reason"] = op["value"]
            elif op["path"] in ("/plannedStart", "/plannedFinish"):
                field = op["path"].removeprefix("/")
                card[field] = None if op["op"] == "remove" else op["value"]
        card["version"] += 1
        return _Response({"id": card_id, "version": str(card["version"])})

    def _dependency(self, req, method: str, body: dict):
        # Mirrors the live contract (confirmed 2026-07-21): duplicate create is HTTP 409
        # "Dependency already exists", deletion is pair-addressed and ignores timing.
        for dependent in body["cardIds"]:
            for blocker in body["dependsOnCardIds"]:
                if method == "POST":
                    existing = self.dependencies.get(dependent, [])
                    if any(e["direction"] == "incoming" and e["cardId"] == blocker
                           for e in existing):
                        if self.duplicate_status != 409:
                            raise _http_error(req.full_url, self.duplicate_status,
                                              json.dumps({"message": "boom"}))
                        raise _http_error(req.full_url, 409, json.dumps(
                            {"statusCode": 409, "error": "Conflict",
                             "message": "Dependency already exists",
                             "data": {"dependsOnCardId": blocker, "cardId": dependent}}))
                pairs = (("incoming", dependent, blocker), ("outgoing", blocker, dependent))
                for direction, holder, other in pairs:
                    entries = self.dependencies.setdefault(holder, [])
                    if method == "POST":
                        entries.append({"direction": direction, "cardId": other,
                                        "timing": body.get("timing")})
                    else:
                        self.dependencies[holder] = [
                            e for e in entries
                            if not (e["direction"] == direction and e["cardId"] == other)]
        return _Response({})

    def _connections(self, method: str, body: dict):
        parent = body["cardIds"][0]
        kids = body["connections"]["children"]
        if method == "POST":
            self.children.setdefault(parent, []).extend(kids)
        else:
            self.children[parent] = [c for c in self.children.get(parent, [])
                                     if c not in kids]
        return _Response({})


# Real-world markdown (heading, bold/italic, link, list, inline code) for the issue #1 richtext
# round-trip step. Chosen because it is render-stable through the richtext layer (issue #78), which
# the step's PASS/FAIL invariant requires. The gh read is faked (no real GitHub call in the suite).
_SMOKE_ISSUE_BODY = (
    "# Title\n\nA paragraph with **bold** and _italic_ and a [link](https://example.com).\n\n"
    "- one\n- two\n\n`inline code` here."
)


@pytest.fixture
def tenant_env(monkeypatch):
    def install(world: FakeTenant, answer: str | None = "smoke",
                issue_bodies: object = "__default__"):
        monkeypatch.setattr("config.ENV_FILE", Path("/nonexistent/.env"))
        for name, value in (("AGILEPLACE_TOKEN", "test-token"),
                            ("AGILEPLACE_HOST", "tenant.test"),
                            ("AGILEPLACE_BOARD_ID", "42")):
            monkeypatch.setenv(name, value)
        monkeypatch.setattr("urllib.request.urlopen", world.urlopen)
        # The issue #1 richtext step reads GitHub via ghkit; fake it so no real gh call happens.
        # Default: a render-stable issue #1 body. Tests pass issue_bodies=[] / None to drive SKIPs.
        bodies = ([{"number": 1, "url": "https://g.test/acme/widgets/issues/1",
                    "state": "OPEN", "body": _SMOKE_ISSUE_BODY}]
                  if issue_bodies == "__default__" else issue_bodies)
        monkeypatch.setattr(smoke.ghkit, "list_issue_bodies", lambda cfg: bodies)
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
        ("PATCH", "card/S1"),             # blocked-state + planned dates set
        ("PATCH", "card/S1"),             # blocked-state + planned dates clear
        ("POST", "card"),                 # child, no external link
        ("PATCH", "card/S2"),             # externalLink add on bare card
        ("POST", "card/connections"),     # connect child
        ("DELETE", "card/connections"),   # disconnect child
        ("POST", "card/dependency"),      # dependency create (child dependsOn parent)
        ("POST", "card/dependency"),      # duplicate-create fact-finding probe
        ("DELETE", "card/dependency"),    # dependency delete
        ("PATCH", "card/S1"),             # description write round-trip
        ("PATCH", "card/S1"),             # description length probe
        ("POST", "card/S1/comment"),      # comment create
        ("PUT", "card/S1/comment/1"),     # comment edit
        ("PUT", "card/S1/comment/1"),     # edit-timestamp fact-finding probe (issue #66)
        ("DELETE", "card/S1/comment/1"),  # comment delete (speculative shape)
        ("PATCH", "card/S1"),             # deliberate stale-version probe
        ("PATCH", "card/S1"),             # issue #1 richtext round-trip: write rendered HTML (#78)
        ("PATCH", "card/S1"),             # issue #1 richtext round-trip: write re-derived HTML (convergence)
        ("DELETE", "card/S2"),            # cleanup child
        ("DELETE", "card/S1"),            # cleanup parent
    ]
    assert set(world.cards) == {"P1"}  # only the pre-existing card survives
    assert world.comments.get("S1", []) == []  # the smoke comment was actually deleted
    out = capsys.readouterr().out
    assert "FAIL" not in out
    assert "wrapped" in out            # single-card GET shape reported
    assert "HTTP 409" in out           # stale probe rejection surfaced verbatim
    assert "404" in out                # post-delete GET confirms the cards are gone
    assert "blocked" in out            # blocked-state round-trip reported
    assert "planned" in out            # planned-date round-trip reported
    assert "duplicate create rejected" in out   # the live 409 contract, mirrored by the double
    assert "Dependency already exists" in out
    assert "description write round-trip" in out
    assert "description length probe" in out
    assert "comment create returns a usable id" in out
    assert "comment list readback finds the created comment" in out
    assert "comment edit round-trip" in out
    assert "comment delete + readback gone" in out
    assert "issue #1 body (" in out   # step 22 printed the repo + body length
    assert "converges under the sync's richtext layer" in out
    assert "comment edit HTML vs stored" in out   # comment-body normalization fact-finding


def test_github_richtext_roundtrip_skips_informationally_when_issue_1_is_absent(tenant_env, capsys):
    """The issue #1 richtext step must be an informational SKIP (never a failure) when issue #1
    doesn't exist -- and the whole run still passes and cleans up."""
    world = tenant_env(FakeTenant(), issue_bodies=[])

    assert smoke.main([]) == 0

    out = capsys.readouterr().out
    assert "SKIP" in out and "issue #1 was not found" in out
    assert "FAIL" not in out
    assert set(world.cards) == {"P1"}  # cleanup still ran


def test_unexpected_duplicate_create_failure_fails_the_run(tenant_env, capsys):
    """PR #61 review finding: only the confirmed HTTP 409 passes the duplicate probe. An auth/5xx/
    transport failure during the duplicate POST must FAIL the smoke run, never hide as an
    informational line that leaves the summary green."""
    tenant = FakeTenant(duplicate_status=503)
    tenant_env(tenant)
    assert smoke.main([]) == 1
    out = capsys.readouterr().out
    assert "FAIL  duplicate dependency create rejected (HTTP 409)" in out
    assert "unexpected failure (not the confirmed 409)" in out


def test_custom_ids_are_unique_per_run(tenant_env, monkeypatch):
    """A leftover throwaway card must never be adoptable by the sync's customId fallback, and two
    smoke runs must never collide -- so custom ids carry a fresh per-run suffix."""
    first = FakeTenant()
    tenant_env(first)
    assert smoke.main([]) == 0
    second = FakeTenant()
    monkeypatch.setattr("urllib.request.urlopen", second.urlopen)
    assert smoke.main([]) == 0

    for world in (first, second):
        parent, child = world.created_custom_ids
        assert parent.startswith("SMOKE-P-") and len(parent) > len("SMOKE-P-")
        assert child.startswith("SMOKE-C-") and len(child) > len("SMOKE-C-")
    assert first.created_custom_ids[0] != second.created_custom_ids[0]


def test_ignored_external_link_write_is_reported_as_failure(tenant_env, capsys):
    """A 2xx PATCH is not proof: the link must be read back, so a server that silently ignores
    /externalLink turns the check into a FAIL."""
    tenant_env(FakeTenant(ignore_external_link=True))

    assert smoke.main([]) == 1

    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "externalLink" in out


def test_tag_add_never_visible_still_summarizes_and_cleans_up(tenant_env, capsys):
    """When the added tag never appears on readback, the remove step must be skipped as a FAIL --
    not crash with an ops_tag_remove ValueError before the summary and cleanup."""
    world = tenant_env(FakeTenant(ignore_tag_add=True))

    assert smoke.main([]) == 1

    out = capsys.readouterr().out
    assert "smoke summary" in out
    assert "FAIL" in out
    assert "Traceback" not in out
    assert set(world.cards) == {"P1"}  # cleanup still ran


def test_comment_not_actually_deleted_is_reported_as_failure(tenant_env, capsys):
    """agileplace_comments.delete_comment's DELETE shape is speculative (issue #66 design doc --
    the web UI never exposed comment deletion). A 2xx response is not proof it worked, so a server
    that silently ignores the delete must turn the readback check into a FAIL, exactly like the
    externalLink-add and tag-add checks above -- never a false PASS that would hide a wrong shape."""
    tenant_env(FakeTenant(ignore_comment_delete=True))

    assert smoke.main([]) == 1

    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "comment delete + readback gone" in out


def test_versionless_create_response_is_informational_not_a_failure(tenant_env, capsys):
    """Live tenants return no version on create (confirmed 2026-07-20); the sync's
    refetch-before-PATCH path handles that, so smoke must report the fact without failing.
    The default double is sparse for the same reason -- this pins what it reports."""
    tenant_env(FakeTenant())

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
