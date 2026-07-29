"""Shared UTC timestamp normalization. Pure, stdlib only, no I/O.

Extracted from comment_sync (issue #66), which introduced it as "the shared timestamp-normalization
helper" but kept it module-private -- description_sync's recency tiebreak (this change) is the
second consumer, and a second copy of the same 15 lines of ISO-8601 edge-case handling is exactly
the duplication the repo's DRY rule exists to prevent. comment_sync imports it from here now; its
own behavior is unchanged.
"""
from __future__ import annotations

from datetime import datetime, timezone


def parse_timestamp(raw: str | None) -> datetime | None:
    """Normalize a timestamp to a UTC-aware datetime so timestamps from different sources (GitHub's
    ISO-8601 `Z` suffix, an explicit offset, a naive local-looking string) become comparable through
    one funnel rather than via raw lexical string comparison.

    Total: any input that isn't a parseable ISO-8601 string -- ``None``, blank, garbage, or simply
    the wrong type -- degrades to ``None`` and never raises, so a comparison site can fail closed on
    the unknown instead of crashing the whole sync. A naive (offset-less) timestamp is *assumed* UTC
    rather than rejected: both producers here emit UTC, and rejecting would turn a cosmetic format
    difference into a silent behavior change at the comparison site."""
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
