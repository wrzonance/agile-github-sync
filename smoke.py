"""One-shot live smoke test for the AgilePlace write path. Stdlib only.

Reads .env exactly like sync.py, previews the target board (title, lanes, existing cards), and only
after an explicit confirmation creates clearly-marked throwaway cards, exercises every write shape
the sync uses -- card create, versioned PATCH tag add/remove, externalLink add on a bare card,
connect/disconnect children, description write + length probe (issue #65), a comment create/list/
edit/delete round-trip (issue #66 -- the delete shape is speculative, never exposed by the web UI),
a typeId replace and a typed card create (each verified via refetch, skipped as informational when
the board has no eligible card type configured), a deliberately stale-version PATCH that MUST be
rejected, a live richtext round-trip of the configured repo's GitHub issue #1 body through the
card description (issue #78 fidelity), and a customId header-format round-trip (issue #93) --
then deletes every throwaway card and confirms they are gone. The steps mirror the ``[live-check]``
items in API-VALIDATION.md so one confirmed run retires them.

GitHub is never WRITTEN (the issue #1 richtext step performs a READ only -- `gh issue list`; dry
runs already exercise every gh read live), and the sync state file is never read or written. Every
server rejection is printed with its full, untruncated response body so an incorrect write shape is
diagnosable straight from the console.
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import sys

import agileplace
import agileplace_comments
import agileplace_description
import board_layout
import card_types
import ghkit
import ghkit_snapshot
import richtext
from config import env_config

PARENT_TITLE = "SMOKE parent (safe to delete)"
CHILD_TITLE = "SMOKE child (safe to delete)"
TYPED_TITLE = "SMOKE typed (safe to delete)"
# Custom ids carry a fresh per-run suffix: the sync's _matching_card falls back to customId
# matching, so a fixed id on a leftover card could be adopted by a concurrent or later sync run
# (and two smoke runs would collide on the board). See PR #51 review.
PARENT_CUSTOM_ID_PREFIX = "SMOKE-P-"
CHILD_CUSTOM_ID_PREFIX = "SMOKE-C-"
TYPED_CUSTOM_ID_PREFIX = "SMOKE-T-"
# example.invalid can never collide with a real issue URL, so a card left behind by a failed
# cleanup can never be adopted by a later sync run's external-link matching.
PARENT_URL = "https://example.invalid/smoke/parent"
CHILD_URL = "https://example.invalid/smoke/child"
EXPECTED_CONFLICT_CODES = (409, 412, 428)
PREVIEW_CARD_LIMIT = 20


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Live write-path smoke test against the configured AgilePlace board.")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    return parser.parse_args(argv)


def _require_cfg() -> dict:
    cfg = env_config()
    missing = [env for env, key in (("AGILEPLACE_TOKEN", "token"), ("AGILEPLACE_HOST", "host"),
                                    ("AGILEPLACE_BOARD_ID", "board_id")) if not cfg.get(key)]
    if missing:
        raise SystemExit(f"smoke mode needs {', '.join(missing)} set (.env) -- refusing to run")
    return cfg


def _preview(cfg: dict) -> list[dict]:
    """Read the board live and show exactly what smoke is pointed at before anything is written."""
    board = agileplace.api(cfg, "GET", f"board/{cfg['board_id']}")
    title = (board.get("title") or "<untitled>") if isinstance(board, dict) else "<untitled>"
    lanes = [lane for lane in (board.get("lanes") or [])
             if isinstance(lane, dict) and lane.get("id") is not None]
    print(f"Board {cfg['board_id']} on {cfg['host']}: '{title}'")
    for lane in lanes:
        drop = "  [default drop lane]" if lane.get("isDefaultDropLane") else ""
        print(f"  lane '{board_layout.lane_title(lane)}' ({lane['id']}){drop}")
    cards = agileplace.list_cards(cfg)
    print(f"{len(cards)} card(s) currently on this board:")
    for card in cards[:PREVIEW_CARD_LIMIT]:
        print(f"  - [{agileplace.custom_id_value(card) or '-'}] {card.get('title', '<untitled>')}")
    if len(cards) > PREVIEW_CARD_LIMIT:
        print(f"  ... and {len(cards) - PREVIEW_CARD_LIMIT} more")
    return lanes


def _confirm(assume_yes: bool) -> bool:
    print("\nSmoke will CREATE two throwaway cards on this board, mutate and connect them, "
          "probe a stale-version write, then DELETE them. No GitHub writes; no sync state.")
    if assume_yes:
        return True
    return input("Type 'smoke' to continue, anything else to abort: ").strip() == "smoke"


def _pick_lane(lanes: list[dict]) -> dict | None:
    return next((lane for lane in lanes if lane.get("isDefaultDropLane")),
                lanes[0] if lanes else None)


def _pick_card_type(board_card_types: list[dict]) -> dict | None:
    """First eligible (isCardType, non-empty title) board card type. Smoke only needs ANY
    real board-configured type to exercise the typeId write path -- unlike the sync's own
    card_types.resolve_card_type_ids, it does not need one matching a specific derived name."""
    return next((card_type for card_type in board_card_types
                if card_type.get("isCardType") and (card_type.get("title") or "").strip()), None)


def _print_http_failure(exc: SystemExit) -> None:
    print(f"ERROR {exc}")
    status = getattr(exc, "http_status", None)
    if status is not None:
        body = getattr(exc, "http_body", "") or ""
        print(f"      server returned HTTP {status}; full response body follows:")
        print(body if body.strip() else "      <empty body>")


def _step(number: int, title: str) -> None:
    print(f"\nSTEP {number}: {title}")


def _has_version(card: dict) -> bool:
    version = card.get("version")
    return version is not None and str(version).strip() != ""


def _check_create_parent(cfg: dict, lane_id: str | None, custom_id: str, created: list[str],
                         results: list) -> tuple[str, str]:
    """Steps 1-2: the sync's exact create shape, then the single-card GET response shape."""
    _step(1, "create parent card (customId + externalLink) -- the sync's create shape")
    parent = agileplace.create_card(cfg, True, PARENT_TITLE, custom_id, PARENT_URL, lane_id)
    parent_id = str(parent.get("id") or "")
    if not parent_id:
        raise SystemExit(f"create response has no card id ({dict(parent)!r}) -- cannot continue")
    created.append(parent_id)
    print(f"      created card {parent_id}")
    results.append(("card create (customId + externalLink accepted)", True, f"id {parent_id}"))
    # Fact-finding, not pass/fail: both outcomes are handled (a version-less card is refetched
    # before any PATCH), so this only reports which patch path the sync will take.
    results.append(("create response carries a resource version", None,
                    f"version={parent.get('version')!r} -- "
                    + ("PATCHes can reuse it" if _has_version(dict(parent))
                       else "the sync's refetch-before-PATCH path is required")))

    _step(2, "single-card GET response shape")
    raw = agileplace.api(cfg, "GET", f"card/{parent_id}")
    shape = 'wrapped {"card": ...}' if isinstance(raw, dict) and "card" in raw else "flat card object"
    print(f"      response is {shape}")
    results.append(("single-card GET shape", True, shape))
    baseline_version = str(agileplace.get_card(cfg, parent_id).get("version"))
    return parent_id, baseline_version


