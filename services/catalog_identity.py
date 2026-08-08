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
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Mapping, Optional


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

    Edge case: a 15+ digit input is malformed and passes through unchanged.
    Note the `<= 14` branch is belt-and-braces rather than load-bearing: zfill
    never truncates, so a bare `digits.zfill(14)` is provably identical. The
    branch documents the intent; the guarantee that we don't silently merge
    unrelated codes comes from zfill's behaviour, not from the gate."""
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


# ---------------------------------------------------------------------------
# P0.2 — deposit gate: which content_keys may receive an entity-scoped deposit.
#
# content_key is nullable + DELIBERATELY non-unique (db/migrations/083): a
# no-GTIN SKU collides on brand+title alone, so two different physical products
# can share one key. An audit that deposits citation_observations / claims /
# offers against such a key cross-contaminates one brand's evidence onto
# another's entity — and downstream lets the claim flow capture a competitor's
# merged product. So before any entity-scoped deposit, the key must be
# RESOLVED: GTIN-derived, high-confidence identity-resolved, or human-reviewed.
# Unresolved subjects still record the audit run + report_jsonb (merchant /
# product_key-scoped, no regression); they just don't accrete on the entity.
# ---------------------------------------------------------------------------

_DEPOSIT_MIN_CONFIDENCE_DEFAULT = 0.85

# How a content_key earned the right to receive an entity-scoped deposit.
DEPOSIT_BASIS_GTIN = "gtin"
DEPOSIT_BASIS_IDENTITY = "identity_high_conf"
DEPOSIT_BASIS_REVIEWED = "reviewed"
DEPOSIT_BASIS_UNRESOLVED = "unresolved"


def deposit_min_confidence() -> float:
    """Identity-confidence threshold for the `identity_high_conf` basis.
    Env-overridable via CONTENT_KEY_DEPOSIT_MIN_CONFIDENCE; conservative
    default so we err toward NOT depositing on a shaky key."""
    raw = os.getenv("CONTENT_KEY_DEPOSIT_MIN_CONFIDENCE")
    if raw is None:
        return _DEPOSIT_MIN_CONFIDENCE_DEFAULT
    try:
        return float(raw)
    except (TypeError, ValueError):
        return _DEPOSIT_MIN_CONFIDENCE_DEFAULT


@dataclass(frozen=True)
class ResolvedDepositKey:
    """Result of the deposit gate. Callers MUST check `is_depositable` before
    writing entity-scoped rows. `content_key` is returned even when unresolved
    (for observability / tagging), but `is_depositable` is then False."""

    content_key: Optional[str]
    basis: str
    confidence: float

    @property
    def is_depositable(self) -> bool:
        return self.basis != DEPOSIT_BASIS_UNRESOLVED and bool(self.content_key)


def resolve_deposit_content_key(
    *,
    brand: Optional[str] = None,
    title: Optional[str] = None,
    gtin: Optional[str] = None,
    existing_content_key: Optional[str] = None,
    identity_confidence: Optional[float] = None,
    review_state: Optional[str] = None,
    min_confidence: Optional[float] = None,
) -> ResolvedDepositKey:
    """Decide whether (and on which basis) an audit deposit may write
    entity-scoped rows against a content_key.

    Precedence, strongest first:
      1. ``gtin``              — a real GTIN was folded into the key
                                 (GS1-canonical; cross-merchant collision is
                                 intended and correct).
      2. ``identity_high_conf`` — the Node identity graph resolved this listing
                                 to the key with confidence >= min_confidence.
      3. ``reviewed``          — a human approved the identity.
      4. ``unresolved``        — none of the above; do NOT deposit entity-scoped.

    The returned key is ``existing_content_key`` when present — the Node
    identity graph is authoritative, so we hang the deposit on the key it
    already resolved rather than minting a divergent one — else a fresh mint
    from brand/title/gtin."""
    threshold = deposit_min_confidence() if min_confidence is None else min_confidence
    gtin_norm = normalize_gtin(gtin)
    key = existing_content_key or make_content_key(brand, title, gtin)

    if gtin_norm and key:
        return ResolvedDepositKey(key, DEPOSIT_BASIS_GTIN, 1.0)

    if (
        existing_content_key
        and identity_confidence is not None
        and identity_confidence >= threshold
    ):
        return ResolvedDepositKey(
            existing_content_key, DEPOSIT_BASIS_IDENTITY, float(identity_confidence)
        )

    if existing_content_key and str(review_state or "").strip().lower() == "reviewed":
        return ResolvedDepositKey(existing_content_key, DEPOSIT_BASIS_REVIEWED, 1.0)

    return ResolvedDepositKey(
        key, DEPOSIT_BASIS_UNRESOLVED, float(identity_confidence or 0.0)
    )


def resolve_deposit_content_key_for_row(
    row: Mapping,
    *,
    identity_confidence: Optional[float] = None,
    min_confidence: Optional[float] = None,
) -> ResolvedDepositKey:
    """Convenience wrapper over a catalog_products-style row dict. Tolerant of
    field-name drift. `identity_confidence` is supplied by the caller (it comes
    from the Node identity graph, not the catalog row)."""

    def _get(*names: str):
        for n in names:
            v = row.get(n)
            if v not in (None, ""):
                return v
        return None

    return resolve_deposit_content_key(
        brand=_get("brand", "brand_name", "vendor"),
        title=_get("title", "product_title", "name"),
        gtin=_get("gtin", "barcode", "upc", "ean"),
        existing_content_key=_get("content_key"),
        identity_confidence=(
            identity_confidence
            if identity_confidence is not None
            else row.get("identity_confidence")
        ),
        review_state=_get("review_state", "identity_review_state"),
        min_confidence=min_confidence,
    )
