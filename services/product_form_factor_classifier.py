"""Form factor + price band classifier (PR-7b).

Classifies products by form factor (gummy / powder / capsule /
liquid / tablet / bar / pod) and price band (mass / mid / premium /
luxury). Used by the cohort-comparison panel to call out structural
positioning insights — e.g., "Grüns is the only gummy in the
15-competitor cohort."

**Why deterministic + keyword-based** (vs LLM call):
  - Form factor + price band are unambiguous classifications from
    title / product_type strings. LLM would add cost + latency
    without precision improvement.
  - Used at cohort-aggregation time across 15+ competitor products
    per audit; LLM cost would multiply quickly.
  - Falls back to "unknown" when ambiguous; downstream consumers
    handle nulls gracefully.

If keyword classification proves too brittle in production (e.g.
new product formats not anticipated), a future PR can route to the
orchestrator's `form_factor_classification` scan_mode (already
defined in the registry) for an LLM fallback.

**Output** when called per product:

```python
{
  "form_factor": "gummy" | "powder" | "capsule" | "liquid" |
                 "tablet" | "bar" | "pod" | "spray" | "patch" |
                 "topical" | "drink" | None,
  "price_band": "mass" | "mid" | "premium" | "luxury" | None,
}
```

When called at cohort level:

```python
{
  "form_factor_summary": {
    "gummy": ["Grüns"],
    "powder": ["AG1", "Bloom", "Huel Daily Greens", ...],
    "capsule": [],
    ...
  },
  "merchant_owns_unique_form_factor": True,    # only the merchant
                                                  # is in their bucket
  "merchant_form_factor": "gummy",
}
```
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


# Form factor keyword patterns. Order matters — first match wins,
# so put more-specific patterns first (e.g. "gummies" before
# generic "candy").
_FORM_FACTOR_PATTERNS: List[tuple] = [
    # Wellness / supplements
    ("gummy", [r"\bgummy\b", r"\bgummies\b", r"\bchewable\b"]),
    ("powder", [
        r"\bpowder\b", r"\bpowdered\b", r"\bpwd\b", r"\bgrams\b",
        r"\bscoop\b", r"\bsachet\b",
    ]),
    ("capsule", [
        r"\bcapsule\b", r"\bcapsules\b", r"\bcaps?\b(?!ule)",
        r"\bsoftgels?\b", r"\bsoft-gels?\b",
    ]),
    ("tablet", [r"\btablet\b", r"\btablets\b", r"\bpill\b", r"\bpills\b"]),
    ("liquid", [
        r"\bliquid\b", r"\btonic\b", r"\bshot\b", r"\bdrops?\b",
        r"\btincture\b", r"\belixir\b", r"\bsyrup\b",
    ]),
    ("bar", [r"\bbar\b", r"\bbars\b", r"\bbites?\b", r"\bcookies?\b"]),
    ("pod", [r"\bpod\b", r"\bpods\b"]),
    ("drink", [r"\bdrink\b", r"\bbeverage\b", r"\bjuice\b", r"\bsmoothie\b"]),
    # Beauty / skincare
    ("spray", [r"\bspray\b", r"\bmist\b", r"\baerosol\b"]),
    ("patch", [r"\bpatch\b", r"\bpatches\b", r"\bmask\b"]),
    (
        "topical",
        [
            r"\bcream\b", r"\bserum\b", r"\bointment\b", r"\bgel\b",
            r"\bbalm\b", r"\blotion\b", r"\boil\b",
        ],
    ),
]


# Pre-compile patterns for performance
_COMPILED_PATTERNS: List[tuple] = [
    (form_factor, [re.compile(p, re.IGNORECASE) for p in patterns])
    for form_factor, patterns in _FORM_FACTOR_PATTERNS
]


def classify_form_factor(
    *,
    product_title: Optional[str] = None,
    product_type: Optional[str] = None,
    description: Optional[str] = None,
) -> Optional[str]:
    """Return the form factor for a single product, or None when no
    keyword pattern matches. Inspects title first (most reliable),
    falls back to product_type then description.

    First-match-wins ordering so more-specific patterns (gummy,
    softgel) take precedence over generic ones (chewable, capsule).
    """
    haystacks = [
        (product_title or "").strip(),
        (product_type or "").strip(),
        (description or "")[:300].strip(),  # cap description scan
    ]
    haystack = " ".join(s for s in haystacks if s)
    if not haystack:
        return None
    for form_factor, patterns in _COMPILED_PATTERNS:
        for pattern in patterns:
            if pattern.search(haystack):
                return form_factor
    return None


# Price band thresholds (in USD, single-unit retail price).
# Calibrated for D2C wellness / beauty / fashion verticals where
# most consumer products fall in $10-$100. High-luxury verticals
# (jewelry, watches) would need different thresholds — caller can
# pass `vertical_overrides` to use a different scale.
_DEFAULT_PRICE_BANDS: List[tuple] = [
    # (max_price_inclusive, band_name)
    (15.0, "mass"),
    (30.0, "mid"),
    (60.0, "premium"),
    # Anything above $60 → luxury
]


def classify_price_band(
    price_usd: Optional[float],
) -> Optional[str]:
    """Return the price band for a numeric price. None when price
    is missing / non-numeric / nonpositive.

    Bands:
      - $0-15: mass
      - $15-30: mid
      - $30-60: premium
      - $60+: luxury
    """
    if price_usd is None:
        return None
    try:
        p = float(price_usd)
    except (TypeError, ValueError):
        return None
    if p <= 0:
        return None
    for max_price, band in _DEFAULT_PRICE_BANDS:
        if p <= max_price:
            return band
    return "luxury"


def classify_product(
    *,
    product_title: Optional[str] = None,
    product_type: Optional[str] = None,
    description: Optional[str] = None,
    price_usd: Optional[float] = None,
) -> Dict[str, Optional[str]]:
    """Combined per-product classifier: returns both form factor and
    price band in a single call. Renderers consume this directly to
    show "Greens Gummies (gummy, premium tier)"."""
    return {
        "form_factor": classify_form_factor(
            product_title=product_title,
            product_type=product_type,
            description=description,
        ),
        "price_band": classify_price_band(price_usd),
    }


def build_cohort_form_factor_summary(
    *,
    merchant_brand: Optional[str],
    merchant_form_factor: Optional[str],
    competitor_brands: List[Dict[str, Any]],
    cohort_audit_runs: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Aggregate form-factor classification across the cohort.

    Inputs:
      - merchant_brand: the audited brand's display name
      - merchant_form_factor: classified from the merchant's audited
        product (typically the flagship SKU)
      - competitor_brands: from category_visibility.competitor_brands
        — names only; form_factor for each is derived from cohort
        audit runs when available, else "unknown"
      - cohort_audit_runs: optional list of cohort audit results
        (PR-2 cohort orchestrator output) — each carries a
        product_title that classify_form_factor can consume

    Output:
      ```python
      {
        "form_factor_summary": {
          "gummy": ["Grüns"],
          "powder": ["AG1", "Bloom", ...],
          "unknown": ["Some Brand"],
          ...
        },
        "merchant_form_factor": "gummy",
        "merchant_owns_unique_form_factor": True,
        "competitors_in_merchant_form_factor": [],
      }
      ```

    The "merchant_owns_unique_form_factor" boolean is the headline
    insight — when True, the renderer can call out the structural
    moat ("only gummy in the cohort"). When False, the merchant is
    one of N brands sharing a form factor; the rendering should
    name the others.
    """
    summary: Dict[str, List[str]] = {}

    # Build a brand→form_factor map from cohort audit runs first
    cohort_run_map: Dict[str, Optional[str]] = {}
    for run in (cohort_audit_runs or []):
        # Cohort runs typically have competitor_brand + product_title
        brand = (run.get("competitor_brand") or "").strip()
        if not brand:
            continue
        # Try to classify from the cohort audit's audited product
        report = run.get("report_jsonb") or {}
        per_product = report.get("per_product") or []
        ff: Optional[str] = None
        for p in per_product:
            product = p.get("product") or {}
            ff = classify_form_factor(
                product_title=product.get("title"),
                product_type=product.get("product_type"),
            )
            if ff:
                break
        cohort_run_map[brand.lower()] = ff

    # Merchant goes into the appropriate bucket
    merchant_ff = (merchant_form_factor or "unknown").lower() or "unknown"
    if merchant_brand:
        summary.setdefault(merchant_ff, []).append(merchant_brand)

    # Competitors: try cohort_run_map, fall back to attempting
    # classification from the brand name itself (low-precision; brand
    # names rarely encode form factor)
    for entry in (competitor_brands or []):
        comp_name = (entry.get("name") or "").strip()
        if not comp_name:
            continue
        ff = cohort_run_map.get(comp_name.lower())
        if ff is None:
            # Fall back: try brand name as both title + product_type
            ff = classify_form_factor(
                product_title=comp_name,
                product_type=comp_name,
            ) or "unknown"
        summary.setdefault(ff, []).append(comp_name)

    # Compute "uniqueness" insight
    competitors_in_merchant_bucket = [
        b for b in summary.get(merchant_ff, []) if b != merchant_brand
    ]
    merchant_owns_unique = (
        merchant_ff != "unknown"
        and len(competitors_in_merchant_bucket) == 0
    )

    return {
        "form_factor_summary": summary,
        "merchant_form_factor": merchant_form_factor,
        "merchant_owns_unique_form_factor": merchant_owns_unique,
        "competitors_in_merchant_form_factor": competitors_in_merchant_bucket,
    }
