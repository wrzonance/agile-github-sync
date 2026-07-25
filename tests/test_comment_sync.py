"""Unit tests for comment_sync.py (issue #66): Task 1's shared timestamp-normalization helper,
Task 4's pure planning core -- provenance-prefix build/parse, sync-identity check, and
`resolve_comment_sync`, the load-bearing invariant suite for the whole feature's planning logic --
plus Task 5's wiring layer: `sync_comments` (the per-issue entrypoint) and `_execute_action` (the
kind/target_side dispatcher it drives), with the verified exception boundary between the two I/O
modules.

Run: pytest -q
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agileplace_comments  # noqa: E402
import comment_sync  # noqa: E402
import ghkit  # noqa: E402
from comment_render import (  # noqa: E402
    ProvenanceHeader,
    build_provenance_prefix,
    parse_provenance_prefix,
)
from comment_sync import (  # noqa: E402
    CommentAction,
    CommentSyncPlan,
    is_sync_authored,
    resolve_comment_sync,
)


# --- totality: never raises, whatever shape shows up -------------------------------------------

@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "not-a-timestamp",
        "2024-13-45T99:99:99Z",  # syntactically ISO-ish, semantically invalid
        "12345",
        "Tuesday, 15 Jan 2024",
        123,  # wrong type entirely -- must not raise, not just "not a str"
        123.456,
        [],
        {},
    ],
)
def test_parse_timestamp_never_raises_and_degrades_to_none(raw):
    assert comment_sync._parse_timestamp(raw) is None


# --- successful parses: GH's ISO-8601 (Z suffix) and offset/naive variants ---------------------

def test_parse_timestamp_parses_gh_style_z_suffix():
    result = comment_sync._parse_timestamp("2024-01-15T10:30:00Z")

    assert result == datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)


def test_parse_timestamp_result_is_tz_aware():
    result = comment_sync._parse_timestamp("2024-01-15T10:30:00Z")

    assert result.tzinfo is not None


def test_parse_timestamp_normalizes_explicit_offset_to_utc():
    result = comment_sync._parse_timestamp("2024-01-15T05:30:00-05:00")

    assert result == datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)


def test_parse_timestamp_assumes_utc_for_naive_input():
    result = comment_sync._parse_timestamp("2024-01-15T10:30:00")

    assert result == datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)


def test_parse_timestamp_two_equivalent_instants_compare_equal_after_parsing():
    """The whole point of the helper: two representations of the same instant from two different
    sides (GH's Z-suffixed UTC vs. AP's hypothetical offset form) must compare equal once both have
    gone through _parse_timestamp -- raw lexical string comparison can't guarantee that."""
    gh_side = comment_sync._parse_timestamp("2024-01-15T10:30:00Z")
    ap_side = comment_sync._parse_timestamp("2024-01-15T05:30:00-05:00")

    assert gh_side == ap_side


# =================================================================================================
# Fixture builders (issue #66 Task 4)
# =================================================================================================

IDENTITY = {"gh_login": "syncbot", "ap_author": "sync@example.com"}
_T0 = "2024-01-01T00:00:00Z"
_T1 = "2024-01-02T00:00:00Z"


def _gh(id, author=None, body="", created=_T0, edited=_T0):
    return {"id": id, "author": author, "body": body, "created": created, "edited": edited}


def _ap(id, name=None, email=None, aid=None, body="", created=_T0, edited=_T0):
    return {"id": id, "body": body, "author_name": name, "author_email": email, "author_id": aid,
            "created": created, "edited": edited}


def _hash(body: str = "") -> str:
    """The ledgered ap_hash for an AP comment carrying `body` -- AP drift is body-hash based
    (AgilePlace exposes no comment edit timestamp)."""
    return comment_sync._ap_body_hash({"body": body})


def _row(gh_id, ap_id, origin, gh_created=_T0, gh_edited=_T0, ap_created=_T0, ap_hash=None,
        deleted=False):
    return {"gh_id": gh_id, "ap_id": ap_id, "origin": origin, "gh_created": gh_created,
            "gh_edited": gh_edited, "ap_created": ap_created, "ap_hash": ap_hash,
            "deleted": deleted}


def _kinds(plan: CommentSyncPlan) -> list[str]:
    return [action.kind for action in plan.actions]


# =================================================================================================
# build_provenance_prefix / parse_provenance_prefix
# =================================================================================================

def test_build_provenance_prefix_gh_origin_exact_wording():
    assert build_provenance_prefix("gh", "alice") == "comment by alice on GitHub"


def test_build_provenance_prefix_ap_origin_exact_wording():
    assert build_provenance_prefix("ap", "bob") == "comment by bob on Agile Place"


def test_build_provenance_prefix_rejects_unknown_side():
    with pytest.raises(ValueError):
        build_provenance_prefix("bogus", "alice")


def test_parse_provenance_prefix_round_trips_gh_origin():
    header = parse_provenance_prefix(build_provenance_prefix("gh", "alice"))
    assert header == ProvenanceHeader(origin_side="gh", author_label="alice")


def test_parse_provenance_prefix_round_trips_ap_origin():
    header = parse_provenance_prefix(build_provenance_prefix("ap", "bob"))
    assert header == ProvenanceHeader(origin_side="ap", author_label="bob")


def test_parse_provenance_prefix_tolerant_of_ap_html_wrapping():
    """AP renders comment bodies as HTML -- the parser must still find an anchored prefix under a
    leading <p> wrapper and surrounding whitespace."""
    wrapped = "  <p>comment by alice on GitHub</p><p>the rest of the comment</p>"
    assert parse_provenance_prefix(wrapped) == ProvenanceHeader(origin_side="gh", author_label="alice")


def test_parse_provenance_prefix_none_for_plain_human_comment():
    assert parse_provenance_prefix("just a regular reply, thanks!") is None


def test_parse_provenance_prefix_none_for_non_string_input():
    assert parse_provenance_prefix(None) is None
    assert parse_provenance_prefix(12345) is None


