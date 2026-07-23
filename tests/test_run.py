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

# The Intake vetting latch's Project item id (issue #63, task 7/8): apply mode gets this real,
# gh-issued id back from "project item-add"; dry mode never calls gh at all, so it only ever sees
# ghproject.add_item's own deterministic placeholder (ITEM_ID_PREFIX + a hash). _normalize_gh
# collapses both to one token so a dry-planned and apply-executed "item-edit --id ..." compare
# equal -- the same idea as _card_role below, just for GitHub Project items instead of AgilePlace
# cards.
LATCH_ITEM_ID = "PVTI_NEW"
ITEM_ID_PREFIX = sync.ghproject.PLANNED_ITEM_ID_PREFIX


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
        self.created_card_body: dict | None = None
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
            # issue #65: keeps agileplace.card_description() on its zero-I/O path -- without this
            # key it falls back to the real (unmocked) GET card/{id}, which this fixture's own
            # open_url dispatch doesn't expect and raises "unexpected AgilePlace request".
            "description": "",
        }

    def run_process(self, argv, **_kwargs):
        args = tuple(argv[1:])
        if args[:2] == ("project", "item-add"):
            # The Intake vetting latch's item-add write (issue #63): a real item id, echoed back
            # just like AgilePlace's own card create -- so the apply run's downstream item-edit
            # write can reference it.
            self.process_writes.append(args)
            return self._completed(argv, {"id": LATCH_ITEM_ID})
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
            # Live create responses are SPARSE: the new card id only -- no version and no
            # customId/laneId echo (validated live 2026-07-21, issue #55). The server still
            # persists the full body, which the single-card GET below serves back.
            self.created_card_body = body
            return _Response({"id": "C2"})
        if method == "GET" and path == "card/C2":
            assert self.created_card_body is not None
            return _Response({
                "card": {
                    "id": "C2",
                    "title": self.created_card_body["title"],
                    "customId": self.created_card_body["customId"],
                    "laneId": self.created_card_body.get("laneId"),
                    "externalLink": self.created_card_body.get("externalLink"),
                    "version": "v2",
                    "tags": [],
                    "plannedStart": None,
                    "plannedFinish": None,
                    "blockedStatus": {"isBlocked": False, "reason": ""},
                    "description": "",  # issue #65: same zero-I/O contract as epic_card above
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


def _run(monkeypatch, tmp_path, *, apply: bool, world_factory=FixtureWorld) -> RunResult:
    world = world_factory()
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


def _normalize_gh(parts: tuple) -> tuple:
    """Collapse the Intake vetting latch's Project item id (real on apply, a deterministic
    placeholder on dry) to one canonical token -- same idea as _card_role, for GitHub Project item
    ids instead of AgilePlace card ids. A no-op for every other gh action (labels/milestone edits
    never contain either shape)."""
    return tuple("<item-id>" if part == LATCH_ITEM_ID or part.startswith(ITEM_ID_PREFIX) else part
                 for part in parts)


def _planned_actions(output: str) -> tuple:
    actions = []
    pattern = re.compile(r"^DRY   (?P<method>\w+) /io/(?P<path>\S+).* body=(?P<body>.+)$")
    for line in output.splitlines():
        match = pattern.match(line)
        if match:
            actions.append(_normalize_http(
                match["method"], match["path"], json.loads(match["body"])))
        elif line.startswith("DRY   gh "):
            actions.append(_normalize_gh(("gh", *shlex.split(line.removeprefix("DRY   gh ")))))
    return tuple(actions)


def _executed_actions(run: RunResult) -> tuple:
    http = tuple(_normalize_http(write.method, write.path, write.body)
                 for write in run.http_writes)
    process = tuple(_normalize_gh(("gh", *write)) for write in run.process_writes)
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


def test_created_card_snapshot_is_refetched_not_the_sparse_create_echo(paired_runs):
    """Issue #55: the sparse create response indexed as the snapshot queued redundant /customId
    and /laneId ops, which then tripped the issue-#8 stale-ops guard and aborted every fresh
    create+sync apply before metadata landed. The apply must refetch the created card, queue no
    redundant identity ops, and drive the batched PATCH to completion."""
    _dry, apply = paired_runs
    task_patch_ops = [op
                      for write in apply.http_writes
                      if write.method == "PATCH" and write.path == "card/C2"
                      for op in write.body]

    assert task_patch_ops  # metadata landed -- the run did not abort
    assert {op["path"] for op in task_patch_ops}.isdisjoint({"/customId", "/laneId"})
    assert apply.state_file.exists()


def test_whole_run_batches_one_versioned_patch_per_card(paired_runs):
    _dry, apply = paired_runs
    patches = [write for write in apply.http_writes if write.method == "PATCH"]
    patch_counts = Counter(write.path for write in patches)

    assert patches
    assert all(count == 1 for count in patch_counts.values())
    assert all(write.headers.get("x-lk-resource-version", "").strip() for write in patches)
    assert len(json.dumps(patches[0].body)) > 200


# --- Intake vetting latch: dry/apply parity for its two new gh writes (issue #63, task 7/8) ------

_VETTED_URL = "https://g.test/acme/widgets/issues/9"


class _IntakeFixtureWorld(FixtureWorld):
    """One off-board, no-work-signal issue with an existing card already sitting in a lane a human
    mapped to "Ready" -- not the Intake lane itself. resolve_issue_stage() resolves "Intake" (bare
    "Backlog" fallback + declared Intake lane + off-board); the card's current lane resolves back to
    "Ready" via stage_for_lane, so apply_latch() takes the promote path: ghproject.add_item then
    ghproject.set_item_status. Reuses FixtureWorld's gh/AgilePlace dispatch, adding only the
    Projects v2 reads (item-list, view, field-list) that path needs -- item-add and item-edit are
    already dispatched by the shared base (see run_process)."""

    lanes = (
        {"id": "L_INTAKE", "title": "Intake Lane", "cardStatus": "notStarted"},
        {"id": "L_VETTED", "title": "Vetted Lane", "cardStatus": "notStarted"},
    )
    issues = (
        {
            "number": 1,
            "title": "[TASK] Needs triage",
            "state": "OPEN",
            "stateReason": None,
            "labels": [],
            "milestone": None,
            "assignees": [],
            "url": _VETTED_URL,
        },
    )

    def __init__(self):
        super().__init__()
        self.epic_card = {
            "id": "C1",
            "version": "v1",
            "title": "Needs triage",
            "customId": "TASK",
            "externalLink": {"url": _VETTED_URL},
            "tags": [],
            "laneId": "L_VETTED",
            "plannedStart": None,
            "plannedFinish": None,
            "blockedStatus": {"isBlocked": False, "reason": ""},
            "childCards": [],
            "description": "",  # issue #65: same zero-I/O contract as FixtureWorld.epic_card
        }

    def run_process(self, argv, **kwargs):
        args = tuple(argv[1:])
        if args[:2] == ("project", "item-list"):
            return self._completed(argv, {"items": []})  # off-board: no items at all
        if args[:2] == ("project", "view"):
            return self._completed(argv, {"id": "PVT_1"})
        if args[:2] == ("project", "field-list"):
            return self._completed(argv, {"fields": [{
                "name": "Status", "id": "F_STATUS",
                "options": [{"name": "Intake", "id": "OPT_INTAKE"},
                            {"name": "Ready", "id": "OPT_READY"}],
            }]})  # Status-only board -- no Start/Target fields (mirrors ghproject.set_item_status's
                  # own "must not reuse main()'s nulled field_meta" contract)
        return super().run_process(argv, **kwargs)


def _configure_intake(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("GH_PROJECT_OWNER", "acme")
    monkeypatch.setenv("GH_PROJECT_NUMBER", "7")
    monkeypatch.setenv("STAGE_LANE_MAP", "Intake=Intake Lane;Ready=Vetted Lane")


def test_latch_writes_dry_run_plans_exactly_what_apply_executes(monkeypatch, tmp_path):
    """Task 7/8: the latch's two new gh writes (project item-add, then project item-edit
    --single-select-option-id) must plan/execute 1:1 like every other write this harness already
    pins. No AgilePlace HTTP write is expected at all -- the latch skips the ordinary lane-move for
    a latched card, and this card/issue pair carries no other metadata drift to sync."""
    _configure_intake(monkeypatch, tmp_path)
    dry = _run(monkeypatch, tmp_path, apply=False, world_factory=_IntakeFixtureWorld)
    apply = _run(monkeypatch, tmp_path, apply=True, world_factory=_IntakeFixtureWorld)

    assert dry.http_writes == ()
    assert dry.process_writes == ()
    assert apply.http_writes == ()

    planned = _planned_actions(dry.output)
    executed = _executed_actions(apply)
    assert planned  # the latch actually planned something this run
    assert [action[:3] for action in planned] == [
        ("gh", "project", "item-add"),
        ("gh", "project", "item-edit"),
    ]
    assert Counter(planned) == Counter(executed)