def _check_tag_roundtrip(cfg: dict, parent_id: str, results: list) -> None:
    """Steps 3-4: tag add then index-based tag removal, each as one versioned PATCH."""
    _step(3, "tag add via one versioned PATCH")
    fresh = agileplace.get_card(cfg, parent_id)
    agileplace.patch_card(cfg, True, fresh, [agileplace.op_tag("smoke-test")])
    tags = agileplace.card_tags(agileplace.get_card(cfg, parent_id))
    added = "smoke-test" in tags
    results.append(("tag add round-trip", added, f"tags now {sorted(tags)}"))
    if not added:
        # ops_tag_remove raises on an absent tag; a failed add must skip the remove, not crash.
        results.append(("tag remove round-trip (index-based ops)", False,
                        "skipped -- the added tag never appeared on readback"))
        return

    _step(4, "tag remove via index-based RFC-6902 ops")
    fresh = agileplace.get_card(cfg, parent_id)
    ops = agileplace.ops_tag_remove(fresh.get("tags") or [], {"smoke-test"})
    agileplace.patch_card(cfg, True, fresh, ops)
    tags = agileplace.card_tags(agileplace.get_card(cfg, parent_id))
    results.append(("tag remove round-trip (index-based ops)", "smoke-test" not in tags,
                    f"tags now {sorted(tags)}"))


def _date_matches(value, expected: str | None) -> bool:
    """Tolerate the server echoing a date with a time component (e.g. 2026-01-01T00:00:00Z)."""
    if expected is None:
        return not value
    return str(value or "").startswith(expected)


def _check_blocked_and_dates(cfg: dict, parent_id: str, results: list) -> None:
    """Steps 5-6: blocked-state and planned-date write round-trips. Planned dates are a sync
    write shape; the blocked-state ops validate API surface only -- the sync never writes the
    flag (issue #57 Phase 2)."""
    _step(5, "set blocked state + planned dates in one versioned PATCH")
    fresh = agileplace.get_card(cfg, parent_id)
    ops = [*agileplace.ops_blocked(True, "smoke block"),
           agileplace.op_planned_date("plannedStart", "2026-01-01"),
           agileplace.op_planned_date("plannedFinish", "2026-01-02")]
    agileplace.patch_card(cfg, True, fresh, ops)
    readback = agileplace.get_card(cfg, parent_id)
    blocked_ok = (agileplace.card_is_blocked(readback)
                  and agileplace.card_block_reason(readback) == "smoke block")
    dates_ok = (_date_matches(readback.get("plannedStart"), "2026-01-01")
                and _date_matches(readback.get("plannedFinish"), "2026-01-02"))
    results.append(("blocked-state write round-trip", blocked_ok,
                    f"isBlocked={agileplace.card_is_blocked(readback)} "
                    f"reason={agileplace.card_block_reason(readback)!r}"))
    results.append(("planned-date write round-trip", dates_ok,
                    f"plannedStart={readback.get('plannedStart')!r} "
                    f"plannedFinish={readback.get('plannedFinish')!r}"))

    _step(6, "clear blocked state + planned dates")
    fresh = agileplace.get_card(cfg, parent_id)
    ops = [*agileplace.ops_blocked(False, None),
           agileplace.op_planned_date("plannedStart", None),
           agileplace.op_planned_date("plannedFinish", None)]
    agileplace.patch_card(cfg, True, fresh, ops)
    readback = agileplace.get_card(cfg, parent_id)
    cleared = (not agileplace.card_is_blocked(readback)
               and _date_matches(readback.get("plannedStart"), None)
               and _date_matches(readback.get("plannedFinish"), None))
    results.append(("blocked-state + planned-date clear round-trip", cleared,
                    f"isBlocked={agileplace.card_is_blocked(readback)} "
                    f"plannedStart={readback.get('plannedStart')!r} "
                    f"plannedFinish={readback.get('plannedFinish')!r}"))


