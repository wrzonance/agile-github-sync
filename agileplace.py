"""AgilePlace (LeanKit) io v2 client for the ongoing sync. Stdlib only.

Auth: AGILEPLACE_TOKEN (Bearer). Tokens have NO scopes -- never commit or log one. Cards match GitHub
issues by external-link URL (customId fallback). Lanes resolve to a stage by TITLE among LEAF lanes,
failing closed when ambiguous. Field updates queued for an existing card are batched into one
versioned JSON-Patch (op-builders + patch_card) so the resource version can't go stale mid-run
(optimistic concurrency); card creation and hierarchy connections use separate POST/DELETE requests.
patch_card never sends an unversioned PATCH: a card missing `version` is refetched once first
(_card_with_version); if the refetch is also version-less or a queued field changed concurrently,
the PATCH and run abort instead of risking a silent stale overwrite or advancing sync state.
API shapes marked "VALIDATE LIVE" follow current Planview docs but are confirmed at first live run.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from types import MappingProxyType

from stages import STAGES, STAGE_CARD_STATUS, lane_matches_stage, title_contains_phrase

REQUEST_TIMEOUT = 30      # seconds per request
MAX_RETRY_SLEEP = 60      # cap a hostile/large Retry-After so a run can't stall for hours
MAX_CARD_PAGE_REQUESTS = 1_000  # absolute guard against absent/hostile pagination metadata
PLANNED_CARD_ID_PREFIX = "planned-card:"


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
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        detail = raw.decode(errors="replace")[:300]
        raise SystemExit(
            f"AgilePlace {method} /{path} failed: invalid JSON response {detail}"
        ) from err
    except urllib.error.HTTPError as err:
        if err.code == 429 and _attempt < 3:
            time.sleep(_retry_after_seconds(err))
            return api(cfg, method, path, body, params, headers, _attempt + 1)
        detail = err.read().decode(errors="replace")
        exc = SystemExit(f"AgilePlace {method} /{path} failed: HTTP {err.code} {detail[:300]}")
        exc.http_status = err.code  # full, untruncated server response for verbose reporters
        exc.http_body = detail
        raise exc from err
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
    print(f"DRY   {method} /io/{path} {note} body={json.dumps(body)}")
    return {}


# --- board / lanes --------------------------------------------------------

def lane_title(lane: dict) -> str:
    return (lane.get("title") or lane.get("name") or "").strip()


def _lanes_with_ids(lanes: list) -> list[dict]:
    valid = []
    for lane in lanes:
        if not isinstance(lane, dict):
            print(f"WARN  lane <{type(lane).__name__}> is not an object -- skipping malformed lane")
            continue
        malformed_text = next(
            (field for field in ("title", "name")
             if lane.get(field) and not isinstance(lane[field], str)),
            None,
        )
        if malformed_text:
            value = lane[malformed_text]
            print(f"WARN  lane id {lane.get('id', '<unknown>')!r} has non-string {malformed_text} "
                  f"({type(value).__name__}) -- skipping malformed lane")
            continue
        if "id" not in lane or lane["id"] is None:
            print(f"WARN  lane '{lane_title(lane) or '<untitled>'}' has no id -- skipping malformed lane")
            continue
        try:
            hash(lane["id"])
        except TypeError:
            print(f"WARN  lane '{lane_title(lane) or '<untitled>'}' has unhashable id "
                  f"({type(lane['id']).__name__}) -- skipping malformed lane")
            continue
        valid.append(lane)
    return valid


def board_layout(cfg: dict) -> list:
    return _lanes_with_ids(api(cfg, "GET", f"board/{cfg['board_id']}").get("lanes", []))


def _ancestor_titles(lane: dict, by_id: dict) -> list[str]:
    titles, parent = [], lane.get("parentLaneId")
    while parent and parent in by_id:
        titles.append(lane_title(by_id[parent]))
        parent = by_id[parent].get("parentLaneId")
    return titles


def _leaf_lanes(lanes: list) -> list:
    """Only leaf lanes hold cards; parent/container lanes must never be a move target."""
    lanes = _lanes_with_ids(lanes)
    parent_ids = {l.get("parentLaneId") for l in lanes if l.get("parentLaneId")}
    return [l for l in lanes if l["id"] not in parent_ids]


def _release_lane(candidates: list[dict], release: str, by_id: dict) -> dict | None:
    """Resolve duplicate candidates to exactly one lane under the requested release ancestor."""
    if len(candidates) == 1:
        return candidates[0]
    if not release:
        return None
    matches = [lane for lane in candidates
               if any(title_contains_phrase(title, release)
                      for title in _ancestor_titles(lane, by_id))]
    return matches[0] if len(matches) == 1 else None


def _mapped_lanes(leaves: list[dict], stage_titles: list[str], release: str,
                  by_id: dict) -> list[dict] | None:
    """Resolve configured titles in order; None means a duplicate title stayed ambiguous."""
    by_title = {}
    for lane in leaves:
        by_title.setdefault(lane_title(lane).lower(), []).append(lane)
    ordered, seen = [], set()
    for wanted in stage_titles:
        matches = by_title.get(wanted.strip().lower(), [])
        selected = _release_lane(matches, release, by_id) if matches else None
        if len(matches) > 1 and selected is None:
            return None
        if selected and selected["id"] not in seen:
            seen.add(selected["id"])
            ordered.append(selected)
    return ordered


def resolve_lane_for_stage(lanes: list, stage: str, release: str, stage_map: dict | None = None, *,
                            quiet: bool = False):
    """(target_lane_or_None, acceptable_lane_ids). STAGE_LANE_MAP wins (first = target, all = in-stage),
    with duplicate titles resolved by release ancestor; else infer by lane title then non-conflicting
    cardStatus, failing CLOSED on ambiguity. Leaf lanes only.

    quiet=True suppresses the STAGE_LANE_MAP-misconfiguration WARN -- for callers that evaluate this
    purely as an internal membership check (not the actual, decisive lane-move call), so one
    misconfiguration doesn't print a duplicate WARN per such check."""
    lanes = _lanes_with_ids(lanes)
    leaves = _leaf_lanes(lanes)
    by_id = {l["id"]: l for l in lanes}

    if stage_map and stage in stage_map:
        ordered = _mapped_lanes(leaves, stage_map[stage], release, by_id)
        if ordered is None:
            return None, set()
        if ordered:
            return ordered[0], {lane["id"] for lane in ordered}
        if not quiet:
            print(f"WARN  STAGE_LANE_MAP lists {stage_map[stage]} for '{stage}' but none match a leaf lane -- inferring")

    cands = [lane for lane in leaves if lane_matches_stage(lane_title(lane), stage)]
    if not cands:
        cands = [
            lane for lane in leaves
            if lane.get("cardStatus") == STAGE_CARD_STATUS[stage]
            and not any(other != stage and lane_matches_stage(lane_title(lane), other)
                        for other in STAGES)
        ]
    if len(cands) == 1:
        return cands[0], {cands[0]["id"]}
    selected = _release_lane(cands, release, by_id)
    if selected:
        return selected, {selected["id"]}
    return None, set()  # none, or still ambiguous -> don't move