def test_parse_provenance_prefix_requires_anchored_match_not_mid_sentence():
    """A human comment that merely mentions the phrase must never false-positive as a mirror."""
    assert parse_provenance_prefix("I saw a comment by alice on GitHub yesterday") is None


def test_parse_provenance_prefix_first_occurrence_suffix_assumption_pinned():
    """Design doc finding #4, accepted as-is: the parser splits on the FIRST occurrence of the
    suffix literal, so an author label that itself contains " on GitHub" degrades the round-trip
    (near-zero-probability real-world case, not hardened with a different delimiter scheme)."""
    author_label = "Bot on GitHub Actions"
    rendered = build_provenance_prefix("gh", author_label)

    header = parse_provenance_prefix(rendered)

    assert header.author_label != author_label
    assert header == ProvenanceHeader(origin_side="gh", author_label="Bot")


# =================================================================================================
# is_sync_authored
# =================================================================================================

def test_is_sync_authored_gh_matches_case_insensitively():
    assert is_sync_authored("gh", "SyncBot", IDENTITY) is True


def test_is_sync_authored_ap_matches_exact():
    assert is_sync_authored("ap", "sync@example.com", IDENTITY) is True


def test_is_sync_authored_ap_matches_email_case_insensitively():
    """Live finding (2026-07-23): AgilePlace's createdBy.emailAddress arrives MIXED-CASE even when
    the configured COMMENT_SYNC_AP_AUTHOR is lowercase. is_sync_authored casefolds both sides, so a
    mixed-case live email still matches the sync identity -- otherwise the sync would fail to
    recognize its own mirrors and re-mirror them as if human-authored."""
    identity = {"gh_login": "syncbot", "ap_author": "maintainer@example.com"}
    assert is_sync_authored("ap", "Maintainer@Example.COM", identity) is True


def test_is_sync_authored_false_for_different_author():
    assert is_sync_authored("gh", "alice", IDENTITY) is False


def test_is_sync_authored_false_when_identity_is_none():
    assert is_sync_authored("gh", "syncbot", None) is False


def test_is_sync_authored_false_when_author_identifier_is_none():
    assert is_sync_authored("gh", None, IDENTITY) is False


def test_is_sync_authored_never_raises_on_malformed_inputs():
    assert is_sync_authored("bogus-side", "syncbot", IDENTITY) is False
    assert is_sync_authored("gh", 12345, IDENTITY) is False
    assert is_sync_authored("gh", "syncbot", {"gh_login": 12345}) is False


# =================================================================================================
# resolve_comment_sync -- required invariants
# =================================================================================================

def test_identity_none_yields_an_empty_plan():
    ledger = [_row(1, 2, "gh")]
    plan = resolve_comment_sync(None, ledger, [_gh(1)], [_ap(2)])

    assert plan == CommentSyncPlan(actions=[])


def test_echo_prevention_ledgered_sync_authored_prefixed_comment_is_never_remirrored():
    """A comment that is sync-authored AND carries a valid provenance prefix AND already has a
    ledger entry must never produce a new mirror_new/adopt_orphan action for that pair -- it's
    simply the steady-state mirror, found via the ledger row's own ids."""
    prefix = build_provenance_prefix("ap", "bob")
    ledger = [_row(1, 2, "ap", ap_hash=_hash("Hello"))]
    gh_comments = [_gh(1, author="syncbot", body=f"{prefix}\n\nHello")]
    ap_comments = [_ap(2, name="bob", body="Hello")]

    plan = resolve_comment_sync(IDENTITY, ledger, gh_comments, ap_comments)

    assert plan.actions == []
    assert "mirror_new" not in _kinds(plan)
    assert "adopt_orphan" not in _kinds(plan)


def test_orphan_mirror_is_readopted_never_double_posted():
    """The crash-between-post-and-state-write case: a sync-authored, prefix-carrying GH comment
    with NO ledger row, whose AP origin also has no ledger row. Must be re-adopted into the ledger
    via a single adopt_orphan action -- never posted again as a fresh mirror_new."""
    prefix = build_provenance_prefix("ap", "bob")
    orphan_mirror = _gh(5, author="syncbot", body=f"{prefix}\n\nHello", created=_T0)
    origin_candidate = _ap(10, name="bob", body="Hello", created=_T0)

    plan = resolve_comment_sync(IDENTITY, [], [orphan_mirror], [origin_candidate])

    assert _kinds(plan) == ["adopt_orphan"]
    action = plan.actions[0]
    assert action.ledger_key == (5, 10)
    assert action.target_side is None
    assert action.existing_mirror_id == 5
    assert "mirror_new" not in _kinds(plan)


def test_orphan_adjacency_picks_the_closest_candidate_by_created_timestamp():
    """Struct #3's "orphan-adjacency gap computation": when several unledgered origin-side comments
    share the orphan's author label, the one whose created timestamp is closest wins. The unmatched
    "far" candidate is a genuinely separate, still-unledgered comment -- it correctly gets its own
    mirror_new rather than vanishing."""
    prefix = build_provenance_prefix("ap", "bob")
    orphan_mirror = _gh(5, author="syncbot", body=f"{prefix}\n\nHi", created="2024-01-05T00:00:00Z")
    far_candidate = _ap(100, name="bob", body="Hi", created="2024-01-01T00:00:00Z")
    near_candidate = _ap(101, name="bob", body="Hi", created="2024-01-05T00:00:05Z")

    plan = resolve_comment_sync(IDENTITY, [], [orphan_mirror], [far_candidate, near_candidate])

    adopt_actions = [a for a in plan.actions if a.kind == "adopt_orphan"]
    assert len(adopt_actions) == 1
    assert adopt_actions[0].ledger_key == (5, 101)
    mirror_new_actions = [a for a in plan.actions if a.kind == "mirror_new"]
    assert [a.ledger_key for a in mirror_new_actions] == [(None, 100)]


