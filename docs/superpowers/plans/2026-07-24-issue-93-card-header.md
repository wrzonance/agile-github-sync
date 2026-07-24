# Card Header Carries the GitHub Issue Number — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** AgilePlace card headers (the `customId` field) carry the GitHub issue number — `0C1 (GitHub Issue #5)` — on new cards, existing cards (via the sync's own drift reconciliation), and the reverse-intake card→issue writeback.

**Architecture:** Key/header split per the approved spec (`docs/superpowers/specs/2026-07-24-issue-93-card-header-design.md`). `stages.issue_custom_id()` stays the internal match key everywhere; a new pure formatter `issue_card_header()` produces the written header and a new pure parser `header_match_key()` folds any card-side value (old format, new format, human-authored) back into key space, so matching, the coherence fence, and intake stay correct across the transition with no migration script.

**Tech Stack:** Python 3 stdlib only (this repo has zero runtime deps). pytest for tests.

## Global Constraints

- Branch: `feat/issue-93` (exists; spec committed as `aaa3b0b`). NEVER commit to `main`.
- `sync.py` is over the 800-line file budget (≈908 lines): its diff must be net-minimal wiring only — no new functions in sync.py.
- All new logic is pure and lives in `stages.py`; no I/O in stages/card_coherence (existing contract).
- Suffix casing is exactly `(GitHub Issue #N)` — capital H in "GitHub".
- Unkeyed issues (no `[KEY]` title prefix) get the bare header `GitHub Issue #5`, never `5 (GitHub Issue #5)`.
- Commits: Conventional Commits, each ending with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Run tests with `python -m pytest -q` from the repo root (stdlib project; tests insert the root on sys.path themselves).
- Immutability: never mutate input dicts; build new values.

---

### Task 1: `stages.py` — header formatter + inverse parser

**Files:**
- Modify: `stages.py` (add `import re` after `from __future__ import annotations`; add two functions + two module-level regexes after `issue_custom_id`, which ends at line 114)
- Test: `tests/test_stages_card_header.py` (new file)

**Interfaces:**
- Consumes: `stages.title_key(title) -> str | None`, `stages.issue_custom_id(issue) -> str` (both exist).
- Produces: `stages.issue_card_header(issue: dict) -> str` and `stages.header_match_key(value: str | None) -> str` — every later task imports these two names from `stages`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_stages_card_header.py`:

```python
"""issue #93: the customId header format (issue_card_header) and its inverse (header_match_key).

The header is the string WRITTEN to a card's customId; issue_custom_id() remains the internal
match key used by matching, the coherence fence, and intake. Round-trip invariant:
header_match_key(issue_card_header(i)) == issue_custom_id(i) for every issue shape.
Run: pytest -q
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stages import header_match_key, issue_card_header, issue_custom_id  # noqa: E402


def test_keyed_task_header_carries_key_and_issue_number():
    assert issue_card_header({"number": 5, "title": "[0C1] RFC 9457 errors"}) == \
        "0C1 (GitHub Issue #5)"


def test_epic_header_uses_the_same_uniform_rule():
    assert issue_card_header({"number": 12, "title": "[EP-0C] Some epic"}) == \
        "EP-0C (GitHub Issue #12)"


def test_unkeyed_header_is_the_bare_github_reference():
    """No redundant '5 (GitHub Issue #5)' -- the suffix alone carries the info."""
    assert issue_card_header({"number": 5, "title": "No key here"}) == "GitHub Issue #5"


def test_match_key_strips_the_header_suffix():
    assert header_match_key("0C1 (GitHub Issue #5)") == "0C1"


def test_match_key_maps_the_bare_header_to_the_issue_number():
    assert header_match_key("GitHub Issue #5") == "5"


def test_match_key_passes_old_format_through_unchanged():
    assert header_match_key("0C1") == "0C1"


def test_match_key_normalizes_empty_and_none_to_empty_string():
    assert header_match_key("") == ""
    assert header_match_key(None) == ""


def test_match_key_strips_only_the_final_suffix():
    assert header_match_key("A (GitHub Issue #5) (GitHub Issue #6)") == "A (GitHub Issue #5)"


def test_match_key_ignores_a_suffix_not_at_the_end():
    assert header_match_key("0C1 (GitHub Issue #5) trailing") == "0C1 (GitHub Issue #5) trailing"


def test_match_key_ignores_a_non_digit_issue_number():
    assert header_match_key("0C1 (GitHub Issue #x)") == "0C1 (GitHub Issue #x)"


@pytest.mark.parametrize("issue", [
    {"number": 5, "title": "[0C1] task"},
    {"number": 12, "title": "[EP-0C] epic"},
    {"number": 7, "title": "unkeyed title"},
])
def test_round_trip_invariant(issue):
    assert header_match_key(issue_card_header(issue)) == issue_custom_id(issue)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest -q tests/test_stages_card_header.py`
Expected: FAIL — `ImportError: cannot import name 'header_match_key' from 'stages'`

- [ ] **Step 3: Implement in `stages.py`**

Add `import re` on its own line directly after `from __future__ import annotations` (line 8). Then append after `issue_custom_id` (after line 114):

```python
# issue #93: the header format written to a card's customId. Only the FINAL ' (GitHub Issue #N)'
# suffix is meaningful; header_match_key's greedy group leaves any earlier lookalike text intact.
_KEYED_HEADER_RE = re.compile(r"(?s)(.+) \(GitHub Issue #\d+\)")
_BARE_HEADER_RE = re.compile(r"GitHub Issue #(\d+)")


def issue_card_header(issue: dict) -> str:
    """The customId header WRITTEN to a card: the sync key plus a visible GitHub issue reference
    ('0C1 (GitHub Issue #5)'), or bare 'GitHub Issue #5' when the title carries no [KEY] (the
    keyed form would redundantly read '5 (GitHub Issue #5)'). issue_custom_id() stays the MATCH
    key; header_match_key() is this format's exact inverse."""
    key = title_key(issue["title"])
    number = issue["number"]
    return f"{key} (GitHub Issue #{number})" if key else f"GitHub Issue #{number}"