# --- cards (reads) --------------------------------------------------------

def _raise_if_before_total(offset: int, expected_total: int | None) -> None:
    if expected_total is not None and offset < expected_total:
        raise SystemExit(
            f"AgilePlace card pagination ended at {offset} before totalRecords {expected_total} "
            "-- refusing to continue with a partial board snapshot"
        )


def _card_path(card_id) -> str:
    return f"card/{urllib.parse.quote(str(card_id), safe='')}"


def list_cards(cfg: dict) -> list[dict]:
    """All cards on the board, paginated to exhaustion.

    AgilePlace may clamp the requested page size, so offsets advance by the number of cards actually
    returned and short-page detection uses the response's effective limit. Pagination fails closed
    after MAX_CARD_PAGE_REQUESTS rather than returning a partial card set to reconciliation."""
    cards, offset, limit = [], 0, 200
    effective_limit = None
    retained_total = None
    trust_total = True
    for _request_count in range(1, MAX_CARD_PAGE_REQUESTS + 1):
        data = api(cfg, "GET", "card", params={"board": cfg["board_id"], "limit": limit,
                                                "offset": offset})
        page = data.get("cards", [])
        cards.extend(page)
        page_meta = data.get("pageMeta")
        page_meta = page_meta if isinstance(page_meta, dict) else {}
        next_offset = offset + len(page)
        total_before_page = retained_total
        if trust_total and retained_total is not None and retained_total < next_offset:
            retained_total = None
            trust_total = False
        if trust_total and "totalRecords" in page_meta:
            total = page_meta["totalRecords"]
            valid_total = (isinstance(total, int) and not isinstance(total, bool)
                           and total >= next_offset)
            if valid_total and retained_total is None:
                retained_total = total
            elif retained_total is not None and (not valid_total or total != retained_total):
                retained_total = None
                trust_total = False
        server_limit = page_meta.get("limit")
        if (isinstance(server_limit, int) and not isinstance(server_limit, bool)
                and server_limit > 0):
            effective_limit = min(limit, server_limit)
        offset = next_offset
        if not page:
            expected_total = total_before_page if total_before_page is not None else retained_total
            _raise_if_before_total(offset, expected_total)
            return cards
        if retained_total is not None and offset >= retained_total:
            return cards
        if effective_limit is not None and len(page) < effective_limit:
            _raise_if_before_total(offset, retained_total)
            return cards
    raise SystemExit(
        f"AgilePlace card pagination exceeded defensive limit of {MAX_CARD_PAGE_REQUESTS} requests "
        f"after receiving {len(cards)} cards -- refusing to continue with a partial board snapshot"
    )