def test_tombstoned_row_never_produces_mirror_new_adopt_or_restore_for_that_pair():
    """Tombstoned rows are inert forever, even if a stale read still turns up one side's id."""
    ledger = [_row(1, 2, "gh", deleted=True)]
    gh_comments = [_gh(1, author="alice", body="original text")]
    ap_comments = []

    plan = resolve_comment_sync(IDENTITY, ledger, gh_comments, ap_comments)

    assert plan.actions == []


def test_resolve_comment_sync_is_deterministic():
    ledger = [_row(1, 2, "gh")]
    gh_comments = [_gh(1, author="alice", body="v2", edited=_T1)]
    ap_comments = [_ap(2, name="alice", body="v1")]

    first = resolve_comment_sync(IDENTITY, ledger, gh_comments, ap_comments)
    second = resolve_comment_sync(IDENTITY, ledger, gh_comments, ap_comments)

    assert first == second


@pytest.mark.parametrize(
    "ledger,gh_comments,ap_comments",
    [
        ([{"gh_id": "not-an-int", "ap_id": 2, "origin": "gh"}], [], []),
        ([{"gh_id": 1, "ap_id": 2, "origin": "bogus"}], [_gh(1)], [_ap(2)]),
        (["not-a-dict"], [_gh(1)], [_ap(2)]),
        ([], ["not-a-dict"], [_ap(2)]),
        ([], [{"id": "not-an-int", "author": "alice"}], []),
        ("not-a-list", [_gh(1)], [_ap(2)]),
    ],
)
def test_resolve_comment_sync_never_raises_on_malformed_data(ledger, gh_comments, ap_comments):
    resolve_comment_sync(IDENTITY, ledger, gh_comments, ap_comments)  # must not raise


def test_unparseable_live_timestamp_excludes_that_side_from_drift():
    """A comment whose current edited-timestamp doesn't parse must be excluded from the drift
    decision (with a WARN, at the wiring layer) rather than being treated as drifted. The plan
    itself carries the (pure, I/O-free) warning text -- `sync_comments` is what actually prints it,
    per `test_sync_comments_warns_on_stderr_for_unparseable_live_timestamp`."""
    ledger = [_row(1, 2, "gh", gh_edited=_T0, ap_hash=_hash("v1"))]
    gh_comments = [_gh(1, author="alice", body="v1", edited="not-a-timestamp")]
    ap_comments = [_ap(2, name="alice", body="v1", edited=_T0)]

    plan = resolve_comment_sync(IDENTITY, ledger, gh_comments, ap_comments)

    assert plan.actions == []
    assert len(plan.warnings) == 1
    assert "gh" in plan.warnings[0] and "1" in plan.warnings[0]


# =================================================================================================
# resolve_comment_sync -- one test per CommentAction kind
# =================================================================================================

def test_mirror_new_for_a_genuine_unledgered_origin_comment():
    gh_comments = [_gh(1, author="alice", body="Hello **world**", created=_T0)]

    plan = resolve_comment_sync(IDENTITY, [], gh_comments, [])

    assert _kinds(plan) == ["mirror_new"]
    action = plan.actions[0]
    assert action.target_side == "ap"
    assert action.ledger_key == (1, None)
    assert action.rendered_body.startswith("<p>comment by alice on GitHub</p>")
    assert "<strong>world</strong>" in action.rendered_body


def test_mirror_new_actions_are_chronologically_ordered_across_interleaved_sources():
    gh_comments = [_gh(1, author="alice", body="third", created="2024-01-03T00:00:00Z")]
    ap_comments = [
        _ap(10, name="bob", body="first", created="2024-01-01T00:00:00Z"),
        _ap(11, name="bob", body="second", created="2024-01-02T00:00:00Z"),
    ]

    plan = resolve_comment_sync(IDENTITY, [], gh_comments, ap_comments)

    assert [a.ledger_key for a in plan.actions] == [(None, 10), (None, 11), (1, None)]


def test_edit_mirror_when_origin_side_drifted():
    ledger = [_row(1, 2, "gh", gh_edited=_T0, ap_hash=_hash("stale mirror text"))]
    gh_comments = [_gh(1, author="alice", body="updated text", edited=_T1)]
    ap_comments = [_ap(2, name="alice", body="stale mirror text", edited=_T0)]

    plan = resolve_comment_sync(IDENTITY, ledger, gh_comments, ap_comments)

    assert _kinds(plan) == ["edit_mirror"]
    action = plan.actions[0]
    assert action.target_side == "ap"
    assert action.existing_mirror_id == 2
    assert "comment by alice on GitHub" in action.rendered_body
    assert "updated text" in action.rendered_body


def test_both_sides_drifted_github_wins():
    """Amended tie-break (design doc 2026-07-23): AgilePlace exposes no comment edit timestamp, so
    most-recent-wins can't run when BOTH sides drift -- GitHub wins deterministically. GH drift is
    timestamp-based; AP drift is a body-hash mismatch."""
    ledger = [_row(1, 2, "gh", gh_edited=_T0, ap_hash=_hash("stale ap body"))]
    gh_comments = [_gh(1, author="alice", body="gh edit", edited=_T1)]              # GH drifted
    ap_comments = [_ap(2, name="alice", body="ap edit, unknowable recency")]        # AP drifted (hash)

    plan = resolve_comment_sync(IDENTITY, ledger, gh_comments, ap_comments)

    assert _kinds(plan) == ["edit_mirror"]
    action = plan.actions[0]
    assert action.target_side == "ap"          # GH wins -> canonical=gh -> write the AP mirror
    assert "gh edit" in action.rendered_body   # GH's content propagates, not AP's


def test_delete_mirror_and_tombstone_when_origin_side_gone():
    ledger = [_row(1, 2, "gh")]
    ap_comments = [_ap(2, name="alice", body="mirrored text")]

    plan = resolve_comment_sync(IDENTITY, ledger, [], ap_comments)

    assert _kinds(plan) == ["delete_mirror_and_tombstone"]
    action = plan.actions[0]
    assert action.target_side == "ap"
    assert action.existing_mirror_id == 2
    assert action.ledger_key == (1, 2)


