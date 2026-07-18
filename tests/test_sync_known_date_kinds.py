"""Unit tests for sync.known_date_kinds (issue #6 follow-up).

known_date_kinds feeds ghproject.unmatched_date_kinds so a Project date field that has NEVER carried a
value project-wide isn't mistaken for a name mismatch and permanently blocked. It has two properties
that are easy to silently break and were previously unpinned by any test:

1. It aggregates ACROSS every issue in issues_state with `any(...)`, not `all(...)` -- one issue with
   prior date history is enough to mark that kind "known", even if other issues in the same state file
   have never synced a date.
2. Per issue, it checks for a real, non-empty VALUE (`v.get(kind)`), not mere key presence
   (`kind in v`) -- a merge-base entry that carries the key with an empty string does not count as
   "known".

Run: pytest -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sync import known_date_kinds  # noqa: E402

URL_A = "https://github.com/o/r/issues/1"
URL_B = "https://github.com/o/r/issues/2"


def test_empty_issues_state_yields_no_known_kinds():
    assert known_date_kinds({}) == frozenset()


def test_any_issue_with_a_value_marks_the_kind_known_even_if_others_never_synced():
    # URL_A has prior start history; URL_B has never synced anything. This must be `any`, not `all`:
    # swapping to `all` would make one never-synced issue mask a kind every other issue already knows.
    issues_state = {URL_A: {"start": "2026-01-01"}, URL_B: {}}
    assert known_date_kinds(issues_state) == frozenset({"start"})


def test_kind_absent_from_every_issue_is_not_known():
    issues_state = {URL_A: {"start": "2026-01-01"}, URL_B: {}}
    assert "target" not in known_date_kinds(issues_state)


def test_present_but_empty_value_does_not_count_as_known():
    # The key is present but empty -- this must NOT count as "known". Swapping `v.get(kind)` for
    # `kind in v` would treat a present-but-empty merge-base entry as evidence of a prior real sync.
    issues_state = {URL_A: {"start": ""}}
    assert known_date_kinds(issues_state) == frozenset()


def test_both_kinds_known_when_some_issue_carries_each():
    issues_state = {URL_A: {"start": "2026-01-01"}, URL_B: {"target": "2026-02-01"}}
    assert known_date_kinds(issues_state) == frozenset({"start", "target"})
