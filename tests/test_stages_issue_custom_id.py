"""issue_custom_id relocation (Task 1/8, issue #62).

issue_custom_id moved from sync.py to stages.py verbatim (pure relocation, no behavior
change). sync.py re-exports it via `from stages import issue_custom_id` so every existing
call site -- including `from sync import issue_custom_id` in
tests/test_removal_authority_card_ids.py and tests/test_sync_dependencies.py -- keeps
resolving to the exact same function object. Run: pytest -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sync  # noqa: E402
from stages import issue_custom_id  # noqa: E402


def test_uses_title_key_when_present():
    issue = {"number": 42, "title": "[EP-0C] Some epic"}
    assert issue_custom_id(issue) == "EP-0C"


def test_falls_back_to_issue_number_when_no_title_key():
    issue = {"number": 42, "title": "Untagged title"}
    assert issue_custom_id(issue) == "42"


def test_sync_module_reexports_the_same_function_object():
    """`from sync import issue_custom_id` must keep resolving -- sync.py imports it from
    stages.py rather than defining its own copy."""
    assert sync.issue_custom_id is issue_custom_id
