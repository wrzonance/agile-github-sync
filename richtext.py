"""Bidirectional translator between GitHub-flavored Markdown and AgilePlace's HTML subset. No I/O --
pure string transforms, exhaustively unit-tested.

Supported subset (both directions)
-----------------------------------
| Markdown              | AgilePlace HTML          |
|------------------------|---------------------------|
| ``**bold**``           | ``<strong>``              |
| ``*italic*``           | ``<em>``                  |
| ``~~strike~~``         | ``<s>``                   |
| `` `code` ``           | ``<code>``                |
| fenced code block      | ``<pre><code>``           |
| ``# Heading`` .. ``######`` | ``<h1>`` .. ``<h6>`` |
| ``- item`` / ``1. item`` | ``<ul><li>`` / ``<ol><li>`` |
| ``[text](href)``       | ``<a href="...">``        |
| blank-line paragraphs  | ``<p>``                   |
| single newline         | ``<br>``                  |

Degradation table (asymmetries -- never a crash, never unsanitized passthrough)
--------------------------------------------------------------------------------
| Input                                          | Output                                        |
|--------------------------------------------------|-------------------------------------------------|
| ``<u>...</u>`` (no Markdown equivalent)          | inline content kept, tag silently dropped       |
| Any HTML tag outside the supported set           | inline content kept, tag silently dropped       |
| ``javascript:``/other disallowed href scheme     | link degrades to text, href omitted             |
| Unclosed ``<strong>``/``<em>``/``<s>``/``<code>`` | force-closed at end of output (stays balanced)  |
| Raw ``<tag>`` typed directly into Markdown source | escaped to literal text, never parsed as a tag  |
| Markdown delimiter with no matching close         | degrades to literal delimiter text, no tag      |
| Nesting past _MAX_INLINE_DEPTH levels deep         | remainder emitted as literal text, never raises |

Implementation is split across three modules kept under this package's file-size budget:
``_richtext_shared`` (data shapes and sanitizers both directions depend on), ``_richtext_html_to_md``
(the HTML->Markdown walker), and ``_richtext_md_to_html`` (the Markdown->HTML block/inline
renderer). This module re-exports the combined public and test-facing surface so callers only ever
need ``import richtext``.
"""
from __future__ import annotations

from _richtext_html_to_md import (
    _escape_markdown_text,
    _MarkdownWalker,
    leankit_html_to_markdown,
)
from _richtext_md_to_html import (
    _MAX_INLINE_DEPTH,
    _escape_html_text,
    _parse_blocks,
    _render_block_html,
    _render_inline_html,
    _unescape_markdown_text,
    markdown_to_leankit_html,
)
from _richtext_shared import _Block, _ListFrame, _sanitize_href

__all__ = [
    "leankit_html_to_markdown",
    "markdown_to_leankit_html",
]
