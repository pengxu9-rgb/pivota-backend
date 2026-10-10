"""agent_pdp_view.market_prices (migration 263): the per-served-market price summary, and the
agent PDP route's buy pick for the BUYER's market.

What is pinned, and why:

  * USD-only products: the legacy columns (currency, price_min, price_max, offer_count, offers) are
    byte-identical with the write flag on and off, and the US summary restates them exactly.
  * USD + SGD siblings: the legacy columns are unchanged (still the modal currency, ties to the
    higher code, top-5 cut across currencies), and the SG summary carries the SGD price range and
    the SGD offers the cross-currency cut drops.
  * SGD-only: an SG summary and NO US one, so no US reader can price it.
  * Flag off: assemble_row emits no market_prices key and the upsert never names the column.
  * Route: no buyer market / the deployment's own market = the response is byte-identical; an SG
    buyer gets the SGD price block and an SGD buy pick when the row carries the SG summary.

Every row is built by the real assembler (assemble_row) from offers in the shape
fetch_offers_for_keys SELECTs, and reaches the route the way the database hands it back
(jsonb as JSON text), never as a hand-made dict no producer makes.
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import routes.agent_pdp_v1 as agent_pdp_v1  # noqa: E402
from services import agent_pdp_view_assembler as assembler  # noqa: E402

CK = "ck_" + "e" * 32
SIG = "sig_" + "e" * 32


@pytest.fixture(autouse=True)
def _served_regions(monkeypatch):
    monkeypatch.setenv("PIVOTA_SERVING_PRICING_REGIONS", "US,SG")
    monkeypatch.delenv(assembler.MARKET_PRICES_FLAG_ENV, raising=False)
    monkeypatch.delenv("AGENT_PDP_V1_MARKET_PRICES_READ", raising=False)
    monkeypatch.delenv("AGENT_PDP_SERVING_MARKET", raising=False)


def _offer(n: int, *, merchant: str, currency: str, market: str, price: str,
           availability: str = "in_stock") -> Dict[str, Any]:
    """One row as fetch_offers_for_keys returns it (its SELECT list, nothing more)."""
    return {
        "offer_id": f"of_{n}", "sku_key": f"sku_{n}", "product_key": "pk_store",
        "merchant_id": merchant, "availability": availability, "currency": currency,
        "list_price": Decimal(price), "merchant_effective_price": None,
        "estimated_best_price": None, "market": market, "offer_type": "retail",
        "is_first_party": False, "merchant_name": merchant,
    }


def _products() -> List[Dict[str, Any]]:
    return [{
        "product_key": "pk_store", "merchant_id": "m_store", "platform": "shopify",
        "source_product_id": "sp_1", "title": "Barrier Cream", "description": "A cream.",
        "brand": "Example", "product_payload": {}, "pdp_lifecycle_stage": "published",
        "pivota_signature_id": SIG, "canonical_url": "https://store.example.com/p/1",
        "sync_status": "live", "product_group_id": "grp_1", "group_is_primary": True,
    }]


def _assemble(offers: List[Dict[str, Any]]) -> Dict[str, Any]:
    row = assembler.assemble_row(
        content_key=CK, products=_products(), skus=[], offers=offers, external_seed=None,
    )
    assert row is not None
    return row


LEGACY = ("currency", "price_min", "price_max", "offer_count", "offers")

USD_ONLY = [
    _offer(1, merchant="m_store", currency="USD", market="US", price="24.00"),
    _offer(2, merchant="m_a", currency="USD", market="US", price="21.00"),
    _offer(3, merchant="m_b", currency="USD", market="US", price="22.50", availability="out_of_stock"),
]

# Six USD offers and their three SGD siblings. SGD amounts are larger numbers, so the legacy
# cross-currency top-5 keeps only USD offers, and USD is the modal currency.
MIXED = [
    _offer(i, merchant=f"m_us{i}", currency="USD", market="US", price=f"{18 + i}.00")
    for i in range(6)
] + [
    _offer(10, merchant="m_sg0", currency="SGD", market="SG", price="32.00"),
    _offer(11, merchant="m_sg1", currency="SGD", market="SG", price="29.90"),
    _offer(12, merchant="m_sg2", currency="SGD", market="US", price="35.00", availability="sold_out"),
]

SGD_ONLY = [
    _offer(1, merchant="m_store", currency="SGD", market="SG", price="32.00"),
    _offer(2, merchant="m_sg1", currency="SGD", market="SG", price="30.00"),
]


# ---------------------------------------------------------------------------
# assembler
# ---------------------------------------------------------------------------


def test_flag_off_emits_no_market_prices_key_and_the_legacy_upsert() -> None:
    row = _assemble(MIXED)
    assert "market_prices" not in row
    assert assembler.upsert_sql_for_row(row) is assembler.UPSERT_SQL
    assert "market_prices" not in assembler.UPSERT_SQL
    assert "market_prices" not in assembler.row_to_upsert_params(row)


@pytest.mark.parametrize("offers", [USD_ONLY, MIXED, SGD_ONLY], ids=["usd", "usd+sgd", "sgd"])
def test_legacy_columns_are_identical_with_the_flag_on(monkeypatch, offers) -> None:
    off = _assemble(offers)
    monkeypatch.setenv(assembler.MARKET_PRICES_FLAG_ENV, "on")
    on = _assemble(offers)
    assert {k: on[k] for k in LEGACY} == {k: off[k] for k in LEGACY}
    assert {k: v for k, v in on.items() if k != "market_prices"} == off
    # ...and so are their upsert binds: only the new one is added.
    params_on = assembler.row_to_upsert_params(on)
    params_off = assembler.row_to_upsert_params(off)
    assert set(params_on) - set(params_off) == {"market_prices"}
    assert {k: params_on[k] for k in params_off} == params_off


def test_usd_only_us_summary_restates_the_legacy_columns(monkeypatch) -> None:
    monkeypatch.setenv(assembler.MARKET_PRICES_FLAG_ENV, "on")
    row = _assemble(USD_ONLY)
    assert set(row["market_prices"]) == {"US"}
    us = row["market_prices"]["US"]
    assert us["currency"] == row["currency"] == "USD"
    assert Decimal(str(us["price_min"])) == row["price_min"] == Decimal("21.00")
    assert Decimal(str(us["price_max"])) == row["price_max"] == Decimal("24.00")
    assert us["offer_count"] == row["offer_count"] == 3
    assert us["offers"] == row["offers"]


def test_usd_plus_sgd_keeps_legacy_usd_and_adds_both_summaries(monkeypatch) -> None:
    monkeypatch.setenv(assembler.MARKET_PRICES_FLAG_ENV, "on")
    row = _assemble(MIXED)

    # Legacy: modal USD, USD range, every offer counted, top-5 holds no SGD offer.
    assert row["currency"] == "USD"
    assert (row["price_min"], row["price_max"]) == (Decimal("18.00"), Decimal("23.00"))
    assert row["offer_count"] == 9
    assert len(row["offers"]) == assembler.OFFER_TOP_N
    assert {o["currency"] for o in row["offers"]} == {"USD"}

    us, sg = row["market_prices"]["US"], row["market_prices"]["SG"]
    assert (us["currency"], us["price_min"], us["price_max"], us["offer_count"]) == ("USD", 18.0, 23.0, 6)
    assert us["offers"] == row["offers"]
    # SG: the SGD range over every SGD offer (sold-out included, like offer_count), no conversion,
    # and the SGD offers in the stored order: sellable first, then price.
    assert (sg["currency"], sg["price_min"], sg["price_max"], sg["offer_count"]) == ("SGD", 29.9, 35.0, 3)
    assert [o["merchant_id"] for o in sg["offers"]] == ["m_sg1", "m_sg0", "m_sg2"]
    assert all(o["currency"] == "SGD" for o in sg["offers"])
    # The offer dicts are the same projection `offers` stores.
    assert set(sg["offers"][0]) == set(row["offers"][0])


def test_primary_store_sgd_sibling_ranks_first_within_sg(monkeypatch) -> None:
    """The store's own SGD sibling (the primary merchant) leads the SG offers, as its USD base
    offer leads the stored ones."""
    monkeypatch.setenv(assembler.MARKET_PRICES_FLAG_ENV, "on")
    offers = MIXED + [_offer(13, merchant="m_store", currency="SGD", market="SG", price="33.00")]
    sg = _assemble(offers)["market_prices"]["SG"]
    assert sg["offers"][0]["merchant_id"] == "m_store"
    assert sg["offers"][0]["is_primary"] is True
    assert sg["offers"][0]["url"] == "https://store.example.com/p/1"


def test_per_market_top_n_caps_each_market_independently(monkeypatch) -> None:
    monkeypatch.setenv(assembler.MARKET_PRICES_FLAG_ENV, "on")
    offers = [
        _offer(i, merchant=f"m_us{i}", currency="USD", market="US", price=f"{10 + i}.00")
        for i in range(7)
    ] + [
        _offer(20 + i, merchant=f"m_sg{i}", currency="SGD", market="SG", price=f"{40 + i}.00")
        for i in range(7)
    ]
    row = _assemble(offers)
    # Tie 7/7: legacy currency resolves to the higher code, exactly as before.
    assert row["currency"] == "USD"
    for region, currency in (("US", "USD"), ("SG", "SGD")):
        entry = row["market_prices"][region]
        assert entry["offer_count"] == 7
        assert len(entry["offers"]) == assembler.OFFER_TOP_N
        assert {o["currency"] for o in entry["offers"]} == {currency}


def test_sgd_only_has_an_sg_summary_and_no_us_one(monkeypatch) -> None:
    monkeypatch.setenv(assembler.MARKET_PRICES_FLAG_ENV, "on")
    row = _assemble(SGD_ONLY)
    assert row["currency"] == "SGD"
    assert set(row["market_prices"]) == {"SG"}
    assert row["market_prices"]["SG"]["price_min"] == 30.0


def test_unserved_region_and_unpriced_rows_get_no_entry(monkeypatch) -> None:
    monkeypatch.setenv(assembler.MARKET_PRICES_FLAG_ENV, "on")
    monkeypatch.setenv("PIVOTA_SERVING_PRICING_REGIONS", "US")
    assert set(_assemble(MIXED)["market_prices"]) == {"US"}
    row = _assemble([])
    # Computed, nothing priced: {} (written as '{}', never NULL = "not computed").
    assert row["market_prices"] == {}
    assert assembler.row_to_upsert_params(row)["market_prices"] == "{}"


def test_flag_on_upsert_names_the_column_in_insert_values_and_update() -> None:
    sql = assembler.UPSERT_SQL_WITH_MARKET_PRICES
    assert sql.count("market_prices") == 4
    assert "CAST(:market_prices AS jsonb)" in sql
    assert "market_prices = EXCLUDED.market_prices" in sql
    # Everything else is UPSERT_SQL byte for byte.
    stripped = (
        sql.replace(" market_prices,", "", 1)
        .replace(" CAST(:market_prices AS jsonb),", "", 1)
        .replace("      market_prices = EXCLUDED.market_prices,\n", "", 1)
    )
    assert stripped == assembler.UPSERT_SQL


# ---------------------------------------------------------------------------
# route: the buy pick for the BUYER's market
# ---------------------------------------------------------------------------


def _as_stored(row: Dict[str, Any], *, with_market_prices: bool) -> Dict[str, Any]:
    """What the route's SELECT hands back for an assembled row: the selected columns, jsonb as
    JSON text (asyncpg registers no codec), numerics as Decimal."""
    params = assembler.row_to_upsert_params(row)
    stored = {col: params.get(col) for col in agent_pdp_v1.AGENT_PDP_VIEW_COLUMNS}
    stored["refreshed_at"] = None
    stored["pdp_renderable"] = True
    if with_market_prices:
        stored["market_prices"] = params.get("market_prices")
    return stored


class _FakeDb:
    def __init__(self, stored: Dict[str, Any]) -> None:
        self.stored = stored
        self.queries: List[str] = []

    async def fetch_one(self, query: str, values: Optional[Dict[str, Any]] = None):
        self.queries.append(str(query))
        if "agent_pdp_view" in str(query):
            row = dict(self.stored)
            if "market_prices" not in str(query):
                row.pop("market_prices", None)
            return row
        return None

    async def fetch_all(self, query: str, values: Optional[Dict[str, Any]] = None):
        return []


def _get(monkeypatch, stored: Dict[str, Any], query: str = "") -> Dict[str, Any]:
    db = _FakeDb(stored)
    monkeypatch.setattr(agent_pdp_v1, "database", db)
    app = FastAPI()
    app.include_router(agent_pdp_v1.router)
    response = TestClient(app).get(f"/api/agent/pdp/{CK}{query}")
    assert response.status_code == 200
    body = response.json()
    body["_queries"] = db.queries
    return body


def _product(body):
    canonical = next(m for m in body["modules"] if m["type"] == "canonical")
    return canonical["data"]["pdp_payload"]["product"]


def _offers(body):
    return next(m for m in body["modules"] if m["type"] == "offers")["data"]["offers"]


def _buy_pick(body):
    return next(o for o in _offers(body) if o["is_buy_pick"])


@pytest.mark.parametrize("offers", [USD_ONLY, MIXED, SGD_ONLY], ids=["usd", "usd+sgd", "sgd"])
@pytest.mark.parametrize("query", ["", "?serving_market=US", "?serving_market=us", "?serving_market=en-US", "?serving_market=ZZ"])
def test_us_and_silent_buyers_are_byte_identical_with_every_flag_on(monkeypatch, offers, query) -> None:
    baseline = _get(monkeypatch, _as_stored(_assemble(offers), with_market_prices=False))
    monkeypatch.setenv(assembler.MARKET_PRICES_FLAG_ENV, "on")
    monkeypatch.setenv("AGENT_PDP_V1_MARKET_PRICES_READ", "on")
    flagged = _get(monkeypatch, _as_stored(_assemble(offers), with_market_prices=True), query)
    assert any("market_prices" in q for q in flagged.pop("_queries"))
    baseline.pop("_queries")
    # freshness depends on the clock only through refreshed_at, which is None in both.
    assert flagged == baseline


def test_read_flag_off_never_names_the_column(monkeypatch) -> None:
    body = _get(monkeypatch, _as_stored(_assemble(MIXED), with_market_prices=False), "?serving_market=SG")
    assert not any("market_prices" in q for q in body["_queries"])


def test_sg_buyer_without_the_summary_gets_an_sgd_buy_pick_from_stored_offers(monkeypatch) -> None:
    """Item 3 alone (no column yet): the buy pick is judged against the buyer's market. With the
    SGD siblings inside the stored top-5 (few offers), an SG buyer's pick is the SGD one; a US
    buyer's is unchanged."""
    offers = [
        _offer(1, merchant="m_store", currency="USD", market="US", price="24.00"),
        _offer(2, merchant="m_store", currency="SGD", market="SG", price="32.00"),
    ]
    stored = _as_stored(_assemble(offers), with_market_prices=False)
    us = _get(monkeypatch, stored)
    sg = _get(monkeypatch, stored, "?serving_market=SG")
    assert _buy_pick(us)["currency"] == "USD"
    assert _buy_pick(sg)["currency"] == "SGD"
    assert _buy_pick(sg)["market_availability"] == "domestic"
    # The price block is the legacy one until the summary is read.
    assert _product(sg)["price"] == _product(us)["price"]


