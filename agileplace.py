"""AgilePlace (LeanKit) io v2 client for the ongoing sync. Stdlib only.

Auth: AGILEPLACE_TOKEN (Bearer). Tokens have NO scopes -- never commit or log one. Cards match GitHub
issues by external-link URL (customId fallback). Lanes resolve to a stage by TITLE among LEAF lanes,
failing closed when ambiguous. ALL mutations to one card are batched into a single versioned JSON-Patch
(op-builders + patch_card) so the resource version can't go stale mid-run (optimistic concurrency).
patch_card never sends an unversioned PATCH: a card missing `version` is refetched once first
(_card_with_version); if the refetch is also version-less, the PATCH is skipped and a WARN is
printed instead of risking a silent stale overwrite (issue #8).
API shapes marked "VALIDATE LIVE" follow current Planview docs but are confirmed at first live run.
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

REQUEST_TIMEOUT = 30      # seconds per request
MAX_RETRY_SLEEP = 60      # cap a hostile/large Retry-After so a run can't stall for hours


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
        secs = float(header)
    except ValueError:
        try:
            secs = (parsedate_to_datetime(header) - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError):
            secs = 5.0
    return min(MAX_RETRY_SLEEP, max(1.0, secs))


def mutate(cfg: dict, apply: bool, method: str, path: str, body=None, headers=None, *, note: str = ""):
    """The single write gate. Dry mode prints the request instead of sending it."""
    if apply:
        return api(cfg, method, path, body=body, headers=headers)
    print(f"DRY   {method} /io/{path} {note} body={json.dumps(body)[:200]}")
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
    """Only leaf lanes hold cards; parent/container lanes must never be a move target."""
    parent_ids = {l.get("parentLaneId") for l in lanes if l.get("parentLaneId")}
    return [l for l in lanes if l["id"] not in parent_ids]


def resolve_lane_for_stage(lanes: list, stage: str, release: str, stage_map: dict | None = None, *,
                            quiet: bool = False):
    """(target_lane_or_None, acceptable_lane_ids). STAGE_LANE_MAP wins (first = target, all = in-stage);
    else infer by lane title then cardStatus, failing CLOSED on ambiguity. Leaf lanes only.

    quiet=True suppresses the STAGE_LANE_MAP-misconfiguration WARN -- for callers that evaluate this
    purely as an internal membership check (not the actual, decisive lane-move call), so one
    misconfiguration doesn't print a duplicate WARN per such check."""
    leaves = _leaf_lanes(lanes)
    by_id = {l["id"]: l for l in lanes}

    if stage_map and stage in stage_map:
        by_title = {}
        for l in leaves:
            by_title.setdefault(lane_title(l).lower(), []).append(l)  # index to lists, not last-wins
        ordered, seen = [], set()
        for wanted in stage_map[stage]:
            for lane in by_title.get(wanted.strip().lower(), []):
                if lane["id"] not in seen:
                    seen.add(lane["id"])
                    ordered.append(lane)
        if ordered:
            return ordered[0], {l["id"] for l in ordered}
        if not quiet:
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


# --- cards (reads) --------------------------------------------------------

def list_cards(cfg: dict) -> list[dict]:
    """All cards on the board, paginated to exhaustion. Requests childCards so connection reconciliation
    can see the existing hierarchy (VALIDATE LIVE: `include` param name + payload shape)."""
    cards, offset, limit = [], 0, 200
    while True:
        data = api(cfg, "GET", "card", params={"board": cfg["board_id"], "limit": limit,
                                                "offset": offset, "include": "childCards"})
        page = data.get("cards", [])
        cards.extend(page)
        total = (data.get("pageMeta") or {}).get("totalRecords")
        offset += limit
        if not page or (total is not None and offset >= total) or len(page) < limit:
            break
    return cards


def get_card(cfg: dict, card_id: str) -> dict:
    """Fetch a single card fresh (GET /io/card/{id}). VALIDATE LIVE: docs don't confirm whether the
    single-card GET wraps the payload as {"card": {...}} (like list_cards' {"cards": [...]}) or
    returns the card fields flat -- defensively unwrap either shape so callers always get a flat
    card dict. See API-VALIDATION.md.
    A 200 response can still carry {"card": null} (e.g. a race with a delete), or even a bare
    top-level null -- both must fail loud here rather than handing callers a bare None that
    crashes downstream with an opaque AttributeError (see issue #8 review finding)."""
    data = api(cfg, "GET", f"card/{card_id}")
    if data is None:
        raise SystemExit(f"AgilePlace GET card/{card_id} returned no card data (got null)")
    card = data.get("card", data)
    if card is None:
        raise SystemExit(f"AgilePlace GET card/{card_id} returned no card data (got {{'card': null}})")
    return card


def card_external_urls(card: dict) -> list[str]:
    links = card.get("externalLinks") or ([card["externalLink"]] if card.get("externalLink") else [])
    return [(l or {}).get("url", "") for l in links if l]


def custom_id_value(card: dict) -> str:
    cid = card.get("customId")
    if isinstance(cid, dict):
        cid = cid.get("value")
    return (cid or "").strip()


def card_tags(card: dict) -> set[str]:
    return {t for t in (card.get("tags") or []) if t}


def card_is_blocked(card: dict) -> bool:
    return bool((card.get("blockedStatus") or {}).get("isBlocked"))


def card_block_reason(card: dict) -> str:
    return (card.get("blockedStatus") or {}).get("reason") or ""


