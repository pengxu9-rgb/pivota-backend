"""Pivota-normalized taxonomy v1 derivation (Phase O-2).

Pure functions. No DB I/O, no LLM calls. Three onboarding paths
(internal merchant sync / external seed mirror / catalog enrichment
agent) call into this module to compute the same 4 columns:

  price_tier     str | None   — deterministic from product.price
  use_case_tags  list[str]    — keyword-matched on title/desc/tags
  lifestyle_tags list[str]    — keyword-matched on title/desc/tags
  demographic    str | None   — keyword-matched on title/desc/tags

Conservative by design — only fires on clear signals. NULL / empty
when ambiguous. Phase O-3 LabelAgent will fill the long tail using
LLM-driven classification.

See docs/PDP_ONBOARDING_PLAYBOOK.md.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional, Sequence


# ---------------------------------------------------------------------------
# price_tier — deterministic from price
# ---------------------------------------------------------------------------


PRICE_TIER_BUCKETS: Sequence[tuple] = (
    # (upper_bound_exclusive, label)
    (50.0, "under_50"),
    (100.0, "50_100"),
    (200.0, "100_200"),
    (500.0, "200_500"),
    (float("inf"), "500_plus"),
)
PRICE_TIER_UNKNOWN = "unknown"


def derive_price_tier(price: Optional[float]) -> Optional[str]:
    """Bucket a numeric USD price into one of:
      under_50 | 50_100 | 100_200 | 200_500 | 500_plus | unknown

    Returns None when the input is None / NaN / negative — distinct
    from "unknown" which is the explicit "we have a 0 price" case.

    The 5 buckets cover the 99% case across beauty / electronics /
    apparel without needing per-vertical thresholds. Tune via Phase
    O-3 if a category needs different bucketing.
    """
    if price is None:
        return None
    try:
        p = float(price)
    except (TypeError, ValueError):
        return None
    if p != p:  # NaN
        return None
    if p < 0:
        return None
    if p == 0:
        return PRICE_TIER_UNKNOWN
    for upper, label in PRICE_TIER_BUCKETS:
        if p < upper:
            return label
    return PRICE_TIER_BUCKETS[-1][1]  # safety net


# ---------------------------------------------------------------------------
# Keyword-based extractors for tag-style columns
# ---------------------------------------------------------------------------


def _normalize_haystack(*sources: object) -> str:
    """Flatten title / description / tags / etc. into a single
    lowercased string for substring + regex matching. Lists / tuples
    are space-joined. None becomes empty string. Non-string
    primitives are stringified."""
    parts: List[str] = []
    for src in sources:
        if src is None:
            continue
        if isinstance(src, (list, tuple, set)):
            for item in src:
                if item is None:
                    continue
                parts.append(str(item))
        else:
            parts.append(str(src))
    return " ".join(parts).lower()


# Lifestyle tokens — high-confidence brand/marketing claims that
# merchants frequently put in product copy. Map literal substrings
# (case-insensitive) to the canonical Pivota token.
_LIFESTYLE_TOKEN_RULES: Sequence[tuple] = (
    ("vegan", "vegan"),
    ("cruelty-free", "cruelty_free"),
    ("cruelty free", "cruelty_free"),
    ("fragrance-free", "fragrance_free"),
    ("fragrance free", "fragrance_free"),
    ("paraben-free", "paraben_free"),
    ("paraben free", "paraben_free"),
    ("sulfate-free", "sulfate_free"),
    ("sulfate free", "sulfate_free"),
    ("hypoallergenic", "hypoallergenic"),
    ("dermatologist tested", "dermatologist_tested"),
    ("dermatologist-tested", "dermatologist_tested"),
    ("organic", "organic"),
    ("sustainable", "sustainable"),
    ("recyclable", "recyclable"),
    ("clean beauty", "clean_beauty"),
    ("non-toxic", "non_toxic"),
    ("gluten-free", "gluten_free"),
    ("gluten free", "gluten_free"),
    ("ethically sourced", "ethically_sourced"),
)


def extract_lifestyle_tags(
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
    tags: Optional[Iterable[str]] = None,
) -> List[str]:
    """Scan title + description + tags for lifestyle / values claims.
    Returns a deduped list of canonical Pivota tokens. Empty when no
    claims found (NOT None) — same semantic as O-1 free-form tags."""
    haystack = _normalize_haystack(title, description, tags)
    if not haystack:
        return []
    out: List[str] = []
    for needle, token in _LIFESTYLE_TOKEN_RULES:
        if needle in haystack and token not in out:
            out.append(token)
    return out


# Use-case tokens — broad usage intent. Tighter rules (whole-word
# regex) so e.g. "professional" doesn't match "non-professional".
_USE_CASE_REGEX_RULES: Sequence[tuple] = (
    (r"\b(daily|every\s?day|everyday)\b", "daily"),
    (r"\b(special\s+occasion|night\s+out|date\s+night|formal\s+wear)\b", "special_occasion"),
    (r"\b(gift\s+set|gift\s+box|holiday\s+set|stocking\s+stuffer)\b", "gift"),
    (r"\b(professional(?:\s+grade)?|pro\s+use|salon\s+grade|salon-quality)\b", "professional"),
    (r"\b(athletic|workout|gym\s+(bag|wear)|sport(?:s|y)?)\b", "sport"),
    (r"\b(travel(?:-?size|\s+size)?|carry-on|on\s+the\s+go)\b", "travel"),
    (r"\b(sample|trial\s+size|mini\s+size)\b", "sample"),
)


def extract_use_case_tags(
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
    tags: Optional[Iterable[str]] = None,
) -> List[str]:
    """Scan for use-case / occasion signals. Whole-word regex matching
    so noise (e.g. "weekday" matching "daily") doesn't fire."""
    haystack = _normalize_haystack(title, description, tags)
    if not haystack:
        return []
    out: List[str] = []
    for pattern, token in _USE_CASE_REGEX_RULES:
        if re.search(pattern, haystack):
            if token not in out:
                out.append(token)
    return out


