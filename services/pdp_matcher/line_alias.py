"""Brand-scoped LINE-name aliases for retailer<->canonical matching.

WHY. The unit-anchored `retailer_match_key` bridges MECHANICAL drift (size/
pack/promo tokens) but deliberately never guesses VOCABULARY drift — brand
line-names that one side carries and the other doesn't. The DeepSeek judge
lane certified that drift at scale (2026-07-17 mining of AUTO verdicts, all
>= 0.85 confidence): SKIN1004 prefixes its "Madagascar (Centella)" line on
retailer listings but not on skin1004.com official titles (93 of 111
certified pairs — 64 drop the whole phrase, 27 drop only "Madagascar"),
COSRX's "Full Fit" line appears official-side only, and sunscreen titles
carry SPF/PA suffix noise on exactly one side (beauty-of-joseon: 8 of 21).
This module makes exactly that CURATED, certified vocabulary deterministic,
so future waves attach without LLM spend.

MECHANISM. `alias_match_keys` returns the bounded set of VARIANT keys a title
can take with certified phrases removed (each phrase alone, all phrases, and
each of those with suncare noise also removed — one side may drop only part
of a line name, so a single greedy strip cannot bridge both observed
classes). Rows are indexed under every variant; a query tries every variant.
Two sides that differ only by certified vocabulary share at least one key.

DESIGN INVARIANTS (mirrors services/brand_alias.py's audit-side rule):
  - ADDITIVE ONLY. An alias can only create a match the primary key missed —
    it never changes or shadows a primary-key match (primary always wins in
    the index; the query tries its primary key first).
  - CURATED, BRAND-SCOPED. Phrases live in LINE_ALIASES keyed by match_brand
    output. No inference, no global stopwords — a phrase is added only with
    certified evidence (judge verdicts / HITL review).
  - AMBIGUITY-GUARDED. If two different products collapse onto the same alias
    key, that alias key is dropped entirely (build_match_index enforces it) —
    a wrong deterministic attach is worse than a residue row.
"""

from __future__ import annotations

import re
from typing import List, Optional

from services.pdp_matcher.retailer_match import match_brand, retailer_match_key

# Brand-scoped line phrases that may legitimately be ABSENT (fully or
# partially) on one side of a retailer<->official pair. Keyed by
# match_brand(brand). Seeded from judge-certified drift pairs (2026-07-17);
# extend via HITL review.
LINE_ALIASES: dict[str, tuple[str, ...]] = {
    "skin1004": ("madagascar centella", "madagascar"),
    "cosrx": ("full fit",),
}

# Sunscreen rating suffixes drift between storefronts ("SPF50+ PA++++" on one
# side only). Post-normalization these are the tokens spf<digits> / spf / pa.
# CURATED like the line phrases: only brands with judge-certified rating-drop
# drift get the strip (an uncurated global strip manufactured cross-product
# matches, e.g. "Ultra Facial Cream SPF30" onto a genuinely different plain
# "Ultra Facial Cream" — 90be1574 review blocker). Cross-RATING attaches are
# refused at match time by spf_rating_conflict regardless of brand.
# NOTE: keyed by match_brand output of the CATALOG-side brand. Storefront
# vendor variants ("Beauty of Joseon UK") key differently — add them here only
# if canonicals are ever minted under that brand form (curation note from the
# 5ae62841 review).
SUNCARE_ALIAS_BRANDS = frozenset({"beauty of joseon"})
_SUNCARE_NOISE_RE = re.compile(r"\b(?:spf\s?\d+|spf|pa)\b")
_SPF_DIGITS_RE = re.compile(r"\bspf\s?(\d+)\b")
_WS_RE = re.compile(r"\s+")


def _clean(s: str) -> str:
    return _WS_RE.sub(" ", s).strip()


def _strip_phrase(text: str, phrase: str) -> str:
    return _clean(re.sub(rf"\b{re.escape(phrase)}\b", " ", text))


def spf_rating_conflict(key_a: str, key_b: str) -> bool:
    """True when both keys carry an explicit SPF rating and the ratings
    differ — an SPF30 listing must never alias-attach to an SPF50 canonical.
    One-sided ratings do NOT conflict: the certified drift class is exactly
    'one storefront drops the rating'."""
    a = set(_SPF_DIGITS_RE.findall(key_a))
    b = set(_SPF_DIGITS_RE.findall(key_b))
    return bool(a) and bool(b) and not (a & b)


def alias_match_keys(
    brand: Optional[str],
    title: Optional[str],
    *,
    include_suncare: bool = True,
) -> List[str]:
    """All alias-variant keys for (brand, title): the primary key's title half
    with certified line phrases (individually and together) and — for brands
    in SUNCARE_ALIAS_BRANDS — suncare noise removed. Never includes the
    primary key itself; empty when there is no primary key or nothing strips.
    Deterministic order, deduped."""
    primary = retailer_match_key(brand, title)
    if not primary:
        return []
    brand_key, _, title_part = primary.partition("::")
    brand_norm = match_brand(brand)
    phrases = LINE_ALIASES.get(brand_norm, ())
    suncare = include_suncare and brand_norm in SUNCARE_ALIAS_BRANDS

    bases = [title_part]
    for phrase in phrases:
        stripped = _strip_phrase(title_part, phrase)
        if stripped:
            bases.append(stripped)
    all_stripped = title_part
    for phrase in phrases:
        all_stripped = _strip_phrase(all_stripped, phrase)
    if all_stripped:
        bases.append(all_stripped)

    variants: List[str] = []
    for base in bases:
        forms = [base]
        if suncare:
            forms.append(_clean(_SUNCARE_NOISE_RE.sub(" ", base)))
        for form in forms:
            if form and form != title_part and form not in variants:
                variants.append(form)
    return [f"{brand_key}::{v}" for v in variants]