def card_child_ids(card: dict) -> set[str]:
    """Existing child-card ids on a parent (from the `childCards` include). VALIDATE LIVE: field/shape."""
    kids = card.get("childCards") or card.get("connectedCards") or []
    return {str(c.get("id") or c.get("cardId")) for c in kids
            if isinstance(c, dict) and (c.get("id") or c.get("cardId"))}


# --- card mutations: op-builders + one versioned PATCH per card ------------

def op_lane(lane_id: str) -> dict:
    return {"op": "replace", "path": "/laneId", "value": lane_id}


def op_tag(tag: str, *, add: bool) -> dict:
    # add appends at /tags/-; remove is by value at /tags (VALIDATE LIVE).
    return {"op": "add", "path": "/tags/-", "value": tag} if add else {"op": "remove", "path": "/tags", "value": tag}


def op_planned_date(field: str, date: str | None) -> dict:
    return {"op": "replace", "path": f"/{field}", "value": date}  # field = plannedStart|plannedFinish


def ops_blocked(blocked: bool, reason: str | None) -> list[dict]:
    # Writable paths are flat (/isBlocked, /blockReason) -- /blockedStatus/* is the nested READ shape
    # only (see card_is_blocked/card_block_reason above). See issue #2.
    # An unblocked card carries no reason: force /blockReason to "" whenever blocked is false, so the
    # patch can never emit the self-contradictory isBlocked=False + non-empty reason.
    blocked = bool(blocked)
    return [{"op": "replace", "path": "/isBlocked", "value": blocked},
            {"op": "add", "path": "/blockReason", "value": (reason or "") if blocked else ""}]


def patch_card(cfg: dict, apply: bool, card: dict, ops: list[dict], note: str = "") -> dict:
    """Send ONE JSON Patch for a card with its resource version (optimistic concurrency). Batching every
    op for a card into a single PATCH is what prevents the version going stale between writes.
    Never sends a PATCH without a resource version: if `card` arrives without one, a fresh refetch is
    attempted first; if that refetch also has no version, the PATCH is skipped entirely (see
    _card_with_version)."""
    if not ops:
        return {}
    versioned = _card_with_version(cfg, apply, card)
    if versioned is None:
        return {}
    return mutate(cfg, apply, "PATCH", f"card/{versioned['id']}", body=ops,
                  headers=_version_headers(versioned), note=note or f"patch card {versioned['id']} ({len(ops)} ops)")


def _has_usable_version(version) -> bool:
    """A resource version is usable when it is present AND non-empty. `None` is the ordinary
    "missing" case; a present-but-empty/whitespace string counts as missing too (some card payloads
    can carry version="" ), since either would otherwise produce a blank x-lk-resource-version header.
    An int/str 0 is a legitimate version number and must stay usable."""
    if version is None:
        return False
    if isinstance(version, str) and version.strip() == "":
        return False
    return True


def _card_with_version(cfg: dict, apply: bool, card: dict) -> dict | None:
    """Guarantee `card` carries a usable resource version before patch_card is allowed to PATCH it.
    Returns `card` unchanged (zero network calls) when a usable version is already present or apply
    is False -- dry runs never trigger a refetch. Otherwise refetches the card fresh: on success
    returns a NEW dict (never mutates `card`) with the refetched version filled in; if the refetch
    also has no usable version, returns None and prints one WARN naming the card so the caller can
    refuse to send an unversioned PATCH instead of risking a silent stale overwrite."""
    if _has_usable_version(card.get("version")) or not apply:
        return card
    fresh = get_card(cfg, card["id"])
    if not _has_usable_version(fresh.get("version")):
        print(f"WARN  card {card['id']} has no resource version after refetch -- "
              f"refusing unversioned PATCH, skipping ops")
        return None
    return {**card, "version": fresh["version"]}


def _version_headers(card: dict) -> dict:
    v = card.get("version")
    return {"x-lk-resource-version": str(v)} if _has_usable_version(v) else {}


def create_card(cfg: dict, apply: bool, title: str, custom_id: str, external_url: str, lane_id: str | None):
    """Create a card (POST /io/card). Returns the new card dict (with id) on --apply, else {}."""
    body = {"boardId": cfg["board_id"], "title": title, "customId": custom_id}
    if lane_id:
        body["laneId"] = lane_id
    if external_url:
        body["externalLink"] = {"label": f"GitHub {custom_id}", "url": external_url}
    return mutate(cfg, apply, "POST", "card", body=body, note=f"create card {custom_id}")


# --- parent/child connections (hierarchy) -- VALIDATE LIVE -------------------

def connect_children(cfg: dict, apply: bool, parent_card_id: str, child_card_ids: list[str]) -> None:
    """Connect a parent card to child cards. Documented io v2 shape:
    POST /io/card/connections {cardIds:[parent], connections:{children:[...]}}. VALIDATE LIVE."""
    ids = [c for c in child_card_ids if c]
    if not ids:
        return
    mutate(cfg, apply, "POST", "card/connections",
           body={"cardIds": [parent_card_id], "connections": {"children": ids}},
           note=f"connect {parent_card_id} -> {len(ids)} child card(s)")


def disconnect_children(cfg: dict, apply: bool, parent_card_id: str, child_card_ids: list[str]) -> None:
    """Remove parent->child connections (so the hierarchy equals the GitHub graph). VALIDATE LIVE."""
    ids = [c for c in child_card_ids if c]
    if not ids:
        return
    mutate(cfg, apply, "DELETE", "card/connections",
           body={"cardIds": [parent_card_id], "connections": {"children": ids}},
           note=f"disconnect {parent_card_id} -> {len(ids)} child card(s)")