def test_restore_mirror_when_mirror_side_gone():
    ledger = [_row(1, 2, "gh")]
    gh_comments = [_gh(1, author="alice", body="original text")]

    plan = resolve_comment_sync(IDENTITY, ledger, gh_comments, [])

    assert _kinds(plan) == ["restore_mirror"]
    action = plan.actions[0]
    assert action.target_side == "ap"
    assert action.existing_mirror_id is None
    assert "comment by alice on GitHub" in action.rendered_body
    assert "original text" in action.rendered_body


def test_tombstone_both_gone_when_neither_side_present():
    ledger = [_row(1, 2, "gh")]

    plan = resolve_comment_sync(IDENTITY, ledger, [], [])

    assert _kinds(plan) == ["tombstone_both_gone"]
    action = plan.actions[0]
    assert action.target_side is None
    assert action.ledger_key == (1, 2)


def test_drop_unpairable_orphan_when_no_origin_candidate_matches():
    prefix = build_provenance_prefix("ap", "ghost")
    orphan_mirror = _gh(5, author="syncbot", body=f"{prefix}\n\nHello")

    plan = resolve_comment_sync(IDENTITY, [], [orphan_mirror], [])

    assert _kinds(plan) == ["drop_unpairable_orphan"]
    action = plan.actions[0]
    assert action.existing_mirror_id == 5
    assert action.target_side is None


def test_steady_state_no_drift_produces_no_action():
    ledger = [_row(1, 2, "gh", gh_edited=_T0, ap_hash=_hash("unchanged mirror"))]
    gh_comments = [_gh(1, author="alice", body="unchanged", edited=_T0)]
    ap_comments = [_ap(2, name="alice", body="unchanged mirror", edited=_T0)]

    plan = resolve_comment_sync(IDENTITY, ledger, gh_comments, ap_comments)

    assert plan.actions == []


# =================================================================================================
# sync_comments / _execute_action -- wiring entrypoint + dispatch (issue #66 Task 5/8)
# =================================================================================================

ISSUE_URL = "https://github.com/acme/widgets/issues/1"


def _cfg(identity=IDENTITY) -> dict:
    return {"comment_sync_identity": identity}


def _issue(number=1, url=ISSUE_URL) -> dict:
    return {"number": number, "url": url}


def _card(card_id="42", plan_only=False) -> dict:
    card = {"id": card_id}
    if plan_only:
        card["_planOnly"] = True
    return card


def _reset_warned_disabled(monkeypatch) -> None:
    monkeypatch.setattr(comment_sync, "_warned_disabled", False)


# --- self-disable WARN: at most once per process run -------------------------------------------

def test_sync_comments_self_disable_warn_fires_at_most_once_per_run(monkeypatch, capsys):
    _reset_warned_disabled(monkeypatch)
    monkeypatch.setattr(ghkit, "list_issue_comments",
                        lambda *a, **k: pytest.fail("must not fetch when self-disabled"))
    cfg = _cfg(identity=None)

    comment_sync.sync_comments(cfg, True, _issue(1, ISSUE_URL), _card(), {})
    comment_sync.sync_comments(cfg, True, _issue(2, ISSUE_URL + "-2"), _card(), {})

    err = capsys.readouterr().err
    assert err.count("WARN") == 1


def test_sync_comments_warns_on_stderr_for_unparseable_live_timestamp(monkeypatch, capsys):
    """The plan-level warning built by `resolve_comment_sync` (see
    `test_unparseable_live_timestamp_excludes_that_side_from_drift`) must actually reach the
    operator: `sync_comments` prints it to stderr, even on a run whose plan has zero actions."""
    issues_state = {ISSUE_URL: {"comments": [_row(1, 2, "gh", gh_edited=_T0, ap_hash=_hash("v1"))]}}
    monkeypatch.setattr(ghkit, "list_issue_comments",
                        lambda cfg, number: [_gh(1, author="alice", body="v1", edited="garbage")])
    monkeypatch.setattr(agileplace_comments, "list_comments",
                        lambda cfg, card_id: [_ap(2, name="alice", body="v1", edited=_T0)])

    comment_sync.sync_comments(_cfg(), True, _issue(), _card(), issues_state)

    err = capsys.readouterr().err
    assert "WARN" in err
    assert "unparseable" in err


def test_sync_comments_noop_on_plan_only_card(monkeypatch):
    monkeypatch.setattr(ghkit, "list_issue_comments",
                        lambda *a, **k: pytest.fail("must not fetch a plan-only card's comments"))
    issues_state = {}

    comment_sync.sync_comments(_cfg(), True, _issue(), _card(plan_only=True), issues_state)

    assert issues_state == {}


# --- delete safety: never delete a comment the sync did not author -----------------------------

def test_delete_flow_never_targets_a_comment_that_is_not_sync_authored(monkeypatch, capsys):
    issues_state = {ISSUE_URL: {"comments": [_row(1, 2, "gh")]}}
    monkeypatch.setattr(ghkit, "list_issue_comments", lambda cfg, number: [])
    monkeypatch.setattr(agileplace_comments, "list_comments",
                        lambda cfg, card_id: [_ap(2, name="a human", body="mirrored text")])
    delete_calls = []
    monkeypatch.setattr(agileplace_comments, "delete_comment",
                        lambda cfg, apply, card_id, comment_id: delete_calls.append(comment_id) or True)

    comment_sync.sync_comments(_cfg(), True, _issue(), _card(), issues_state)

    assert delete_calls == []
    assert issues_state[ISSUE_URL]["comments"][0] == _row(1, 2, "gh")
    assert "not sync-authored" in capsys.readouterr().err


# --- full-run ledger invariants: no partial pair, exactly one live mirror per live origin -------

