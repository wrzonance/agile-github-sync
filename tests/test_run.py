"""Whole-run dry/apply parity tests at the real process and HTTP boundaries."""
from __future__ import annotations

import io
import json
import re
import shlex
import subprocess
import sys
import urllib.parse
from collections import Counter
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import sync  # noqa: E402


EPIC_URL = "https://g.test/acme/widgets/issues/1"
TASK_URL = "https://g.test/acme/widgets/issues/2"
PLAN_ID_PREFIX = sync.agileplace.PLANNED_CARD_ID_PREFIX


@dataclass(frozen=True)
class HttpWrite:
    method: str
    path: str
    body: object
    headers: dict[str, str]


@dataclass(frozen=True)
class RunResult:
    output: str
    http_writes: tuple[HttpWrite, ...]
    process_writes: tuple[tuple[str, ...], ...]
    state_file: Path


class _Response:
    def __init__(self, payload: object):
        self._payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self._payload


class FixtureWorld:
    """One stable starting snapshot with recorded mutations at the two real transports."""

    lanes = (
        {"id": "L1", "title": "Backlog", "cardStatus": "notStarted"},
        {"id": "L2", "title": "Ready", "cardStatus": "notStarted"},
    )
    issues = (
        {
            "number": 1,
            "title": "[EP] Parent",
            "state": "OPEN",
            "stateReason": None,
            "labels": [{"name": "type:epic"}, {"name": "agent:ready"}],
            "milestone": None,
            "assignees": [],
            "url": EPIC_URL,
        },
        {
            "number": 2,
            "title": "[TASK] Build widget",
            "state": "OPEN",
            "stateReason": None,
            "labels": [
                {"name": "feature"},
                {"name": "agent:ready"},
                {"name": "area:sync"},
                {"name": "priority:medium"},
                {"name": "test:boundary"},
                {"name": "platform:github"},
                {"name": "platform:agileplace"},
            ],
            "milestone": None,
            "assignees": [],
            "url": TASK_URL,
        },
    )

    def __init__(self):
        self.http_writes: list[HttpWrite] = []
        self.process_writes: list[tuple[str, ...]] = []
        self.created_card: dict | None = None
        self.epic_card = {
            "id": "C1",
            "version": "v1",
            "title": "Parent",
            "customId": "EP",
            "externalLink": {"url": EPIC_URL},
            "tags": ["type:epic", "from-ap"],
            "laneId": "L2",
            "plannedStart": None,
            "plannedFinish": None,
            "blockedStatus": {"isBlocked": False, "reason": ""},
            "childCards": [],
        }

    def run_process(self, argv, **_kwargs):
        args = tuple(argv[1:])
        if args[:2] in {("issue", "edit"), ("project", "item-edit")}:
            self.process_writes.append(args)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if args[:2] == ("repo", "view"):
            payload = ({"nameWithOwner": "acme/widgets", "url": "https://g.test/acme/widgets"}
                       if "nameWithOwner,url" in args else "acme/widgets\n")
            return self._completed(argv, payload)
        if args[:2] == ("issue", "list"):
            return self._completed(argv, self.issues)
        if args[:2] == ("api", "graphql"):
            query = next(arg for arg in args if arg.startswith("query="))
            if "pullRequests(states:OPEN" in query:
                return self._completed(argv, {
                    "data": {"repository": {"pullRequests": {"nodes": []}}},
                })
            if "subIssues(first:100)" in query:
                return self._completed(argv, {
                    "data": {"repository": {"issue": {"subIssues": {
                        "nodes": [{"number": 2}],
                    }}}},
                })
        if args and args[0] == "api" and "/dependencies/blocked_by" in " ".join(args):
            issue_number = int(next(part for part in args if "/dependencies/blocked_by" in part)
                               .split("/issues/")[1].split("/")[0])
            blocker = {
                "number": 1,
                "repository_url": "https://api.g.test/repos/acme/widgets",
            }
            return self._completed(argv, [[blocker]] if issue_number == 2 else [[]])
        raise AssertionError(f"unexpected gh command: {args!r}")

    def open_url(self, request, **_kwargs):
        method = request.get_method()
        parsed = urllib.parse.urlparse(request.full_url)
        path = parsed.path.removeprefix("/io/")
        body = json.loads(request.data) if request.data else None
        headers = {key.lower(): value for key, value in request.header_items()}
        if method != "GET":
            self.http_writes.append(HttpWrite(method, path, body, headers))
        if method == "GET" and path == "board/42":
            return _Response({"lanes": self.lanes})
        if method == "GET" and path == "card":
            return _Response({
                "cards": [self.epic_card],
                "pageMeta": {"totalRecords": 1, "limit": 200},
            })
        if method == "GET" and path == "card/C1/connection/children":
            return _Response({
                "cards": [],
                "pageMeta": {"offset": 0, "limit": 200, "totalRecords": 0},
            })
        if method == "POST" and path == "card":
            self.created_card = {"id": "C2", **body}
            return _Response(self.created_card)
        if method == "GET" and path == "card/C2":
            assert self.created_card is not None
            return _Response({
                "card": {
                    **self.created_card,
                    "version": "v2",
                    "tags": [],
                    "plannedStart": None,
                    "plannedFinish": None,
                    "blockedStatus": {"isBlocked": False, "reason": ""},
                },
            })
        if method in {"POST", "DELETE"} and path == "card/connections":
            return _Response({})
        if method == "GET" and path.startswith("card/") and path.endswith("/dependency"):
            return _Response({"dependencies": []})
        if method in {"POST", "DELETE"} and path == "card/dependency":
            return _Response({})
        if method == "PATCH" and path == "card/C2":
            return _Response({"id": "C2", "version": "v3"})
        raise AssertionError(f"unexpected AgilePlace request: {method} {path}")

    @staticmethod
    def _completed(argv, payload):
        stdout = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")


