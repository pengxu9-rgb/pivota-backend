"""Automated checks that stand in for an operator reading every row of a dry run.

Each rule is measured, not imagined:
  * 2026-09-23 k-touch.us: "3CE - TONE UP TINT 40ml" typed `LIP TINT` by the merchant; its own
    description sells it as a tone-up cream for the complexion. Caught only by reading the row.
    -> lip_row_copy_not_about_lips, lip_row_implausible_size, title_contradicts_category.
  * 2026-09-23 k-touch.us: "3CE Soft Matte Lipstick - Warmish Move" carries a rice cleanser's
    pasted description. -> lip_row_copy_not_about_lips.
  * 2026-09-09 limecrime.com: a literal `TEST Product` priced $999,999,999.00 in /products.json.
    -> placeholder_product.
  * Rows the opt-in lip title door placed (#2257) are listed as INFO: they passed its guards, and an
    operator reviewing a held store should see them, but they do not hold a store on their own.

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
_LIP_WORD = re.compile(r"\b(?:lips?|lipsticks?|lip\s*(?:colou?r|tint|gloss|balm|liner|stain)|pout|mouth|smile)\b", re.I)
_MIN_COPY_CHARS = 60  # shorter copy is not evidence either way

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
    from services.curated_brand_feed import CATEGORY_CONFIDENCE_LIP_TITLE, _title_paths

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
            if len(copy.strip()) >= _MIN_COPY_CHARS and not _LIP_WORD.search(copy):
                flags.append(_flag("lip_row_copy_not_about_lips", BLOCK, record,
                                   f"filed under {category} but its {len(copy)}-char description never "
                                   f"mentions lips: {copy.strip()[:90]!r}"))
            big = [s for s in _size_units(f"{title} {variant_titles}") if s >= 20]
            if big:
                flags.append(_flag("lip_row_implausible_size", BLOCK, record,
                                   f"filed under {category} at {max(big):g} ml/g; lip formats are a few ml/g"))
            try:
                door = abs(float(pdp.get("category_confidence")) - CATEGORY_CONFIDENCE_LIP_TITLE) < 1e-6
            except (TypeError, ValueError):
                door = False
            if door:
                flags.append(_flag("placed_by_lip_title", INFO, record,
                                   "category from the product's own title (--lip-title-evidence)"))

        # The title names one or more leaves and the row is filed under none of them: the merchant
        # type (or a measured shelf) and the product's own name disagree. "Essence Toner" on a
        # "Cleansers" shelf names serum AND toner -- neither is cleanser.
        named = _title_paths(title)
        if category and named and category not in named:
            flags.append(_flag("title_contradicts_category", BLOCK, record,
                               f"filed under {category}; the title names {sorted(named)}"))

        prices = _prices(record)
        vendor = str(pdp.get("brand") or "")
        if (_PLACEHOLDER.search(title) or re.search(r"\bdev\b", vendor, re.I)
                or any(p <= 0.5 or p >= 1000 for p in prices)):
            flags.append(_flag("placeholder_product", BLOCK, record,
                               f"looks like a test/placeholder row (prices {sorted(set(prices))[:4]})"))
    return flags


def blocking(flags: Iterable[Dict[str, Any]], *, accepted: Iterable[str] = ()) -> List[Dict[str, Any]]:
    """The BLOCK flags an approval has not accepted by key."""
    ok = set(accepted or ())
    return [f for f in flags if f.get("severity") == BLOCK and f.get("key") not in ok]