def test_sync_run_ledger_never_leaves_a_partial_pair_and_every_origin_has_one_mirror(monkeypatch):
    """One run exercising edit_mirror, delete_mirror_and_tombstone, tombstone_both_gone, and
    mirror_new together: after a successful apply=True run, every persisted row must have both
    ids (a live pair) or be tombstoned (never exactly one id with deleted=False), and every living
    origin comment must have picked up exactly one living mirror."""
    issues_state = {ISSUE_URL: {"comments": [
        _row(1, 2, "gh", gh_edited=_T0, ap_hash=_hash("stale mirror text")),  # drift gh->ap, edit
        _row(3, 4, "ap"),                               # ap origin gone -> delete gh mirror
        _row(5, 6, "gh"),                               # both gone -> tombstone
    ]}}
    gh_state = [_gh(1, author="alice", body="updated text", edited=_T1),
               _gh(3, author="syncbot", body="mirror of the ap origin"),
               _gh(10, author="alice", body="brand new gh comment", created=_T0, edited=_T0)]
    ap_state = [_ap(2, name="alice", body="stale mirror text", edited=_T0)]

    monkeypatch.setattr(ghkit, "list_issue_comments", lambda cfg, number: [dict(c) for c in gh_state])
    monkeypatch.setattr(agileplace_comments, "list_comments",
                        lambda cfg, card_id: [dict(c) for c in ap_state])

    def _update_ap(cfg, apply, card_id, comment_id, html):
        for c in ap_state:
            if c["id"] == comment_id:
                c["body"], c["edited"] = html, _T1
        return apply

    def _create_ap(cfg, apply, card_id, html):
        if not apply:
            return None
        new = _ap(50, name=None, body=html, created=_T0, edited=_T0)
        ap_state.append(new)
        return new

    def _delete_gh(cfg, apply, comment_id):
        if apply:
            gh_state[:] = [c for c in gh_state if c["id"] != comment_id]
        return apply

    monkeypatch.setattr(agileplace_comments, "update_comment", _update_ap)
    monkeypatch.setattr(agileplace_comments, "create_comment", _create_ap)
    monkeypatch.setattr(ghkit, "delete_issue_comment", _delete_gh)

    comment_sync.sync_comments(_cfg(), True, _issue(), _card(), issues_state)

    rows = issues_state[ISSUE_URL]["comments"]
    for row in rows:
        both_ids_present = row["gh_id"] is not None and row["ap_id"] is not None
        assert both_ids_present or row["deleted"] is True, row

    live_pairs = {(r["gh_id"], r["ap_id"]) for r in rows if not r["deleted"]}
    assert live_pairs == {(1, 2), (10, 50)}

    # Echo prevention: the edited row's own persisted fingerprints must be the FRESH, post-write
    # values -- GH's edited timestamp is _T1, and the AP hash matches the AP mirror's now-edited
    # body -- not the stale pre-write values. A bug in `_apply_edit_effects` that failed to refresh a
    # side would leave these stale, and the next run's drift check would re-plan an edit_mirror for a
    # comment that never actually changed.
    row_1_2 = next(r for r in rows if r["gh_id"] == 1 and r["ap_id"] == 2)
    assert row_1_2["gh_edited"] == _T1
    ap_2 = next(c for c in ap_state if c["id"] == 2)
    assert row_1_2["ap_hash"] == comment_sync._ap_body_hash(ap_2)

    # Re-running the pure planner against the rebuilt ledger and the post-write live state must
    # find a steady state (no actions) -- the strongest possible confirmation that every succeeded
    # action's effect (edit/delete/tombstone/mirror_new) was correctly reconciled into the ledger.
    steady_plan = resolve_comment_sync(IDENTITY, rows, [dict(c) for c in gh_state],
                                       [dict(c) for c in ap_state])
    assert steady_plan.actions == []


# --- restore_mirror when origin is on AP: must not misattribute origin_side --------------------

def test_restore_mirror_from_ap_origin_replaces_stale_row_not_duplicate(monkeypatch):
    """Regression for origin_side misattribution in `_apply_create_result`: when the ORIGIN comment
    lives on AP and its GH mirror was deleted, `restore_mirror`'s `origin_ids` is the ledger row's
    full pre-existing (gh_id, ap_id) pair -- BOTH already non-None -- so a heuristic keyed on
    `origin_ids[0] is not None` always (wrongly) resolves to origin_side='gh'. After a successful
    restore, the persisted ledger must carry exactly ONE live row for this origin: the stale row
    (keyed on the now-dead gh_id) replaced by a new row keyed on the freshly created gh_id and the
    SAME preserved ap_id -- never both rows coexisting."""
    issues_state = {ISSUE_URL: {"comments": [_row(100, 555, "ap")]}}
    gh_state: list[dict] = []  # GH mirror #100 was deleted by a human
    ap_state = [_ap(555, name="alice", body="origin text on AP")]

    monkeypatch.setattr(ghkit, "list_issue_comments", lambda cfg, number: [dict(c) for c in gh_state])
    monkeypatch.setattr(agileplace_comments, "list_comments",
                        lambda cfg, card_id: [dict(c) for c in ap_state])

    def _create_gh(cfg, apply, number, body):
        if not apply:
            return None
        new_id = 999
        gh_state.append({"id": new_id, "author": "syncbot", "body": body,
                         "created": _T1, "edited": _T1})
        return new_id

    monkeypatch.setattr(ghkit, "create_issue_comment", _create_gh)

    comment_sync.sync_comments(_cfg(), True, _issue(), _card(), issues_state)

    rows = issues_state[ISSUE_URL]["comments"]
    live_rows = [r for r in rows if not r["deleted"]]
    assert len(live_rows) == 1, rows
    assert live_rows[0]["gh_id"] == 999
    assert live_rows[0]["ap_id"] == 555
    assert live_rows[0]["origin"] == "ap"
    assert not any(r["gh_id"] == 100 for r in rows), rows


