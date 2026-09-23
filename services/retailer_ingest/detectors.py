"""Automated checks that stand in for an operator reading every row of a dry run.

Each rule is measured, not imagined:
  * 2026-09-23 k-touch.us: "3CE - TONE UP TINT 40ml" typed `LIP TINT` by the merchant; its own
    description sells it as a tone-up cream for the complexion. Caught only by reading the row.
    -> lip_row_copy_not_about_lips, lip_row_implausible_size, title_contradicts_category.
  * 2026-09-23 k-touch.us: "3CE Soft Matte Lipstick - Warmish Move" carries a rice cleanser's
    pasted description. -> lip_row_copy_not_about_lips.
  * 2026-09-09 limecrime.com: a literal `TEST Product` priced $999,999,999.00 in /products.json.
    -> placeholder_product.
  * Rows the opt-in lip title door placed (#2257) are a per-row BLOCK: unattended, nothing but the
    title vouches for them (review of #2263: "Lipstick Poster" typed "Misc" reaches a lip leaf).

A BLOCK flag holds the job for review; INFO never does. A flag's `key` is stable across runs of
the same cohort (rule + handle), so an approval can accept exactly the flags it looked at.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

BLOCK = "block"
INFO = "info"

_LIP_PREFIX = "beauty/makeup/lip/"

# A lip product's own copy talks about lips. Measured 2026-09-23 on the 10 lip products applied by
# hand plus the k-touch.us tone-up cream: every real one mentions lips; the tone-up cream's copy
# ("brighten ... your complexion", "dull skin") and a Soft Matte Lipstick page carrying a rice
# CLEANSER's pasted description never do. A face-word list was tried first and held 4 of the 10 real
# lipsticks ("flatters every skin tone", "brightens the complexion") -- lipstick copy uses those words.
_LIP_WORD = re.compile(r"\b(?:lips?|lipsticks?|lip\s*(?:colou?r|tint|gloss|balm|liner|stain)|pout|mouth|smile)\b|립|입술", re.I)
_MIN_COPY_CHARS = 60  # shorter copy is not evidence either way
# Balms and scrubs are the lip leaves whose copy talks about "chapped skin" and whose tins and jars
# run 20-30 g (Vaseline Lip Therapy 20g, Sugar Lip Scrub 30g): the copy and size rules skip them.
_LIP_COLOUR_LEAVES = frozenset({"beauty/makeup/lip/lipstick", "beauty/makeup/lip/tint", "beauty/makeup/lip/gloss",
                                "beauty/makeup/lip/liner", "beauty/makeup/lip/oil"})
# Area leaves the non-face rule (#2248) files a product under on purpose; its title names the face
# leaf it was moved AWAY from ("Hand Cream" -> body/care), which is not a contradiction.
_AREA_LEAF = re.compile(r"^beauty/(?:body|haircare)/")
# A set/kit/multi-pack filed as ONE product: its own shelf is beauty/sets.
_SET_TITLE = re.compile(r"\b(?:sets?|kits?|bundles?|trio|\d+\s*-?\s*(?:pcs|pieces?|ea)|special\s+edition|"
                        r"duo\s+edition|double\s+edition|\d+\s*x\s*\d+\s*(?:ml|g))\b", re.I)

# A lip product ships in a few ml/g (balm tins reach ~15-18 g); 20+ is a face or body format.
_SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*(ml|g|oz|fl\.?\s*oz)\b", re.I)
_PLACEHOLDER = re.compile(r"\b(?:test\s*product|dummy|do\s+not\s+buy|placeholder|sample\s+product)\b", re.I)


def _pdp(record: Any) -> Dict[str, Any]:
    pdp = record.get("pdp") if isinstance(record, dict) else None
    return pdp if isinstance(pdp, dict) else {}


def _handle(record: Dict[str, Any]) -> Optional[str]:
    for offer in record.get("offers") or []:
        url = str((offer or {}).get("canonical_url") or "")
        if "/products/" in url:
            return url.split("/products/", 1)[1].split("?", 1)[0].strip("/").casefold() or None
    return None


def _prices(record: Dict[str, Any]) -> List[float]:
    out = []
    for offer in record.get("offers") or []:
        try:
            out.append(float((offer or {}).get("price")))
        except (TypeError, ValueError):
            continue
    return out


def _size_units(text: str) -> List[float]:
    """Sizes normalised to ml/g (oz x 28.35 is close enough for a 20 cut)."""
    sizes = []
    for number, unit in _SIZE.findall(text or ""):
        value = float(number)
        if "oz" in unit.lower():
            value *= 28.35
        sizes.append(value)
    return sizes


def _flag(rule: str, severity: str, record: Dict[str, Any], detail: str) -> Dict[str, Any]:
    pdp = _pdp(record)
    handle = _handle(record)
    return {
        "key": f"{rule}:{handle or pdp.get('product_name') or '?'}",
        "rule": rule,
        "severity": severity,
        "handle": handle,
        "product_name": pdp.get("product_name") or pdp.get("title"),
        "category_path": pdp.get("category_path"),
        "merchant_product_type": pdp.get("category_source_product_type"),
        "detail": detail,
    }


def detect(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    from services.curated_brand_feed import CATEGORY_CONFIDENCE_LIP_TITLE, _NON_FACE_TITLE, _title_paths

    flags: List[Dict[str, Any]] = []
    for record in records or []:
        pdp = _pdp(record)
        if not pdp:
            continue
        title = str(pdp.get("product_name") or pdp.get("title") or "")
        category = str(pdp.get("category_path") or "")
        copy = str(pdp.get("attribute_summary") or "")
        variant_titles = " ".join(str((v or {}).get("title") or "") for v in pdp.get("variants") or []
                                  if isinstance(v, dict))
        is_lip = category.startswith(_LIP_PREFIX)

        if is_lip:
            if (category in _LIP_COLOUR_LEAVES and len(copy.strip()) >= _MIN_COPY_CHARS
                    and not _LIP_WORD.search(copy)):
                flags.append(_flag("lip_row_copy_not_about_lips", BLOCK, record,
                                   f"filed under {category} but its {len(copy)}-char description never "
                                   f"mentions lips: {copy.strip()[:90]!r}"))
            big = [s for s in _size_units(f"{title} {variant_titles}") if s >= 20] \
                if category in _LIP_COLOUR_LEAVES else []
            if big:
                flags.append(_flag("lip_row_implausible_size", BLOCK, record,
                                   f"filed under {category} at {max(big):g} ml/g; lip formats are a few ml/g"))
            try:
                door = abs(float(pdp.get("category_confidence")) - CATEGORY_CONFIDENCE_LIP_TITLE) < 1e-6
            except (TypeError, ValueError):
                door = False
            if door:
                # Unattended, no merchant type vouches for this row, and no word list can name every
                # non-product ("Lipstick Poster"). So it holds the cohort until someone accepts this
                # row's key -- the pipeline's form of an operator reading each placed row.
                flags.append(_flag("placed_by_lip_title", BLOCK, record,
                                   "category from the product's own title (--lip-title-evidence); "
                                   "no merchant type vouches for it -- accept this row to apply it"))

        # The title names one or more leaves and the row is filed under none of them: the merchant
        # type (or a measured shelf) and the product's own name disagree. "Essence Toner" on a
        # "Cleansers" shelf names serum AND toner -- neither is cleanser.
        named = _title_paths(title)
        # Exempt only what the non-face rule moved on purpose: an area leaf whose OWN title names that
        # area ("Hand Cream" -> body/care). A "Vitamin C Serum" typed "Hair" is still a contradiction.
        moved_by_area_rule = bool(_AREA_LEAF.match(category) and _NON_FACE_TITLE.search(title))
        if category and named and category not in named and not moved_by_area_rule:
            flags.append(_flag("title_contradicts_category", BLOCK, record,
                               f"filed under {category}; the title names {sorted(named)}"))

        if category and not category.startswith("beauty/sets/") and _SET_TITLE.search(title):
            flags.append(_flag("set_filed_as_single_product", BLOCK, record,
                               f"the title names a set/multi-pack but it is filed under {category}"))

        prices = _prices(record)
        vendor = str(pdp.get("brand") or "")
        if (_PLACEHOLDER.search(title) or re.search(r"\bdev\b", vendor, re.I)
                or any(p <= 0.5 or p >= 1000 for p in prices)):
            flags.append(_flag("placeholder_product", BLOCK, record,
                               f"looks like a test/placeholder row (prices {sorted(set(prices))[:4]})"))
    return flags


def blocking(flags: Iterable[Dict[str, Any]], *, accepted: Iterable[str] = ()) -> List[Dict[str, Any]]:
    """The BLOCK flags an approval has not accepted by key (cohort-level flags are never accepted)."""
    ok = set(accepted or ())
    # A cohort-level flag (plan not ready, a guard conflict) names no row: accepting its key would
    # accept every future instance of it, so it can never be accepted -- only fixed.
    return [f for f in flags if f.get("severity") == BLOCK
            and (f.get("acceptable") is False or f.get("key") not in ok)]
