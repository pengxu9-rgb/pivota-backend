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
# '100ml', '3 ea', '24 patches' — runs on normalize_title output, where dotted
# unit spellings ("fl.oz.", "fl. oz.", "oz.") have already collapsed to
# "fl oz" / "oz", so they need no cases here.
_SIZE_RE = re.compile(rf"\b\d+(?:\.\d+)?\s*{_UNIT}\b")
# DECIMAL sizes ('3.38 fl.oz.(100ml)', '1.69 oz.', '6.5g') must be stripped
# from the RAW title: normalize_title drops '.' and splits "3.38" into "3 38",
# so _SIZE_RE then eats "38 fl oz" but orphans the "3" into the key — real miss
# 2026-07-18 HITL: "Dynasty Cream 3.38 fl.oz.(100ml)" keyed "dynasty cream 3"
# and failed to match the "Dynasty Cream" canonical. Volume/weight units only,
# and still unit-anchored: decimal active strengths carry no unit ("aloe 54.2",
# "vitamin c 21.5"), so identity numbers cannot match here.
_RAW_DECIMAL_SIZE_RE = re.compile(
    r"(?<![\w.])\d+\.\d+\s*(?:fl\.?\s?oz|oz|ml|mg|kg|g|l)\.?(?!\w)",
    re.IGNORECASE,
)
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
    # Decimal sizes first, on the RAW title (normalize_title splits "3.38" into
    # "3 38" — see _RAW_DECIMAL_SIZE_RE); title_norm above stays un-stripped so
    # the all-packaging fallback below keeps its collision protection.
    t = normalize_title(_RAW_DECIMAL_SIZE_RE.sub(" ", title))
    # Then size units ("3ea", "24 patches"), so a 'NxM' pack note collapses to a
    # stray 'x' that _STRAY_X_RE clears — rather than orphaning the unit word.
    t = _SIZE_RE.sub(" ", t)
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

# Sentinel index entry carrying alias keys blocked for ALIAS-lane lookups
# (alias-vs-primary cross-product collisions). "\x00" prefix cannot collide
# with a real match key (normalize output is printable tokens).
ALIAS_BLOCKED_SENTINEL = "\x00alias_blocked"


def _scope_rank(row: Mapping[str, Any]) -> int:
    return _SCOPE_RANK.get(str(row.get("pdp_scope") or ""), 0)


def build_match_index(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index our canonical rows by retailer_match_key.

    rows: mappings with at least 'brand' and 'title' (optional 'pdp_scope').
    On key collision, keeps the highest scope rank (canonical > merchant_owned).
    Rows whose key is "" are skipped."""
    row_list = [dict(r) for r in rows]
    index: dict[str, dict[str, Any]] = {}
    for row in row_list:
        key = retailer_match_key(row.get("brand"), row.get("title"))
        if not key:
            continue
        existing = index.get(key)
        if existing is None or _scope_rank(row) > _scope_rank(existing):
            index[key] = row

    # Line-alias entries (services/pdp_matcher/line_alias.py) — ADDITIVE ONLY:
    # a primary key is never shadowed, and an alias key claimed by two
    # different products is dropped entirely (a wrong deterministic attach is
    # worse than a residue row). A row whose alias key collides with a
    # DIFFERENT product's PRIMARY key blocks that key for ALIAS lookups too
    # (primary-key lookups keep working): the primary owner and the alias
    # claimant are two products one alias-stripped title away from each
    # other, so an alias-lane query on that key is ambiguous by construction.
    from services.pdp_matcher.line_alias import alias_match_keys

    alias_owner: dict[str, dict[str, Any]] = {}
    ambiguous: set = set()
    for row in row_list:
        for akey in alias_match_keys(row.get("brand"), row.get("title")):
            owner = index.get(akey)
            if owner is not None:
                if owner.get("product_key") != row.get("product_key"):
                    ambiguous.add(akey)
                continue
            prev = alias_owner.get(akey)
            if prev is not None:
                if prev.get("product_key") != row.get("product_key"):
                    ambiguous.add(akey)
                continue
            alias_owner[akey] = row
    for akey, row in alias_owner.items():
        if akey not in ambiguous and akey not in index:
            index[akey] = {**row, "_alias_indexed": True}
    if ambiguous:
        index[ALIAS_BLOCKED_SENTINEL] = {"keys": frozenset(ambiguous)}
    return index


def match_record(
    index: Mapping[str, Mapping[str, Any]],
    brand: Optional[str],
    title: Optional[str],
) -> Optional[dict[str, Any]]:
    """Return the canonical row matching (brand, title), or None. Pure lookup.
    Primary key first; on a miss, the query's line-alias form (both sides
    collapse to the same alias-canonical key, so this bridges a phrase carried
    by either side)."""
    key = retailer_match_key(brand, title)
    if not key:
        return None
    hit = index.get(key)
    if hit is not None:
        return dict(hit)
    from services.pdp_matcher.line_alias import alias_match_keys, spf_rating_conflict

    blocked = (index.get(ALIAS_BLOCKED_SENTINEL) or {}).get("keys") or frozenset()
    for akey in alias_match_keys(brand, title):
        if akey in blocked:
            continue
        hit = index.get(akey)
        if hit is None:
            continue
        # never alias-attach across explicit, DIFFERENT SPF ratings — an
        # SPF30 listing must not land on an SPF50 canonical.
        hit_key = retailer_match_key(hit.get("brand"), hit.get("title"))
        if spf_rating_conflict(key, hit_key):
            continue
        return {**hit, "_alias_matched": True}
    return None
