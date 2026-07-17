"""AgilePlace (LeanKit) io v2 client for the ongoing sync. Stdlib only.

Auth: AGILEPLACE_TOKEN (Bearer). Tokens have NO scopes -- never commit or log one. Cards are matched to
GitHub epics by their external-link URL; lanes are resolved to a stage by TITLE among LEAF lanes
(LeanKit cardStatus has only 3 values, so In progress / In review share 'started'), failing closed when
ambiguous. Card writes carry the card version (optimistic concurrency). Tag add/remove use io v2
JSON-patch and are validated at first live run.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from stages import STAGE_CARD_STATUS, lane_matches_stage

REQUEST_TIMEOUT = 30  # seconds; bounds every AgilePlace call


def api(cfg: dict, method: str, path: str, body=None, params=None, headers=None, _attempt=0):
    if not cfg.get("token"):
        raise SystemExit(f"BUG: API call ({method} /{path}) without a token -- offline mode must not reach the API")
    if not cfg.get("host"):
        raise SystemExit("AGILEPLACE_HOST is not set (.env) -- required for API calls")
    url = f"https://{cfg['host']}/io/{path.lstrip('/')}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {cfg['token']}", "Content-Type": "application/json",
                 "Accept": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as err:
        if err.code == 429 and _attempt < 3:
            time.sleep(_retry_after_seconds(err))
            return api(cfg, method, path, body, params, headers, _attempt + 1)
        detail = err.read().decode(errors="replace")[:300]
        raise SystemExit(f"AgilePlace {method} /{path} failed: HTTP {err.code} {detail}") from err
    except urllib.error.URLError as err:  # DNS / refused / TLS / timeout -- no writes performed
        raise SystemExit(f"AgilePlace {method} /{path} unreachable: {err.reason}") from err


def _retry_after_seconds(err) -> float:
    header = (err.headers.get("Retry-After") or "5").strip()
    try:
        return max(1.0, float(header))
    except ValueError:
        try:
            when = parsedate_to_datetime(header)
            return max(1.0, (when - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            return 5.0


def mutate(cfg: dict, apply: bool, method: str, path: str, body=None, headers=None, *, note: str = ""):
    """The single write gate. Dry mode prints the request instead of sending it."""
    if apply:
        return api(cfg, method, path, body=body, headers=headers)
    print(f"DRY   {method} /io/{path} {note} body={json.dumps(body)[:160]}")
    return {}


# --- board / lanes --------------------------------------------------------

def board_layout(cfg: dict) -> list:
    return api(cfg, "GET", f"board/{cfg['board_id']}").get("lanes", [])


def lane_title(lane: dict) -> str:
    return (lane.get("title") or lane.get("name") or "").strip()


def _ancestor_titles(lane: dict, by_id: dict) -> list[str]:
    titles, parent = [], lane.get("parentLaneId")
    while parent and parent in by_id:
        titles.append(lane_title(by_id[parent]))
        parent = by_id[parent].get("parentLaneId")
    return titles


def _leaf_lanes(lanes: list) -> list:
    """Only leaf lanes hold cards; parent/container lanes must never be chosen as a move target."""
    parent_ids = {l.get("parentLaneId") for l in lanes if l.get("parentLaneId")}
    return [l for l in lanes if l["id"] not in parent_ids]


def resolve_lane_for_stage(lanes: list, stage: str, release: str, stage_map: dict | None = None):
    """Resolve a stage to (target_lane_or_None, acceptable_lane_ids).

    STAGE_LANE_MAP wins when it names lanes for the stage: the first listed lane is the move target and
    ALL listed lanes are 'already in that stage' (so a card manually moved between equivalent lanes --
    e.g. New Requests <-> Approved, both Backlog -- is left alone). Otherwise infer by lane title, then
    cardStatus, failing CLOSED (None) on ambiguity rather than guessing a wrong lane. Only leaf lanes
    (which hold cards) are ever chosen.
    """
    leaves = _leaf_lanes(lanes)
    by_id = {l["id"]: l for l in lanes}

    if stage_map and stage in stage_map:
        by_title = {lane_title(l).lower(): l for l in leaves}
        ordered, seen = [], set()
        for wanted in stage_map[stage]:
            lane = by_title.get(wanted.strip().lower())
            if lane and lane["id"] not in seen:
                seen.add(lane["id"])
                ordered.append(lane)
        if ordered:
            return ordered[0], {l["id"] for l in ordered}
        print(f"WARN  STAGE_LANE_MAP lists {stage_map[stage]} for '{stage}' but none match a leaf lane -- inferring")

    cands = [l for l in leaves if lane_matches_stage(lane_title(l), stage)]
    if not cands:
        cands = [l for l in leaves if l.get("cardStatus") == STAGE_CARD_STATUS[stage]]
    if len(cands) == 1:
        return cands[0], {cands[0]["id"]}
    if release and len(cands) > 1:
        in_release = [l for l in cands
                      if any(release.lower() in t.lower() for t in _ancestor_titles(l, by_id))]
        if len(in_release) == 1:
            return in_release[0], {in_release[0]["id"]}
    return None, set()  # none, or still ambiguous -> don't move


# --- cards ----------------------------------------------------------------

def list_cards(cfg: dict) -> list[dict]:
    """All cards on the board, paginated to exhaustion (io v2 returns pageMeta.totalRecords)."""
    cards, offset, limit = [], 0, 200
    while True:
        data = api(cfg, "GET", "card", params={"board": cfg["board_id"], "limit": limit, "offset": offset})
        page = data.get("cards", [])
        cards.extend(page)
        total = (data.get("pageMeta") or {}).get("totalRecords")
        offset += limit
        if not page or (total is not None and offset >= total) or len(page) < limit:
            break
    return cards


def card_external_urls(card: dict) -> list[str]:
    links = card.get("externalLinks") or ([card["externalLink"]] if card.get("externalLink") else [])
    return [(l or {}).get("url", "") for l in links if l]


def custom_id_value(card: dict) -> str:
    cid = card.get("customId")
    if isinstance(cid, dict):
        cid = cid.get("value")
    return (cid or "").strip()


def find_card(cards: list[dict], epic_url: str, epic_key: str) -> dict | None:
    """Match by external-link URL first (robust, key-independent), then by customId == epic key."""
    if epic_url:
        for card in cards:
            if epic_url in card_external_urls(card):
                return card
    if epic_key:
        for card in cards:
            if custom_id_value(card) == epic_key:
                return card
    return None


def card_tags(card: dict) -> set[str]:
    return {t for t in (card.get("tags") or []) if t}


def _version_headers(card: dict) -> dict:
    v = card.get("version")
    return {"x-lk-resource-version": str(v)} if v is not None else {}


def move_card(cfg: dict, apply: bool, card: dict, lane_id: str) -> None:
    mutate(cfg, apply, "PATCH", f"card/{card['id']}",
           body=[{"op": "replace", "path": "/laneId", "value": lane_id}],
           headers=_version_headers(card), note=f"move card {card['id']}")


def edit_tag(cfg: dict, apply: bool, card: dict, tag: str, *, add: bool) -> None:
    """Add or remove one card tag (io v2 JSON-patch). Add appends at /tags/-, remove is by value at
    /tags -- validated at first live run."""
    op = {"op": "add", "path": "/tags/-", "value": tag} if add else {"op": "remove", "path": "/tags", "value": tag}
    mutate(cfg, apply, "PATCH", f"card/{card['id']}", body=[op],
           headers=_version_headers(card), note=f"{'add' if add else 'remove'} tag {tag}")
