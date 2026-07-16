"""Size/pack/promo-normalized match key for aligning a *retailer's* product
title with our size-agnostic canonical family record.

WHY. `catalog_identity.make_content_key` folds the normalized title (digits
kept — size is treated as identity) into the key, which is correct for our own
canonical minting. But a retailer's listing title carries packaging noise our
canonical title does not: the size suffix ("... Essence **100ml**"), pack counts
("3ea x 24 patches"), and promo/bundle words ("double duo", "freegift"). Keyed
naively, the retailer SKU mints a *different* content_key and looks brand-new —
so the retailer offer is not attached and, worse, a duplicate canonical is
minted.

Motivating data — StyleKorean COSRX pilot, 2026-07-16: naive brand+title match
recovered **3 / 107** products (we already had 120 COSRX rows); this normalized
key recovered **52 / 107**. See project memory `stylekorean_commerce_index_pilot`.

This is a PROPOSE-then-attach layer. It NEVER changes content_key generation and
never auto-merges — `sibling of services/pdp_matcher/deterministic.py`, it only
proposes a canonical candidate for a retailer listing; the caller (intake door /
review) decides whether to attach the offer.

UNIT-ANCHORED. A number is dropped only when immediately followed by a size/pack
unit, so active-strength numbers that ARE identity ("96 mucin", "vitamin c 23",
"aha 7", "aloe 54.2") survive while "100ml" / "24 patches" / "3ea" are stripped.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Optional

from services.catalog_identity import normalize_brand, normalize_title

# Size / weight / pack units. A leading number + one of these = packaging, dropped.
_UNIT = (
    r"(?:ml|l|g|kg|mg|oz|fl\s?oz|ct|ea|pcs?|"
    r"sheets?|patches|patch|pads?|masks?|"
    r"set|sets|kit|kits|tablets?|capsules?|caps|softgels?|sticks?)"
)
# '100ml', '3 ea', '24 patches', '3.52 oz'
_SIZE_RE = re.compile(rf"\b\d+(?:\.\d+)?\s*{_UNIT}\b")
# 'x 24' / '3 x 24' leftovers from 'NxM' pack notation
_XPACK_RE = re.compile(r"\b\d*\s*x\s*\d+\b")
# stray single 'x' left after size tokens around it are removed
_STRAY_X_RE = re.compile(r"(?:^|\s)x(?:\s|$)")

# Storefront descriptor tokens some vendors append to the brand name
# ("COSRX Official", "... Flagship") that aren't part of the brand identity.
# Conservative on purpose: dropping "shop"/"beauty"/"store" would corrupt real
# brands ("The Face Shop", "Huda Beauty"). A brand-alias map (services/brand_alias)
# is the general fix; this handles the common storefront-vendor case.
_BRAND_DESCRIPTOR_TOKENS = frozenset({"official", "flagship"})


def match_brand(brand: Optional[str]) -> str:
    """normalize_brand + drop a trailing storefront descriptor. So a Shopify
    vendor "COSRX Official" and a retailer/catalog "COSRX" both key as "cosrx".
    Never strips to empty (keeps at least one token)."""
    toks = normalize_brand(brand).split()
    while len(toks) > 1 and toks[-1] in _BRAND_DESCRIPTOR_TOKENS:
        toks.pop()
    return " ".join(toks)


# Bundle / promo / marketing words that don't carry product identity.
# Deliberately NOT here (they ARE product-line identity; stripping them silently
# merged distinct SKUs — 2026-07-16 review): "refill" (different SKU/packaging),
# "double" (Etude "Double Lasting" line), "plus" (reformulations, "X Plus").
# A missed match falls to the residue/Gemini lane — the safe direction.
_PROMO = frozenset({
    "freegift", "free", "gift", "duo", "bundle", "pack",
    "special", "edition", "renewal", "planet", "holiday", "limited",
    "value", "big", "jumbo", "set", "kit", "new",
})


def retailer_match_key(brand: Optional[str], title: Optional[str]) -> str:
    """Return `<brand_norm>::<stripped_title>` for retailer<->canonical matching.

    Returns "" when brand or title normalize to empty (caller must treat "" as
    'no key' — never a match). Deterministic, no I/O."""
    brand_norm = match_brand(brand)
    title_norm = normalize_title(title)
    if not brand_norm or not title_norm:
        return ""
    # Size units first ("3ea", "24 patches"), so a 'NxM' pack note collapses to a
    # stray 'x' that _STRAY_X_RE clears — rather than orphaning the unit word.
    t = _SIZE_RE.sub(" ", title_norm)
    t = _XPACK_RE.sub(" ", t)
    t = _STRAY_X_RE.sub(" ", t)
    tokens = t.split()
    # Drop a leading brand prefix in the title BEFORE promo filtering — a brand
    # containing a promo-ish token ("Double Dare") would otherwise lose part of
    # its prefix to the filter and never prefix-strip. Brand-official storefronts
    # often prefix the brand ("COSRX Advanced Snail ..."), while retailers/our
    # canonical do not — without this they key differently and never match.
    brand_tokens = brand_norm.split()
    if brand_tokens and tokens[: len(brand_tokens)] == brand_tokens:
        tokens = tokens[len(brand_tokens):]
    tokens = [w for w in tokens if w not in _PROMO]
    stripped = " ".join(tokens).strip()
    if not stripped:
        # Title was ALL packaging/promo (e.g. "Favorites Set") — fall back to the
        # full normalized title so we don't collide unrelated all-promo titles.
        stripped = title_norm
    return f"{brand_norm}::{stripped}"


# Scope preference when several of our rows share one match key: a
# multi_merchant_canonical row is the intended attach target over a
# merchant_owned duplicate.
_SCOPE_RANK = {"multi_merchant_canonical": 2, "merchant_owned": 1}


def _scope_rank(row: Mapping[str, Any]) -> int:
    return _SCOPE_RANK.get(str(row.get("pdp_scope") or ""), 0)


def build_match_index(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index our canonical rows by retailer_match_key.

    rows: mappings with at least 'brand' and 'title' (optional 'pdp_scope').
    On key collision, keeps the highest scope rank (canonical > merchant_owned).
    Rows whose key is "" are skipped."""
    index: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = retailer_match_key(row.get("brand"), row.get("title"))
        if not key:
            continue
        existing = index.get(key)
        if existing is None or _scope_rank(row) > _scope_rank(existing):
            index[key] = dict(row)
    return index


def match_record(
    index: Mapping[str, Mapping[str, Any]],
    brand: Optional[str],
    title: Optional[str],
) -> Optional[dict[str, Any]]:
    """Return the canonical row matching (brand, title), or None. Pure lookup."""
    key = retailer_match_key(brand, title)
    if not key:
        return None
    hit = index.get(key)
    return dict(hit) if hit is not None else None
