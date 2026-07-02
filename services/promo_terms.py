"""Shared non-attribute merchant-noise detection for the audit pipeline.

Some strings ride in on merchant tags / PDP copy but are NEVER product
attributes, and when one leaks into the SKU attribute graph or a generated query
axis it produces trust-killing, merchant-facing nonsense. Two families:

  - promotional/marketing terms ("skincare discount", "free shipping", "20% off")
    -> "best moisturizer for skincare discount";
  - operational / storefront-app tags ("exclude_rebuy", "yotpo", "preorder") --
    directives for Shopify apps, never something a shopper searches. DAMDAM's
    PORE CARE RITUAL carried the tag `exclude_rebuy` (tells the Rebuy upsell app
    to skip the product); it became the query "best set for rebuy exclude" AND
    cascaded into a fabricated competitor ("AI names Rebuy, not your product",
    citing rebuyengine.com). Flagged live on the DAMDAM report.

This module is the single source of truth for that gate so query generation
(`agent_center_bd_report_service._clean_prompt_term`) and attribute-graph
construction (`sku_sidewalk._add_attr`) can never drift apart.

Kept as a dependency-free leaf module: both `agent_center_bd_report_service`
and `sku_sidewalk` import it, and `agent_center_bd_report_service` already
imports from `sku_sidewalk`, so the promo gate cannot live in either of them
without a circular import.

Two match modes:
  - single-token stop set: unambiguous promo words, matched on word boundaries
    so real attributes are untouched ("sale" is dropped, "salicylic" is not);
  - multi-word phrases: matched as substrings of the collapsed term, so tokens
    that are legit alone but promo in context ("free shipping" vs "paraben
    free") are only rejected in the promo phrasing.
"""

from __future__ import annotations

import re

PROMO_STOP_TOKENS = frozenset({
    "discount", "discounts", "discounted",
    "sale", "sales",
    "coupon", "coupons",
    "promo", "promos", "promotion", "promotions", "promotional",
    "clearance", "markdown", "markdowns", "blowout",
    "bogo",
    "voucher", "vouchers",
    "rebate", "rebates",
    "cashback",
    "freebie", "freebies",
    "giveaway", "giveaways", "sweepstakes",
    "bestseller", "bestsellers",  # merchandising label, not an attribute
    "clearout",
})
PROMO_STOP_PHRASES = (
    "% off",
    "percent off",
    "free shipping",
    "free delivery",
    "free gift",
    "gift with purchase",
    "buy one get one",
    "on sale",
    "flash sale",
    "limited time",
    "cash back",
    "money back",
    "best price",
    "lowest price",
    "shop now",
    "add to cart",
    "black friday",
    "cyber monday",
)

# Operational / storefront-app tags: unambiguous system directives and app names
# that leak in as merchant product tags but are never product attributes. Kept
# narrow and collision-free — every token here would be nonsense as a standalone
# shopper term ("exclude" survives "excludes"/"exclusive"; app names are unique).
OPERATIONAL_STOP_TOKENS = frozenset({
    # storefront-app directives / names
    "rebuy", "rebuyengine",
    "loox", "yotpo", "judgeme", "okendo", "gorgias", "klaviyo", "stamped",
    # visibility / catalog-state directives
    "exclude", "excluded",
    "unpublished", "noindex",
    "preorder", "preorders", "preorderable",
    "backorder", "backorders", "backordered",
})
OPERATIONAL_STOP_PHRASES = (
    "exclude from",
    "hide from search",
    "back in stock",
    "out of stock",
    "gift card",
    "gift cards",
)

_ALL_STOP_TOKENS = PROMO_STOP_TOKENS | OPERATIONAL_STOP_TOKENS
_ALL_STOP_PHRASES = PROMO_STOP_PHRASES + OPERATIONAL_STOP_PHRASES


def is_promo_term(text: str) -> bool:
    """True when a cleaned term is promotional/marketing OR operational-app noise
    rather than a product attribute. `text` must already be lowercased and
    whitespace-collapsed (as produced by `_clean_prompt_term` or
    `sku_sidewalk._clean_attr`)."""
    if not text:
        return False
    if any(phrase in text for phrase in _ALL_STOP_PHRASES):
        return True
    tokens = set(re.findall(r"[a-z0-9]+", text))
    return bool(tokens & _ALL_STOP_TOKENS)
