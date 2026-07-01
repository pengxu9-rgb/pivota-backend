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

Pure functions (no DB/IO). Additive: annotation only sets `market_availability` /
`is_buy_pick`; it never drops offers.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

DEFAULT_SERVING_MARKET = "US"

MARKET_DOMESTIC = "domestic"
MARKET_CROSS_BORDER = "cross_border"


def _norm_market(value: Any) -> str:
    return str(value or "").strip().upper()


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
    pick: Optional[Dict[str, Any]] = None
    if pool:
        pick = min(pool, key=lambda o: (not _in_stock(o.get("availability")), float(o["price"])))
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


def annotate_offer_nodes(nodes: List[Any], serving_market: str = DEFAULT_SERVING_MARKET) -> List[Any]:
    """Duck-typed variant for OfferNode (search path): sets .market_availability +
    .is_buy_pick in place against the request market. Reads .market/.availability/
    .pricing.* -- no model import, so this module stays dependency-free. Same rule
    as the dict path (domestic preferred, cross-border fallback).
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
    pick = None
    if pool:
        pick = min(pool, key=lambda np: (not _in_stock(getattr(np[0], "availability", None)), np[1]))[0]
    for n in nodes:
        n.is_buy_pick = pick is not None and n is pick
    return nodes