def _check_child_and_link(cfg: dict, lane_id: str | None, custom_id: str, created: list[str],
                          results: list) -> str:
    """Steps 7-8: create a card with no external link, then PATCH-add /externalLink (init 04 shape)."""
    _step(7, "create child card without an external link")
    child = agileplace.create_card(cfg, True, CHILD_TITLE, custom_id, "", lane_id)
    child_id = str(child.get("id") or "")
    if not child_id:
        raise SystemExit(f"child create response has no card id ({dict(child)!r}) -- cannot continue")
    created.append(child_id)
    print(f"      created card {child_id}")
    results.append(("card create without external link", True, f"id {child_id}"))

    _step(8, "add /externalLink to the bare card, then read it back")
    fresh = agileplace.get_card(cfg, child_id)
    if not _has_version(fresh):
        raise SystemExit(f"card {child_id} has no resource version -- refusing unversioned PATCH")
    body = [{"op": "add", "path": "/externalLink", "value": {"label": "SMOKE", "url": CHILD_URL}}]
    print(f"      PATCH /io/card/{child_id} body={json.dumps(body)}")
    agileplace.api(cfg, "PATCH", f"card/{child_id}", body=body,
                   headers={"x-lk-resource-version": str(fresh["version"])})
    # A 2xx is not proof the server honored the op -- read the link back before reporting PASS.
    urls = agileplace.card_external_urls(agileplace.get_card(cfg, child_id))
    results.append(("externalLink add on a bare card", CHILD_URL in urls,
                    f"external urls now {urls}"))
    return child_id


def _check_connections(cfg: dict, parent_id: str, child_id: str, results: list) -> None:
    """Steps 7-8: connect/disconnect round-trip through the documented Connections shapes."""
    _step(9, "connect child -> parent, then read children back")
    agileplace.connect_children(cfg, True, parent_id, [child_id])
    children = agileplace.card_child_ids(cfg, parent_id)
    results.append(("connect children + child read round-trip", children == frozenset({child_id}),
                    f"children read back: {sorted(children) if children is not None else 'unavailable'}"))

    _step(10, "disconnect child, then confirm the authoritative empty read")
    agileplace.disconnect_children(cfg, True, parent_id, [child_id])
    children = agileplace.card_child_ids(cfg, parent_id)
    results.append(("disconnect children + authoritative empty read", children == frozenset(),
                    f"children read back: {sorted(children) if children is not None else 'unavailable'}"))


def _check_dependencies(cfg: dict, parent_id: str, child_id: str, results: list) -> None:
    """Steps 11-12: dependency create/read/delete round-trip through the shapes captured live
    2026-07-21 (undocumented API -- see API-VALIDATION.md "Dependencies API discovery"), plus a
    pin on the confirmed duplicate-create contract (HTTP 409 Conflict, nothing else passes)."""
    _step(11, "create dependency child dependsOn parent, then read it back")
    agileplace.create_dependencies(cfg, True, child_id, [parent_id])
    entries = agileplace.card_dependencies(cfg, child_id)
    incoming = agileplace.incoming_dependency_ids(entries) if entries is not None else None
    timing_ok = entries is not None and any(
        e.get("direction") == "incoming" and str(e.get("cardId")) == parent_id
        and e.get("timing") == agileplace.DEPENDENCY_TIMING for e in entries)
    results.append(("dependency create + incoming read round-trip",
                    incoming == {parent_id} and timing_ok,
                    f"incoming read back: {sorted(incoming) if incoming is not None else 'unavailable'}"))

    # The duplicate-create contract was confirmed live 2026-07-21: HTTP 409 Conflict. Only that
    # exact rejection passes -- acceptance contradicts the recorded contract, and a 401/5xx/
    # transport failure must fail the run, never hide behind an informational line.
    try:
        agileplace.create_dependencies(cfg, True, child_id, [parent_id])
        dup_ok = False
        dup_detail = "duplicate create ACCEPTED -- contradicts the confirmed HTTP 409 contract"
    except SystemExit as exc:
        dup_ok = getattr(exc, "http_status", None) == 409
        dup_detail = (f"duplicate create rejected: {exc}" if dup_ok
                      else f"unexpected failure (not the confirmed 409): {exc}")
    entries = agileplace.card_dependencies(cfg, child_id)
    incoming = agileplace.incoming_dependency_ids(entries) if entries is not None else None
    dup_count = (sum(1 for e in entries if e.get("direction") == "incoming"
                     and str(e.get("cardId")) == parent_id) if entries is not None else None)
    results.append(("duplicate dependency create rejected (HTTP 409)", dup_ok,
                    f"{dup_detail}; incoming entries for parent now: {dup_count}"))

    _step(12, "delete the dependency, then confirm the empty read")
    agileplace.delete_dependencies(cfg, True, child_id, [parent_id])
    entries = agileplace.card_dependencies(cfg, child_id)
    incoming = agileplace.incoming_dependency_ids(entries) if entries is not None else None
    results.append(("dependency delete + empty read round-trip", incoming == set(),
                    f"incoming read back: {sorted(incoming) if incoming is not None else 'unavailable'}"))


