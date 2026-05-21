"""Brand display casing.

Pairs with `services.catalog_identity.normalize_brand`, which is a
*search/identity* normalizer (always lowercases) — distinct from this
*display* normalizer. Per the team feedback "SQL search keys are never
display fallbacks" (PIVOTA-Agent PR #1434), the same brand string cannot
serve both surfaces; this module is the display side.

The PIVOTA-Agent JS counterpart lives at `src/pdpBuilder.js`
(`titleCaseBrand` / `BRAND_DISPLAY_OVERRIDES`). Keep the two override
tables in sync when adding new brands.
"""

from __future__ import annotations

from typing import Optional

# Brands whose canonical display casing is *not* simple Title Case.
# Keys are fully lowercase; values are the canonical form.
BRAND_DISPLAY_OVERRIDES = {
    "kvd": "KVD",
    "kvd beauty": "KVD Beauty",
    "kvd vegan beauty": "KVD Vegan Beauty",
    "colourpop": "ColourPop",
    "colourpop cosmetics": "ColourPop Cosmetics",
    "mac": "MAC",
    "nyx": "NYX",
    "nars": "NARS",
    "glamglow": "GLAMGLOW",
    "milk makeup": "Milk Makeup",
}


def proper_case_brand(value: Optional[str]) -> str:
    """Restore display casing on brand strings the ingest pipeline
    stored lowercase.

    Acts only when the input is fully lowercase. Any uppercase character
    means the source already supplied the canonical form, so we leave it
    alone — that's what makes this safe to apply at write *or* read time
    without corrupting properly-cased data (NARS, ColourPop, L'Oréal).
    """
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if text != text.lower():
        return text
    if text in BRAND_DISPLAY_OVERRIDES:
        return BRAND_DISPLAY_OVERRIDES[text]
    return " ".join(
        (word[:1].upper() + word[1:]) if word else word
        for word in text.split(" ")
    )