# --- adopt_orphan through the full wiring pipeline -----------------------------------------------

def test_adopt_orphan_persists_full_pair_with_timestamps_through_wiring(monkeypatch):
    """The adopt_orphan ledger-persistence effect (`_apply_ledger_effect`'s adopt_orphan branch)
    exercised through the full sync_comments -> _run_plan -> _rebuild_ledger pipeline -- every other
    test that reaches this branch stops at the pure `resolve_comment_sync` plan. A sync-authored,
    prefix-carrying GH mirror with no ledger row, paired against its unledgered AP origin, must land
    in the persisted ledger with BOTH ids and BOTH sides' created/edited timestamps populated from
    the fresh fetch -- never left as a partial pair or with swapped/missing timestamps."""
    prefix = build_provenance_prefix("ap", "bob")
    issues_state = {ISSUE_URL: {"comments": []}}
    gh_comments = [_gh(5, author="syncbot", body=f"{prefix}\n\nHello", created=_T0, edited=_T0)]
    ap_comments = [_ap(10, name="bob", body="Hello", created=_T0, edited=_T1)]

    monkeypatch.setattr(ghkit, "list_issue_comments", lambda cfg, number: [dict(c) for c in gh_comments])
    monkeypatch.setattr(agileplace_comments, "list_comments",
                        lambda cfg, card_id: [dict(c) for c in ap_comments])

    comment_sync.sync_comments(_cfg(), True, _issue(), _card(), issues_state)

    rows = issues_state[ISSUE_URL]["comments"]
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["gh_id"] == 5 and row["ap_id"] == 10
    assert row["deleted"] is False
    assert row["gh_created"] == _T0 and row["gh_edited"] == _T0
    assert row["ap_created"] == _T0 and row["ap_hash"] == _hash("Hello")


# --- adopt_orphan origin_side must not be inferred from an id-equality heuristic ----------------

def test_adopt_orphan_origin_side_survives_id_collision_between_platforms():
    """Regression: `_apply_ledger_effect`'s adopt_orphan branch used to infer origin_side via
    `existing_mirror_id == gh_id`, which only happens to be correct when the mirror sits on the GH
    side (existing_mirror_id IS gh_id by construction there). When the mirror instead sits on the AP
    side, that heuristic is silently wrong if the AP mirror's own id numerically collides with the
    unrelated GH origin candidate's id -- two independent id spaces that can coincide. The action's
    own explicit `origin_side` field (set at plan time, where the true origin side is known) must be
    used instead, so an id collision can never flip which side is treated as the protected,
    human-authored origin."""
    prefix = build_provenance_prefix("gh", "alice")
    ap_orphan_mirror = _ap(42, email="sync@example.com", body=f"<p>{prefix}</p>Hello", created=_T0)
    gh_origin_candidate = _gh(42, author="alice", body="Hello", created=_T0)

    plan = resolve_comment_sync(IDENTITY, [], [gh_origin_candidate], [ap_orphan_mirror])

    adopt_actions = [a for a in plan.actions if a.kind == "adopt_orphan"]
    assert len(adopt_actions) == 1
    action = adopt_actions[0]
    assert action.ledger_key == (42, 42)
    assert action.existing_mirror_id == 42
    assert action.origin_side == "gh"

    rows_by_key: dict = {}
    comment_sync._apply_ledger_effect(action, None, rows_by_key,
                                      {42: gh_origin_candidate}, {42: ap_orphan_mirror}, True)

    assert rows_by_key[(42, 42)]["origin"] == "gh"


# --- confirmation refetch failure: don't record unconfirmed writes as drift-triggering state ----

def test_rebuild_ledger_leaves_new_mirror_unledgered_when_confirmation_refetch_fails(monkeypatch):
    """Regression (issue #66 Codex P1 #3): a just-created mirror whose post-write refetch fails must
    NOT be recorded with a None mirror-side edited baseline -- next run that baseline makes
    `_side_drifted` misread the mirror's real edited timestamp as fresh drift and blind-write it back
    over the origin. Instead the pair is left UNLEDGERED (the mirror carries a provenance prefix, so
    the next run re-adopts it as an orphan, re-verifying both sides), rather than persisting an
    unconfirmed guess as if it were confirmed state."""
    gh_origin = _gh(1, author="alice", body="origin text", created=_T0, edited=_T0)
    monkeypatch.setattr(comment_sync, "_fetch_both_sides", lambda cfg, number, card_id: None)

    action = CommentAction("mirror_new", "ap", (1, None), "rendered", None, (1, None))
    observed = {"id": 99}

    rows = comment_sync._rebuild_ledger(_cfg(), 1, "42", [], [(action, observed)],
                                        {1: gh_origin}, {})

    assert rows == []  # unconfirmed create left unledgered -> re-adopted next run, never a phantom pair


def test_rebuild_ledger_preserves_existing_origin_timestamps_on_refetch_failure(monkeypatch):
    """The other half of the same invariant: for a row that ALREADY exists (here an edit_mirror on a
    live pair), a failed confirmation refetch must fall back to the pre-write snapshot, NOT empty
    maps -- so the already-known ORIGIN side's created/edited timestamps are never blanked to None
    (a blank would make the next run misread a live edited timestamp as drift and overwrite the human
    origin)."""
    gh_origin = _gh(1, author="alice", body="origin text", created=_T0, edited=_T1)
    ap_mirror = _ap(2, email="sync@example.com", body="<p>comment by alice on GitHub</p>x", edited=_T1)
    monkeypatch.setattr(comment_sync, "_fetch_both_sides", lambda cfg, number, card_id: None)

    ledger = [_row(1, 2, "gh", gh_edited=_T0, ap_hash=_hash("old mirror body"))]
    action = CommentAction("edit_mirror", "ap", (1, 2), "rendered new body", 2, (1, 2))

    rows = comment_sync._rebuild_ledger(_cfg(), 1, "42", ledger, [(action, None)],
                                        {1: gh_origin}, {2: ap_mirror})

    assert len(rows) == 1
    row = rows[0]
    assert row["gh_id"] == 1 and row["ap_id"] == 2
    assert row["gh_created"] == _T0 and row["gh_edited"] == _T1  # preserved from fallback, not blanked
    # AP-targeted edit + refetch failure: leave ap_hash as the UNCONFIRMED sentinel (None), NOT the
    # hash of what we wrote -- AgilePlace normalizes stored HTML, so a what-we-wrote hash would
    # mis-register as AP drift next run. The next run adopts a baseline instead (confirm_ap_baseline).
    assert row["ap_hash"] is None


