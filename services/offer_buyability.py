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


def is_buyable_in(offer: Dict[str, Any], serving_market: str = DEFAULT_SERVING_MARKET) -> bool:
    """True when this offer can be purchased in the serving market.

    An offer whose market matches the serving market is buyable. An unknown/blank
    market is treated as buyable -- most of the catalog is single-market US with
    market defaulted, and we must not regress those to non-buyable. A concrete
    foreign market (e.g. KR while serving US) is NOT buyable here.
    """
    om = _norm_market(offer.get("market"))
    if not om:
        return True
    return om == _norm_market(serving_market) or (_norm_market(serving_market) or DEFAULT_SERVING_MARKET) == om


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

    def _in_stock(o: Dict[str, Any]) -> bool:
        return str(o.get("availability") or "").strip().lower() in {"in_stock", "instock", "available"}

    candidates = [o for o in out if o.get("buyable") and o.get("price") is not None]
    pick: Optional[Dict[str, Any]] = None
    if candidates:
        # cheapest; in-stock wins ties by sorting out-of-stock last
        pick = min(candidates, key=lambda o: (not _in_stock(o), float(o["price"])))
    for o in out:
        o["is_buy_pick"] = pick is not None and o is pick
    return out