def get_card(cfg: dict, card_id: str) -> dict:
    """Fetch a single card fresh (GET /io/card/{id}). VALIDATE LIVE: docs don't confirm whether the
    single-card GET wraps the payload as {"card": {...}} (like list_cards' {"cards": [...]}) or
    returns the card fields flat -- defensively unwrap either shape so callers always get a flat
    card dict. See API-VALIDATION.md.
    A 200 response can still carry {"card": null} (e.g. a race with a delete), or even a bare
    top-level null -- both must fail loud here rather than handing callers a bare None that
    crashes downstream with an opaque AttributeError (see issue #8 review finding).
    The exact shape being unconfirmed also means a non-dict, non-null body (a bare list, string,
    number, or bool) is plausible -- that must fail loud too, rather than reaching `.get()` and
    raising an opaque AttributeError (see issue #3 review finding)."""
    data = api(cfg, "GET", _card_path(card_id))
    if data is None:
        raise SystemExit(f"AgilePlace GET card/{card_id} returned no card data (got null)")
    if not isinstance(data, dict):
        raise SystemExit(
            f"AgilePlace GET card/{card_id} returned unexpected JSON type "
            f"({type(data).__name__}, expected an object)"
        )
    card = data.get("card", data)
    if card is None:
        raise SystemExit(f"AgilePlace GET card/{card_id} returned no card data (got {{'card': null}})")
    if not isinstance(card, dict):
        raise SystemExit(
            f"AgilePlace GET card/{card_id} returned unexpected card JSON type "
            f"({type(card).__name__}, expected an object)"
        )
    return card


def _warn_card_field(card: dict, detail: str) -> None:
    print(f"WARN  card {card.get('id', '<unknown>')} {detail} -- skipping malformed value")


def card_external_urls(card: dict) -> list[str]:
    if "externalLinks" in card:
        links = card["externalLinks"]
        if not isinstance(links, list):
            _warn_card_field(card, f"has non-array externalLinks ({type(links).__name__})")
            return []
    else:
        link = card.get("externalLink")
        links = [link] if link else []

    urls = []
    for link in links:
        if not isinstance(link, dict):
            _warn_card_field(card, f"has non-object external link ({type(link).__name__})")
            continue
        if link:
            url = link.get("url", "")
            if not isinstance(url, str):
                _warn_card_field(card, f"has non-string external link URL ({type(url).__name__})")
                continue
            urls.append(url)
    return urls


