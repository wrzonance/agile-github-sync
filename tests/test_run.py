"""Whole-run dry/apply parity tests at the real process and HTTP boundaries."""
from __future__ import annotations

import hashlib
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
            # issue #65: keeps agileplace_description.card_description() on its zero-I/O path -- without this
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
    # Comment-sync identity: force BLANK, not delenv. env_config() -> load_env_file() does
    # os.environ.setdefault() from the repo-root .env (see this function's top comment: it
    # "repopulates deleted variables"), so a merely-DELETED identity var is refilled from a dev box's
    # production .env -- enabling comment sync and hitting un-stubbed endpoints (4 test_run failures
    # on a machine whose .env exports COMMENT_SYNC_*; the ENV_FILE redirect above did not save it).
    # A present-but-BLANK value survives setdefault (it only fills UNSET keys) and
    # _parse_comment_sync_identity treats blank as disabled, so the baseline world is deterministic
    # regardless of ambient env OR .env contents. The enabled scenario sets real values on top.
    for name in ("COMMENT_SYNC_GH_LOGIN", "COMMENT_SYNC_AP_AUTHOR"):
        monkeypatch.setenv(name, "")


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


# --- Comment sync: dry/apply parity through the run harness (issue #66) --------------------------
#
# CI has only ever run comment sync self-DISABLED -- the identity vars are unset there and now
# explicitly cleared in _configure -- so the feature had never been exercised end-to-end (the #87
# fall-through class the WIRED_TEST_FILES guard exists to catch). This scenario runs WITH identity
# configured, seeding one human-authored origin comment on each side of the issue-1 / card-C1 pair so
# comment sync plans a mirror_new in BOTH directions, and pins that the dry run plans exactly what
# apply executes. The pre-EXISTING epic card C1 is used deliberately -- issue 2's card is created
# this run, so it's _planOnly in dry mode and comment sync no-ops on it (same as sync_description),
# which would make dry and apply diverge. Exhaustive planning logic stays in tests/test_comment_sync.py.

_GH_ORIGIN_COMMENT = {
    "id": 1001, "user": {"login": "alice"}, "body": "origin comment on GitHub",
    "created_at": "2024-01-01T00:00:00Z", "updated_at": "2024-01-01T00:00:00Z",
}
_AP_ORIGIN_COMMENT = {
    "id": "2001",  # the live POST/list serializes the id as a STRING of digits (API-VALIDATION.md)
    "createdBy": {"fullName": "bob", "emailAddress": "bob@example.com"},
    "text": "<p>origin comment on AgilePlace</p>",
    "createdOn": "2024-01-02T00:00:00Z", "lastModified": "2024-01-02T00:00:00Z",
}


def _is_gh_comment_path(path: str) -> bool:
    return path.endswith("/comments") or "/issues/comments/" in path


class _CommentFixtureWorld(FixtureWorld):
    """FixtureWorld plus the GitHub and AgilePlace comment endpoints comment sync drives. Seeds one
    human-authored origin comment on each side of the issue-1 / card-C1 pair (authored by alice/bob,
    neither the configured sync identity), so comment sync mirrors each to the other side. Issue 2 /
    card C2 carry no comments. Reuses the base gh/AgilePlace dispatch for everything else."""

    def run_process(self, argv, **kwargs):
        args = tuple(argv[1:])
        if args and args[0] == "api" and len(args) > 3 and _is_gh_comment_path(args[3]):
            return self._gh_comment(argv, args)
        return super().run_process(argv, **kwargs)

    def _gh_comment(self, argv, args):
        path = args[3]
        if path.endswith("/comments"):  # collection: list (read) or create (write)
            if "--slurp" in args:
                number = int(path.split("/issues/")[1].split("/")[0])
                page = [_GH_ORIGIN_COMMENT] if number == 1 else []
                return self._completed(argv, [page])
            self.process_writes.append(args)
            return self._completed(argv, {"id": 3001})
        self.process_writes.append(args)  # item path: edit (PATCH) or delete (DELETE)
        return self._completed(argv, {"id": 3002})

    def open_url(self, request, **kwargs):
        parsed = urllib.parse.urlparse(request.full_url)
        path = parsed.path.removeprefix("/io/")
        if path.endswith("/comment") or "/comment/" in path:
            method = request.get_method()
            if method != "GET":
                body = json.loads(request.data) if request.data else None
                headers = {key.lower(): value for key, value in request.header_items()}
                self.http_writes.append(HttpWrite(method, path, body, headers))
            return self._ap_comment(method, path)
        return super().open_url(request, **kwargs)

    def _ap_comment(self, method, path):
        if method == "GET":  # list
            card_id = path.split("/")[1]
            return _Response({"comments": [_AP_ORIGIN_COMMENT] if card_id == "C1" else []})
        if method == "POST":  # create -> a sparse string id, like the live tenant
            return _Response({"id": "4001"})
        return _Response({})  # PUT edit / DELETE


