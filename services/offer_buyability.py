"""Market-aware buyability for served offers.

The index carries offers from multiple markets on one product: a brand's own
KRW/market=KR listing (identity/content anchor) alongside a US retailer offer.
Against the request's serving market, each offer is either a DOMESTIC buy (its
market matches the buyer's) or a CROSS_BORDER one (a different market -- possibly
shippable, but with caveats: shipping, duties, currency). We deliberately do NOT
collapse cross-border into "not buyable": `market` is the only geo signal we have
(there is no ships_to/fulfillment data yet) and it is ~100% a US default, so a
hard market-equality gate would erase the whole catalog for a non-US buyer. The
honest verdict is domestic-vs-cross-border, and the buy pick prefers domestic but
falls back to a clearly-flagged cross-border offer rather than "nothing to buy".

When real fulfillment reach (ships_to) lands, cross_border can be resolved
further into shippable vs unavailable; the served field stays the same.

CURRENCY IS NOT COMPARABLE (ADR-024 Phase 0, item 1). The buy pick used to be
`min(pool, key=... float(price))` over a pool the cross-border fallback can fill
with several currencies at once, so a 4500 JPY offer "beat" a 12 GBP one as raw
floats. That is this repo's recurring cross-unit defect in its fourth layer
(ingestion, read, presentation, and here — selection). The pick therefore now
narrows to ONE currency before any price comparison: the serving market's
expected currency when the pool holds it, else the largest single-currency group.
Ordering INSIDE that group is unchanged (in-stock first, then lowest price), so a
single-currency pool -- the overwhelmingly common all-USD/US case -- picks exactly
what it picked before. We never convert and never rank across currencies.

Pure functions (no DB/IO). Additive: annotation only sets `market_availability` /
`is_buy_pick`; it never drops offers.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

DEFAULT_SERVING_MARKET = "US"

MARKET_DOMESTIC = "domestic"
MARKET_CROSS_BORDER = "cross_border"

# The currency an offer must be priced in to be a same-currency buy for a serving
# market -- the regions ADR-024 measured real supply for, nothing speculative.
# An UNMAPPED market has no expected currency (None) and falls through to the
# largest-single-currency rule below; it must never quietly become USD, which is
# the assumption every one of the four currency defects was built on.
#
# NOT reused from routes/employee_products.MARKET_EXPECTED_CURRENCY, the repo's
# other such map, for two reasons: it lives in a route module (a service must not
# import a route to answer this), and its membership is a different question --
# it is the CSV-import validator's list, carrying non-ISO keys ("UK", "EU") and
# missing HR/FI/SE/HK, the regions ADR-024 measured real non-USD supply in. The
# two converge in ADR-024 Phase 1's services/region_pricing; fold this copy in
# there rather than growing a third.
EXPECTED_CURRENCY_BY_MARKET: Dict[str, str] = {
    "US": "USD", "GB": "GBP", "JP": "JPY", "FR": "EUR", "HR": "EUR",
    "FI": "EUR", "AU": "AUD", "SE": "SEK", "KR": "KRW", "HK": "HKD",
    "SG": "SGD", "CA": "CAD",
}

# Partition key for an offer that declares no currency. Its own bucket, never
# merged into USD: "no currency stated" is not evidence of dollars.
NO_CURRENCY = "(none)"


def _norm_market(value: Any) -> str:
    return str(value or "").strip().upper()


def expected_currency_for_market(serving_market: Any) -> Optional[str]:
    """The pricing currency a domestic buy in `serving_market` should carry, or
    None when we have not mapped that market. None is an honest "unknown", not
    a licence to assume USD."""
    return EXPECTED_CURRENCY_BY_MARKET.get(_norm_market(serving_market))


def _currency_key(value: Any) -> str:
    return str(value or "").strip().upper() or NO_CURRENCY


def _same_currency_candidates(
    pool: Sequence[Tuple[Any, str]], expected_currency: Optional[str]
) -> List[Any]:
    """Narrow a priced candidate pool to exactly ONE currency, before any price
    comparison happens.

    `pool` is [(candidate, currency_key)] in stable input order. Prefers the
    serving market's expected currency; with none of those present, takes the
    LARGEST single-currency group -- dict preserves first-seen order and max()
    keeps the first maximum, so a tie resolves to the group whose first offer
    appeared first in the input. The result is never mixed-currency, which is
    the whole point: no min() ever spans two units.
    """
    groups: Dict[str, List[Any]] = {}
    for candidate, currency_key in pool:
        groups.setdefault(currency_key, []).append(candidate)
    if not groups:
        return []
    if expected_currency and expected_currency in groups:
        return groups[expected_currency]
    return max(groups.values(), key=len)


def offer_market_availability(
    offer_market: Any, serving_market: str = DEFAULT_SERVING_MARKET
) -> str:
    """domestic when the offer serves the buyer's market, else cross_border.

    A blank/unknown offer market is assumed to be the index's default market
    (US-oriented): domestic when serving that default, cross_border otherwise --
    so a US-default catalog isn't falsely reported as domestic to a foreign buyer,
    but also isn't erased.
    """
    sm = _norm_market(serving_market) or DEFAULT_SERVING_MARKET
    om = _norm_market(offer_market)
    if not om:
        om = DEFAULT_SERVING_MARKET
    return MARKET_DOMESTIC if om == sm else MARKET_CROSS_BORDER


def _in_stock(availability: Any) -> bool:
    return str(availability or "").strip().lower() in {"in_stock", "instock", "available"}


def annotate_offer_buyability(
    offers: List[Dict[str, Any]],
    serving_market: str = DEFAULT_SERVING_MARKET,
) -> List[Dict[str, Any]]:
    """Set `market_availability` (domestic|cross_border) per dict-offer and
    `is_buy_pick` on the offer to present as the buy: cheapest in-stock DOMESTIC
    offer, falling back to cheapest in-stock CROSS_BORDER when none is domestic.

    "Cheapest" is only asked WITHIN one currency (see the module docstring): the
    pool is narrowed to a single currency first, reading each offer's own
    `currency` key.
    """
    sm = _norm_market(serving_market) or DEFAULT_SERVING_MARKET
    out: List[Dict[str, Any]] = []
    for o in offers or []:
        oo = dict(o)
        oo["market_availability"] = offer_market_availability(o.get("market"), sm)
        out.append(oo)

    def priced(avail: str) -> List[Dict[str, Any]]:
        return [o for o in out if o["market_availability"] == avail and o.get("price") is not None]

    pool = priced(MARKET_DOMESTIC) or priced(MARKET_CROSS_BORDER)
    candidates = _same_currency_candidates(
        [(o, _currency_key(o.get("currency"))) for o in pool],
        expected_currency_for_market(sm),
    )
    pick: Optional[Dict[str, Any]] = None
    if candidates:
        pick = min(candidates, key=lambda o: (not _in_stock(o.get("availability")), float(o["price"])))
    for o in out:
        o["is_buy_pick"] = pick is not None and o is pick
    return out


def _node_price(node: Any) -> Optional[float]:
    pricing = getattr(node, "pricing", None)
    if pricing is None:
        return None
    for attr in ("estimated_best_price", "merchant_effective_price", "list_price", "exact_quote_price"):
        v = getattr(pricing, attr, None)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def _node_currency(node: Any) -> Any:
    """The node's own pricing currency (PivotPricing.currency), or None."""
    pricing = getattr(node, "pricing", None)
    return getattr(pricing, "currency", None) if pricing is not None else None