def header_match_key(value: str | None) -> str:
    """The MATCH key encoded in a card's customId header -- the exact inverse of
    issue_card_header(), applied to every card-side read so old-format ('0C1') and header-format
    ('0C1 (GitHub Issue #5)') cards resolve to the same key during the transition. A bare
    'GitHub Issue #5' folds to '5' (the unkeyed fallback key). Any other value (old-format,
    human-authored, smoke) passes through unchanged; None/empty normalizes to ''."""
    value = value or ""
    keyed = _KEYED_HEADER_RE.fullmatch(value)
    if keyed:
        return keyed.group(1)
    bare = _BARE_HEADER_RE.fullmatch(value)
    if bare:
        return bare.group(1)
    return value
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest -q tests/test_stages_card_header.py`
Expected: all PASS. Then `python -m pytest -q tests/test_stages_issue_custom_id.py` — still PASS (no behavior change to `issue_custom_id`).

- [ ] **Step 5: Commit**

```bash
git add stages.py tests/test_stages_card_header.py
git commit -m "feat(stages): customId header formatter + inverse match-key parser (#93)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: `agileplace.create_card` — `link_label` parameter

**Files:**
- Modify: `agileplace.py:563-581` (`create_card`)
- Test: `tests/test_agileplace.py` (append two tests; file already imports `create_card`, `CFG`, and `patch`)

**Interfaces:**
- Produces: `create_card(cfg, apply, title, custom_id, external_url, lane_id, type_id=None, type_title=None, link_label=None)` — Task 3's sync call site passes `link_label=f"GitHub {key}"`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_agileplace.py`)

```python
# --- issue #93: link_label keeps the external-link label short -----------------------------

def test_create_card_link_label_overrides_the_derived_label():
    """The sync passes the SHORT key label; the header-format custom_id must not leak into it
    (would read 'GitHub 0C1 (GitHub Issue #5)')."""
    with patch("agileplace.api", return_value={"id": "new"}) as api_mock:
        create_card(CFG, True, "Title", "0C1 (GitHub Issue #5)", "https://example.com", None,
                    link_label="GitHub 0C1")
    _, kwargs = api_mock.call_args
    assert kwargs["body"]["externalLink"] == {"label": "GitHub 0C1",
                                              "url": "https://example.com"}


def test_create_card_without_link_label_keeps_the_derived_label_byte_identical():
    """Every existing caller (smoke.py included) passes no link_label and must send the exact
    body it always sent."""
    with patch("agileplace.api", return_value={"id": "new"}) as api_mock:
        create_card(CFG, True, "Title", "CID-1", "https://example.com", None)
    _, kwargs = api_mock.call_args
    assert kwargs["body"]["externalLink"] == {"label": "GitHub CID-1",
                                              "url": "https://example.com"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest -q tests/test_agileplace.py -k link_label`
