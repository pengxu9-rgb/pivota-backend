"""Automated checks that stand in for an operator reading every row of a dry run.

Each rule is measured, not imagined:
  * 2026-09-23 k-touch.us: "3CE - TONE UP TINT 40ml" typed `LIP TINT` by the merchant; its own
    description sells it as a tone-up cream for the complexion. Caught only by reading the row.
    -> lip_row_copy_not_about_lips, lip_row_implausible_size, title_contradicts_category.
  * 2026-09-23 k-touch.us: "3CE Soft Matte Lipstick - Warmish Move" carries a rice cleanser's
    pasted description. -> lip_row_copy_not_about_lips.
  * 2026-09-09 limecrime.com: a literal `TEST Product` priced $999,999,999.00 in /products.json.
    -> placeholder_product.
  * 2026-09-26 headandshoulders.com: a "where to buy" brand site pricing EVERY storefront variant at
    1.00 (120 of 120). The drain applied 74 PDPs / 152 offers at $1.00 unflagged: $1.00 clears the
    per-row rule, and the signal is store-wide. Measured the same day over every store this lane has
    written (186 store/currency cohorts, 39,514 offers): headandshoulders.com is the only one whose
    modal price is <= 2.00 at a modal share over 0.22, or whose <= 1.00 share is over 0.20 (next:
    timelybasket.com, 10/46 at 2.00 and 9/46 at 1.00).
    honest.com's storefront carries 125 of 317 variants at 1.00, mostly diapers. -> placeholder_price_store.
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
# A set/kit/multi-pack filed as ONE product: its own shelf is beauty/sets. A count of 1 ("1pc",
# "1ea" -- how Korean retailers label single items) is a single product.
_SET_TITLE = re.compile(r"\b(?:sets?|kits?|bundles?|trio|(?:[2-9]|\d{2,})\s*-?\s*(?:pcs?|pieces?|ea)|special\s+edition|"
                        r"duo\s+edition|double\s+edition|\d+\s*x\s*\d+\s*(?:ml|g))\b", re.I)

# A lip product ships in a few ml/g (balm tins reach ~15-18 g); 20+ is a face or body format.
_SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*(ml|g|oz|fl\.?\s*oz)\b", re.I)
_PLACEHOLDER = re.compile(r"\b(?:test\s*product|dummy|do\s+not\s+buy|placeholder|sample\s+product)\b", re.I)

# Store-level placeholder pricing (placeholder_price_store), judged over the WHOLE cohort passed to
# detect(). Measured 2026-09-26 (see the module docstring): every threshold in the grid min 10-30 /
# modal share 0.6-0.9 / modal ceiling 2-5 / <=1.00 share 0.2-0.4 held headandshoulders.com and nothing
# else. The values below sit between the placeholder stores and the nearest legitimate ones.
_STORE_MIN_VARIANTS = 20       # a smaller cohort is not evidence of a store-wide price
_STORE_MODAL_SHARE = 0.80      # headandshoulders.com 1.00; max legitimate store with a mode <= 2.00: 0.217
_STORE_MODAL_CEILING = 2.00    # a flat $10 / $23 store (biodance.com 0.53 at 23.00) is a real price list
_STORE_TOKEN_PRICE = 1.00      # the "see store" token price
_STORE_TOKEN_SHARE = 0.30      # honest.com 0.39-0.40; max legitimate: timelybasket.com 0.196


def _pdp(record: Any) -> Dict[str, Any]:
    pdp = record.get("pdp") if isinstance(record, dict) else None
    return pdp if isinstance(pdp, dict) else {}


def _handle(record: Dict[str, Any]) -> Optional[str]:
    from services.catalog_enrichment_agent.ingestion import listing_handle
    # The FIRST offer that names a product decides, as before listing_handle existed (a /products/ URL
    # with an empty handle still answers None rather than falling through to the next offer).
    for offer in record.get("offers") or []:
        url = str((offer or {}).get("canonical_url") or "")
        handle = listing_handle(url)
        if handle or "/products/" in url:
            return handle
    return None


def _prices(record: Dict[str, Any]) -> List[float]:
    out = []
    for offer in record.get("offers") or []:
        try:
            out.append(float((offer or {}).get("price")))
        except (TypeError, ValueError):
            continue
    return out


def _variant_prices(record: Dict[str, Any]) -> List[float]:
    """Every variant's own price (pdp.variants, one per sellable storefront variant), rounded to the
    cent; the offer prices when the record carries no variants."""
    out = []
    for variant in _pdp(record).get("variants") or []:
        try:
            out.append(round(float((variant or {}).get("price")), 2))
        except (TypeError, ValueError, AttributeError):
            continue
    return out or [round(p, 2) for p in _prices(record)]


def _store_placeholder_flags(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """placeholder_price_store: the store prices its catalogue with a token, so no per-row price is real.

    A BLOCK per row whose prices are ALL at or below the placeholder price -- keyed like every row flag,
    so an operator can accept a real $1 item or exclude rows. A row with any price above it is a real
    product carrying a token-priced variant, and is not held by this rule."""
    priced = [(record, _variant_prices(record)) for record in records]
    priced = [(record, prices) for record, prices in priced if prices]
    every = [p for _, prices in priced for p in prices]
    total = len(every)
    if total < _STORE_MIN_VARIANTS:
        return []
    counts: Dict[float, int] = {}
    for p in every:
        counts[p] = counts.get(p, 0) + 1
    # Ties go to the LOWER price: the placeholder is the cheap one.
    modal, modal_n = min(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    token_n = sum(1 for p in every if p <= _STORE_TOKEN_PRICE)
    ceilings = []
    if modal_n / total >= _STORE_MODAL_SHARE and modal <= _STORE_MODAL_CEILING:
        ceilings.append(modal)
    if token_n / total >= _STORE_TOKEN_SHARE:
        ceilings.append(_STORE_TOKEN_PRICE)
    if not ceilings:
        return []
    ceiling = max(ceilings)
    evidence = (f"store-wide placeholder pricing: {modal_n}/{total} variants at {modal:.2f}, "
                f"{token_n}/{total} at or below {_STORE_TOKEN_PRICE:.2f}")
    return [_flag("placeholder_price_store", BLOCK, record,
                  f"{evidence}; every price of this row is <= {ceiling:.2f} ({sorted(set(prices))[:4]})")
            for record, prices in priced if max(prices) <= ceiling]


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


def detect(records: Iterable[Dict[str, Any]], *, store_level: bool = True) -> List[Dict[str, Any]]:
    """Per-row rules over each record, then (store_level) the rules judged over the whole cohort.

    `records` IS the cohort for the store-level rules: pass every row of the store, never a subset.
    A caller re-checking a subset it already judged as part of the whole passes store_level=False."""
    from services.curated_brand_feed import CATEGORY_CONFIDENCE_LIP_TITLE, _NON_FACE_TITLE, _title_paths

    flags: List[Dict[str, Any]] = []
    cohort: List[Dict[str, Any]] = []
    for record in records or []:
        pdp = _pdp(record)
        if not pdp:
            continue
        cohort.append(record)
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
    if store_level:
        flags.extend(_store_placeholder_flags(cohort))
    return flags


def blocking(flags: Iterable[Dict[str, Any]], *, accepted: Iterable[str] = ()) -> List[Dict[str, Any]]:
    """The BLOCK flags an approval has not accepted by key (cohort-level flags are never accepted)."""
    ok = set(accepted or ())
    # A cohort-level flag (plan not ready, a guard conflict) names no row: accepting its key would
    # accept every future instance of it, so it can never be accepted -- only fixed.
    return [f for f in flags if f.get("severity") == BLOCK
            and (f.get("acceptable") is False or f.get("key") not in ok)]
