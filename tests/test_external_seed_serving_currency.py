"""A seed is served only in the currency of the market it is served to.

Peng 2026-09-26: "Fallback results are essentially wrong results -- we should not show them to the
agent frontend." Measured on prod the same day, the find_products_multi seed lane (which binds NO
partition) could reach 1,061 active non-USD / currency-less seeds (SGD 666, JPY 306, KRW 23,
GBP 20, NULL 46) beside 21,096 USD ones, and 7 of its 57 caller requests since 09-20 served SGD
next to USD. SG rows are stored in the 'US' partition on purpose, so even the lanes that DO bind
`market='US'` reach the SGD ones: the partition is not the currency.

The rule lives in `fetch_external_seed_rows`, the seam every seed search lane goes through, and
these tests EXECUTE the real function (SQLite, the harness shape of
tests/test_external_seed_quarantine_gate.py) rather than matching its text.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3

import pytest

from services.external_seed_search import (
    DEFAULT_SEED_SERVING_MARKET,
    fetch_external_seed_rows,
    seed_serving_currency,
)
from services.region_pricing import REGION_PRICING_CURRENCY


class _SqliteSeeds:
    """Enough of the `databases` interface for fetch_external_seed_rows, on SQLite.

    A sqlite URL makes the function take its non-Postgres path (no SET LOCAL); the Postgres-only
    seed_data JSON probes are collapsed to a never-matching literal -- they are orthogonal to the
    currency conjunct, which passes through verbatim. Counts every statement it executes.
    """

    url = "sqlite:///:memory:"

    def __init__(self, seeds):
        self.executed = 0
        self._conn = sqlite3.connect(":memory:")
        self._conn.execute(
            "CREATE TABLE external_product_seeds ("
            " id TEXT, external_product_id TEXT, market TEXT, tool TEXT, utm_template TEXT,"
            " partner_type TEXT, disclosure_text TEXT, destination_url TEXT, canonical_url TEXT,"
            " domain TEXT, title TEXT, image_url TEXT, price_amount REAL, price_currency TEXT,"
            " availability TEXT, seed_data TEXT, status TEXT, notes TEXT,"
            " created_by_employee_id TEXT, attached_product_key TEXT, attached_variant_id TEXT,"
            " seller_ref TEXT, seed_kind TEXT,"
            " destination_checked_at TEXT, destination_http_status INTEGER, destination_verdict TEXT,"
            " destination_failure_streak INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT)"
        )
        self._conn.execute(
            "CREATE TABLE catalog_source_quarantine (quarantine_id INTEGER, match_type TEXT,"
            " match_value TEXT, state TEXT, expires_at TEXT)"
        )
        for sid, market, currency in seeds:
            self._conn.execute(
                "INSERT INTO external_product_seeds (id, external_product_id, domain, title, status,"
                " market, price_amount, price_currency, seed_data, destination_url, created_at, updated_at)"
                " VALUES (?,?,'brand.com','hydrating serum','active',?,10,?,'{}','https://brand.com/p',"
                " '2026-01-01','2026-01-01')",
                (sid, sid, market, currency),
            )

    @staticmethod
    def _portable(sql: str) -> str:
        sql = re.sub(r"seed_data\s*#>>\s*'\{[^}]*\}'", "''", sql)
        sql = re.sub(r"seed_data\s*(->>?\s*'[^']*'\s*)+", "''", sql)
        return sql

    def _run(self, query, values):
        self.executed += 1
        sql = self._portable(str(query))
        values = values or {}
        ordered = [k for k in re.findall(r"(?<!:):(\w+)", sql) if k in values]
        for key in sorted(values, key=len, reverse=True):
            sql = re.sub(rf"(?<!:):{key}\b", "?", sql)
        return self._conn.execute(sql, [values[k] for k in ordered])

    async def fetch_all(self, query, values=None):
        cur = self._run(query, values)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    async def fetch_one(self, query, values=None):
        rows = await self.fetch_all(query, values)
        return rows[0] if rows else None


# The prod shapes, measured 2026-09-26: USD brand stores and SG retailers share the 'US' partition;
# the JP partition is JPY; a handful of US rows carry no currency at all.
CORPUS = [
    ("us_usd", "US", "USD"),
    ("us_usd_padded", "US", " usd "),
    ("us_sgd", "US", "SGD"),
    ("us_gbp", "US", "GBP"),
    ("us_aud", "US", "AUD"),
    ("us_jpy", "US", "JPY"),
    ("us_null", "US", None),
    ("us_blank", "US", ""),
    ("jp_jpy", "JP", "JPY"),
    ("kr_krw", "KR", "KRW"),
]


def _fetch(db=None, **kw):
    db = db or _SqliteSeeds(CORPUS)
    kw.setdefault("query", "serum")
    kw.setdefault("limit", 50)
    kw.setdefault("only_unattached", False)
    kw.setdefault("include_total_count", True)
    return asyncio.run(fetch_external_seed_rows(database=db, **kw))


def _ids(result):
    return sorted(r["id"] for r in result["rows"])


# --------------------------------------------------------------------------- US: accept / refuse

def test_a_us_partition_read_serves_usd_and_refuses_every_other_currency():
    result = _fetch(market="US")
    # ACCEPT: USD, including a padded lower-case code (same normalisation as region_pricing's
    # catalog_offers predicate). REFUSE: SGD / GBP / AUD / JPY stored in the US partition, and a
    # row with no currency -- unknown is not assumed to be USD.
    assert _ids(result) == ["us_usd", "us_usd_padded"]


def test_the_unpartitioned_multi_lane_read_defaults_to_the_us_currency():
    """The find_products_multi lane binds market=None. A request that named no market is a US
    request (every caller request in prod telemetry since 09-20 was market-less), so it must get
    USD -- not every partition's currency at once, which is what it got before."""
    result = _fetch(market=None, serving_market=None)
    assert _ids(result) == ["us_usd", "us_usd_padded"]
    assert DEFAULT_SEED_SERVING_MARKET == "US"