Expected: FAIL — `TypeError: create_card() got an unexpected keyword argument 'link_label'` (first test); second may pass already — that's fine, it pins the fallback.

- [ ] **Step 3: Implement**

In `agileplace.py`, change `create_card`'s signature and the label line only:

```python
def create_card(cfg: dict, apply: bool, title: str, custom_id: str, external_url: str,
                lane_id: str | None, type_id: str | None = None,
                type_title: str | None = None,
                link_label: str | None = None) -> Mapping[str, object]:
    """Create a card, or return a plan-only read-only snapshot when ``apply`` is false.

    ``type_id`` (when truthy) is sent as the card's typeId; ``type_title`` never reaches the API --
    it only feeds the dry-run snapshot's nested type.title for same-pass read-back. ``link_label``
    (issue #93) is used verbatim as the external-link label when provided; None keeps the classic
    ``GitHub {custom_id}`` derivation for every existing caller.
    """
    body = {"boardId": cfg["board_id"], "title": title, "customId": custom_id}
    if lane_id:
        body["laneId"] = lane_id
    if external_url:
        label = link_label if link_label is not None else f"GitHub {custom_id}"
        body["externalLink"] = {"label": label, "url": external_url}
```

(The rest of the function — `typeId`, dry-run snapshot, mutate calls — is unchanged. The label lives only in the POST body, which dry-run and apply share, so plan output automatically matches `--apply`; `_planned_card_snapshot` needs no change.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest -q tests/test_agileplace.py`
Expected: all PASS (existing create_card tests prove byte-identical default behavior).

- [ ] **Step 5: Commit**

```bash
git add agileplace.py tests/test_agileplace.py
git commit -m "feat(agileplace): create_card link_label param keeps link labels short (#93)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: `sync.py` wiring — write the header, match in key space

**Files:**
- Modify: `sync.py:36` (import), `sync.py:311-312` (`_reconciled_custom_id_index`), `sync.py:500-502` (create site inside `_ensure_cards_for_syncable_issues`), `sync.py:604` (index build), `sync.py:676-678` (drift rewrite)
- Test: `tests/test_sync.py` (append), `tests/test_sync_main.py` (fixture update + append)

**Interfaces:**
- Consumes: `stages.issue_card_header`, `stages.header_match_key` (Task 1); `create_card(..., link_label=...)` (Task 2).
- Produces: board indexes (`all_card_by_cid`, and downstream `card_by_cid`) keyed by **normalized** match keys; cards written with header-format customIds. Tasks 4-5 rely on that index convention.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_sync.py` (it already imports `_reconciled_custom_id_index`):

```python
def test_reconciled_index_releases_a_renamed_header_format_custom_id():
    """issue #93: the card stores the HEADER ('X (GitHub Issue #1)'); the index and the release
    bookkeeping operate on the normalized KEY ('X'). A rename [X]->[Y] must still release 'X'."""
    issue = {"number": 1, "title": "[Y] renamed", "url": "https://example.test/issues/1"}
    card = {"id": "C1", "customId": "X (GitHub Issue #1)"}
    reconciled, released = _reconciled_custom_id_index(
        [issue], {issue["url"]: card}, {"X": card})
    assert "X" not in reconciled
    assert reconciled["Y"] is card
    assert released == frozenset({"X"})
```

Append to `tests/test_sync_main.py`:

```python
# --- issue #93: customId header format ------------------------------------------------------

def test_old_format_custom_id_upgrades_to_header_format(tmp_path):
    """A pre-#93 card ('1') drifts from the desired header ('GitHub Issue #1'); the existing
    reconciliation queues the rewrite -- this IS the migration."""
    _, _, patch_card_mock, _ = _run_main_once(
        tmp_path, ({}, []), card={**_card(), "customId": "1"})
    ops = [op for call in patch_card_mock.call_args_list for op in call.args[3]]
    assert {"op": "replace", "path": "/customId", "value": "GitHub Issue #1"} in ops


def test_header_format_custom_id_queues_no_rewrite(tmp_path):
    _, _, patch_card_mock, _ = _run_main_once(tmp_path, ({}, []))
    ops = [op for call in patch_card_mock.call_args_list for op in call.args[3]]
    assert not [op for op in ops if op["path"] == "/customId"]


def test_custom_id_fallback_matches_a_header_format_card(tmp_path):
    """A header-format card with NO external link must still match via the customId fallback
    (normalized index) rather than triggering a duplicate create."""
    card = {k: v for k, v in _card().items() if k != "externalLink"}
    _, _, _, create_card_mock = _run_main_once(tmp_path, ({}, []), card=card)
    create_card_mock.assert_not_called()


def test_created_card_uses_header_custom_id_and_short_link_label(tmp_path):
    _, _, _, create_card_mock = _run_main_once(tmp_path, ({}, []), existing_cards=[])
    args, kwargs = create_card_mock.call_args
    assert args[3] == "GitHub Issue #1"          # the custom_id argument is the header
    assert kwargs["link_label"] == "GitHub 1"    # the link label stays the short key
```

Also in `tests/test_sync_main.py`, update the shared `_card()` fixture (line ~41) so the board card represents the post-transition state (otherwise every existing main() test would now see a spurious customId rewrite op):

```python
def _card():
    # "description": "" (issue #65) keeps agileplace_description.card_description() on its zero-I/O path --
    # without the key it falls back to the real (unmocked) agileplace.get_card(), which hits the
    # live HTTP client and SystemExits (confirmed live in the design spike).
    # customId carries the issue #93 header format (the post-transition board state); the
    # old-format upgrade path keeps its own dedicated test below.
    return {"id": "C1", "version": 1, "customId": "GitHub Issue #1",
            "externalLink": {"url": ISSUE_URL}, "tags": [],
            "plannedStart": "2026-02-01", "plannedFinish": None, "laneId": None,
            "description": ""}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest -q tests/test_sync.py -k header_format tests/test_sync_main.py -k "header_format or fallback_matches or short_link_label"`
Expected: the four new tests FAIL (wrong op values / unexpected create / missing link_label kwarg).

- [ ] **Step 3: Implement the wiring (five small edits)**

1. `sync.py:36` — extend the stages import:

```python
from stages import (epic_key_for_task, header_match_key, is_retired_issue, issue_card_header,
                    issue_custom_id,
```
(keep the remaining imported names exactly as they are — only add the two new names in alphabetical position).

2. `_reconciled_custom_id_index` (line ~312):

```python
        current_custom_id = header_match_key(agileplace.custom_id_value(url_match))
```

3. Create site in `_ensure_cards_for_syncable_issues` (line ~500):

```python
        created = agileplace.create_card(cfg, apply, issue_card_title(issue),
                                         issue_card_header(issue), issue["url"],
                                         lane["id"] if lane else None,
                                         type_id=type_id, type_title=derived_type if type_id else None,
                                         link_label=f"GitHub {key}")
```
(`key = issue_custom_id(issue)` already exists above; the `card_by_cid[key] = created` registration below stays keyed by `key` — match space.)

4. Board index build (line ~604):

```python
        cid = header_match_key(agileplace.custom_id_value(card))
```

5. Drift rewrite (lines ~676-678):

```python
        header = issue_card_header(issue)
        if agileplace.custom_id_value(card) != header:
            queue(card, [agileplace.op_custom_id(header)], f"customId->{header}")
            print(f"{'sync ' if apply else 'DRY  '} [{key}] customId")
```

- [ ] **Step 4: Run the full suite; repair fixtures representing "already in sync" cards**

Run: `python -m pytest -q`
Any remaining failure will be an existing main()-level test whose hand-built card carries a key-format customId and now sees one extra `/customId` op (or a changed op count). Policy: if the test's intent is "card already in sync", update that fixture's customId to its issue's header format (`issue_card_header` of the fixture issue — e.g. `"1"` → `"GitHub Issue #1"`, `"X"` → `"X (GitHub Issue #1)"`); keep old-format values ONLY in tests that specifically exercise the upgrade. Do NOT relax op-count assertions. Likely files: `tests/test_sync_main.py` (inline cards beyond `_card()`), `tests/test_sync_intake_call_site.py`, `tests/test_sync_card_coherence.py`, `tests/test_sync_card_types.py`.

Expected after repairs: full suite PASS.

- [ ] **Step 5: Commit**

```bash
git add sync.py tests/test_sync.py tests/test_sync_main.py
git add -u tests/
git commit -m "feat(sync): write header-format customIds, match in normalized key space (#93)

Existing cards upgrade through the ordinary drift reconciliation -- no
migration script. Indexes normalize via header_match_key so old- and
new-format cards keep matching during the transition.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: `intake.py` — writeback writes the header; candidate check normalizes

**Files:**
- Modify: `intake.py:24` (import), `intake.py:145` (`_is_candidate` last line), `intake.py:217-222` (`_writeback_key` → `_writeback_header`), `intake.py:255-257` (`_writeback` call)
- Test: `tests/test_intake.py` (append)

**Interfaces:**
- Consumes: `stages.issue_card_header`, `stages.header_match_key` (Task 1).
- Produces: promoted cards carry header-format customIds; `_is_candidate` never mistakes a managed card (either format) for a candidate.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_intake.py`; it imports the `intake` module)

```python
# --- issue #93: header-format customId ------------------------------------------------------

def test_writeback_header_carries_the_issue_number():
    assert intake._writeback_header("[0C1] Fix the thing", 5) == "0C1 (GitHub Issue #5)"


def test_writeback_header_unkeyed_title_is_the_bare_github_reference():
    assert intake._writeback_header("Fix the thing", 7) == "GitHub Issue #7"


def test_is_candidate_disqualifies_a_header_format_custom_id():
    """A managed card already rewritten to '0C1 (GitHub Issue #5)' must normalize back to '0C1'
    for the managed-set check -- NOT become an intake candidate (which would file a duplicate
    GitHub issue for a card the sync already owns)."""
    card = {"id": "C9", "title": "Some card", "laneId": "L1",
            "customId": "0C1 (GitHub Issue #5)"}
    assert not intake._is_candidate(card, {"L1"}, set(), {"0C1"})


def test_is_candidate_still_disqualifies_an_old_format_custom_id():
    card = {"id": "C9", "title": "Some card", "laneId": "L1", "customId": "0C1"}
    assert not intake._is_candidate(card, {"L1"}, set(), {"0C1"})
```

(If `tests/test_intake.py`'s `_is_candidate` helpers use a different card-lane shape, mirror the shape its existing `_is_candidate` tests use — the lane must be IN `intake_lane_ids`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest -q tests/test_intake.py -k "writeback_header or format_custom_id"`
Expected: FAIL — `AttributeError: module 'intake' has no attribute '_writeback_header'`, and the header-format candidate test asserts False but gets True.

- [ ] **Step 3: Implement**

1. `intake.py:24`:

```python
from stages import header_match_key, issue_card_header, issue_custom_id, title_key
```

2. `_is_candidate` last line (line ~145):

```python
    return header_match_key(agileplace.custom_id_value(card)) not in managed_custom_ids
```

3. Rename + reimplement `_writeback_key` (lines ~217-222). First check nothing else references the old name (`grep -rn _writeback_key . tests/`), then:

```python
def _writeback_header(card_title: str, issue_number: int) -> str:
    """The customId written back onto a promoted card -- the SAME header format the ordinary sync
    writes (stages.issue_card_header, issue #93), computed from the CARD's own title, never
    fetched back from GitHub. header_match_key() folds it back to the key `_is_candidate`'s
    disqualification check and the ordinary sync's matching use."""
    return issue_card_header({"title": card_title, "number": issue_number})
```

4. In `_writeback` (line ~255), the call site becomes:

```python
    key = _writeback_header(card.get("title", ""), issue["number"])
```
(the following `patch_card(..., note=f"intake customId -> {key}")` line is unchanged and now logs the header).

- [ ] **Step 4: Run the intake suites; repair promote-flow expectations**

Run: `python -m pytest -q tests/test_intake.py tests/test_intake_writeback_version_conflict.py tests/test_sync_intake_call_site.py tests/test_ghkit_intake.py`
Any failure asserting the written-back customId equals the bare key: update the expected value to the header format (same policy as Task 3 — never relax the assertion, update the expected string).
Expected after repairs: PASS. Then `python -m pytest -q` — full suite PASS.

- [ ] **Step 5: Commit**

```bash
git add intake.py tests/
git commit -m "feat(intake): card->issue writeback writes the header-format customId (#93)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: `card_coherence.py` — retired-card index normalizes

**Files:**
- Modify: `card_coherence.py:47` (import), `card_coherence.py:207-210` (`retired_card_by_cid` inside `fence_run_indices`)
- Test: `tests/test_card_coherence.py` (append)

**Interfaces:**
- Consumes: `stages.header_match_key` (Task 1).
- Produces: retirement reservations keep deferring active issues whose key is held by a retiring card in EITHER customId format.

- [ ] **Step 1: Write the failing test** (append to `tests/test_card_coherence.py`)

```python
def test_retirement_reservation_matches_a_header_format_retiring_card():
    """issue #93: a retiring card already rewritten to 'KEY (GitHub Issue #2)' still reserves the
    key 'KEY' -- the active issue sharing it must defer, exactly as with an old-format card."""
    active = {"url": "https://github.com/o/r/issues/1", "title": "[KEY] one", "number": 1}
    retired = {"url": "https://github.com/o/r/issues/2", "title": "[KEY] two", "number": 2,
              "state_reason": "NOT_PLANNED"}
    retiring_card = {"id": "200", "customId": "KEY (GitHub Issue #2)"}
    all_card_by_url = {retired["url"]: retiring_card}
    all_card_by_cid = {"KEY": retiring_card}  # sync builds this index normalized (Task 3)

    result = fence_run_indices({}, [active], [retired], all_card_by_url, all_card_by_cid)

    assert result.syncable_issues == [], "the active issue must be deferred, not adopt the retiring card"
    assert len(result.warnings) == 1
    assert result.warnings[0].startswith("WARN  deferring active card [KEY]: customId is held by")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest -q tests/test_card_coherence.py -k header_format_retiring`
Expected: FAIL — `result.syncable_issues == [active]` (the raw-keyed internal index misses the reservation).

- [ ] **Step 3: Implement**

1. `card_coherence.py:47`:

```python
from stages import header_match_key, issue_custom_id
```

2. Inside `fence_run_indices` (lines ~207-210):

```python
    retired_card_by_cid = {
        header_match_key(agileplace.custom_id_value(card)): card
        for card in retired_cards if agileplace.custom_id_value(card)
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest -q tests/test_card_coherence.py tests/test_sync_card_coherence.py tests/test_sync_contested_cards.py`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add card_coherence.py tests/test_card_coherence.py
git commit -m "feat(card_coherence): retirement reservations normalize header-format customIds (#93)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: smoke step 23 + docs (project rule: every feature gets a smoke step)

**Files:**
- Modify: `smoke.py` (new `_check_custom_id_header` after `_check_github_richtext_roundtrip` ends at line ~460; wire into `_run_checks` at line ~638; extend the module docstring's feature list with "a customId header-format round-trip (issue #93)")
- Modify: `tests/test_smoke.py` (fake tenant `_patch` + expected sequence + one new failure-mode test)
- Modify: `API-VALIDATION.md` (new `[live-check]` item), `README.md` (document the header format near the customId-fallback sentence at line 14)

**Interfaces:**
- Consumes: `agileplace.get_card`, `agileplace.patch_card`, `agileplace.op_custom_id`, `agileplace.custom_id_value` (all exist); `PARENT_CUSTOM_ID_PREFIX` (exists).
- Produces: smoke step 23 — the live proof the AgilePlace API preserves parens/`#`/spaces in customId.

- [ ] **Step 1: Write the failing tests** (in `tests/test_smoke.py`)

1. In `FakeTenant.__init__`-adjacent flags, add `ignore_custom_id = False` alongside `ignore_external_link` (mirror its declaration style). In `_patch`'s op loop add:

```python
            elif op["path"] == "/customId":
                if not self.ignore_custom_id:
                    card["customId"] = op["value"]
```

2. In `test_confirmed_run_executes_whole_sequence_and_cleans_up`, insert into `world.writes` after the two richtext PATCH lines (before the cleanup DELETEs):

```python
        ("PATCH", "card/S1"),             # customId header-format round-trip (issue #93)
```

and add to the output assertions:

```python
    assert "customId header-format round-trip" in out
```

3. New failure-mode test (2xx-is-not-proof, mirroring `test_ignored_external_link_write_is_reported_as_failure`):

```python
def test_ignored_custom_id_write_is_reported_as_failure(tenant_env, capsys):
    """A 2xx PATCH is not proof: the header must be read back, so a server that silently ignores
    the /customId replace (or normalizes away the parens/#) is reported as a failing shape."""
    tenant = FakeTenant()
    tenant.ignore_custom_id = True
    tenant_env(tenant)

    assert smoke.main([]) == 1

    out = capsys.readouterr().out
    assert "FAIL  customId header-format round-trip" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest -q tests/test_smoke.py`
Expected: FAIL — the sequence test's expected `writes` list has one extra PATCH the run never makes, and the new test finds no such summary line.

- [ ] **Step 3: Implement in `smoke.py`**

Add after `_check_github_richtext_roundtrip`:

```python
def _check_custom_id_header(cfg: dict, parent_id: str, run_id: str, results: list) -> None:
    """Step 23: header-format customId round-trip (issue #93). The sync now writes customIds like
    '0C1 (GitHub Issue #5)'; this proves the live API accepts and preserves parens, '#', and
    spaces verbatim. The probe value keeps the per-run smoke prefix so stages.header_match_key()
    normalizes a leaked leftover to this run's unique key -- NEVER write a bare 'GitHub Issue #N'
    here (that would normalize to a real unkeyed issue's match key and could be adopted by a
    later sync run)."""
    _step(23, "customId header-format round-trip -- parens/#/spaces must survive verbatim")
    header = f"{PARENT_CUSTOM_ID_PREFIX}{run_id} (GitHub Issue #999999)"
    fresh = agileplace.get_card(cfg, parent_id)
    agileplace.patch_card(cfg, True, fresh, [agileplace.op_custom_id(header)])
    echoed = agileplace.custom_id_value(agileplace.get_card(cfg, parent_id))
    results.append(("customId header-format round-trip", echoed == header,
                    f"read back {echoed!r}"))
```

Wire it as the LAST check in `_run_checks` (after `_check_github_richtext_roundtrip(cfg, parent_id, results)`):

```python
    _check_custom_id_header(cfg, parent_id, run_id, results)
```

(`_run_checks` already receives `run_id`.) Extend the module docstring's enumerated write shapes with `a customId header-format round-trip (issue #93)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest -q tests/test_smoke.py`
Expected: all PASS.

- [ ] **Step 5: Update docs**

1. `API-VALIDATION.md`: add, matching the surrounding `[live-check]` bullet style in the card-write section:

```markdown
- [live-check] `customId` accepts and preserves an issue #93 header-format value
  (`KEY (GitHub Issue #N)` — parens, `#`, spaces) verbatim on PATCH replace, confirmed by
  refetch. Exercised by smoke step 23.
```

2. `README.md`: directly after the line-14 sentence describing URL-first/customId-fallback matching, add:

```markdown
Card headers (the AgilePlace `customId`) carry the issue reference — `0C1 (GitHub Issue #5)`,
or `GitHub Issue #5` for issues without a `[KEY]` title prefix. Existing cards are upgraded in
place by the ordinary sync run.
```

(Adjust wrapping to the file's prose width; keep the surrounding paragraph's tone.)

- [ ] **Step 6: Commit**

```bash
git add smoke.py tests/test_smoke.py API-VALIDATION.md README.md
git commit -m "feat(smoke): step 23 proves header-format customIds round-trip live (#93)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Full verification

**Files:** none new.

- [ ] **Step 1: Full suite**

Run: `python -m pytest -q`
Expected: 0 failures (baseline was 1,446 tests passing; this branch adds ~20).

- [ ] **Step 2: Dry-run sanity (only if `.env` is configured on this machine)**

Run: `python sync.py` (no `--apply`)
Expected in the DRY output: existing cards show `customId->… (GitHub Issue #N)` rewrite plans; no duplicate-create plans for cards that already exist. If `.env` is absent, note it and skip — the live check happens on the work PC alongside the smoke run.

- [ ] **Step 3: Verify branch state and hand off**

Run: `git log --oneline main..feat/issue-93` — spec + plan + 6 implementation commits, none on main.
Do NOT push or open a PR in this task — branch completion (push, draft PR per repo rules, review) is driven by the superpowers:finishing-a-development-branch flow after the user reviews.
