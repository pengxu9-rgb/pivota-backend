"""PDP category classifier shared by:

- scripts/backfill_pdp_category_path.py (Phase 2 — populate catalog_products.category_path)
- services/pivot_query_service.py recall path (Phase 2b — bias recall toward
  category_path matches when the query is a category alias)

Patterns ported from PIVOTA-Agent-mainline-verify/src/services/externalSeedProducts.js
BEAUTY_CATEGORY_PATTERNS, augmented with explicit taxonomy paths.

DRY rule: the patterns live HERE. Both the backfill script and the search
path import from this module. Adding/removing a pattern updates everywhere
at once.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

_SUNSCREEN_RE = re.compile(
    r"\b(sunscreen|sun\s*screen|broad\s+spectrum|spf\s*\d{2,3}\+?|pa\s*\+{2,4}|"
    r"sun\s+(?:serum|fluid|cream|gel|milk|stick)|"
    r"uv\s*(?:protection|shield|defen[cs]e|lock))\b",
    re.IGNORECASE,
)

# (category_label, taxonomy_path, regex). Order matters — more specific
# patterns appear earlier; the first match wins.
CATEGORY_PATTERNS: List[Tuple[str, str, "re.Pattern[str]"]] = [
    ("Brush", "beauty/tools/brush", re.compile(
        r"\b(brush|makeup brush|foundation brush|powder brush|blush brush|shader brush|kabuki)\b",
        re.IGNORECASE)),
    ("Shampoo", "beauty/haircare/shampoo", re.compile(
        r"\b(shampoo|dry shampoo|clarifying shampoo)\b", re.IGNORECASE)),
    ("Conditioner", "beauty/haircare/conditioner", re.compile(
        r"\b(conditioner|deep conditioner|leave-in conditioner|leave in conditioner)\b",
        re.IGNORECASE)),
    ("Hair Styling", "beauty/haircare/styling", re.compile(
        r"\b(edge control|styling gel|hair-thickening|hair thickening|"
        r"detangling spray|hair clip|hair clips|edge styling)\b",
        re.IGNORECASE)),
    ("Hair Care", "beauty/haircare/general", re.compile(
        r"\b(hair care|hair repair|repair bundle|maintenance crew|"
        r"detangling|leave-in|leave in|hair)\b",
        re.IGNORECASE)),
    ("Sunscreen", "beauty/skincare/sun/sunscreen", _SUNSCREEN_RE),
    ("Fragrance", "beauty/fragrance/perfume", re.compile(
        r"\b(perfume|parfum|eau de parfum|eau de toilette|cologne|scent)\b|"
        r"\bfragrance\b(?![-\s]?free)\b",
        re.IGNORECASE)),
    ("Cleanser", "beauty/skincare/cleanse/cleanser", re.compile(
        r"\b(cleanser|cleansing|face wash|facial wash|"
        r"cleansing milk|cleansing foam|cleansing gel|wash)\b",
        re.IGNORECASE)),
    ("Toner", "beauty/skincare/treat/toner", re.compile(
        r"\b(toner|mist|pad)\b", re.IGNORECASE)),
    ("Mask", "beauty/skincare/treat/mask", re.compile(
        r"\b(face mask|clay mask|charcoal mask|sheet mask|gel mask|sleeping mask|"
        r"sleep mask|wash[-\s]?off mask|under eye patch|eye patch|pimple patch|"
        r"lip\s?patch|mask)\b",
        re.IGNORECASE)),
    ("Exfoliant", "beauty/skincare/treat/exfoliant", re.compile(
        r"\b(exfoliant|exfoliating|exfoliation|peel|peeling|peeling gel|peel pads?|"
        r"scrub|polish)\b",
        re.IGNORECASE)),
    ("Treatment", "beauty/skincare/treat/treatment", re.compile(
        r"\b(spot[-\s]?target(?:ing|ed)?|spot[-\s]?treatment|blemish|acne|"
        r"clarifying treatment|targeting gel|treatment gel)\b",
        re.IGNORECASE)),
    ("Serum", "beauty/skincare/treat/serum", re.compile(
        r"\b(serum|essence|ampoule|concentrate)\b", re.IGNORECASE)),
    ("Primer", "beauty/makeup/face/primer", re.compile(
        r"\b(primer|pore prep|pore[-\s]?filling)\b", re.IGNORECASE)),
    ("Concealer", "beauty/makeup/face/concealer", re.compile(
        r"\b(concealer)\b", re.IGNORECASE)),
    ("Foundation", "beauty/makeup/face/foundation", re.compile(
        r"\b(foundation|skin tint|tint stick|foundation stick|cushion foundation)\b",
        re.IGNORECASE)),
    ("Powder", "beauty/makeup/face/powder", re.compile(
        r"\b(powder|setting powder|pressed powder|loose powder|"
        r"blurring powder|finishing powder)\b",
        re.IGNORECASE)),
    ("Highlighter", "beauty/makeup/face/highlighter", re.compile(
        r"\b(highlighter|illuminator|luminizer|luminiser|killawatt)\b",
        re.IGNORECASE)),
    ("Blush", "beauty/makeup/face/blush", re.compile(
        r"\b(blush|cheeks out|cheek tint|flush)\b", re.IGNORECASE)),
    ("Bronzer", "beauty/makeup/face/bronzer", re.compile(
        r"\b(bronzer|contour)\b", re.IGNORECASE)),
    ("Eyeshadow", "beauty/makeup/eye/eyeshadow", re.compile(
        r"\b(eye\s?shadow|eyeshadow|eye color|eye colour)\b", re.IGNORECASE)),
    ("Eyeliner", "beauty/makeup/eye/eyeliner", re.compile(
        r"\b(eyeliner|eye liner|liquid liner|pencil liner|flypencil)\b",
        re.IGNORECASE)),
    ("Mascara", "beauty/makeup/eye/mascara", re.compile(
        r"\b(mascara)\b", re.IGNORECASE)),
    ("Brow Pencil", "beauty/makeup/eye/brow", re.compile(
        r"\b(brow pencil|eyebrow pencil|brow definer|brow sculptor|brow styler)\b",
        re.IGNORECASE)),
    ("Lip Balm", "beauty/makeup/lip/balm", re.compile(
        r"\b(lip balm|lip treatment|lip scrub|scrubstick)\b", re.IGNORECASE)),
    ("Lipstick", "beauty/makeup/lip/lipstick", re.compile(
        r"\b(lipstick|lip color|lip colour|liquid lip|lip luxe|lip lacquer|"
        r"lip gloss|lip oil|lip liner|lip stain|lip tint|pout lip|gloss luxe|"
        r"gloss drip)\b",
        re.IGNORECASE)),
    ("Moisturizer", "beauty/skincare/moisturize/cream", re.compile(
        r"\b(moisturizer|moisturiser|cream|lotion|gel cream|gel-cream|barrier cream)\b",
        re.IGNORECASE)),
]


def classify(text: Optional[str]) -> Optional[Tuple[str, str]]:
    """Return (category_label, category_path) on first matching pattern, else None."""
    if not text:
        return None
    for label, path, pattern in CATEGORY_PATTERNS:
        if pattern.search(text):
            return (label, path)
    return None


def resolve_path_from_row(
    *,
    category: Optional[str],
    product_type: Optional[str],
    title: Optional[str],
) -> Optional[Tuple[str, str]]:
    """Try category, product_type, title in priority order. Used by the backfill."""
    for candidate in (category, product_type, title):
        hit = classify(candidate)
        if hit is not None:
            return hit
    return None


def category_path_prefix_for_query(query: Optional[str]) -> Optional[str]:
    """Used by the recall path: when the user query matches a known category,
    return a 3-segment prefix like 'beauty/makeup/lip/' so the SQL can do
    `WHERE category_path LIKE :prefix || '%'`. Returning None means the
    query does NOT match a known category and recall should fall back to
    the existing trigram text scan.

    Example: 'lipstick' → 'beauty/makeup/lip/' (matches 'beauty/makeup/lip/lipstick'
    AND 'beauty/makeup/lip/balm' so users see both lipstick and lip balm
    rows on a generic 'lipstick' search). Adjust the slice depth if a more
    precise match is desired.
    """
    hit = classify(query)
    if hit is None:
        return None
    _, path = hit
    # Slice to category-parent level (drop the final segment).
    parts = path.rsplit("/", 1)
    if len(parts) <= 1:
        return path + "/"
    return parts[0] + "/"