def _check_description(cfg: dict, parent_id: str, results: list) -> None:
    """Steps 13-14: agileplace_description.op_description's exact write shape through the same versioned-PATCH
    path (patch_card) the sync itself uses (description_sync.sync_description), then a
    fact-finding probe of the configured ap_description_max_length against the live server (the
    issue #65 design doc's "max-length probe": cross-check config.DEFAULT_AP_DESCRIPTION_MAX_LENGTH
    -- or its .env override -- against what the real API actually accepts).

    NOTE -- untested replace-vs-remove uncertainty (issue #65 design decision #7): op_description
    always issues an RFC-6902 "replace", including when description_sync writes "" to clear a
    card's description. Other fields that need clearing (planned dates, blocked-state) use a
    dedicated "remove" op instead of a replace-to-null/empty -- see steps 5-6's live-validated
    contract above -- but whether AgilePlace's PATCH endpoint honors a replace-to-"" the same way
    for /description has never been checked against the real API. This step only validates a
    non-empty replace round-trips correctly; it deliberately does NOT exercise clearing to "", so
    that uncertainty stays open (tracked in the design doc) until a live run confirms one way or
    the other."""
    _step(13, "description write via op_description through patch_card")
    fresh = agileplace.get_card(cfg, parent_id)
    html = "<p>SMOKE description write</p>"
    agileplace.patch_card(cfg, True, fresh, [agileplace_description.op_description(html)])
    readback = agileplace_description.card_description(cfg, agileplace.get_card(cfg, parent_id))
    results.append(("description write round-trip (op_description via patch_card)",
                    readback == html, f"description now {readback!r}"))

    _step(14, "description length probe (fact-finding, not pass/fail)")
    limit = cfg["ap_description_max_length"]
    probe_html = f"<p>{'x' * limit}</p>"
    fresh = agileplace.get_card(cfg, parent_id)
    try:
        agileplace.patch_card(cfg, True, fresh, [agileplace_description.op_description(probe_html)])
    except SystemExit as exc:
        _print_http_failure(exc)
        results.append(("description length probe", None,
                        f"server REJECTED a {len(probe_html)}-char description "
                        f"(configured max_length={limit}): {exc}"))
        return
    readback = agileplace_description.card_description(cfg, agileplace.get_card(cfg, parent_id))
    results.append(("description length probe", None,
                    f"sent {len(probe_html)} chars, server stored {len(readback)} chars back "
                    f"(configured max_length={limit})"))


def _write_description(cfg: dict, parent_id: str, html: str) -> str:
    """Write `html` to the card's description via the sync's own op_description/patch_card path and
    return what the server stored back (as read by card_description)."""
    fresh = agileplace.get_card(cfg, parent_id)
    agileplace.patch_card(cfg, True, fresh, [agileplace_description.op_description(html)])
    return agileplace_description.card_description(cfg, agileplace.get_card(cfg, parent_id))


def _diff_window(sent: str, stored: str) -> str:
    """Whole verbatim SENT/STORED when short; otherwise a first-divergence window (index +/-60), so
    a live run reveals exactly what AgilePlace normalizes without dumping huge bodies."""
    if max(len(sent), len(stored)) <= 400:
        return f"SENT={sent!r}  STORED={stored!r}"
    n = min(len(sent), len(stored))
    i = next((k for k in range(n) if sent[k] != stored[k]), n)
    lo, hi = max(0, i - 60), i + 60
    return f"first divergence at index {i}: sent[{sent[lo:hi]!r}] vs stored[{stored[lo:hi]!r}]"


def _report_html_diff(label: str, sent: str, stored: str, results: list) -> None:
    """INFO the server's HTML normalization: identical, or the verbatim/first-divergence diff."""
    if sent == stored:
        results.append((f"{label} (fact-finding)", None, f"{len(sent)} chars, server stored verbatim"))
    else:
        results.append((f"{label} (fact-finding)", None,
                        f"sent {len(sent)} chars, server stored {len(stored)} -- NORMALIZED. "
                        + _diff_window(sent, stored)))