def test_sg_buyer_with_the_summary_sees_sgd_price_and_the_cut_sgd_offers(monkeypatch) -> None:
    monkeypatch.setenv(assembler.MARKET_PRICES_FLAG_ENV, "on")
    monkeypatch.setenv("AGENT_PDP_V1_MARKET_PRICES_READ", "on")
    row = _assemble(MIXED)
    body = _get(monkeypatch, _as_stored(row, with_market_prices=True), "?serving_market=SG")

    product = _product(body)
    assert product["currency"] == "SGD"
    assert product["price"] == {"current": {"amount": 29.9, "currency": "SGD"}}
    assert (product["price_min"], product["price_max"]) == (29.9, 35.0)
    assert "market_prices" not in product
    offers = _offers(body)
    # SGD offers first (none of them survived the legacy cut), then the stored USD ones.
    assert [o["currency"] for o in offers] == ["SGD"] * 3 + ["USD"] * 5
    pick = _buy_pick(body)
    assert (pick["currency"], pick["merchant_id"], pick["market_availability"]) == ("SGD", "m_sg1", "domestic")
    assert all(o["market_availability"] == "cross_border" for o in offers if o["currency"] == "USD")


def test_sg_buyer_on_a_usd_only_row_keeps_the_legacy_view(monkeypatch) -> None:
    """No SG summary = not priced for SG: the row is served as before (the gateway's serving
    currency guard decides what an SG page shows), never relabelled."""
    monkeypatch.setenv(assembler.MARKET_PRICES_FLAG_ENV, "on")
    monkeypatch.setenv("AGENT_PDP_V1_MARKET_PRICES_READ", "on")
    body = _get(monkeypatch, _as_stored(_assemble(USD_ONLY), with_market_prices=True), "?serving_market=SG")
    assert _product(body)["currency"] == "USD"
    assert {o["currency"] for o in _offers(body)} == {"USD"}