def test_unconfirmed_ap_hash_adopts_a_baseline_next_run_never_reverse_edits(monkeypatch):
    """Refetch-failure fail-safe (server-normalization robustness): a row whose ap_hash is the
    unconfirmed None sentinel must NOT be treated as AP drift. The next run plans a ledger-only
    confirm_ap_baseline that records the current AP body hash -- no reverse-edit against the GH
    origin -- so AP drift detection resumes without churn."""
    # Pure planner: None ap_hash + a live pair with no GH drift -> confirm_ap_baseline, not edit.
    ledger = [_row(1, 2, "gh", gh_edited=_T0, ap_hash=None)]
    gh_comments = [_gh(1, author="alice", body="origin", edited=_T0)]
    ap_comments = [_ap(2, name="alice", body="<p>server-normalized mirror body</p>")]
    plan = resolve_comment_sync(IDENTITY, ledger, gh_comments, ap_comments)
    assert _kinds(plan) == ["confirm_ap_baseline"]
    assert plan.actions[0].target_side is None  # ledger-only, no I/O

    # Through the wiring: apply persists ap_hash = the observed (normalized) body's hash.
    issues_state = {ISSUE_URL: {"comments": [_row(1, 2, "gh", gh_edited=_T0, ap_hash=None)]}}
    monkeypatch.setattr(ghkit, "list_issue_comments",
                        lambda cfg, number: [_gh(1, author="alice", body="origin", edited=_T0)])
    monkeypatch.setattr(agileplace_comments, "list_comments",
                        lambda cfg, card_id: [_ap(2, name="alice", body="<p>server-normalized mirror body</p>")])
    comment_sync.sync_comments(_cfg(), True, _issue(), _card(), issues_state)
    row = issues_state[ISSUE_URL]["comments"][0]
    assert row["ap_hash"] == _hash("<p>server-normalized mirror body</p>")


# --- exception boundary: a GitHub write can raise SystemExit too, not just AgilePlace -----------

def test_execute_one_action_warns_not_crashes_when_gh_write_raises_systemexit(monkeypatch, capsys):
    """Regression: `ghkit.create_issue_comment`/`edit_issue_comment`/`delete_issue_comment` all raise
    SystemExit when `_repo_context()` fails to resolve the target repo -- the same tri-state idiom
    `agileplace_comments` uses. `_execute_one_action`'s SystemExit handler only forgave the AP side,
    re-raising for GH and crashing the whole sync run instead of downgrading to a WARN."""
    action = CommentAction("mirror_new", "gh", (None, 2), "rendered body", None, (None, 2))

    def _boom(cfg, apply, number, body):
        raise SystemExit("create_issue_comment: repo context unavailable for issue #1")

    monkeypatch.setattr(ghkit, "create_issue_comment", _boom)

    ok, observed = comment_sync._execute_one_action(_cfg(), True, action, 1, "42")

    assert ok is False
    assert observed is None
    assert "WARN" in capsys.readouterr().err


# --- restore_mirror reproduces mirror_new's body byte-for-byte ----------------------------------

def test_restore_mirror_reproduces_a_fresh_mirror_new_body_byte_identically():
    gh_comments = [_gh(1, author="alice", body="original text")]

    restore_plan = resolve_comment_sync(IDENTITY, [_row(1, 2, "gh")], gh_comments, [])
    fresh_plan = resolve_comment_sync(IDENTITY, [], gh_comments, [])

    assert _kinds(restore_plan) == ["restore_mirror"]
    assert _kinds(fresh_plan) == ["mirror_new"]
    assert restore_plan.actions[0].rendered_body == fresh_plan.actions[0].rendered_body


# =================================================================================================
# Module import purity
# =================================================================================================

def test_comment_sync_module_is_import_pure(monkeypatch):
    """No network/subprocess/filesystem I/O may run merely from importing comment_sync."""
    def _boom(*args, **kwargs):
        raise AssertionError("comment_sync must not invoke subprocess at import time")

    monkeypatch.setattr(subprocess, "run", _boom)
    import importlib
    importlib.reload(comment_sync)


# =================================================================================================
# Draft-phase Codex-finding regressions (self-identity confirmed): #1 origin classification,
# #2 reverse-edit prefix, #5 tombstone verify, #7 AP exception boundary, #8 unparseable-ts warning
# =================================================================================================

# Self-identity is the PRIMARY configuration: the identity map pairs the maintainer's OWN accounts
# (design doc, e.g. thewrz <-> maintainer@example.com), so the person who authors ordinary comments
# IS the sync identity. The distinct-bot IDENTITY above is a valid SECONDARY config kept for the
# other tests -- both must satisfy the same invariants.
SELF_IDENTITY = {"gh_login": "maintainer", "ap_author": "maintainer@example.com"}


def test_maintainer_own_unprefixed_comment_is_mirrored_as_origin():
    """Issue #66 Codex P1 #1: a comment the maintainer posts under the configured identity, with NO
    provenance prefix, is their own ordinary comment and MUST be mirrored like anyone else's --
    previously it was silently dropped (identity-authored + no prefix), so the maintainer's own
    comments never synced at all."""
    gh_comments = [_gh(1, author="maintainer", body="my own note")]

    plan = resolve_comment_sync(SELF_IDENTITY, [], gh_comments, [])

    assert _kinds(plan) == ["mirror_new"]
    action = plan.actions[0]
    assert action.target_side == "ap"
    assert action.ledger_key == (1, None)
    assert "my own note" in action.rendered_body


