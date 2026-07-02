"""Shared promo/marketing-term detection for the audit pipeline.

A promotional term ("skincare discount", "free shipping", "20% off") is
merchandising noise, never a product attribute. When such a term leaks into the
SKU attribute graph or a generated query axis it produces trust-killing,
merchant-facing nonsense like "best moisturizer for skincare discount". This
module is the single source of truth for that gate so query generation
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


def is_promo_term(text: str) -> bool:
    """True when a cleaned term is promotional/marketing noise rather than a
    product attribute. `text` must already be lowercased and whitespace-collapsed
    (as produced by `_clean_prompt_term` or `sku_sidewalk._clean_attr`)."""
    if not text:
        return False
    if any(phrase in text for phrase in PROMO_STOP_PHRASES):
        return True
    tokens = set(re.findall(r"[a-z0-9]+", text))
    return bool(tokens & PROMO_STOP_TOKENS)