def _check_github_richtext_roundtrip(cfg: dict, parent_id: str, results: list) -> None:
    """Step 22: a live richtext round-trip of a REAL GitHub issue body through the card description --
    the exact translation layer description/comment sync uses (issue #78 fidelity), driven by
    real-world markdown (headings, lists, code fences, links) rather than a synthetic fixture. Reads
    the configured repo's issue #1 body via ghkit (a gh READ -- smoke performs no GitHub WRITES),
    renders it to AgilePlace HTML, writes it, reads it back, then re-derives the sync's HTML from the
    (possibly server-normalized) readback and writes THAT too.

    PASS/FAIL on the CONVERGENCE invariant, not byte-equality: writing the sync's re-derived HTML must
    reach a fixed point -- the second readback equals the first readback OR equals the re-derived HTML
    itself. That is exactly "after the first sync write, later runs see no drift" even when AgilePlace
    normalizes stored HTML (confirmed live 2026-07-24: it does). The sent-vs-stored HTML difference is
    fact-finding INFO. An absent/unreadable/blank issue #1 is an informational SKIP, never a failure."""
    _step(22, "live GitHub issue #1 body -> card description richtext round-trip (issue #78)")
    repo_label = str(cfg.get("target_repo_path") or "the configured repo")
    bodies = ghkit.list_issue_bodies(cfg)
    if bodies is None:
        results.append(("GitHub issue #1 richtext round-trip", None,
                        f"SKIP: could not read issues from {repo_label} (gh read failed)"))
        return
    issue_one = next((i for i in bodies if i.get("number") == 1), None)
    if issue_one is None or not (issue_one.get("body") or "").strip():
        state = "has a blank body" if issue_one is not None else "was not found"
        results.append(("GitHub issue #1 richtext round-trip", None,
                        f"SKIP: {repo_label} issue #1 {state} -- nothing to round-trip"))
        return

    markdown = issue_one["body"]
    print(f"      read {repo_label} issue #1 body ({len(markdown)} chars of markdown)")
    html = richtext.markdown_to_leankit_html(markdown)
    readback = _write_description(cfg, parent_id, html)
    _report_html_diff("issue #1 rendered HTML vs stored", html, readback, results)

    # Convergence: re-derive the sync's HTML from the server-normalized readback and write it back.
    # If the sync's render reaches a fixed point, later runs see no description drift.
    md_back = richtext.leankit_html_to_markdown(readback)
    html2 = richtext.markdown_to_leankit_html(md_back)
    readback2 = _write_description(cfg, parent_id, html2)
    converged = readback2 == readback or readback2 == html2
    results.append(("issue #1 body converges under the sync's richtext layer -- no perpetual drift "
                    "(issue #78)", converged,
                    f"2nd readback {'==' if readback2 == readback else '!='} 1st readback; "
                    f"{'==' if readback2 == html2 else '!='} re-rendered html2 "
                    f"(md_back {len(md_back)} chars)"))


def _check_custom_id_header(cfg: dict, parent_id: str, run_id: str, results: list) -> None:
    """Step 23: header-format customId round-trip (issue #93). The sync now writes customIds like
    '0C1 (GitHub Issue #5)'; this proves the live API accepts and preserves parens, '#', and
    spaces verbatim. The probe value keeps the per-run smoke prefix so stages.header_match_key()
    normalizes a leaked leftover to this run's unique key -- NEVER write a bare 'GitHub Issue #N'
    here (that would normalize to a real unkeyed issue's match key and could be adopted by a
    later sync run). This step must run LAST among the parent-card checks: it overwrites the
    parent card's customId away from the plain per-run prefix that earlier steps still key their
    own lookups against, and only the run's final cleanup (which deletes the card outright) comes
    after it."""
    _step(23, "customId header-format round-trip -- parens/#/spaces must survive verbatim")
    header = f"{PARENT_CUSTOM_ID_PREFIX}{run_id} (GitHub Issue #999999)"
    fresh = agileplace.get_card(cfg, parent_id)
    agileplace.patch_card(cfg, True, fresh, [agileplace.op_custom_id(header)])
    echoed = agileplace.custom_id_value(agileplace.get_card(cfg, parent_id))
    results.append(("customId header-format round-trip", echoed == header,
                    f"read back {echoed!r}"))


def _find_comment(comments: list[dict], comment_id: int) -> dict | None:
    return next((comment for comment in comments if comment.get("id") == comment_id), None)


_COMMENT_TIME_KEY_RE = re.compile(r"(?i)(date|time|modif|edit|updat|stamp|created|changed)")


def _comment_time_like(obj: dict) -> dict:
    """The subset of `obj`'s top-level items whose KEY looks date/time-ish -- values kept so a live
    run reveals the actual timestamp value under whatever name AgilePlace uses."""
    return {key: value for key, value in obj.items()
            if isinstance(key, str) and _COMMENT_TIME_KEY_RE.search(key)}


