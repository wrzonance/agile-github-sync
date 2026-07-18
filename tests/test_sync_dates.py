"""Unit tests for sync.sync_dates: merge-base gating on the GitHub-side write outcome (issue #6).

Bug pinned here: sync_dates previously advanced its merge-base (prev[kind]) whenever apply was True,
even when the GitHub-side write was silently skipped (e.g. item_id/field_id missing). That masks the
mismatch forever -- GitHub's actual value never changes, but the next run compares against the
already-advanced base and sees no diff. The fix: only advance prev[kind] when the GitHub-side value is
already correct (new == gh_date) or the write is confirmed to have happened
(ghproject.set_project_date returned True).

TEST-CONSTRUCTION NOTE: do NOT simulate "write skipped" by setting the GitHub-side read (pitem[kind])
to None across runs -- that exercises reconcile_value's legitimate "GitHub genuinely cleared it" path,
not "the write attempt was skipped". Instead hold pitem[kind] constant at whatever GitHub actually has,
and control write success by mocking ghproject.set_project_date's return value directly.

Run: pytest -q
"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sync import sync_dates  # noqa: E402


def _issue(url="https://github.com/o/r/issues/1", title="[T1] widget"):
    return {"url": url, "title": title}


def _card(planned_start=None, planned_finish=None):
    return {"id": "C1", "plannedStart": planned_start, "plannedFinish": planned_finish}


def _field_meta():
    return {"project_id": "PVT_1", "start_field_id": "SF_1", "target_field_id": "TF_1"}


def _pitem(item_id="PVTI_1", start=None, target=None):
    return {"item_id": item_id, "start": start, "target": target}


class _Queue:
    """Records every queue(card, ops, note) call for assertions."""
    def __init__(self):
        self.calls = []

    def __call__(self, card, ops, note):
        self.calls.append((card, ops, note))


def _issues_state(url, **prev):
    return {url: dict(prev)}


# --- merge-base advance invariant -------------------------------------------
# prev[kind] advances iff apply is True AND (new == gh_date already, OR ghproject.set_project_date
# returned True for the write that was attempted).

def test_prev_does_not_advance_when_gh_write_is_skipped():
    """new != gh_date, ghproject.set_project_date returns False (write skipped) -> prev[kind] must
    stay at its old value, not silently advance to `new`."""
    issue = _issue()
    card = _card(planned_start="2026-02-01")                  # AgilePlace changed the date
    pitem = _pitem(start="2026-01-01")                        # GitHub unchanged
    state = _issues_state(issue["url"], start="2026-01-01")   # base == current gh_date
    queue = _Queue()
    with patch("sync.ghproject.set_project_date", return_value=False) as write_mock:
        sync_dates({}, True, issue, card, pitem, _field_meta(), state, queue)
    write_mock.assert_called_once()
    assert state[issue["url"]]["start"] == "2026-01-01"       # NOT advanced to "2026-02-01"


def test_prev_advances_when_gh_write_succeeds():
    """Same setup, but the write is confirmed (True) -> prev[kind] advances to `new`."""
    issue = _issue()
    card = _card(planned_start="2026-02-01")
    pitem = _pitem(start="2026-01-01")
    state = _issues_state(issue["url"], start="2026-01-01")
    queue = _Queue()
    with patch("sync.ghproject.set_project_date", return_value=True) as write_mock:
        sync_dates({}, True, issue, card, pitem, _field_meta(), state, queue)
    write_mock.assert_called_once()
    assert state[issue["url"]]["start"] == "2026-02-01"


def test_prev_advances_when_new_already_matches_gh_no_write_attempted():
    """new == gh_date (AgilePlace is the stale side) -> no write is attempted at all, and prev[kind]
    still advances -- nothing was skipped, there was simply nothing to write."""
    issue = _issue()
    card = _card(planned_start=None)                          # AgilePlace stale
    pitem = _pitem(start="2026-01-01")                         # GitHub already correct
    state = _issues_state(issue["url"], start=None)            # base: AgilePlace's old (unset) value
    queue = _Queue()
    with patch("sync.ghproject.set_project_date") as write_mock:
        sync_dates({}, True, issue, card, pitem, _field_meta(), state, queue)
    write_mock.assert_not_called()
    assert state[issue["url"]]["start"] == "2026-01-01"


def test_prev_does_not_advance_when_apply_is_false():
    """Dry run: never mutate the merge base, regardless of write outcome."""
    issue = _issue()
    card = _card(planned_start="2026-02-01")
    pitem = _pitem(start="2026-01-01")
    state = _issues_state(issue["url"], start="2026-01-01")
    queue = _Queue()
    with patch("sync.ghproject.set_project_date", return_value=True):
        sync_dates({}, False, issue, card, pitem, _field_meta(), state, queue)
    assert state[issue["url"]]["start"] == "2026-01-01"        # unchanged


# --- AgilePlace queue writes stay unconditional ------------------------------
# Gating applies only to the GH-side merge-base advance, never to the queue(card, [...]) call.
#
# A single reconcile_value() result always equals gh_date or ap_date (never both differ at once for
# the same kind), so a GH write and an AP queue write can't both be attempted for the SAME kind in one
# call. To pin "gh_write_ok never leaks into the queue path", use two independent kinds in one
# sync_dates call: "start" needs a GH write that gets skipped, "target" needs only an AP queue write.
# The target queue write must fire and target's merge-base must advance normally, unaffected by
# start's skipped write.

def test_queue_write_happens_even_when_gh_write_is_skipped_for_another_kind():
    issue = _issue()
    card = _card(planned_start="2026-02-01", planned_finish="2026-04-01")   # AP: start changed, target stale
    pitem = _pitem(start="2026-01-01", target="2026-05-01")                 # GH: start unchanged, target changed
    state = _issues_state(issue["url"], start="2026-01-01", target="2026-04-01")
    queue = _Queue()
    with patch("sync.ghproject.set_project_date", return_value=False) as write_mock:
        sync_dates({}, True, issue, card, pitem, _field_meta(), state, queue)
    write_mock.assert_called_once()                              # only "start" needed an attempted GH write
    assert state[issue["url"]]["start"] == "2026-01-01"          # gated: GH write was skipped
    assert state[issue["url"]]["target"] == "2026-05-01"         # unaffected: advanced normally
    assert len(queue.calls) == 1
    _, ops, note = queue.calls[0]
    assert ops == [{"op": "replace", "path": "/plannedFinish", "value": "2026-05-01"}]
    assert note == "plannedFinish=2026-05-01"


def test_queue_write_skipped_only_when_ap_already_matches_new():
    """queue() is only called when the AgilePlace side actually needs to change -- unrelated to any
    GH-side gating."""
    issue = _issue()
    card = _card(planned_start="2026-01-01")                   # already matches the resolved value
    pitem = _pitem(start="2026-01-01")
    state = _issues_state(issue["url"], start="2026-01-01")
    queue = _Queue()
    with patch("sync.ghproject.set_project_date") as write_mock:
        sync_dates({}, True, issue, card, pitem, _field_meta(), state, queue)
    write_mock.assert_not_called()
    assert queue.calls == []


# --- unmatched_kinds guard ----------------------------------------------------

def test_unmatched_kind_is_skipped_entirely():
    """A kind flagged in unmatched_kinds is neither written to GitHub nor queued to AgilePlace, and
    its merge-base entry is left untouched."""
    issue = _issue()
    card = _card(planned_start="2026-02-01")
    pitem = _pitem(start="2026-01-01")
    state = _issues_state(issue["url"], start="2026-01-01")
    queue = _Queue()
    with patch("sync.ghproject.set_project_date") as write_mock:
        sync_dates({}, True, issue, card, pitem, _field_meta(), state, queue, frozenset({"start"}))
    write_mock.assert_not_called()
    assert queue.calls == []
    assert state[issue["url"]]["start"] == "2026-01-01"