def custom_id_value(card: dict) -> str:
    cid = card.get("customId")
    if isinstance(cid, dict):
        cid = cid.get("value")
    return (cid or "").strip()


def card_tags(card: dict) -> set[str]:
    tags = card.get("tags", [])
    if not isinstance(tags, list):
        _warn_card_field(card, f"has non-array tags ({type(tags).__name__})")
        return set()
    valid = set()
    for tag in tags:
        if not isinstance(tag, str):
            _warn_card_field(card, f"has non-string tag ({type(tag).__name__})")
            continue
        if tag:
            valid.add(tag)
    return valid


def _blocked_status(card: dict) -> dict:
    status = card.get("blockedStatus", {})
    if not isinstance(status, dict):
        _warn_card_field(card, f"has non-object blockedStatus ({type(status).__name__})")
        return {}
    return status


def card_is_blocked(card: dict) -> bool:
    return bool(_blocked_status(card).get("isBlocked"))


def card_block_reason(card: dict) -> str:
    return _blocked_status(card).get("reason") or ""


def _parse_child_page(data, expected_offset: int, requested_limit: int) -> tuple[tuple[str, ...], int, int]:
    """Validate one documented ``connection/children`` response page."""
    if data is None:
        raise ValueError("response is null")
    if not isinstance(data, dict):
        raise ValueError(f"response is {type(data).__name__}, expected object")
    if "cards" not in data:
        raise ValueError("response is missing cards")
    cards = data["cards"]
    page_meta = data.get("pageMeta")
    if not isinstance(cards, list):
        raise ValueError(f"cards is {type(cards).__name__}, expected array")
    if not isinstance(page_meta, dict):
        raise ValueError(f"pageMeta is {type(page_meta).__name__}, expected object")

    total = page_meta.get("totalRecords")
    offset = page_meta.get("offset")
    limit = page_meta.get("limit")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise ValueError(f"invalid totalRecords {total!r}")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset != expected_offset:
        raise ValueError(f"invalid offset {offset!r}, expected {expected_offset}")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError(f"invalid limit {limit!r}")
    effective_limit = min(requested_limit, limit)
    if len(cards) > effective_limit:
        raise ValueError(f"page contains {len(cards)} cards but effective limit is {effective_limit}")
    if expected_offset + len(cards) > total:
        raise ValueError(f"page ends after totalRecords {total}")

    child_ids = []
    for card in cards:
        card_id = card.get("id") if isinstance(card, dict) else None
        if (not isinstance(card_id, (str, int)) or isinstance(card_id, bool)
                or not str(card_id).strip()):
            raise ValueError("child card has invalid id")
        child_ids.append(str(card_id))
    return tuple(child_ids), total, effective_limit


def _warn_child_read(parent_card_id: str, detail: str) -> None:
    print(f"WARN  card {parent_card_id} child-card read FAILED: {detail} -- reconciliation is add-only")


def card_child_ids(cfg: dict, parent_card_id: str) -> frozenset[str] | None:
    """Return a complete child-id snapshot, or ``None`` when the read is not authoritative."""
    parent_id = str(parent_card_id)
    path = f"{_card_path(parent_id)}/connection/children"
    child_ids: list[str] = []
    offset, requested_limit, expected_total = 0, 200, None
    for _request_count in range(1, MAX_CARD_PAGE_REQUESTS + 1):
        try:
            data = api(cfg, "GET", path, params={"offset": offset, "limit": requested_limit})
            page_ids, total, effective_limit = _parse_child_page(data, offset, requested_limit)
        except (SystemExit, ValueError) as err:
            _warn_child_read(parent_id, str(err) or "request aborted")
            return None
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            _warn_child_read(parent_id, f"totalRecords changed from {expected_total} to {total}")
            return None
        child_ids.extend(page_ids)
        if len(set(child_ids)) != len(child_ids):
            _warn_child_read(parent_id, "duplicate child id across pages")
            return None
        offset += len(page_ids)
        if offset == expected_total:
            return frozenset(child_ids)
        if not page_ids or len(page_ids) < effective_limit:
            _warn_child_read(parent_id, f"pagination ended at {offset} before totalRecords {expected_total}")
            return None
    _warn_child_read(parent_id, f"pagination exceeded defensive limit of {MAX_CARD_PAGE_REQUESTS} requests")
    return None