# Demographic — single value, not a list. Conservative: prefer NULL
# over guessing. The order matters: kids first (most specific), then
# men / women, then unisex (least specific).
_DEMOGRAPHIC_REGEX_RULES: Sequence[tuple] = (
    (r"\b(kids?|child(?:ren)?|baby|babies|toddler|teen)\b", "kids"),
    (r"\b(men[''']?s|for\s+men|menswear|gentleman|gentlemen)\b", "men"),
    (r"\b(women[''']?s|for\s+women|ladies|for\s+her|girls?|womenswear)\b", "women"),
    (r"\bunisex\b", "unisex"),
)


def extract_demographic(
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
    tags: Optional[Iterable[str]] = None,
) -> Optional[str]:
    """Identify demographic from explicit signals. Returns None when
    ambiguous — Phase O-3 LabelAgent fills these later from broader
    context (brand norms, product category, image analysis, etc.).

    A product matching multiple categories (e.g. "men's and women's
    accessories") yields the first hit per the rule order — the
    LabelAgent should reclassify when ambiguity matters."""
    haystack = _normalize_haystack(title, description, tags)
    if not haystack:
        return None
    for pattern, label in _DEMOGRAPHIC_REGEX_RULES:
        if re.search(pattern, haystack):
            return label
    return None


# ---------------------------------------------------------------------------
# Top-level helper — compute all 4 columns at once
# ---------------------------------------------------------------------------


def derive_taxonomy_v1(
    *,
    price: Optional[float] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    tags: Optional[Iterable[str]] = None,
) -> dict:
    """Compute all 4 taxonomy v1 columns in one call. Each onboarding
    path uses this so the derivation is identical across Path A
    (Shopify/Wix/Woo/BigCommerce ingest), Path B (external seed
    mirror), and Path C (catalog enrichment agent).

    Returns a dict with keys price_tier / use_case_tags /
    lifestyle_tags / demographic. List values are always [] (not None)
    when nothing matched — the same NULL-vs-empty semantic Phase O-1
    set up for `tags`. The price_tier and demographic scalars CAN be
    None (signal absent)."""
    return {
        "price_tier": derive_price_tier(price),
        "use_case_tags": extract_use_case_tags(
            title=title, description=description, tags=tags
        ),
        "lifestyle_tags": extract_lifestyle_tags(
            title=title, description=description, tags=tags
        ),
        "demographic": extract_demographic(
            title=title, description=description, tags=tags
        ),
    }
