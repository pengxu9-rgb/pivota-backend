"""Cosmetic guard for product/identity names before they render into
merchant-facing copy (NBA, strategic brief, verdict headers).

CONSERVATIVE BY DESIGN. This removes only unambiguous serialization debris and
one known rendering artifact (a trailing "'s page", e.g. a title like
"...30 sticks" that a template turned into "...30 sticks's page" and leaked into
a name field). It deliberately does NOT touch legitimate names: apostrophes
("Paula's Choice", "NOMAD'S CREAM"), parentheses/unit qualifiers ("(75ml)"),
ampersands ("Bond & Repair"), and bracketed tags all pass through unchanged.

This is a render-time band-aid, not a fix. The durable fix is clean identity
resolution upstream (ADR-009); over-aggressive sanitizing here would mangle real
names, which is a worse trust failure than the rare dirty one.
"""
from __future__ import annotations

import re
from typing import Any, Optional

# A trailing possessive-"page" artifact: "...30 sticks's page" -> "...30 sticks".
# Real product names do not end in "'s page"; this only ever comes from a
# "{title}'s page" template leaking into a name field. Anchored to end-of-string.
_TRAILING_POSSESSIVE_PAGE = re.compile(r"[’'`’]s\s+page\s*$", re.IGNORECASE)

# Trailing truncation debris: an ellipsis char or a run of 2+ dots at the end.
# (Single trailing "." is left alone — it can be legitimate, e.g. an abbrev.)
_TRAILING_ELLIPSIS = re.compile(r"(?:\.{2,}|…)\s*$")

_WHITESPACE = re.compile(r"\s+")

# Straight + curly quote/backtick characters we treat as wrapping if they
# enclose the WHOLE string (a single layer only).
_WRAPPING_QUOTES = ("\"", "'", "`", "“", "”", "‘", "’")

_MAX_LEN = 100


def sanitize_display_name(value: Any, *, max_len: int = _MAX_LEN) -> str:
    """Return a merchant-safe display name, or "" when there's nothing usable.

    Callers keep their own fallback (e.g. `sanitize_display_name(x) or "this SKU"`).
    Idempotent; safe on None.
    """
    if value is None:
        return ""
    text = _WHITESPACE.sub(" ", str(value)).strip()
    if not text:
        return ""

    # Strip a single layer of wrapping quotes only if BOTH ends are quotes.
    if len(text) >= 2 and text[0] in _WRAPPING_QUOTES and text[-1] in _WRAPPING_QUOTES:
        inner = text[1:-1].strip()
        if inner:
            text = inner

    text = _TRAILING_POSSESSIVE_PAGE.sub("", text).strip()
    text = _TRAILING_ELLIPSIS.sub("", text).strip()
    if not text:
        return ""

    # Length cap so a paragraph-as-name can't render as a title.
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text