def annotate_offer_nodes(nodes: List[Any], serving_market: str = DEFAULT_SERVING_MARKET) -> List[Any]:
    """Duck-typed variant for OfferNode (search path): sets .market_availability +
    .is_buy_pick in place against the request market. Reads .market/.availability/
    .pricing.* -- no model import, so this module stays dependency-free. Same rule
    as the dict path (domestic preferred, cross-border fallback, and the same
    single-currency narrowing before any price comparison -- shared, not
    re-spelled). The node's currency lives on .pricing.currency, not beside
    .market.
    """
    sm = _norm_market(serving_market) or DEFAULT_SERVING_MARKET
    nodes = nodes or []
    for n in nodes:
        n.market_availability = offer_market_availability(getattr(n, "market", None), sm)

    def priced(avail: str) -> List[Any]:
        return [
            (n, _node_price(n)) for n in nodes
            if getattr(n, "market_availability", None) == avail and _node_price(n) is not None
        ]

    pool = priced(MARKET_DOMESTIC) or priced(MARKET_CROSS_BORDER)
    candidates = _same_currency_candidates(
        [(np, _currency_key(_node_currency(np[0]))) for np in pool],
        expected_currency_for_market(sm),
    )
    pick = None
    if candidates:
        pick = min(
            candidates,
            key=lambda np: (not _in_stock(getattr(np[0], "availability", None)), np[1]),
        )[0]
    for n in nodes:
        n.is_buy_pick = pick is not None and n is pick
    return nodes