def test_the_count_is_filtered_like_the_page():
    """A conjunct on the page query but not the count would advertise rows no page can hold."""
    result = _fetch(market=None)
    assert result["total_count"] == 2


def test_the_lean_where_path_is_filtered_too():
    """The lean branch rebuilds the text clause; the currency conjunct must not ride on it."""
    result = _fetch(market=None, query="hydrating serum", lean_where_min_tokens=2)
    assert _ids(result) == ["us_usd", "us_usd_padded"]


# --------------------------------------------------------------------------- other markets

def test_a_jp_request_on_the_unpartitioned_lane_gets_jpy_only():
    """The currency follows the BUYER's market, not a hard-coded USD: a JP buyer gets the JP
    partition's JPY rows and the JPY one filed under US, and no USD."""
    result = _fetch(market=None, serving_market="JP")
    assert _ids(result) == ["jp_jpy", "us_jpy"]


def test_an_sg_request_gets_the_sg_rows_the_us_partition_holds():
    result = _fetch(market=None, serving_market=" sg ")
    assert _ids(result) == ["us_sgd"]


def test_serving_market_outranks_the_partition():
    """A lane may read the US partition for a buyer in another market; the buyer decides."""
    result = _fetch(market="US", serving_market="JP")
    assert _ids(result) == ["us_jpy"]


def test_a_market_with_no_known_currency_serves_no_seed_and_runs_no_query():
    db = _SqliteSeeds(CORPUS)
    result = _fetch(db, market=None, serving_market="DE")
    assert result["rows"] == [] and result["total_count"] == 0
    assert result["serving_currency_unknown"] is True
    assert db.executed == 0


# --------------------------------------------------------------------------- one rule, not two

@pytest.mark.parametrize("market", sorted(REGION_PRICING_CURRENCY))
def test_the_serving_currency_is_region_pricings_table(market):
    assert seed_serving_currency(market) == REGION_PRICING_CURRENCY[market]
    assert seed_serving_currency(market.lower()) == REGION_PRICING_CURRENCY[market]


