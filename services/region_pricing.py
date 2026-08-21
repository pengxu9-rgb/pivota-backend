"""Region -> expected pricing currency, and the "priced for this region" predicate.

ADR-024 (PR #1796), Phase 1 item 5: introduce `has_offer_priced_for_region`
BESIDE `_HAS_US_OFFER_EXISTS`, not in place of the stored verdict it feeds. This
module is the one place a region's expected pricing currency is written down.

WHAT THE PREDICATE ASKS, and what it deliberately does not:

  * It gates on `catalog_offers.currency`, NEVER on `catalog_offers.market`.
    `market` is a NOT NULL DEFAULT 'US' (migration 149, "refine to real per-offer
    geo when modeled") that the external-seed writers never set, so it reads 'US'
    on every row and "carries no signal" — the wording is
    index_pipeline_state_service.py:600's, and the has_us_offer gate was moved off
    it for exactly that reason. `currency` is written truthfully by every writer
    (0 of 18,279 live priced offers carry it null), which is how the INR/ZAR/GBP
    mispricings were detected in the first place.

  * FULFILMENT REACHABILITY IS NOT MODELLED. "Priced in GBP" is not "buyable from
    the UK", and a UK store that ships to the US is a US-buyable offer priced in
    GBP. That second, weaker gate needs `merchant_region_support` (ADR-024 Phase
    2b) and does not exist yet. The predicate is named for what it actually asks
    so no caller can mistake the two.

  * REGION IS NEVER INFERRED FROM CURRENCY, in either direction. This map reads
    region -> expected currency and is not invertible: USD is priced by stores in
    many countries, and a KR/HK exporter legitimately prices in USD (see
    services/storefront_currency: market and currency are "different axes").

  * NO CONVERSION, EVER. This is a membership test on a currency code. Nothing
    here compares or converts amounts across currencies — ADR-024 commitment 5,
    and the reason it exists is that this repo has shipped the same cross-unit
    defect in four separate layers.

The map covers exactly the regions ADR-024 measured real supply for on prod
2026-08-21 (GBP 780 · EUR 608 · JPY 333 · AUD 26 · SEK 25 · KRW 23 · HKD 22 ·
SGD 14 · CAD 12 servable non-USD offers). An unlisted region RAISES rather than
falling back to USD: choosing a default for an unmapped buyer region is a policy
decision that belongs to the caller who knows the request, not to the table.

Pure string building, no DB/IO. The only value interpolated into SQL is the
currency code, and it comes out of this fixed map — never out of caller input.
"""

from __future__ import annotations

from typing import Dict

from services.priced_offer_sql import priced_offer_exists_sql

# ISO-3166-1 alpha-2 region -> the ISO-4217 code an offer must be priced in to
# count as priced FOR that region. Adding a region is a data change here plus the
# supply to back it; it is not a schema change anywhere.
REGION_PRICING_CURRENCY: Dict[str, str] = {
    "US": "USD",
    "GB": "GBP",
    "JP": "JPY",
    "FR": "EUR",
    "HR": "EUR",
    "FI": "EUR",
    "AU": "AUD",
    "SE": "SEK",
    "KR": "KRW",
    "HK": "HKD",
    "SG": "SGD",
    "CA": "CAD",
}


def normalize_region(region: str) -> str:
    """Canonical form of a region code: trimmed and upper-cased.

    Case and surrounding whitespace are NORMALIZED (so 'gb' and ' GB ' both mean
    GB — an ISO-3166 code is case-insensitive and a request field routinely
    arrives lower-cased). Everything else is REFUSED by the lookup below; this
    function only spells the canonical form, it does not validate membership.
    """
    return str(region or "").strip().upper()


def pricing_currency_for_region(region: str) -> str:
    """The currency an offer must be priced in to be priced for `region`.

    Raises ValueError for any region this module has not measured supply for —
    including a well-formed but unlisted code like 'DE', and including garbage.
    There is deliberately no default: a silent USD fallback is how a non-US buyer
    gets answered with a US-only catalog and no one finds out.
    """
    normalized = normalize_region(region)
    try:
        return REGION_PRICING_CURRENCY[normalized]
    except KeyError:
        raise ValueError(
            f"unknown pricing region {normalized or region!r}; "
            f"known regions: {', '.join(sorted(REGION_PRICING_CURRENCY))}. "
            "Choosing a fallback region is the caller's policy decision, not this module's."
        ) from None


def region_currency_predicate(region: str, *, alias: str = "co") -> str:
    """The bare currency conjunct for one ``catalog_offers`` alias.

    Exposed separately from the EXISTS below because that is the shape
    ``priced_offer_exists_sql``'s ``extra_predicate`` seam takes, and a caller
    narrowing some other query must not re-spell it by hand.
    """
    return f"upper(trim(coalesce({alias}.currency, ''))) = '{pricing_currency_for_region(region)}'"


def has_offer_priced_for_region_sql(
    product_key_expr: str, region: str, *, alias: str = "co"
) -> str:
    """``EXISTS`` over ``catalog_offers``: a real price for this product_key, in
    the currency `region` expects.

    Same shape as every other priced-offer test — it IS ``priced_offer_exists_sql``
    with the currency conjunct handed to its ``extra_predicate`` seam, so the
    suppression and ``coalesce(merchant_effective_price, list_price) > 0`` rules
    stay in their single source of truth.

    ``product_key_expr`` is SQL, not a value (``cp.product_key``), exactly as in
    ``priced_offer_exists_sql``.

    With ``region='US'`` this emits the byte-identical string
    ``index_pipeline_state_service._HAS_US_OFFER_EXISTS`` has always emitted —
    asserted in tests/test_region_pricing.py so the two cannot drift while both
    names exist.
    """
    return priced_offer_exists_sql(
        product_key_expr,
        alias=alias,
        extra_predicate=region_currency_predicate(region, alias=alias),
    )
