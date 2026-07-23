"""AgilePlace card-comment I/O (issue #66 Task 2/8). List/create/update/delete a card's comments,
plus tolerant normalization into the ApComment shape comment_sync consumes.

Split out as its own module -- rather than growing agileplace.py -- following #65's
agileplace_description.py precedent: agileplace.py is at the project's 800-line file cap (672 lines
on this branch's base, confirmed via API-VALIDATION.md/the regression-budget tests) and this
feature's own decision record requires ZERO lines added to it. Depends on agileplace.api/mutate/
get_card only; agileplace.py has no dependency back on this module, so there is no import cycle.

Endpoint shapes (design doc 2026-07-23-issue-66-comment-sync-design.md): list reads
`GET /io/card/{cardId}/comment`, falling back once to the card GET's top-level `comments` array on
ANY shape surprise (mirrors get_card's own {"card": {...}}-or-flat tolerance). Create is
`POST /io/card/{cardId}/comment {"text": <html>}`; update is
`PUT /io/card/{cardId}/comment/{commentId}`; delete is a **speculative**
`DELETE /io/card/{cardId}/comment/{commentId}` -- the web UI never exposed comment deletion, so this
shape is pinned live by the new smoke.py step, not by public docs. Per-comment field names
(`createdBy`, `createdOn`, and the edited-timestamp key) are VALIDATE LIVE the same way -- see
API-VALIDATION.md; `_normalize_ap_comment` stays defensive rather than trusting any single guess.
"""
from __future__ import annotations

import urllib.parse

import agileplace


def _comment_collection_path(card_id) -> str:
    return f"card/{urllib.parse.quote(str(card_id), safe='')}/comment"


def _comment_item_path(card_id, comment_id) -> str:
    return f"{_comment_collection_path(card_id)}/{urllib.parse.quote(str(comment_id), safe='')}"