def test_an_unknown_market_has_no_serving_currency():
    for market in ("DE", "EU-DE", "en-US", "ZZ"):
        assert seed_serving_currency(market) is None
    assert seed_serving_currency(None) == "USD"
    assert seed_serving_currency("") == "USD"
    # metadata.market reaches the lane raw: a blank one named no market, it is not an unknown one.
    assert seed_serving_currency("   ") == "USD"


def test_a_blank_serving_market_falls_back_to_the_partition_then_us():
    assert _ids(_fetch(market=None, serving_market="  ")) == ["us_usd", "us_usd_padded"]
    assert _ids(_fetch(market="JP", serving_market="")) == ["jp_jpy"]


# --------------------------------------------------------------------------- the multi lane's wiring

async def _multi_lane_seed_cards(monkeypatch, metadata, *, stage_a_empty=False):
    """Drive find_products_multi end to end with the REAL fetch over the SQLite corpus, so what is
    asserted is what the handler hands the seed reader -- not a kwarg it happens to spell."""
    import routes.agent_shop_gateway as gw

    db = _SqliteSeeds(CORPUS)
    real_fetch = fetch_external_seed_rows
    seen = []

    async def fetch_over_corpus(**kwargs):
        seen.append({k: kwargs.get(k) for k in ("market", "serving_market")})
        if stage_a_empty and not kwargs.get("include_seed_data_text_match"):
            return {"rows": [], "query_timeout": False, "query_ms": 0, "total_count": 0}
        return await real_fetch(**{**kwargs, "database": db})

    async def no_rows(query, values=None):
        return []

    monkeypatch.setattr(gw.database, "fetch_all", no_rows)
    monkeypatch.setattr(gw, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(gw, "fetch_external_seed_rows", fetch_over_corpus)
    monkeypatch.setattr(gw, "MULTI_SEARCH_SHOPPING_ENABLE_SEED_TEXT_SCAN", True)

    payload = gw.FindProductsMultiPayload(
        search=gw.MultiSearchFilters(query="hydrating serum", page=1, limit=20, in_stock_only=False)
    )
    result = await gw._handle_find_products_multi(payload, metadata, gw.BackgroundTasks())
    cards = [p for p in result.get("products") or [] if p.get("source") == "external_seed"]
    return cards, seen


def _card_currencies(cards):
    return {str(c.get("currency") or c.get("price_currency") or "").strip().upper() for c in cards}


@pytest.mark.asyncio
async def test_a_market_less_multi_request_is_served_usd_seeds_only(monkeypatch):
    cards, seen = await _multi_lane_seed_cards(monkeypatch, {"source": "shopping-agent"})
    assert seen and all(call == {"market": None, "serving_market": None} for call in seen)
    assert cards, "the USD seeds must still serve"
    assert _card_currencies(cards) == {"USD"}


@pytest.mark.asyncio
async def test_a_jp_multi_request_is_served_in_jpy(monkeypatch):
    cards, seen = await _multi_lane_seed_cards(
        monkeypatch, {"source": "shopping-agent", "market": "JP"}
    )
    assert seen and all(call == {"market": None, "serving_market": "JP"} for call in seen)
    assert cards, "a JP buyer still gets the JPY seeds"
    assert _card_currencies(cards) == {"JPY"}


@pytest.mark.asyncio
async def test_the_stage_b_text_scan_fallback_carries_the_buyer_market_too(monkeypatch):
    """Stage B runs only when stage A found nothing -- the fallback of the fallback. It is a second
    call site, and must not be the one place an SGD / JPY seed still reaches a US buyer."""
    cards, seen = await _multi_lane_seed_cards(
        monkeypatch, {"source": "shopping-agent", "market": "JP"}, stage_a_empty=True
    )
    assert len(seen) == 2, "stage B did not run"
    assert all(call == {"market": None, "serving_market": "JP"} for call in seen)
    assert cards and _card_currencies(cards) == {"JPY"}
