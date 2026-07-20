"""Durable category_kind resolution (K-beauty common-core / category modules).

The agent-decision-grade data contract requires a durable category_kind in
{skincare, haircare, supplement} -- it drives claim-safety screening, required
disclaimers, and the serving gate's category checks, so the contract says it
must be durable and correct, NOT heuristic-defaulted.

Today only topical beauty paths are classified (catalog_products.category_path,
e.g. "beauty/skincare/..."); supplements have no category signal at all. This
resolver maps both:
  * skincare / haircare -- authoritative from the beauty category_path prefix.
  * supplement -- conservative text detection (no path exists), gated on
    ingestible signals so topical beauty (makeup sticks, setting powder) is
    never misread as a supplement.
Anything else returns None ("unknown") -- we never guess a category_kind.

Pure functions (no DB / no I/O): used by the sync write path, a backfill, and
read-time fallback; unit-tested with synthetic inputs.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional

SKINCARE = "skincare"
HAIRCARE = "haircare"
SUPPLEMENT = "supplement"
CATEGORY_KINDS = frozenset({SKINCARE, HAIRCARE, SUPPLEMENT})

# Ingestible dosage forms (BB Lab uses "30 sticks"; Ownist jelly/powder, etc.).
_SUPPLEMENT_FORM_RE = re.compile(
    r"\b(?:capsules?|tablets?|softgels?|gummies|gummy|sachets?|sticks?|"
    r"powder|jelly|drink\s+mix|drinkable)\b",
    re.IGNORECASE,
)
# Explicit ingestible-supplement nouns -- strong enough on their own.
_SUPPLEMENT_NOUN_RE = re.compile(
    r"\b(?:dietary\s+supplements?|supplements?|probiotics?|multivitamins?|"
    r"vitamins?|nutricosmetics?|inner\s*beauty)\b",
    re.IGNORECASE,
)
# Ingestible actives -- only count as supplement when paired with a dosage form,
# since some (collagen, hyaluronic acid) also appear in topical skincare.
_INGESTIBLE_ACTIVE_RE = re.compile(
    r"\b(?:collagen|biotin|probiotics?|hyaluronic\s+acid|glutathione|"
    r"vitamin\s*[a-e]|zinc|omega|peptides?)\b",
    re.IGNORECASE,
)


def _blob(*values: Any) -> str:
    parts = [str(v or "") for v in values]
    return " ".join(parts).lower()


def _tags_text(tags: Any) -> str:
    if isinstance(tags, str):
        return tags.lower()
    if isinstance(tags, Iterable):
        return " ".join(str(t or "") for t in tags).lower()
    return ""


def _looks_like_supplement(text: str) -> bool:
    if _SUPPLEMENT_NOUN_RE.search(text):
        return True
    if _SUPPLEMENT_FORM_RE.search(text) and _INGESTIBLE_ACTIVE_RE.search(text):
        return True
    return False


# Color-cosmetic (makeup) nouns. A product whose title/type names one of these is
# makeup, NOT skincare/haircare -- even if an upstream path or ingest stamped it
# skincare. Deliberately conservative: unambiguous color cosmetics only. Hybrids
# (BB/CC cream, tinted moisturizer, primer, lip balm/treatment, makeup remover)
# are intentionally EXCLUDED -- they carry real skincare function and must not be
# demoted.
_MAKEUP_RE = re.compile(
    r"\b(?:foundation|concealer|eye\s?shadow|shadowstix|mascara|eye\s?liner|"
    r"lipstick|lip\s+gloss|lip\s+liner|lip\s+stain|lip\s+crayon|lip\s+velvet|"
    r"blush|bronzer|highlighter|setting\s+(?:powder|spray)|finishing\s+powder|"
    r"pressed\s+powder|loose\s+powder|brow\s+(?:pencil|gel|pomade|powder|mascara)|"
    r"eyebrow\s+(?:pencil|gel)|palette|colou?r\s+correct|kohl|kajal|"
    r"match\s+stix|skinstick)\b",
    re.IGNORECASE,
)


def is_makeup(
    title: Optional[str] = None,
    product_type: Optional[str] = None,
    category: Optional[str] = None,
    tags: Any = None,
) -> bool:
    """True when the product is a color cosmetic (makeup). Conservative positive
    detection so it can safely demote a mis-stamped skincare/haircare row without
    catching hybrid skincare-makeup (BB cream, tinted moisturizer, primer).

    Matches on the TITLE only: in practice product_type/category are unreliable
    (real skincare like "Firming-Lifting Cream" is stamped product_type='Bronzer'),
    so trusting them would wrongly demote genuine skincare. The title is the
    reliable signal — a foundation's title says foundation. (product_type/category
    are accepted for API symmetry but not trusted for the makeup decision.)"""
    return bool(_MAKEUP_RE.search(str(title or "").lower()))


# Non-ingestible beauty subcategories that are never one of the three contract
# kinds and must NOT fall through to supplement text-detection (a makeup "stick"
# / setting "powder" is not a dose; a beauty DEVICE is not a topical or a dose).
_TOPICAL_BEAUTY_PREFIXES = (
    "beauty/makeup",
    "beauty/tools",
    "beauty/devices",   # beauty_device family (hair-styling / skincare-energy / …)
    "beauty/fragrance",
    "beauty/nail",
    "beauty/body",
    "beauty/bath",
    "beauty/sets",
)


def resolve_category_kind(
    category_path: Optional[str] = None,
    product_type: Optional[str] = None,
    title: Optional[str] = None,
    tags: Any = None,
) -> Optional[str]:
    """Resolve a durable category_kind, or None when it can't be determined
    confidently. Skincare/haircare come authoritatively from the beauty
    category_path; supplements from an explicit supplement path or conservative
    ingestible-signal detection. We never TEXT-guess skincare/haircare -- those
    must come from a durable path, not a heuristic (per the data contract)."""
    # Tolerate an exact subcategory-less path ("beauty/skincare") as well as a
    # nested one ("beauty/skincare/serum"); a trailing slash is insignificant.
    path = (category_path or "").strip().lower().rstrip("/")
    is_skin = path == "beauty/skincare" or path.startswith("beauty/skincare/")
    is_hair = path == "beauty/haircare" or path.startswith("beauty/haircare/")
    if is_skin or is_hair:
        # Guard: a color cosmetic mis-pathed under skincare/haircare (a real
        # occurrence — e.g. a foundation stamped skincare) is still makeup, not a
        # contract kind. Prevents makeup from getting skincare claim-safety/framing.
        if is_makeup(title=title, product_type=product_type, tags=tags):
            return None
        return SKINCARE if is_skin else HAIRCARE
    # An explicit supplement path is authoritative (e.g. a "beauty-supplements"
    # subcategory) -- recognized before the topical-beauty short-circuit so a
    # path literally naming supplements is never dropped to None.
    if "supplement" in path:
        return SUPPLEMENT
    # Clearly-topical beauty subcategories are not a contract kind, and must not
    # reach supplement detection.
    if any(path.startswith(prefix) for prefix in _TOPICAL_BEAUTY_PREFIXES):
        return None

    # No discriminating path (bare "beauty", "beauty/wellness", or no path):
    # conservative ingestible-text detection for supplements only.
    text = _blob(product_type, title) + " " + _tags_text(tags)
    if _looks_like_supplement(text):
        return SUPPLEMENT
    return None
