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
2. Every other character in Unicode category Cc (control), Cf (format), Co (private use) or Cs
   (a lone surrogate, which json.loads makes from "\ud83d") is DROPPED -- EXCEPT the four format
   characters in `KEPT_FORMAT_CHARS`. What goes: the bidi embeddings and overrides
   U+202A-U+202E and the isolates U+2066-U+2069 (the ones that reverse or re-order the text
   AROUND them, e.g. the variant title a door appends after the product name), the invisible
   spacers (ZWSP U+200B, U+FEFF, WORD JOINER U+2060, SOFT HYPHEN U+00AD, U+180E), and the
   emoji TAG characters U+E0020-U+E007F (invisible ASCII -- "ASCII smuggling" into an agent's
   context; services/llm_fence strips them for the same reason, and the cost is that a
   subdivision flag degrades to a plain black flag).
   Cn (unassigned) is deliberately NOT dropped: what is unassigned depends on the running
   Python's Unicode version, and a new emoji must not vanish on an older interpreter.
   KEPT, because dropping them only mangles legitimate display and none of them can re-order
   text beyond itself: ZWNJ U+200C and ZWJ U+200D (Persian and Indic orthography, and the joiner
   inside emoji sequences such as a family or "woman technologist"), and LRM U+200E / RLM U+200F
   (single strong-direction MARKS that mixed Hebrew/Arabic + Latin titles use to place a
   neighbouring number or bracket -- a mark has no scope, unlike an embedding, override or
   isolate, so it cannot flip a run of text that follows it).
3. Runs of whitespace collapse to ONE space, and the ends are stripped.
4. NFC-normalise, so one title typed two ways is stored one way. This runs AFTER the drop, not
   before it: "e", U+200B, U+0301 only composes to "\u00e9" once the ZWSP is gone, and doing it
   first would leave a result that a second pass changes (the rule must be idempotent, because
   the reader re-applies it to what the writer already cleaned).
5. Cap at `max_chars` CODE POINTS -- a Python str index is a code point, so a non-BMP emoji is
   never split into half a surrogate pair -- then strip the END of spaces AND of the kept format
   characters, and map empty to None. Stripping the kept characters there does two jobs: a cap
   that cuts between a ZWJ and the emoji it joins leaves no dangling joiner, and a title that is
   nothing but kept characters and spaces (a lone LRM, "ZWJ ZWNJ") is None rather than a
   string that shows nothing. The cost: a trailing LRM/RLM a merchant put after a final bracket
   is dropped; a door appends its own text after the name anyway.

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

#: The Cf characters that are NOT dropped: ZWNJ, ZWJ, LRM, RLM. See the module docstring, step 2.
KEPT_FORMAT_CHARS = frozenset({"\u200c", "\u200d", "\u200e", "\u200f"})

#: What the end of a result is stripped of: the one space whitespace folds to, and the kept
#: format characters, none of which means anything with nothing visible after it.
_TRAILING_STRIP = " " + "".join(sorted(KEPT_FORMAT_CHARS))

#: Longest product name shown, in code points. The ledger's `product_name` is TEXT (no limit), so
#: this is Shopify's own product-title limit: no real storefront title is longer.
MAX_PRODUCT_NAME = 255


def clean_display_text(value: Any, *, max_chars: int) -> Optional[str]:
    """One line of merchant text, safe to store and show, at most `max_chars` code points; or None.

    Anything that is not a `str` is None: this never invents text from a dict or a number.
    Idempotent: `clean_display_text(clean_display_text(x, max_chars=n), max_chars=n)` is the same.
    """
    if not isinstance(value, str):
        return None
    text = "".join(
        ch for ch in value
        if ch.isspace() or ch in KEPT_FORMAT_CHARS
        or unicodedata.category(ch) not in DROPPED_CATEGORIES
    )
    text = unicodedata.normalize("NFC", " ".join(text.split()))
    # AFTER the cap, strip the end of whitespace AND of the kept format characters (step 5): a cap
    # can cut between a ZWJ and the emoji it joins, leaving a dangling joiner; and a title made of
    # nothing but kept characters and spaces ("LRM", "ZWJ ZWNJ") would otherwise be a non-None
    # string that shows nothing. Folded whitespace is only ever " " here, so " " is the whole set.
    return text[:max_chars].rstrip(_TRAILING_STRIP) or None


def clean_product_name(value: Any) -> Optional[str]:
    """A catalog/storefront product name for display: `clean_display_text` at `MAX_PRODUCT_NAME`.

    The cart-link lane stores it on the purchase row, and a door composes the line item as
    "<product name> -- <variant title>": an unterminated U+202E left in the name would reverse the
    (clean) variant title printed after it, so both halves go through the same rule."""
    return clean_display_text(value, max_chars=MAX_PRODUCT_NAME)
