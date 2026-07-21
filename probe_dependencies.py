"""Read-only discovery probe for the AgilePlace dependencies API (issue #57, Phase 0).

The io v2 public docs do not document the Dependencies feature's endpoints, so this
probe answers one question against a real tenant: is there a readable dependencies
resource, and under which path? It issues GET requests only -- the read-only
invariant is pinned by tests/test_probe_dependencies.py -- so it needs no
confirmation prompt and is safe to run against a production board.

Reads .env exactly like sync.py/smoke.py. Findings belong in API-VALIDATION.md.
Design: docs/superpowers/specs/2026-07-21-blocked-by-dependencies-design.md.

Run: python probe_dependencies.py [--card-id ID]
"""
from __future__ import annotations

import argparse
import urllib.error
import urllib.parse
import urllib.request

import agileplace
from config import env_config

BODY_EXCERPT = 500


def probe_get(cfg: dict, path: str, params: dict | None = None) -> tuple[int, str]:
    """(status, body_text) for one GET. Unlike agileplace.api, every HTTP status is
    data here, never an error -- a probe's 404 is its answer, not a failure."""
    url = f"https://{cfg['host']}/io/{path.lstrip('/')}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method="GET", headers={
        "Authorization": f"Bearer {cfg['token']}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=agileplace.REQUEST_TIMEOUT) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode(errors="replace")


def candidate_probes(card_id: str, board_id: str) -> list[tuple[str, dict | None]]:
    """Candidate (path, params) pairs for a readable dependencies resource."""
    return [
        (f"card/{card_id}/dependencies", None),
        (f"card/{card_id}/dependency", None),
        ("dependencies", {"cardId": card_id}),
        ("dependency", {"cardId": card_id}),
        (f"board/{board_id}/dependencies", None),
        (f"card/{card_id}/connection/dependencies", None),
    ]


def _require_env() -> dict:
    cfg = env_config()
    missing = [env for env, key in (("AGILEPLACE_TOKEN", "token"), ("AGILEPLACE_HOST", "host"),
                                    ("AGILEPLACE_BOARD_ID", "board_id")) if not cfg.get(key)]
    if missing:
        raise SystemExit(f"dependency probe needs {', '.join(missing)} set (.env) -- refusing to run")
    return cfg


def _dump_card_keys(cfg: dict, card_id: str) -> None:
    card = agileplace.get_card(cfg, card_id)
    keys = sorted(card)
    dependish = [k for k in keys if "depend" in k.lower()]
    print(f"card GET top-level keys: {', '.join(keys)}")
    print("dependency-ish keys: "
          + (", ".join(dependish) if dependish else "none -- not embedded in card reads"))


def _check_control(cfg: dict, card_id: str) -> None:
    """A documented, known-good GET. If this fails, credentials/plumbing are broken
    and every candidate MISS below would be meaningless -- stop instead."""
    path = f"card/{card_id}/connection/children"
    status, body = probe_get(cfg, path, {"limit": 1, "offset": 0})
    if status != 200:
        raise SystemExit(f"control endpoint {path} returned HTTP {status} -- probe plumbing or "
                         f"credentials are broken; candidate results would be meaningless\n"
                         f"{body[:BODY_EXCERPT]}")
    print(f"control {path} -> HTTP 200 (plumbing OK)")


def _probe_candidates(cfg: dict, card_id: str) -> list[str]:
    found = []
    for path, params in candidate_probes(card_id, str(cfg["board_id"])):
        shown = path + ("?" + urllib.parse.urlencode(params) if params else "")
        status, body = probe_get(cfg, path, params)
        if status == 200:
            found.append(shown)
            print(f"FOUND {shown} -> HTTP 200")
            print(f"      {body[:BODY_EXCERPT]}")
        elif status == 404:
            print(f"MISS  {shown} -> HTTP 404")
        else:
            print(f"      {shown} -> HTTP {status}")
            print(f"      {body[:BODY_EXCERPT]}")
    return found


def _summarize(found: list[str]) -> None:
    print("\n--- probe summary ---")
    if found:
        print("readable dependency endpoint(s) found:")
        for endpoint in found:
            print(f"  {endpoint}")
        print("record the response shapes in API-VALIDATION.md; next step is the write probe "
              "(issue #57, Phase 0b)")
    else:
        print("no readable dependency endpoint found among the candidates.")
        print("fallback: open browser devtools (Network tab), create ONE dependency between two")
        print("cards in the AgilePlace UI, and capture the request(s) -- method, URL, JSON body")
        print("('Copy as cURL' is ideal). Record them in API-VALIDATION.md (issue #57).")


def _first_card_id(cfg: dict) -> str:
    cards = agileplace.list_cards(cfg)
    if not cards:
        raise SystemExit("board has no cards to probe against -- pass --card-id")
    return str(cards[0]["id"])


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only probe for the undocumented AgilePlace dependencies API (issue #57)")
    parser.add_argument("--card-id", help="probe against this card instead of the board's first card")
    args = parser.parse_args()
    cfg = _require_env()

    card_id = args.card_id or _first_card_id(cfg)
    print(f"Probing dependencies read surface with card {card_id} on board {cfg['board_id']}")
    _dump_card_keys(cfg, card_id)
    _check_control(cfg, card_id)
    _summarize(_probe_candidates(cfg, card_id))


if __name__ == "__main__":
    main()