def _configure(monkeypatch, tmp_path):
    # env_config() re-reads .env on every call and repopulates deleted variables, so a configured
    # checkout would leak its real GH_PROJECT_* into this offline world (seen live 2026-07-21 as
    # 'unexpected gh command: project item-list <real project>'). Point the loader at nothing.
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / "no-such.env")
    values = {
        "TARGET_REPO_PATH": str(tmp_path),
        "AGILEPLACE_TOKEN": "test-token",
        "AGILEPLACE_HOST": "tenant.test",
        "AGILEPLACE_BOARD_ID": "42",
    }
    for name in (
        *values,
        "GH_PROJECT_OWNER",
        "GH_PROJECT_NUMBER",
        "LABEL_SYNC_IGNORE",
        "STAGE_LANE_MAP",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def _run(monkeypatch, tmp_path, *, apply: bool) -> RunResult:
    world = FixtureWorld()
    state_file = tmp_path / ("apply-state.json" if apply else "dry-state.json")
    monkeypatch.setattr(subprocess, "run", world.run_process)
    monkeypatch.setattr("urllib.request.urlopen", world.open_url)
    monkeypatch.setattr(sync, "STATE_FILE", state_file)
    monkeypatch.setattr(sys, "argv", ["sync.py", *(["--apply"] if apply else [])])
    output = io.StringIO()
    with redirect_stdout(output):
        sync.main()
    return RunResult(
        output.getvalue(),
        tuple(world.http_writes),
        tuple(world.process_writes),
        state_file,
    )


def _card_role(card_id: str) -> str:
    card_id = urllib.parse.unquote(card_id)
    if card_id == "C1":
        return "epic"
    if card_id == "C2" or card_id.startswith(PLAN_ID_PREFIX):
        return "task"
    raise AssertionError(f"unknown card identity: {card_id}")


def _normalize_http(method: str, path: str, body: object):
    if method == "POST" and path == "card":
        return "create", json.dumps(body, sort_keys=True)
    if method == "POST" and path == "card/connections":
        return (
            "connect",
            tuple(_card_role(card_id) for card_id in body["cardIds"]),
            tuple(_card_role(card_id) for card_id in body["connections"]["children"]),
        )
    if method == "PATCH" and path.startswith("card/"):
        return "patch", _card_role(path.removeprefix("card/")), json.dumps(body, sort_keys=True)
    if method in {"POST", "DELETE"} and path == "card/dependency":
        return (
            "depend" if method == "POST" else "undepend",
            tuple(_card_role(card_id) for card_id in body["cardIds"]),
            tuple(_card_role(card_id) for card_id in body["dependsOnCardIds"]),
        )
    raise AssertionError(f"unexpected mutation in action set: {method} {path}")


def _planned_actions(output: str) -> tuple:
    actions = []
    pattern = re.compile(r"^DRY   (?P<method>\w+) /io/(?P<path>\S+).* body=(?P<body>.+)$")
    for line in output.splitlines():
        match = pattern.match(line)
        if match:
            actions.append(_normalize_http(
                match["method"], match["path"], json.loads(match["body"])))
        elif line.startswith("DRY   gh "):
            actions.append(("gh", *shlex.split(line.removeprefix("DRY   gh "))))
    return tuple(actions)


def _executed_actions(run: RunResult) -> tuple:
    http = tuple(_normalize_http(write.method, write.path, write.body)
                 for write in run.http_writes)
    process = tuple(("gh", *write) for write in run.process_writes)
    return http + process


@pytest.fixture
def paired_runs(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    return _run(monkeypatch, tmp_path, apply=False), _run(monkeypatch, tmp_path, apply=True)


def test_new_card_dry_run_plans_every_action_apply_executes(paired_runs):
    dry, apply = paired_runs

    assert dry.http_writes == ()
    assert dry.process_writes == ()
    assert not dry.state_file.exists()
    assert Counter(_planned_actions(dry.output)) == Counter(_executed_actions(apply))
    assert [action[0] for action in _planned_actions(dry.output)] == [
        "create", "gh", "connect", "depend", "patch",
    ]
    assert PLAN_ID_PREFIX not in json.dumps([
        {"path": write.path, "body": write.body} for write in apply.http_writes
    ])
    assert PLAN_ID_PREFIX not in apply.state_file.read_text(encoding="utf-8")


def test_whole_run_batches_one_versioned_patch_per_card(paired_runs):
    _dry, apply = paired_runs
    patches = [write for write in apply.http_writes if write.method == "PATCH"]
    patch_counts = Counter(write.path for write in patches)

    assert patches
    assert all(count == 1 for count in patch_counts.values())
    assert all(write.headers.get("x-lk-resource-version", "").strip() for write in patches)
    assert len(json.dumps(patches[0].body)) > 200