# --- existing-card field updates: op-builders + one versioned PATCH per card ---

def op_custom_id(custom_id: str) -> dict:
    return {"op": "replace", "path": "/customId", "value": custom_id}


def op_lane(lane_id: str) -> dict:
    return {"op": "replace", "path": "/laneId", "value": lane_id}


def op_tag(tag: str) -> dict:
    # Appends at /tags/- -- confirmed against the LeanKit Node client docs. Tag removal is a
    # separate op-builder (see ops_tag_remove below): RFC 6902's remove op has no `value` member,
    # so removal must be index-based, not value-based (issue #3).
    return {"op": "add", "path": "/tags/-", "value": tag}


def ops_tag_remove(current_tags: list[str], tags_to_remove: set[str]) -> list[dict]:
    """Build RFC-6902 index-based remove ops -- {"op": "remove", "path": f"/tags/{i}"}, no `value`
    member -- for every index in `current_tags` whose value is in `tags_to_remove`, sorted in
    DESCENDING index order so multiple removals stay correct when batched into one PATCH: an
    earlier removal must never shift a later op's target index (RFC 6902 applies ops sequentially
    to the evolving document). `current_tags` MUST be the card's raw, unfiltered tags array
    (card.get("tags") or []) -- NOT card_tags()'s deduped set -- or computed indices won't match
    what AgilePlace actually holds. A tag value appearing at multiple indices yields one remove op
    per occurrence, not just the first.

    Every name in `tags_to_remove` is expected to be present in `current_tags` -- callers derive
    their removal names from card_tags(card) against this same card snapshot, so a miss here
    signals a real bug upstream, not a benign no-op: raises ValueError naming every unmatched tag
    (plus current_tags, for context) rather than silently letting the caller believe the tag is
    gone when it never was. Returns [] for an empty tags_to_remove.

    Issue #3: replaces the undocumented value-based {"op":"remove","path":"/tags","value":tag} --
    no public LeanKit docs describe it, and RFC 6902's remove op has no `value` member.
    """
    if not tags_to_remove:
        return []
    missing = {tag for tag in tags_to_remove if tag not in current_tags}
    if missing:
        raise ValueError(
            f"ops_tag_remove: tag(s) {sorted(missing)} not found in current_tags {current_tags!r}"
        )
    indices = [i for i, t in enumerate(current_tags)
               if isinstance(t, str) and t in tags_to_remove]
    return [{"op": "remove", "path": f"/tags/{i}"} for i in sorted(indices, reverse=True)]