def _probe_comment_edit_shape(cfg: dict, parent_id: str, comment_id, results: list) -> None:
    """Fact-finding for issue #66. CONFIRMED live 2026-07-24: an AgilePlace comment carries NO edit
    timestamp -- its only keys are cardId/createdBy/createdOn/id/text -- so AP-side drift is detected
    by a body-hash (design amendment; see API-VALIDATION.md + comment_sync). These steps are kept as
    cheap fact-finding: they dump the RAW persisted comment's top-level keys and a fact-finding PUT's
    raw response body (values only for date/time-like keys), so a future AgilePlace version that DID
    add an edit-timestamp field would surface here. Never pass/fail (informational)."""
    raw = agileplace.api(cfg, "GET", agileplace_comments._comment_collection_path(parent_id))
    raw_list = raw.get("comments") if isinstance(raw, dict) else raw if isinstance(raw, list) else []
    raw_comment = next((c for c in (raw_list or []) if str(c.get("id")) == str(comment_id)), None)
    if raw_comment is not None:
        results.append(("RAW comment top-level keys after edit (fact-finding)", None,
                        f"{sorted(raw_comment)}"))
        results.append(("RAW comment date/time-like fields after edit (fact-finding)", None,
                        f"{_comment_time_like(raw_comment)}"))
    put_response = agileplace.mutate(cfg, True, "PUT",
                                     agileplace_comments._comment_item_path(parent_id, comment_id),
                                     body={"text": "<p>SMOKE comment (edit probe)</p>"},
                                     note=f"edit-timestamp fact-find on comment {comment_id}")
    if isinstance(put_response, dict) and put_response:
        results.append(("PUT response top-level keys (fact-finding)", None,
                        f"{sorted(put_response)}"))
        results.append(("PUT response date/time-like fields (fact-finding)", None,
                        f"{_comment_time_like(put_response)}"))


def _check_comments(cfg: dict, parent_id: str, results: list) -> None:
    """Steps 15-18: agileplace_comments' full CRUD surface. Create, then a list-readback (the
    per-comment author/timestamp field names are VALIDATE LIVE per the issue #66 design doc --
    reported informational, not pass/fail, until a real tenant confirms them), an edit whose body
    round-trips (edited-timestamp movement is also informational), and finally the DELETE, whose
    shape is speculative -- the web UI never exposed comment deletion -- so the readback-confirms-
    gone check is a hard PASS/FAIL, mirroring the externalLink-add and tag-add checks above: a 2xx
    response alone is never proof a write actually happened."""
    _step(15, "create a comment via agileplace_comments.create_comment")
    comment = agileplace_comments.create_comment(cfg, True, parent_id, "<p>SMOKE comment</p>")
    comment_id = comment.get("id") if comment else None
    if comment_id is None:
        raise SystemExit(f"comment create response has no usable id ({comment!r}) -- cannot continue")
    print(f"      created comment {comment_id}")
    results.append(("comment create returns a usable id", True, f"id={comment_id}"))

    _step(16, "list comments, confirm the readback shape (author/timestamp fields are fact-finding)")
    comments = agileplace_comments.list_comments(cfg, parent_id)
    found = _find_comment(comments, comment_id)
    results.append(("comment list readback finds the created comment", found is not None,
                    f"{len(comments)} comment(s) now on the card"))
    edited_before = found.get("edited") if found else None
    if found is not None:
        results.append(("comment author-field shape (fact-finding)", None,
                        f"author_name={found['author_name']!r} author_email={found['author_email']!r} "
                        f"author_id={found['author_id']!r}"))
        results.append(("comment created/edited timestamp shape (fact-finding)", None,
                        f"created={found['created']!r} edited={edited_before!r}"))

    _step(17, "edit the comment via agileplace_comments.update_comment (richer HTML)")
    # Richer HTML than a plain <p> so comment-body normalization gets probed (AgilePlace normalizes
    # stored description HTML -- confirmed live 2026-07-24; comment bodies may too). PASS/FAIL is
    # normalization-INSENSITIVE: the stored body canonicalized to Markdown must equal the sent body
    # canonicalized (the same md-level compare description_sync uses), not byte-equality.
    edited_html = ("<p>SMOKE comment (edited) with <strong>bold</strong> and <code>code</code>:</p>"
                   "<ul><li>one</li><li>two</li></ul>")
    agileplace_comments.update_comment(cfg, True, parent_id, comment_id, edited_html)
    comments = agileplace_comments.list_comments(cfg, parent_id)
    found_after_edit = _find_comment(comments, comment_id)
    stored_body = found_after_edit["body"] if found_after_edit else ""
    body_matches = (found_after_edit is not None
                    and richtext.leankit_html_to_markdown(stored_body)
                    == richtext.leankit_html_to_markdown(edited_html))
    results.append(("comment edit round-trip (PUT), normalization-insensitive", body_matches,
                    f"canonical-Markdown match; stored body {stored_body!r}" if found_after_edit
                    else "comment vanished after edit"))
    if found_after_edit is not None:
        _report_html_diff("comment edit HTML vs stored", edited_html, stored_body, results)
    edited_after = found_after_edit.get("edited") if found_after_edit else None
    results.append(("comment edited timestamp moves after PUT (fact-finding)", None,
                    f"before={edited_before!r} after={edited_after!r}"))
    # Live run 2026-07-23 saw edited=None even after the PUT -- dump the raw shape so a re-run finds
    # the actual edit-timestamp field name (if AgilePlace exposes one). See API-VALIDATION.md.
    _probe_comment_edit_shape(cfg, parent_id, comment_id, results)

    _step(18, "delete the comment (speculative shape), then confirm it is gone on readback")
    agileplace_comments.delete_comment(cfg, True, parent_id, comment_id)
    comments = agileplace_comments.list_comments(cfg, parent_id)
    gone = _find_comment(comments, comment_id) is None
    results.append(("comment delete + readback gone (speculative shape)", gone,
                    f"{len(comments)} comment(s) remain"))