def _extract_raw_comments(data) -> list:
    """The raw comment array from a response that may be a bare list or wrapped under "comments"
    (mirroring list_cards' {"cards": [...]} convention). Raises ValueError for any other shape so
    the caller can trigger the get_card fallback instead of silently reporting zero comments."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("comments"), list):
        return data["comments"]
    raise ValueError(f"unexpected comment-list shape ({type(data).__name__}, expected a list or "
                     f"an object carrying a 'comments' array)")


def _try_primary_comment_read(cfg: dict, card_id) -> tuple[list[dict] | None, Exception | None]:
    try:
        data = agileplace.api(cfg, "GET", _comment_collection_path(card_id))
        raw_comments = _extract_raw_comments(data)
        return [_normalize_ap_comment(raw) for raw in raw_comments], None
    except (SystemExit, ValueError) as err:
        return None, err


def _try_fallback_comment_read(cfg: dict, card_id) -> tuple[list[dict] | None, Exception | None]:
    try:
        card = agileplace.get_card(cfg, card_id)
        raw_comments = card.get("comments")
        if not isinstance(raw_comments, list):
            raise ValueError(f"card 'comments' field is {type(raw_comments).__name__}, expected a list")
        return [_normalize_ap_comment(raw) for raw in raw_comments], None
    except (SystemExit, ValueError) as err:
        return None, err


def list_comments(cfg: dict, card_id) -> list[dict]:
    """Every comment on a card, normalized into ApComment dicts. Primary read hits the dedicated
    comment endpoint; on ANY shape surprise (a non-list/non-{"comments":[...]} body, a SystemExit
    from the API call itself, or an item _normalize_ap_comment can't parse) this falls back exactly
    once to the card GET's top-level `comments` array. Raises only when NEITHER shape yields a
    usable list -- a genuinely broken read must fail the run loud, never silently report zero
    comments (which would look identical to "no comments yet" and could resurrect a tombstoned
    ledger row as a brand-new comment to mirror)."""
    comments, primary_err = _try_primary_comment_read(cfg, card_id)
    if comments is not None:
        return comments
    comments, fallback_err = _try_fallback_comment_read(cfg, card_id)
    if comments is not None:
        return comments
    raise SystemExit(
        f"AgilePlace card {card_id} comment read FAILED via both the comment endpoint "
        f"({primary_err}) and the card fallback ({fallback_err})"
    )


def create_comment(cfg: dict, apply: bool, card_id, html: str) -> dict | None:
    """Post one comment (already richtext-rendered by the caller). apply=False takes mutate's
    dry-run path -- a DRY print, zero network -- and returns None. apply=True posts and normalizes
    the response through _normalize_ap_comment so the caller immediately gets a real ApComment dict
    carrying the new id; a response that can't be parsed raises (ValueError) rather than letting a
    created-but-unparsed comment masquerade as success to the ledger."""
    result = agileplace.mutate(cfg, apply, "POST", _comment_collection_path(card_id),
                               body={"text": html}, note=f"comment on card {card_id}")
    if not apply:
        return None
    return _normalize_ap_comment(result)


def update_comment(cfg: dict, apply: bool, card_id, comment_id, html: str) -> bool:
    """Edit an existing comment's text via PUT. Returns True only when the write actually happened
    (apply=True and mutate reached the API) -- a dry run must never report success, matching
    ghkit.edit_issue_body's own apply-gated boolean contract."""
    agileplace.mutate(cfg, apply, "PUT", _comment_item_path(card_id, comment_id),
                      body={"text": html}, note=f"edit comment {comment_id} on card {card_id}")
    return apply


def delete_comment(cfg: dict, apply: bool, card_id, comment_id) -> bool:
    """DELETE a comment -- speculative shape (see module docstring); pinned live by the new
    smoke.py step. Returns True only when the write actually happened (apply=True)."""
    agileplace.mutate(cfg, apply, "DELETE", _comment_item_path(card_id, comment_id),
                      note=f"delete comment {comment_id} on card {card_id}")
    return apply


def _comment_author_fields(created_by) -> tuple[str | None, str | None, str | None]:
    """Best-effort author identity from a comment's `createdBy` field -- name, then email, then a
    bare id -- mirroring intake.card_created_by_name's confirmed name/email fallback for AgilePlace
    user objects, but additionally keeping the id as a last resort: a comment author, unlike a card
    creator, must stay identifiable even when the object carries neither a readable name nor an
    email, since comment_sync.is_sync_authored needs SOMETHING to compare against. Never raises --
    any non-dict shape (bare id string/int, None, list, empty dict) yields all-None."""
    if not isinstance(created_by, dict):
        return None, None, None
    name = created_by.get("fullName")
    email = created_by.get("emailAddress")
    raw_id = created_by.get("id")
    name = name.strip() if isinstance(name, str) and name.strip() else None
    email = email.strip() if isinstance(email, str) and email.strip() else None
    author_id = (str(raw_id) if isinstance(raw_id, (str, int)) and not isinstance(raw_id, bool)
                and str(raw_id).strip() else None)
    return name, email, author_id


def _first_present(raw: dict, *keys: str):
    for key in keys:
        if key in raw:
            return raw[key]
    return None


def _raw_timestamp(value) -> str | None:
    """A comment timestamp kept as a raw string VERBATIM (the ledger persists exact strings, see
    struct #1), or None for a blank/absent/non-string value. Deliberately NOT parse-validated here:
    a present-but-unparseable value is kept so the planner's `comment_sync._timestamp_warning` can
    surface an unrecognized AgilePlace timestamp format (issue #66 Codex P2 #8) instead of it
    vanishing to None before the planner ever sees it. Every comparison site parses at use via
    `comment_sync._parse_timestamp`, so a garbage value is still excluded from drift/adjacency --
    just no longer silently. Mirrors ghkit._normalize_gh_comment's own raw-string pass-through."""
    return value if isinstance(value, str) and value.strip() else None


def _coerce_comment_id(raw_id) -> int | None:
    """The comment id as an int, or None when it can't be one. Accepts a real int (never a bool)
    and -- since the live POST /io/card/{cardId}/comment response serializes the new id as a STRING
    of digits (e.g. '2491550223', confirmed against a real tenant 2026-07-23; see API-VALIDATION.md)
    -- an all-ASCII-digit string, coerced to int. Rejects bools, floats, non-digit strings, and
    None so the ledger's gh_id/ap_id int|None contract (comment_sync struct #1) still holds."""
    if isinstance(raw_id, bool):
        return None
    if isinstance(raw_id, int):
        return raw_id
    if isinstance(raw_id, str):
        text = raw_id.strip()
        if text.isascii() and text.isdigit():
            return int(text)
    return None


def _normalize_ap_comment(raw: dict) -> dict:
    """Normalize one raw AgilePlace comment payload into the ApComment shape. Pure -- no I/O.
    Raises ValueError when `raw` isn't an object or its id is missing/unusable (a comment the
    sync can't identify is a genuine unrecovered failure, not a value to paper over); every other
    field degrades gracefully instead of raising, since a comment with a missing author or
    unparseable timestamp is still a real comment the sync must be able to mirror/ledger."""
    if not isinstance(raw, dict):
        raise ValueError(f"AgilePlace comment payload is {type(raw).__name__}, expected an object")
    comment_id = _coerce_comment_id(raw.get("id"))
    if comment_id is None:
        raise ValueError(f"AgilePlace comment has an unusable id ({raw.get('id')!r}, "
                         f"expected an int or a digit string)")
    body = raw.get("text")
    author_name, author_email, author_id = _comment_author_fields(raw.get("createdBy"))
    created = _first_present(raw, "createdOn", "created")
    edited = _first_present(raw, "lastModified", "modifiedOn", "updatedOn", "editedOn")
    return {
        "id": comment_id,
        "body": body if isinstance(body, str) else "",
        "author_name": author_name,
        "author_email": author_email,
        "author_id": author_id,
        "created": _raw_timestamp(created),
        "edited": _raw_timestamp(edited),
    }