def op_planned_date(field: str, date: str | None) -> dict:
    # field = plannedStart|plannedFinish. The server type-validates replace values on these paths as
    # strings and 422s on null (observed live 2026-07-21, issue #52), so clearing must be a remove.
    # Callers only queue a clear when the card currently has the date, so remove-on-absent can't occur.
    if date is None:
        return {"op": "remove", "path": f"/{field}"}
    return {"op": "replace", "path": f"/{field}", "value": date}


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
    attempted first. The fresh snapshot must still match every field targeted by the queued ops;
    otherwise the run aborts before its caller can persist merge state (see _card_with_version)."""
    if not ops:
        return {}
    versioned = _card_with_version(cfg, apply, card, ops)
    if versioned is None:
        raise SystemExit(
            f"AgilePlace card {card.get('id', '<unknown>')} PATCH aborted after version refetch "
            "validation failed -- refusing to save sync state"
        )
    return mutate(cfg, apply, "PATCH", _card_path(versioned["id"]), body=ops,
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


def _lane_id(card: dict) -> str:
    lane = card.get("lane")
    nested_id = lane.get("id") if isinstance(lane, dict) else None
    lane_id = card.get("laneId")
    return str(lane_id if lane_id is not None else nested_id if nested_id is not None else "")


def _card_value_for_patch_path(card: dict, path: str):
    """Return the read-side value whose snapshot a supported JSON-Patch path depends on."""
    root = path.removeprefix("/").split("/", 1)[0]
    if not path.startswith("/") or not root:
        raise ValueError(f"invalid JSON-Patch path {path!r}")
    if root == "customId":
        return custom_id_value(card)
    if root == "laneId":
        return _lane_id(card)
    if root == "tags":
        return card.get("tags") or []
    if root in {"plannedStart", "plannedFinish"}:
        return card.get(root)
    if root == "isBlocked":
        return card_is_blocked(card)
    if root == "blockReason":
        return card_block_reason(card)
    raise ValueError(f"unsupported JSON-Patch path {path!r}")


def _changed_patch_paths(card: dict, fresh: dict, ops: list[dict]) -> list[str]:
    """List queued paths whose read-side values changed, including malformed/unknown paths."""
    changed = set()
    for op in ops:
        path = op.get("path") if isinstance(op, dict) else None
        if not isinstance(path, str):
            changed.add("<missing>")
            continue
        try:
            if _card_value_for_patch_path(card, path) != _card_value_for_patch_path(fresh, path):
                changed.add(path)
        except ValueError:
            changed.add(path)
    return sorted(changed)


def _card_with_version(cfg: dict, apply: bool, card: dict, ops: list[dict] | None = None) -> dict | None:
    """Guarantee `card` carries a usable resource version before patch_card is allowed to PATCH it.
    Returns `card` unchanged (zero network calls) when a usable version is already present or apply
    is False -- dry runs never trigger a refetch. Otherwise refetches the card fresh: on success
    returns the fresh card dict (never mutates `card`) only when every field targeted by `ops` still
    matches the original snapshot. If the refetch has no usable version, returns a different card,
    carries an unknown op path, or reveals a concurrent change to a targeted field, returns None and
    prints one WARN so patch_card can abort the run before sync state advances."""
    if _has_usable_version(card.get("version")) or not apply:
        return card
    fresh = get_card(cfg, card["id"])
    if not _has_usable_version(fresh.get("version")):
        print(f"WARN  card {card['id']} has no resource version after refetch -- "
              f"refusing unversioned PATCH, skipping ops")
        return None
    if fresh.get("id") is None or str(fresh["id"]) != str(card["id"]):
        print(f"WARN  card {card['id']} refetch returned different card id {fresh.get('id')!r} -- "
              "refusing PATCH with mismatched identity, skipping ops")
        return None
    changed_paths = _changed_patch_paths(card, fresh, ops or [])
    if changed_paths:
        print(f"WARN  card {card['id']} fields {changed_paths} changed between snapshot and version "
              "refetch -- refusing PATCH with stale ops, skipping ops")
        return None
    return fresh


def _version_headers(card: dict) -> dict:
    v = card.get("version")
    return {"x-lk-resource-version": str(v)} if _has_usable_version(v) else {}


def _planned_card_snapshot(title: str, custom_id: str, external_url: str,
                           lane_id: str | None) -> Mapping[str, object]:
    """Read-only card defaults for continuing one dry run after a planned creation.

    The synthetic identity is deterministic for this plan but has meaning only inside the current
    dry run. It is never a server identity and callers must not persist it. Keeping every value in
    the mapping immutable makes the snapshot itself immutable without copying caller-owned inputs.
    """
    identity_source = f"{custom_id}\0{external_url}".encode("utf-8")
    plan_id = PLANNED_CARD_ID_PREFIX + hashlib.sha256(identity_source).hexdigest()[:16]
    return MappingProxyType({
        "id": plan_id,
        "title": title,
        "customId": custom_id,
        "laneId": lane_id,
        "plannedStart": None,
        "plannedFinish": None,
        "_planOnly": True,
        "_planOnlyExternalUrl": external_url,
    })


def create_card(cfg: dict, apply: bool, title: str, custom_id: str, external_url: str,
                lane_id: str | None) -> Mapping[str, object]:
    """Create a card, or return a plan-only read-only snapshot when ``apply`` is false."""
    body = {"boardId": cfg["board_id"], "title": title, "customId": custom_id}
    if lane_id:
        body["laneId"] = lane_id
    if external_url:
        body["externalLink"] = {"label": f"GitHub {custom_id}", "url": external_url}
    if not apply:
        mutate(cfg, False, "POST", "card", body=body, note=f"create card {custom_id}")
        return _planned_card_snapshot(title, custom_id, external_url, lane_id)
    return mutate(cfg, True, "POST", "card", body=body, note=f"create card {custom_id}")


def delete_card(cfg: dict, apply: bool, card_id: str) -> None:
    """Delete a card outright (io v2 DELETE /io/card/{cardId}). Used by smoke mode cleanup only --
    the ongoing sync never deletes cards."""
    mutate(cfg, apply, "DELETE", _card_path(card_id), note=f"delete card {card_id}")


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


# --- dependencies (sequencing) -- shapes confirmed live 2026-07-21, issue #57 ---
# The endpoints are undocumented in the io v2 public docs; every shape below was captured from the
# UI and validated against the production tenant (API-VALIDATION.md "Dependencies API discovery").

DEPENDENCY_TIMING = "finishToStart"


def card_dependencies(cfg: dict, card_id: str) -> list[dict] | None:
    """The card's dependency entries ({direction, cardId, timing, ...}), or None when the read
    fails or is unrecognized. None means UNKNOWN -- callers must skip reconciliation for the card
    (fail closed), never treat it as an empty set. The confirmed response carries the whole list
    with no pageMeta; if the server ever paginates this, the shape check below fails closed."""
    try:
        data = api(cfg, "GET", f"{_card_path(card_id)}/dependency")
    except SystemExit as err:
        print(f"WARN  card {card_id} dependency read FAILED: {err} -- skipping dependency reconciliation")
        return None
    entries = data.get("dependencies") if isinstance(data, dict) else None
    # Reject anything short of a complete, well-formed snapshot: a pageMeta member would mean
    # the server started paginating (entries could be missing -> re-create -> 409 abort), and an
    # entry without a usable direction/cardId hides state the diff would then act against.
    if (not isinstance(data, dict) or "pageMeta" in data or not isinstance(entries, list)
            or any(not _valid_dependency_entry(e) for e in entries)):
        print(f"WARN  card {card_id} dependency read returned an unrecognized shape -- "
              "skipping dependency reconciliation")
        return None
    return entries


def _valid_dependency_entry(entry) -> bool:
    return (isinstance(entry, dict)
            and entry.get("direction") in {"incoming", "outgoing"}
            and bool(entry.get("cardId")))


def incoming_dependency_ids(entries: list[dict]) -> set[str]:
    """Ids of the cards this card depends on (its blockers), from a card_dependencies read."""
    return {str(entry["cardId"]) for entry in entries
            if entry.get("direction") == "incoming" and entry.get("cardId")}


def create_dependencies(cfg: dict, apply: bool, card_id: str, depends_on_ids) -> None:
    """Make card_id depend on each of depends_on_ids (finish-to-start; they block it)."""
    ids = sorted(str(i) for i in depends_on_ids if i)
    if not ids:
        return
    mutate(cfg, apply, "POST", "card/dependency",
           body={"cardIds": [str(card_id)], "dependsOnCardIds": ids, "timing": DEPENDENCY_TIMING},
           note=f"depend {card_id} on {len(ids)} card(s)")


def delete_dependencies(cfg: dict, apply: bool, card_id: str, depends_on_ids) -> None:
    """Remove card_id's dependency on each of depends_on_ids (pair-addressed; no dependency ids
    exist -- see API-VALIDATION.md)."""
    ids = sorted(str(i) for i in depends_on_ids if i)
    if not ids:
        return
    mutate(cfg, apply, "DELETE", "card/dependency",
           body={"cardIds": [str(card_id)], "dependsOnCardIds": ids},
           note=f"undepend {card_id} from {len(ids)} card(s)")