def test_sg_buyer_on_a_not_yet_backfilled_row_keeps_the_legacy_view(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_PDP_V1_MARKET_PRICES_READ", "on")
    stored = _as_stored(_assemble(MIXED), with_market_prices=False)
    stored["market_prices"] = None  # column present, row not recomputed yet
    body = _get(monkeypatch, stored, "?serving_market=SG")
    assert _product(body)["currency"] == "USD"
    assert len(_offers(body)) == assembler.OFFER_TOP_N


def test_buyer_market_normalisation_matches_the_search_routes() -> None:
    norm = agent_pdp_v1._normalize_buyer_market
    assert norm("sg") == "SG" and norm(" US ") == "US"
    for junk in (None, "", "en-US", "US,SG", "USA", "ZZ", 7):
        assert norm(junk) is None


def test_direct_call_without_fastapi_defaults_is_silent() -> None:
    """A Query() default object (direct call) is not a market."""
    assert agent_pdp_v1._normalize_buyer_market(agent_pdp_v1.Query(default=None)) is None


def test_stored_row_round_trips_through_json(monkeypatch) -> None:
    monkeypatch.setenv(assembler.MARKET_PRICES_FLAG_ENV, "on")
    params = assembler.row_to_upsert_params(_assemble(MIXED))
    decoded = json.loads(params["market_prices"])
    assert decoded["SG"]["offers"][0]["price"] == 29.9