def _configure_comment_identity(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("COMMENT_SYNC_GH_LOGIN", "syncbot")
    monkeypatch.setenv("COMMENT_SYNC_AP_AUTHOR", "syncbot@example.com")


_AP_COMMENT_POST_RE = re.compile(r"^DRY   POST /io/card/\S+/comment .* body=")
_GH_COMMENT_POST_RE = re.compile(r"^DRY   gh api issue \d+ comment -- POST$")


def _comment_creates_planned(output: str) -> tuple[int, int]:
    """(ap_creates, gh_creates): comment mirror-creates the dry run PLANNED, read from its DRY lines."""
    ap = sum(1 for line in output.splitlines() if _AP_COMMENT_POST_RE.match(line))
    gh = sum(1 for line in output.splitlines() if _GH_COMMENT_POST_RE.match(line))
    return ap, gh


def _comment_creates_executed(run: RunResult) -> tuple[int, int]:
    """(ap_creates, gh_creates): comment mirror-creates the run EXECUTED, from its recorded writes."""
    ap = sum(1 for w in run.http_writes if w.method == "POST" and w.path.endswith("/comment"))
    gh = sum(1 for w in run.process_writes
             if w and w[0] == "api" and len(w) > 3 and w[3].endswith("/comments") and "POST" in w)
    return ap, gh


def test_comment_sync_dry_run_plans_the_mirror_creates_apply_executes(monkeypatch, tmp_path):
    """Issue #66 e2e: with identity configured, comment sync mirrors a human comment on each side to
    the other. The dry run must PLAN exactly the mirror-creates the apply run EXECUTES -- one AP
    create and one GH create (both directions) -- and the dry run must itself write nothing."""
    _configure_comment_identity(monkeypatch, tmp_path)
    dry = _run(monkeypatch, tmp_path, apply=False, world_factory=_CommentFixtureWorld)
    apply = _run(monkeypatch, tmp_path, apply=True, world_factory=_CommentFixtureWorld)

    assert _comment_creates_planned(dry.output) == (1, 1)  # both directions planned
    assert _comment_creates_executed(dry) == (0, 0)         # dry writes nothing
    assert _comment_creates_executed(apply) == (1, 1)       # apply executes exactly the plan


def test_configure_clears_ambient_comment_sync_env_keeping_baseline_deterministic(monkeypatch,
                                                                                  tmp_path):
    """Regression guard for the env-isolation fix: even when the developer's environment exports the
    production comment-sync identity, _configure must clear it so the baseline world (which stubs NO
    comment endpoints) never enables comment sync. If _configure stops clearing them, the base
    FixtureWorld would hit an un-stubbed comment endpoint and this run would raise 'unexpected ...'."""
    monkeypatch.setenv("COMMENT_SYNC_GH_LOGIN", "thewrz")
    monkeypatch.setenv("COMMENT_SYNC_AP_AUTHOR", "maintainer@example.com")
    _configure(monkeypatch, tmp_path)  # must delenv the two above

    dry = _run(monkeypatch, tmp_path, apply=False)
    apply = _run(monkeypatch, tmp_path, apply=True)

    assert _comment_creates_planned(dry.output) == (0, 0)
    assert _comment_creates_executed(apply) == (0, 0)


# --- Comment sync: AP-edit detected via body hash -> GH mirror edit (issue #66 amendment) --------
#
# AgilePlace comments carry NO edit timestamp (confirmed live 2026-07-24), so an AP-side edit is
# detected by a body-hash mismatch against the ledgered ap_hash. This seeds a LEDGERED gh<->ap pair
# whose AP origin body has changed since the last sync, and pins that the run plans+executes exactly
# one GH mirror edit (edit_mirror target=gh) across dry and apply -- the wiring for the amended drift
# model. Exhaustive hash-drift logic (GH-wins tie-break, echo, refetch-failure) stays in the unit suite.

_AP_ORIGIN_OLD = "<p>original ap origin body</p>"          # what the ledgered ap_hash was taken from
_AP_ORIGIN_EDITED = "<p>EDITED ap origin body</p>"         # a human changed it since -> hash mismatch


def _ap_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _seed_comment_ledger(state_path: Path, row: dict) -> None:
    """Pre-write the sync-state file with one comments-ledger row on the epic (issue 1 / card C1),
    so a run starts from an already-synced pair. target/board/schema must match what main() loads."""
    state = {
        "schema": sync.STATE_SCHEMA, "target": "acme/widgets", "board": "42",
        "issues": {EPIC_URL: {"card_id": "C1", "comments": [row]}},
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")


class _CommentEditFixtureWorld(FixtureWorld):
    """A LEDGERED gh<->ap comment pair on issue-1 / card-C1 whose AP origin body has been edited since
    the last sync (GH mirror unchanged). AP carries no edit timestamp, so the edit surfaces as a
    body-hash mismatch and the GH mirror is updated. Issue 2 / card C2 carry no comments."""

    def run_process(self, argv, **kwargs):
        args = tuple(argv[1:])
        if args and args[0] == "api" and len(args) > 3 and _is_gh_comment_path(args[3]):
            path = args[3]
            if path.endswith("/comments"):  # list
                number = int(path.split("/issues/")[1].split("/")[0])
                page = [{"id": 901, "user": {"login": "syncbot"},
                         "body": "comment by bob on Agile Place\n\nold mirror text",
                         "created_at": "2024-01-01T00:00:00Z",
                         "updated_at": "2024-01-01T00:00:00Z"}] if number == 1 else []
                return self._completed(argv, [page])
            self.process_writes.append(args)  # PATCH edit (or DELETE)
            return self._completed(argv, {"id": 901})
        return super().run_process(argv, **kwargs)

    def open_url(self, request, **kwargs):
        parsed = urllib.parse.urlparse(request.full_url)
        path = parsed.path.removeprefix("/io/")
        if path.endswith("/comment") or "/comment/" in path:
            method = request.get_method()
            if method != "GET":
                body = json.loads(request.data) if request.data else None
                headers = {key.lower(): value for key, value in request.header_items()}
                self.http_writes.append(HttpWrite(method, path, body, headers))
            if method == "GET":
                card_id = path.split("/")[1]
                comments = [{"id": 801,
                             "createdBy": {"fullName": "bob", "emailAddress": "bob@example.com"},
                             "text": _AP_ORIGIN_EDITED,
                             "createdOn": "2024-01-01T00:00:00Z"}] if card_id == "C1" else []
                return _Response({"comments": comments})
            return _Response({})
        return super().open_url(request, **kwargs)


_GH_COMMENT_PATCH_RE = re.compile(r"^DRY   gh api issue comment \d+ -- PATCH$")


def _comment_edits_planned(output: str) -> int:
    return sum(1 for line in output.splitlines() if _GH_COMMENT_PATCH_RE.match(line))


def _comment_edits_executed(run: RunResult) -> int:
    return sum(1 for w in run.process_writes
               if w and w[0] == "api" and len(w) > 3 and "/issues/comments/" in w[3] and "PATCH" in w)


def test_comment_sync_ap_edit_detected_via_hash_edits_gh_mirror(monkeypatch, tmp_path):
    """Issue #66 amendment: AgilePlace exposes no comment edit timestamp, so an AP-side edit is
    detected by a body-hash mismatch against the ledger. A ledgered pair whose AP origin body changed
    since the last sync must plan and execute exactly one GH mirror edit, 1:1 across dry and apply."""
    _configure_comment_identity(monkeypatch, tmp_path)
    row = {
        "gh_id": 901, "ap_id": 801, "origin": "ap",
        "gh_created": "2024-01-01T00:00:00Z", "gh_edited": "2024-01-01T00:00:00Z",  # GH unchanged
        "ap_created": "2024-01-01T00:00:00Z", "ap_hash": _ap_hash(_AP_ORIGIN_OLD), "deleted": False,
    }
    for name in ("dry-state.json", "apply-state.json"):
        _seed_comment_ledger(tmp_path / name, row)

    dry = _run(monkeypatch, tmp_path, apply=False, world_factory=_CommentEditFixtureWorld)
    apply = _run(monkeypatch, tmp_path, apply=True, world_factory=_CommentEditFixtureWorld)

    assert _comment_edits_planned(dry.output) == 1          # AP hash drift -> GH mirror edit planned
    assert _comment_edits_executed(apply) == 1              # and executed (PATCH issues/comments/901)
    assert _comment_creates_planned(dry.output) == (0, 0)   # no creates: the pair is already ledgered
