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
from services.region_pricing import pricing_currency_for_region_or_none

DEFAULT_SERVING_MARKET = "US"

MARKET_DOMESTIC = "domestic"
MARKET_CROSS_BORDER = "cross_border"

# The currency an offer must be priced in to be a same-currency buy for a serving
# market -- the regions ADR-024 measured real supply for, nothing speculative.
# An UNMAPPED market has no expected currency (None) and falls through to the
# largest-single-currency rule below; it must never quietly become USD, which is
# the assumption every one of the four currency defects was built on.
#
# The region->currency map itself lives in services/region_pricing (ADR-024
# Phase 1); this module holds only the SOFT lookup policy: an unmapped serving
# market yields None -- an honest "unknown" that routes to the
# largest-single-currency rule below -- never a silent USD, which is the
# assumption every one of the four currency defects was built on.
# (routes/employee_products.MARKET_EXPECTED_CURRENCY remains separate on
# purpose: it is the CSV-import validator's list, a different question, with
# non-ISO keys.)

# Partition key for an offer that declares no currency. Its own bucket, never
# merged into USD: "no currency stated" is not evidence of dollars.
NO_CURRENCY = "(none)"


def _norm_market(value: Any) -> str:
    return str(value or "").strip().upper()


def expected_currency_for_market(serving_market: Any) -> Optional[str]:
    """The pricing currency a domestic buy in `serving_market` should carry, or
    None when we have not mapped that market. None is an honest "unknown", not
    a licence to assume USD."""
    return pricing_currency_for_region_or_none(_norm_market(serving_market))


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


# The one in-stock vocabulary. Exported because a second caller (the UCP probe's
# variant selector) needs the SAME answer this module gives: two copies of this
# set drift, and a drift here means one lane calls a variant buyable while the
# other calls it dead.
IN_STOCK_AVAILABILITY = frozenset({"in_stock", "instock", "available"})


def _in_stock(availability: Any) -> bool:
    return str(availability or "").strip().lower() in IN_STOCK_AVAILABILITY


# "THIS SELLER CANNOT SELL IT", spelled once. The ORDER vocabulary, and the other half of the one
# above: IN_STOCK_AVAILABILITY is an explicit "yes", this is an explicit "no", and a value in
# neither (`unknown`, NULL, empty) is no statement at all. Two orderings bind it:
#   - offers.resolve's catalog arm (routes/agent_shop_gateway): its SQL ORDER BY, the `in_stock`
#     flag it emits, and `_rank_offers_merit_first`, which reads that flag. (Internal offers are
#     read the same way since #2221: their flag is the eligibility gate's verdict.)
#   - agent_pdp_view.offers (services/agent_pdp_view_assembler.aggregate_offers), before the
#     top-N cut, through `availability_is_known_unavailable` below.
# It lived in the gateway until the second caller needed it; a service importing a router to
# read one frozenset would be the wrong way round.
#
# NOT THE REPO'S RAW-STRING VOCABULARY. utils.availability_vocabulary owns "is this out of stock"
# for raw platform/feed strings (phrases, schema.org IRIs, `discontinued`, `reserved`, ...), and
# the writers that feed catalog_offers normalize through it, so the column holds only its
# canonical output: prod 2026-09-22, all 33,344 rows are in_stock / out_of_stock / `unknown`.
# This set is the literal subset SQL can bind (`= ANY(:unavailable)`) to read those STORED
# values; a test pins that every token in it is out of stock to the owner. Do not point a reader
# of raw strings at this set — use the owner.
#
# UNKNOWN IS NOT OUT OF STOCK. `unknown` (the column's server default), NULL, empty or any value
# not in this set ranks WITH the in-stock offers, by price, and never behind them. Two reasons, both measured rather than preferred:
#   1. Every lane already reports it that way: the seed lane maps `availability: "unknown"` to
#      `in_stock: True`, and the catalog arm maps NULL to `in_stock: True`. A three-way rank (in
#      stock > unknown > out of stock) could only be applied where the raw column survives, i.e.
#      to that arm alone, and would then order offers by a distinction the flag on them does not
#      show — and that the gateway's `best_offer` (PIVOTA-Agent offersToSignals, which reads the
#      flag since PIVOTA-Agent#2240) could not reproduce.
#   2. Absence of a stock statement is not evidence against a seller. Demoting it is the same
#      error the gateway's verification tier refuses to make for an unchecked offer.
# In prod on 2026-09-18 every live retailer offer said `in_stock` (1,178) or `out_of_stock` (86),
# so the choice changes no row served today; it decides what the next feed with gaps gets.
# (All unsuppressed catalog_offers the same day: in_stock 19,480, out_of_stock 1,197, unknown 682
# — no other spelling.)
#
# NOT the buy pick's rule. `annotate_offer_buyability` above asks the positive question (is it
# explicitly in stock?), so there an `unknown` offer loses to an in-stock one. That picks ONE offer
# to present as the buy; this orders the list. They agree on every explicit value.
OFFER_UNAVAILABLE_AVAILABILITIES: frozenset[str] = frozenset(
    {"out_of_stock", "outofstock", "sold_out", "soldout", "unavailable"}
)


def availability_is_known_unavailable(availability: Any) -> bool:
    """True only on an explicit statement that this seller cannot sell it now: an
    ``availability`` in OFFER_UNAVAILABLE_AVAILABILITIES, trimmed and case-insensitive.
    None, empty, ``unknown`` and any other value are False."""
    return str(availability or "").strip().lower() in OFFER_UNAVAILABLE_AVAILABILITIES


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
