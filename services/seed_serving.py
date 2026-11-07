"""The currency an external_product_seeds row is priced in, the market that currency serves, and whether
it is served. One place, for every consumer of a seed row.

WHY THIS MODULE EXISTS. `external_product_seeds.market` is a storage PARTITION, not a market. Every seed
is written with market 'US' (services/catalog_enrichment_agent/ingestion.SEED_PARTITION_MARKET), the SGD
seeds and the AUD/JPY acquisition seeds included. A consumer that reads `row["market"]` as "the market
this row is for", or `seed_serving_currency(row["market"])` as "the currency it should be in", gets every
non-USD seed wrong. That mistake has been fixed one consumer at a time since 2026-10; the
tests/test_seed_partition_is_not_a_market.py guard now fails CI on a new one, and its allowlist lists
the sites that still do it.

ADR-026 (resolved question 2) dropped the seed partition: a seed is served by its currency. This module
is that rule for a row. The partition is NEVER read here.

THE ONE SANCTIONED INFERENCE OF A MARKET FROM A CURRENCY. ADR-024/026 say a market is never inferred
from a currency. A seed is the exception, and the only one: it declares no market, only a currency,
until it has per-market offers (ADR-026 P4). `seed_offer_market` already declares a seed-copied offer's
market this way (#2701/#2704), through the same inverse map (region_pricing.market_for_pricing_currency).
A currency that several markets price (EUR) or that none prices has no market: None, never a default.

Every rule here is built from code already on main and is not spelled a second time:
  * the row's currency is `seed_offer_currency.seed_currency_of` (column and page read must agree,
    seed_data.price_currency is not evidence; a projected `seed_currency` is the SQL's own answer);
  * currency -> market is `region_pricing.market_for_pricing_currency`;
  * market -> currency for a buyer is `external_seed_search.seed_serving_currency`;
  * the served currencies are the served pricing regions (`serving_pricing_regions`) through region_pricing.

No import of routes.*: jobs and scripts import this cheaply.
"""

from __future__ import annotations

import logging
from typing import Any, FrozenSet, Iterable, Mapping, Optional

from services.external_seed_search import seed_serving_currency
from services.region_pricing import (
    REGION_PRICING_CURRENCY,
    market_for_pricing_currency,
    pricing_currency_for_region_or_none,
)
from services.seed_offer_currency import currency_code, seed_currency_of, seed_currency_sql

logger = logging.getLogger(__name__)


def seed_row_currency(row: Mapping[str, Any]) -> Optional[str]:
    """The currency the seed row is priced in, or None when it states none or its column and page read
    disagree. Exactly `seed_offer_currency.seed_currency_of`."""
    return seed_currency_of(row)


def seed_row_market(row: Mapping[str, Any]) -> Optional[str]:
    """The market the seed row's own currency prices: USD -> US, SGD -> SG, AUD -> AU, JPY -> JP.

    None for a currency several markets share (EUR), for one no market prices, and for a row that states
    no currency. NEVER the row's `market` partition, and never a default. This is the store's market for
    a fetch, a probe, a locale, a UTM tag or a redirect token minted off the row."""
    return market_for_pricing_currency(seed_row_currency(row))


def served_currencies() -> FrozenSet[str]:
    """The currencies the served pricing regions price in: `serving_pricing_regions()` (the market registry's served
    markets, which PIVOTA_SERVING_PRICING_REGIONS may only narrow), the list every serving path reads, each mapped
    through region_pricing. E.g. {'USD', 'SGD'}.

    FAILS SAFE TO EMPTY, loudly: an empty list, or a registry or env that does not load, leaves the refresh tier on
    the partition's currency alone (`refresh_queue_tier` with no served currencies), never "every currency served".

    Moved here from services/external_referral_readiness.served_region_currencies (2026-11-06), which re-exports it.
    """
    try:
        from services.index_pipeline_state_service import serving_pricing_regions

        regions = list(serving_pricing_regions() or [])
    except Exception as exc:
        logger.warning("external referral refresh: no served pricing regions (%s); the queue tier uses the "
                       "partition's currency alone", exc)
        return frozenset()
    return frozenset(c for c in (pricing_currency_for_region_or_none(r) for r in regions) if c)


def seed_row_served(row: Mapping[str, Any], *, served: Optional[Iterable[str]] = None) -> bool:
    """Whether the seed row's own currency is a served one: `served` when given, else `served_currencies()`.

    A row that states no currency is never served, and blank codes in `served` serve nothing. An empty
    `served` serves nothing at all (the fail-safe of `served_currencies`)."""
    currency = seed_row_currency(row)
    if currency is None:
        return False
    pool = served_currencies() if served is None else served
    return currency in {code for code in (currency_code(c) for c in pool) if code}


def seed_row_served_for(row: Mapping[str, Any], buyer_market: Optional[str]) -> bool:
    """Whether the seed row is served to `buyer_market`: its own currency is the one
    `seed_serving_currency(buyer_market)` names, the existing buyer rule.

    That rule reads a missing buyer market as the default serving market (US, so USD); it is not
    restated here. A buyer market with no known currency ('DE', 'EU-DE') and a row that states no
    currency are never served."""
    currency = seed_row_currency(row)
    wanted = seed_serving_currency(buyer_market)
    return currency is not None and wanted is not None and currency == wanted


def _single_market_currencies() -> Mapping[str, str]:
    """currency -> market for every currency exactly one market prices, from REGION_PRICING_CURRENCY,
    in the table's order. The same answer as market_for_pricing_currency, as data."""
    out = {}
    for currency in dict.fromkeys(REGION_PRICING_CURRENCY.values()):
        market = market_for_pricing_currency(currency)
        if market is not None:
            out[currency] = market
    return out


def market_for_currency_sql(currency_expr: str) -> str:
    """`market_for_pricing_currency` in SQL over a currency expression that is already normalised (trimmed,
    upper-cased, NULL when absent): one WHEN per currency exactly one market prices, ELSE NULL.

    Generated from REGION_PRICING_CURRENCY, so a market added there is added here. The only values
    interpolated are the table's own ISO codes. Plain CASE, so it runs on SQLite and Postgres alike."""
    whens = " ".join(f"WHEN '{currency}' THEN '{market}'" for currency, market in _single_market_currencies().items())
    return f"(CASE {currency_expr} {whens} ELSE NULL END)"


def seed_row_market_sql(alias: str) -> str:
    """`seed_row_market` in SQL for an external_product_seeds alias: the market the row's own currency
    (`seed_offer_currency.seed_currency_sql`) prices, or NULL. Postgres (jsonb).

    FOR JOBS, CENSUSES AND REFRESH, NOT THE RECALL QUERY. seed_currency_sql reads seed_data, which is
    ~25 KB and TOASTed: its docstring measured +30-35% on scoped recall for a four-reference form. The
    simple CASE below references the currency once, so it detoasts once, but the hot search SQL keeps its
    column-only SEED_SERVING_CURRENCY_CLAUSE (services/external_seed_search)."""
    return market_for_currency_sql(seed_currency_sql(alias))


__all__ = [
    "market_for_currency_sql",
    "seed_row_currency",
    "seed_row_market",
    "seed_row_market_sql",
    "seed_row_served",
    "seed_row_served_for",
    "served_currencies",
]
