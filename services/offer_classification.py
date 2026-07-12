"""Offer classification + buy-box resolution for the agent decision surface.

The agent-decision-grade data contract requires offers to declare whether they
are the brand selling direct or a third-party retailer, whether they are
first-party, and which market they serve -- so an agent can resolve a single
best US offer and frame "why buy direct" honestly.

Two deterministic rules, no fabrication:
  * external_referral offers are never first-party, but the ingest LANE alone
    does not reveal the SELLER — a brand's own D2C storefront and a marketplace
    mirror both ingest as external_referral. So offer_type = None ("unknown")
    here; the real brand-vs-retailer verdict is derived from the offer DOMAIN by
    services/offer_seller_identity.py and STORED at write/backfill time. This
    function never guesses 'retailer' from the lane (Fix Plan C: do not guess).
  * internal_merchant offers are first-party, but 'brand_direct' ONLY when the
    merchant is explicitly verified as the brand (carried in via
    catalog_merchants.metadata_json.brand_relationship). Unverified internal
    merchants get offer_type = None ("unknown"), never an assumed brand_direct.

Pure functions (no DB / no I/O) so they unit-test cleanly and can be reused by
the read path, the sync write path, and PR4's serving gate.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, List, Optional, Sequence

OFFER_TYPE_BRAND_DIRECT = "brand_direct"
OFFER_TYPE_RETAILER = "retailer"
DEFAULT_MARKET = "US"

# Availability tokens that mean "cannot buy right now".
_UNBUYABLE_AVAILABILITY = {
    "out_of_stock",
    "outofstock",
    "unavailable",
    "discontinued",
    "sold_out",
    "soldout",
}


def is_first_party_track(catalog_track: Optional[str]) -> bool:
    """Internal-merchant offers are first-party; external referrals are not."""
    return catalog_track == "internal_merchant"


def classify_offer_type(
    catalog_track: Optional[str],
    brand_relationship: Optional[str] = None,
) -> Optional[str]:
    """Resolve offer_type from the offer's track + the merchant's *verified*
    brand relationship. Returns None ("unknown") when the LANE cannot honestly
    name the seller: an external_referral (seller = brand-or-retailer, decided
    by domain, not the lane) or an internal merchant not verified as the brand.
    We never assume 'retailer' from external_referral or 'brand_direct' from an
    unverified internal merchant.
    """
    if catalog_track == "external_referral":
        # Seller identity is domain-derived + stored (services/offer_seller_identity.py);
        # a stored NULL means "no domain evidence" -> unknown. Do NOT guess retailer.
        return None
    if catalog_track == "internal_merchant":
        if (brand_relationship or "").strip().lower() == OFFER_TYPE_BRAND_DIRECT:
            return OFFER_TYPE_BRAND_DIRECT
        return None
    return None


def _is_buyable(availability: Optional[str]) -> bool:
    token = (availability or "").strip().lower()
    return token not in _UNBUYABLE_AVAILABILITY


def _effective_price(offer: Any) -> Optional[Decimal]:
    pricing = getattr(offer, "pricing", None)
    if pricing is None:
        return None
    for attr in ("merchant_effective_price", "estimated_best_price", "list_price"):
        value = getattr(pricing, attr, None)
        if isinstance(value, Decimal):
            return value
    return None


def select_best_us_offer(offers: Sequence[Any]) -> Optional[Any]:
    """Pick the single best buyable US offer from a list of OfferNode-likes.

    A buyable US offer = market 'US' (the default), not flagged out-of-stock,
    and priced. Ranked by lowest effective price, tie-broken toward first-party
    (brand-direct) offers. Returns None when nothing qualifies (the contract's
    serving gate treats that as "no US-buyable offer").
    """
    candidates: List[Any] = []
    for offer in offers:
        market = getattr(offer, "market", None) or DEFAULT_MARKET
        if market != DEFAULT_MARKET:
            continue
        if not _is_buyable(getattr(offer, "availability", None)):
            continue
        if _effective_price(offer) is None:
            continue
        candidates.append(offer)

    if not candidates:
        return None

    def rank(offer: Any):
        price = _effective_price(offer)
        first_party_rank = 0 if getattr(offer, "is_first_party", False) else 1
        return (price, first_party_rank)

    return sorted(candidates, key=rank)[0]