def _check_type_id_roundtrip(cfg: dict, parent_id: str, card_type: dict, results: list) -> None:
    """Step 19: card_types.op_type's /typeId replace on the existing parent card, then read the
    nested type back -- the same shape sync_card_type queues for a derived-type drift fix."""
    _step(19, "typeId replace via one versioned PATCH, then read the type back")
    type_id = str(card_type["id"])
    fresh = agileplace.get_card(cfg, parent_id)
    agileplace.patch_card(cfg, True, fresh, [card_types.op_type(type_id)])
    readback = agileplace.get_card(cfg, parent_id)
    nested = readback.get("type") if isinstance(readback.get("type"), dict) else {}
    results.append(("typeId replace round-trip", str(nested.get("id")) == type_id,
                    f"type now {nested!r}"))


def _check_create_with_type_id(cfg: dict, lane_id: str | None, custom_id: str, card_type: dict,
                               created: list[str], results: list) -> None:
    """Step 20: create a card with typeId set, then refetch it. The create response is confirmed
    sparse (no customId/laneId echo, no version -- see API-VALIDATION.md 2026-07-21), so this
    checks the refetch, never the create response itself."""
    _step(20, "create card with typeId, verified via refetch (create response is sparse)")
    type_id = str(card_type["id"])
    type_title = (card_type.get("title") or "").strip()
    card = agileplace.create_card(cfg, True, TYPED_TITLE, custom_id, "", lane_id,
                                  type_id=type_id, type_title=type_title)
    card_id = str(card.get("id") or "")
    if not card_id:
        raise SystemExit(f"typed-create response has no card id ({dict(card)!r}) -- cannot continue")
    created.append(card_id)
    print(f"      created card {card_id}")
    readback = agileplace.get_card(cfg, card_id)
    nested = readback.get("type") if isinstance(readback.get("type"), dict) else {}
    results.append(("create with typeId, confirmed via refetch", str(nested.get("id")) == type_id,
                    f"refetched type {nested!r}"))


def _check_type_id_writes(cfg: dict, lane_id: str | None, parent_id: str, run_id: str,
                          created: list[str], results: list) -> None:
    """Steps 19-20, gated on the board actually having an eligible card type configured: neither
    step is a sync precondition (a board with no card types configured never derives one), so a
    board without one reports both as informational skips rather than failing the run."""
    card_type = _pick_card_type(board_layout.board_layout(cfg).card_types)
    if card_type is None:
        skip_detail = "skipped -- board has no eligible (isCardType) card type configured"
        results.append(("typeId replace round-trip", None, skip_detail))
        results.append(("create with typeId, confirmed via refetch", None, skip_detail))
        return
    _check_type_id_roundtrip(cfg, parent_id, card_type, results)
    _check_create_with_type_id(cfg, lane_id, TYPED_CUSTOM_ID_PREFIX + run_id, card_type,
                               created, results)


def _check_stale_patch(cfg: dict, parent_id: str, stale_version: str, results: list) -> None:
    """Step 21: a stale x-lk-resource-version PATCH must be rejected, not silently applied."""
    _step(21, "deliberately stale-version PATCH (the server MUST reject this)")
    body = [agileplace.op_tag("smoke-stale")]
    print(f"      PATCH /io/card/{parent_id} with stale x-lk-resource-version={stale_version} "
          f"body={json.dumps(body)}")
    try:
        agileplace.api(cfg, "PATCH", f"card/{parent_id}", body=body,
                       headers={"x-lk-resource-version": stale_version})
    except SystemExit as exc:
        status = getattr(exc, "http_status", None)
        _print_http_failure(exc)
        if status in EXPECTED_CONFLICT_CODES:
            print("      (this rejection is the EXPECTED outcome)")
            results.append(("stale-version PATCH rejected (optimistic concurrency)", True,
                            f"HTTP {status}"))
        else:
            results.append(("stale-version PATCH rejected (optimistic concurrency)", False,
                            f"unexpected rejection: {exc}"))
        return
    results.append(("stale-version PATCH rejected (optimistic concurrency)", False,
                    "server ACCEPTED a stale write -- optimistic concurrency is not protecting cards"))


def _confirmed_gone(cfg: dict, card_id: str) -> bool:
    try:
        agileplace.api(cfg, "GET", f"card/{card_id}")
    except SystemExit as exc:
        status = getattr(exc, "http_status", None)
        print(f"      GET card/{card_id} after delete -> HTTP {status}")
        return status == 404
    print(f"      GET card/{card_id} after delete still returns the card")
    return False


