"""Market-aware buyability for served offers.

The commerce index carries offers from multiple markets on one product: a
brand's own KRW/market=KR listing (identity/content anchor, but a cross-border
purchase for a US agent) alongside a US retailer offer that is the actual buy.
The served row is market-agnostic; this decides, against the request's serving
market, which offers are buyable *here* and which one is the buy pick -- so an
agent never treats a cross-border listing as a same-market purchase.

Pure functions (no DB/IO). Additive: annotation only sets `buyable` /
`is_buy_pick`; it never drops offers (a non-buyable offer is still shown as a
reference/where-else-it-sells signal).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

DEFAULT_SERVING_MARKET = "US"


def _norm_market(value: Any) -> str:
    return str(value or "").strip().upper()


def market_is_buyable(offer_market: Any, serving_market: str = DEFAULT_SERVING_MARKET) -> bool:
    """Core rule: an offer's market can be purchased in the serving market.

    A matching market is buyable. An unknown/blank offer market is treated as
    buyable -- most of the catalog is single-market US with market defaulted, and
    we must not regress those. A concrete foreign market (e.g. KR while serving
    US) is NOT buyable here.
    """
    om = _norm_market(offer_market)
    if not om:
        return True
    return om == (_norm_market(serving_market) or DEFAULT_SERVING_MARKET)


def is_buyable_in(offer: Dict[str, Any], serving_market: str = DEFAULT_SERVING_MARKET) -> bool:
    """Dict-offer convenience over market_is_buyable (agent_pdp_view.offers)."""
    return market_is_buyable(offer.get("market"), serving_market)


def annotate_offer_buyability(
    offers: List[Dict[str, Any]],
    serving_market: str = DEFAULT_SERVING_MARKET,
) -> List[Dict[str, Any]]:
    """Return offers with `buyable` set per offer and `is_buy_pick` on the one
    to present as the buy: the cheapest buyable offer (in-stock preferred). When
    no offer is buyable in the serving market (e.g. a brand-direct-only foreign
    listing), no offer is the buy pick -- an honest "not purchasable here yet".
    """
    sm = _norm_market(serving_market) or DEFAULT_SERVING_MARKET
    out: List[Dict[str, Any]] = []
    for o in offers or []:
        oo = dict(o)
        oo["buyable"] = is_buyable_in(o, sm)
        out.append(oo)

    candidates = [o for o in out if o.get("buyable") and o.get("price") is not None]
    pick: Optional[Dict[str, Any]] = None
    if candidates:
        # cheapest; in-stock wins ties by sorting out-of-stock last
        pick = min(candidates, key=lambda o: (not _in_stock(o.get("availability")), float(o["price"])))
    for o in out:
        o["is_buy_pick"] = pick is not None and o is pick
    return out


def _in_stock(availability: Any) -> bool:
    return str(availability or "").strip().lower() in {"in_stock", "instock", "available"}


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
    """Duck-typed variant for OfferNode (search path): sets .buyable + .is_buy_pick
    on each node in place against the request's serving market. Reads .market,
    .availability, .pricing.* -- no model import, so services.offer_buyability
    stays dependency-free. Same rule as the dict path.
    """
    sm = _norm_market(serving_market) or DEFAULT_SERVING_MARKET
    nodes = nodes or []
    for n in nodes:
        n.buyable = market_is_buyable(getattr(n, "market", None), sm)
    priced = [(n, _node_price(n)) for n in nodes if getattr(n, "buyable", False)]
    priced = [(n, pr) for n, pr in priced if pr is not None]
    pick = None
    if priced:
        pick = min(priced, key=lambda np: (not _in_stock(getattr(np[0], "availability", None)), np[1]))[0]
    for n in nodes:
        n.is_buy_pick = pick is not None and n is pick
    return nodes
