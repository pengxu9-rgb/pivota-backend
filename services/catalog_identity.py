"""Content-derived product identity for catalog_products.content_key.

Sibling of services/catalog_sync_service.make_pivota_signature_id (the
sig_* minter). Where sig_* identifies a (merchant, platform, source_id)
tuple — deliberately merchant-scoped — content_key identifies the
*physical product* across merchants and paths. Same brand + title +
GTIN → same key, regardless of who's selling it.

See plans/rosy-mixing-bengio.md Stage 1 for the broader roadmap.

Composition (locked design 2026-05-12):

    content_key = "ck_" + sha256(
        normalize_brand(brand) + "::" +
        normalize_title(title) + "::" +
        (gtin or "")
    )[:32]

Returns None when brand or title are empty — we don't want a deceptive
all-collide key like ck_<hash_of_empty_brand>.

Normalization is intentionally minimal in v1. Lowercase, collapse
whitespace, strip punctuation. We can tighten as Stage 1 duplicate
inventory telemetry reveals real-world drift (e.g. "Tom Ford" vs
"Tom Ford Beauty" — same brand, different surface — likely needs
brand-alias map). Start simple; iterate from data.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Optional


_KEY_PREFIX = "ck_"
_KEY_HEX_LEN = 32  # sha256[:32] = 128 bits, plenty for collision-free at our scale


# Brand-suffix tokens that don't carry identity information. Stripped
# during normalize_brand so "Glow Recipe", "Glow Recipe Inc.", and
# "Glow Recipe LLC" all produce the same brand_normalized.
_BRAND_SUFFIX_TOKENS = (
    "inc", "inc.",
    "llc", "llc.",
    "ltd", "ltd.",
    "corp", "corp.",
    "co.", "co",
    "company",
    "®", "™", "(r)", "(tm)",
)


# Title-suffix tokens often used as size/qualifier — kept for now (size
# IS identity for our purposes; 30ml vs 50ml are different products).
# This is a deliberate non-strip. If duplicate inventory shows over-
# splitting on packaging variants, revisit.


def normalize_brand(brand: Optional[str]) -> str:
    """Lowercase, strip suffix tokens (Inc/LLC/®/™), trim. Returns ''
    on None/empty (caller decides whether that's acceptable)."""
    if not brand or not isinstance(brand, str):
        return ""
    text = brand.strip().lower()
    # Strip registered/trademark marks anywhere in the string
    for mark in ("®", "™"):
        text = text.replace(mark, "")
    # Strip parenthesized marks
    text = re.sub(r"\s*\((r|tm)\)\s*", " ", text)
    # Strip trailing suffix tokens (Inc, LLC, Co., etc.)
    tokens = text.split()
    while tokens and tokens[-1].rstrip(".,") in {t.rstrip(".,") for t in _BRAND_SUFFIX_TOKENS}:
        tokens.pop()
    text = " ".join(tokens)
    # Collapse internal whitespace
    return re.sub(r"\s+", " ", text).strip()


def normalize_title(title: Optional[str]) -> str:
    """Unicode-normalize, lowercase, strip non-essential punctuation,
    collapse whitespace. Returns '' on None/empty.

    Punctuation policy: keep hyphens (they often distinguish products
    like 'Anti-Aging Serum'), drop everything else. Numbers/units kept
    (sizes are identity)."""
    if not title or not isinstance(title, str):
        return ""
    # NFKD strips combining accents → "Crème" becomes "creme"
    text = unicodedata.normalize("NFKD", title)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    # Drop punctuation except hyphen and digits/letters/spaces
    text = re.sub(r"[^\w\s\-]", " ", text, flags=re.UNICODE)
    # Collapse repeated whitespace and trim
    return re.sub(r"\s+", " ", text).strip()


def normalize_gtin(gtin: Optional[str]) -> str:
    """Canonicalize a GTIN to its 14-digit GS1 form.

    Real-world catalogs mix:
      - GTIN-12 / UPC-A (12 digits) — common US retail
      - GTIN-13 / EAN-13 (13 digits) — common EU + most modern retail
      - GTIN-14 (14 digits) — case-pack / outer-pack codes
      - Misc separators (hyphens, spaces) and leading zeros

    All four refer to the same physical product when the underlying
    digits match — GS1 explicitly defines shorter forms as right-aligned
    within the 14-digit form padded with leading zeros. So the canonical
    move is: strip non-digits, then left-pad to 14. Now "773602443796"
    (UPC-A), "0773602443796" (GTIN-13), and "00773602443796" (GTIN-14)
    all produce "00773602443796" → collide on content_key.

    Returns '' on empty/None — caller's content_key falls back to
    brand+title only.

    Edge case: a 15+ digit input is malformed; pass through unchanged
    rather than truncating, so we don't silently merge unrelated codes."""
    if not gtin or not isinstance(gtin, str):
        return ""
    digits = re.sub(r"\D", "", gtin.strip())
    if not digits:
        return ""
    if len(digits) <= 14:
        return digits.zfill(14)
    return digits  # malformed, leave as-is


def make_content_key(
    brand: Optional[str],
    title: Optional[str],
    gtin: Optional[str] = None,
) -> Optional[str]:
    """Returns the content_key for a (brand, title, gtin) triple, or
    None when brand or title are empty.

    Same brand + title + missing-GTIN → same key (GTIN coerced to '').
    Same brand + title + different GTIN → different keys.
    Different brand OR different title → different keys.

    Stable across runtime; no randomness. The 32-hex truncation is
    plenty for our scale (4M+ keys before 0.001% collision risk per
    birthday-bound estimate)."""
    brand_norm = normalize_brand(brand)
    title_norm = normalize_title(title)
    if not brand_norm or not title_norm:
        return None
    gtin_norm = normalize_gtin(gtin)
    raw = f"{brand_norm}::{title_norm}::{gtin_norm}".encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()[:_KEY_HEX_LEN]
    return f"{_KEY_PREFIX}{digest}"


def is_content_key(value: Optional[str]) -> bool:
    """True iff value looks like a content_key. Used by Stage 3's
    /api/agent/pdp/{id} endpoint to dispatch id type."""
    if not isinstance(value, str):
        return False
    if not value.startswith(_KEY_PREFIX):
        return False
    rest = value[len(_KEY_PREFIX):]
    return len(rest) == _KEY_HEX_LEN and all(c in "0123456789abcdef" for c in rest)
