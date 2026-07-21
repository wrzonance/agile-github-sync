"""One-shot live smoke test for the AgilePlace write path. Stdlib only.

Reads .env exactly like sync.py, previews the target board (title, lanes, existing cards), and only
after an explicit confirmation creates two clearly-marked throwaway cards, exercises every write
shape the sync uses -- card create, versioned PATCH tag add/remove, externalLink add on a bare card,
connect/disconnect children, and a deliberately stale-version PATCH that MUST be rejected -- then
deletes both cards and confirms they are gone. The steps mirror the ``[live-check]`` items in
API-VALIDATION.md so one confirmed run retires them.

GitHub is never touched (dry run already exercises every gh read live), and the sync state file is
never read or written. Every server rejection is printed with its full, untruncated response body so
an incorrect write shape is diagnosable straight from the console.
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys

import agileplace
from config import env_config

PARENT_TITLE = "SMOKE parent (safe to delete)"
CHILD_TITLE = "SMOKE child (safe to delete)"
# Custom ids carry a fresh per-run suffix: the sync's _matching_card falls back to customId
# matching, so a fixed id on a leftover card could be adopted by a concurrent or later sync run
# (and two smoke runs would collide on the board). See PR #51 review.
PARENT_CUSTOM_ID_PREFIX = "SMOKE-P-"
CHILD_CUSTOM_ID_PREFIX = "SMOKE-C-"
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
        print(f"  lane '{agileplace.lane_title(lane)}' ({lane['id']}){drop}")
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
    """Steps 5-6: blocked-state and planned-date writes -- the sync's remaining PATCH shapes."""
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


def _check_stale_patch(cfg: dict, parent_id: str, stale_version: str, results: list) -> None:
    """Step 9: a stale x-lk-resource-version PATCH must be rejected, not silently applied."""
    _step(11, "deliberately stale-version PATCH (the server MUST reject this)")
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
    _check_stale_patch(cfg, parent_id, baseline_version, results)


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
        print(f"\nUsing lane '{agileplace.lane_title(lane)}' ({lane['id']}) for the throwaway cards")
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