def _cleanup(cfg: dict, created: list[str], results: list) -> None:
    if created:
        print(f"\nCLEANUP: deleting throwaway card(s) {', '.join(reversed(created))}")
    for card_id in reversed(created):  # children before parents
        try:
            agileplace.delete_card(cfg, True, card_id)
            results.append((f"delete card {card_id} + 404 after delete",
                            _confirmed_gone(cfg, card_id), ""))
        except SystemExit as exc:
            _print_http_failure(exc)
            results.append((f"delete card {card_id}", False,
                            f"DELETE THIS CARD BY HAND on the board -- {exc}"))


def _run_checks(cfg: dict, lane_id: str | None, run_id: str, created: list[str],
                results: list) -> None:
    parent_id, baseline_version = _check_create_parent(
        cfg, lane_id, PARENT_CUSTOM_ID_PREFIX + run_id, created, results)
    _check_tag_roundtrip(cfg, parent_id, results)
    _check_blocked_and_dates(cfg, parent_id, results)
    child_id = _check_child_and_link(cfg, lane_id, CHILD_CUSTOM_ID_PREFIX + run_id, created, results)
    _check_connections(cfg, parent_id, child_id, results)
    _check_dependencies(cfg, parent_id, child_id, results)
    _check_description(cfg, parent_id, results)
    _check_comments(cfg, parent_id, results)
    _check_type_id_writes(cfg, lane_id, parent_id, run_id, created, results)
    _check_stale_patch(cfg, parent_id, baseline_version, results)
    _check_github_richtext_roundtrip(cfg, parent_id, results)
    _check_custom_id_header(cfg, parent_id, run_id, results)
    _check_issue_graph_batch(cfg, results)


def _check_issue_graph_batch(cfg: dict, results: list) -> None:
    """Step 24: the batched issue-graph read (issue #98) against the live GitHub API -- proves
    this host's GraphQL schema serves the batch's field shapes (comments.databaseId/author/body/
    createdAt/updatedAt, repository-qualified blockedBy, subIssues) and that the batch's
    normalized comments byte-match ghkit.list_issue_comments, the per-issue REST reader the sync
    falls back to (ledger ids must be identical across both paths). GitHub READS only -- smoke
    performs no GitHub writes; no cards involved."""
    _step(24, "batched issue-graph read cross-checks the per-issue comment reader")
    graph = ghkit_snapshot.fetch_issue_graph(cfg)
    if graph is None:
        results.append(("issue-graph batched read (issue #98)", False,
                        "fetch_issue_graph returned None -- GraphQL query failed on this host"))
        return
    results.append(("issue-graph batched read (issue #98)", True,
                    f"{len(graph.comments)} comment snapshot(s), "
                    f"blocked_by {'ok' if graph.blocked_by is not None else 'unusable (None)'}, "
                    f"{len(graph.sub_issues)} sub-issue set(s)"))
    probe = next((n for n in sorted(graph.comments) if graph.comments[n]),
                 min(graph.comments, default=None))
    if probe is None:
        results.append(("issue-graph comments match the per-issue REST reader", None,
                        "SKIP: repo has no issues in the batch to cross-check"))
        return
    rest = ghkit.list_issue_comments(cfg, probe)
    if rest is None:
        results.append(("issue-graph comments match the per-issue REST reader", None,
                        f"SKIP: REST comment read for issue #{probe} failed"))
        return
    results.append(("issue-graph comments match the per-issue REST reader",
                    graph.comments[probe] == rest,
                    f"issue #{probe}: batch {len(graph.comments[probe])} vs REST {len(rest)} "
                    f"comment(s)"))


def _summarize(results: list) -> int:
    print("\n--- smoke summary (cross-check the [live-check] items in API-VALIDATION.md) ---")
    failed = False
    for name, ok, detail in results:
        failed = failed or ok is False  # ok=None is informational and never fails the run
        marker = {True: "PASS", False: "FAIL", None: "INFO"}[ok]
        print(f"{marker}  {name}" + (f" -- {detail}" if detail else ""))
    if failed:
        print("smoke FAILED -- fix the shapes above before trusting a live --apply run")
        return 1
    print("smoke OK -- every exercised write shape behaved as coded")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    cfg = _require_cfg()
    lanes = _preview(cfg)
    if not _confirm(args.yes):
        print("aborted -- nothing was written")
        return 0
    lane = _pick_lane(lanes)
    if lane is not None:
        print(f"\nUsing lane '{board_layout.lane_title(lane)}' ({lane['id']}) for the throwaway cards")
    run_id = secrets.token_hex(3)
    print(f"Throwaway custom-id suffix for this run: {run_id}")
    created: list[str] = []
    results: list[tuple[str, bool, str]] = []
    try:
        _run_checks(cfg, str(lane["id"]) if lane else None, run_id, created, results)
    except SystemExit as exc:
        _print_http_failure(exc)
        results.append(("smoke sequence ran to completion", False, str(exc)))
    finally:
        _cleanup(cfg, created, results)
    return _summarize(results)


if __name__ == "__main__":
    sys.exit(main())
