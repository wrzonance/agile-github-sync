"""Two-way GitHub issue <-> AgilePlace card comment sync (issue #66). Module scaffold: this file
currently carries only the shared timestamp-normalization helper (Task 1). Resolver, provenance,
and wiring functions land in later tasks; when they do, this module imports agileplace_comments,
ghkit, and richtext at module level only -- no I/O at import time.
"""
from __future__ import annotations

from datetime import datetime, timezone


def _parse_timestamp(raw: str | None) -> datetime | None:
    """Normalizes a comment timestamp to a UTC-aware datetime so both sides of a sync (GH's
    ISO-8601, AP's not-yet-confirmed format) become comparable through one funnel rather than via
    raw lexical string comparison. Total: any input that isn't a parseable ISO-8601 string --
    ``None``, blank, garbage, or simply the wrong type -- degrades to ``None`` and never raises, so a
    comparison site can exclude the comment (with a WARN) instead of crashing the whole sync.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
