r"""Merchant-controlled text made safe to STORE and SHOW as one line: `clean_display_text`.

WHY THIS EXISTS. The Reap cart-link lane records the live Shopify variant title ("07 BURGUNDY
INK") from the storefront's own `/products/<handle>.js` into the cart proof, and from there it
reaches `CartLinkItem.variant_title`, `reap_agentic_purchases.variant_title` and the 202/replay
response bodies (#2459). That title is typed by the MERCHANT. Before this function it was passed
through verbatim, so a newline, U+202E RIGHT-TO-LEFT OVERRIDE (which reverses the rendering of
everything after it in an owner view, an email or a log line), a zero-width space or a private-use
glyph all survived into storage and into our own responses.

THE RULE, in order:

1. Every whitespace character -- including the control-category ones, TAB and LF -- is a
   separator, so it is KEPT by step 2 and folded by step 3. Folding rather than dropping is why
   "07\nBURGUNDY" reads "07 BURGUNDY", not "07BURGUNDY".
2. Every other character in Unicode category Cc (control), Cf (format: the bidi embeddings and
   overrides U+202A-U+202E, the isolates U+2066-U+2069, LRM/RLM, ZWSP U+200B, ZWJ/ZWNJ, U+FEFF),
   Co (private use) or Cs (a lone surrogate, which json.loads makes from "\ud83d") is DROPPED.
   None of them renders as itself, so none of them is part of what a merchant sells.
   Cn (unassigned) is deliberately NOT dropped: what is unassigned depends on the running
   Python's Unicode version, and a new emoji must not vanish on an older interpreter.
3. Runs of whitespace collapse to ONE space, and the ends are stripped.
4. NFC-normalise, so one title typed two ways is stored one way. This runs AFTER the drop, not
   before it: "e", U+200B, U+0301 only composes to "\u00e9" once the ZWSP is gone, and doing it
   first would leave a result that a second pass changes (the rule must be idempotent, because
   the reader re-applies it to what the writer already cleaned).
5. Cap at `max_chars` CODE POINTS -- a Python str index is a code point, so a non-BMP emoji is
   never split into half a surrogate pair -- strip the end again, and map empty to None.

HTML IS LEFT AS TEXT, ON PURPOSE. "<b>Rose</b>" stays "<b>Rose</b>". This is a storage/display
guard, not an escaper: escaping here would double-escape in every renderer that already escapes,
and a renderer that does NOT escape is wrong whatever we store. Escaping is the renderer's job.

NEAR-OWNERS, and why this is not one of them. `services.reap_agentic_client._safe_partner_text`
cleans Reap's partner labels for display and keeps Co/Cs and does not normalise;
`services.reap_agentic_purchase._clean_buyer_text` strips buyer fields but does not fold whitespace
(a buyer's street keeps its shape); `services.text_normalization.display_name.sanitize_display_name`
is a cosmetic guard (quotes, trailing ellipsis) that appends "..." on cap. Each has callers whose
behaviour must not move, so this is the one rule for merchant display text going forward.
"""
from __future__ import annotations

import unicodedata
from typing import Any, Optional

#: Unicode general categories dropped from display text. See the module docstring, step 2.
DROPPED_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cs"})


def clean_display_text(value: Any, *, max_chars: int) -> Optional[str]:
    """One line of merchant text, safe to store and show, at most `max_chars` code points; or None.

    Anything that is not a `str` is None: this never invents text from a dict or a number.
    Idempotent: `clean_display_text(clean_display_text(x, max_chars=n), max_chars=n)` is the same.
    """
    if not isinstance(value, str):
        return None
    text = "".join(
        ch for ch in value if ch.isspace() or unicodedata.category(ch) not in DROPPED_CATEGORIES
    )
    text = unicodedata.normalize("NFC", " ".join(text.split()))
    return text[:max_chars].rstrip() or None