def test_identity_authored_comment_with_prefix_is_a_mirror_not_a_fresh_origin():
    """The counterpart guard: identity-authored AND carrying a provenance prefix is a sync mirror
    (echo), re-adopted as an orphan when unledgered -- never re-mirrored as a fresh origin."""
    prefix = build_provenance_prefix("ap", "bob")
    gh_comments = [_gh(5, author="maintainer", body=f"{prefix}\n\nHello")]
    ap_comments = [_ap(10, name="bob", body="Hello")]

    plan = resolve_comment_sync(SELF_IDENTITY, [], gh_comments, ap_comments)

    assert _kinds(plan) == ["adopt_orphan"]
    assert "mirror_new" not in _kinds(plan)
    assert plan.actions[0].ledger_key == (5, 10)


def test_reverse_edit_of_a_mirror_writes_the_origin_prefix_less_never_doubled():
    """Issue #66 Codex P1 #2: when the MIRROR side drifts (a human edited the mirrored copy), the
    edit propagates back to the prefix-less origin. The mirror's body already leads with a provenance
    header; it must be STRIPPED before translating back, so the human origin never accumulates a
    (doubled) `comment by ... on ...` header. Invariant: the prefix appears zero times in the origin
    write."""
    prefix = build_provenance_prefix("gh", "alice")  # origin is GH, mirror is AP
    ledger = [_row(1, 2, "gh", gh_edited=_T0, ap_hash=_hash("stale pre-edit mirror body"))]
    gh_comments = [_gh(1, author="alice", body="original", edited=_T0)]  # origin unchanged
    ap_comments = [_ap(2, name="alice", body=f"<p>{prefix}</p><p>edited on the mirror</p>")]  # mirror drifted (hash)

    plan = resolve_comment_sync(IDENTITY, ledger, gh_comments, ap_comments)

    assert _kinds(plan) == ["edit_mirror"]
    action = plan.actions[0]
    assert action.target_side == "gh"  # writing the origin
    assert action.rendered_body.count("comment by alice on GitHub") == 0
    assert "edited on the mirror" in action.rendered_body


def test_delete_tombstone_is_skipped_when_the_mirror_is_still_present_on_readback(capsys):
    """Issue #66 Codex P2 #5: the AgilePlace DELETE shape is speculative. A 2xx that didn't actually
    remove the comment (mirror still present on the post-write refetch) must NOT tombstone the row --
    that would strand the visible mirror, ids ignored forever. WARN and leave the row live to retry."""
    action = CommentAction("delete_mirror_and_tombstone", "ap", (1, 2), None, 2, (1, 2))
    rows_by_key = {(1, 2): _row(1, 2, "gh")}
    still_present_ap = {2: _ap(2, name="alice", body="still here")}

    comment_sync._apply_ledger_effect(action, None, rows_by_key, {1: _gh(1)}, still_present_ap, True)

    assert rows_by_key[(1, 2)]["deleted"] is False  # not tombstoned
    assert "WARN" in capsys.readouterr().err


def test_delete_tombstone_applied_when_the_mirror_is_confirmed_gone():
    """The confirmed-gone path: the mirror is absent from the post-write refetch -> tombstone it."""
    action = CommentAction("delete_mirror_and_tombstone", "ap", (1, 2), None, 2, (1, 2))
    rows_by_key = {(1, 2): _row(1, 2, "gh")}

    comment_sync._apply_ledger_effect(action, None, rows_by_key, {1: _gh(1)}, {}, True)

    assert rows_by_key[(1, 2)]["deleted"] is True


def test_execute_one_action_warns_not_crashes_when_ap_response_shape_is_invalid(monkeypatch, capsys):
    """Issue #66 Codex P2 #7: `agileplace_comments.create_comment` raises ValueError when the POST
    response can't be normalized to an id (possibly AFTER the comment was created). That must be a
    warned per-action skip, never re-raised past the AP branch to abort the whole run."""
    action = CommentAction("mirror_new", "ap", (1, None), "rendered body", None, (1, None))

    def _boom(cfg, apply, card_id, html):
        raise ValueError("AgilePlace comment has an unusable id (None, expected an int or a digit string)")

    monkeypatch.setattr(agileplace_comments, "create_comment", _boom)

    ok, observed = comment_sync._execute_one_action(_cfg(), True, action, 1, "42")

    assert ok is False
    assert observed is None
    assert "WARN" in capsys.readouterr().err




# --- issue #98: prefetched GitHub comment snapshots ------------------------------------------

def test_fetch_both_sides_uses_prefetched_gh_comments(monkeypatch):
    """A batched-graph comment list bypasses the per-issue ghkit read entirely; the AgilePlace
    side still fetches as today."""
    monkeypatch.setattr(ghkit, "list_issue_comments",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("prefetched snapshot must bypass the per-issue read")))
    monkeypatch.setattr(agileplace_comments, "list_comments", lambda cfg, cid: [])
    gh = [{"id": 1, "author": "alice", "body": "hi", "created": "c", "edited": "e"}]

    assert comment_sync._fetch_both_sides({}, 5, "C1", gh_comments=gh) == (gh, [])


def test_fetch_both_sides_without_prefetch_reads_per_issue(monkeypatch):
    """gh_comments=None keeps today's per-issue read (the overflow/normalization fallback)."""
    gh = [{"id": 2, "author": None, "body": "b", "created": "c", "edited": "e"}]
    monkeypatch.setattr(ghkit, "list_issue_comments", lambda cfg, number: gh)
    monkeypatch.setattr(agileplace_comments, "list_comments", lambda cfg, cid: [])

    assert comment_sync._fetch_both_sides({}, 5, "C1", gh_comments=None) == (gh, [])
