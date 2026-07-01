"""onboard_external_brand_from_crawl threads the offer's real market/currency
(e.g. a Korean D2C brand sells in KRW) instead of hardcoding US/USD. Verifies the
SQL bind params _upsert_seed + _set_category_and_offer hand to the DB.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.onboard_external_brand_from_crawl as onboard  # noqa: E402


class _FakeDB:
    def __init__(self, fetch_one_result=None):
        self.calls = []
        self._fetch_one_result = fetch_one_result

    async def execute(self, sql, params=None):
        self.calls.append((sql, params or {}))

    async def fetch_one(self, sql, params=None):
        return self._fetch_one_result


@pytest.fixture
def fake_db(monkeypatch):
    db = _FakeDB()
    monkeypatch.setattr(onboard, "database", db)
    return db


async def test_upsert_seed_carries_krw_market(fake_db):
    await onboard._upsert_seed({
        "external_product_id": "anuko_32",
        "title": "Bond & Repair Hair Oil 75ml",
        "price_amount": 26900,
        "market": "KR",
        "price_currency": "KRW",
    })
    _, params = fake_db.calls[0]
    assert params["market"] == "KR"
    assert params["currency"] == "KRW"
    assert params["price"] == 26900


async def test_upsert_seed_defaults_to_us_usd(fake_db):
    await onboard._upsert_seed({
        "external_product_id": "x1",
        "title": "Some US Product",
        "price_amount": 24.99,
    })
    _, params = fake_db.calls[0]
    assert params["market"] == "US"
    assert params["currency"] == "USD"


async def test_set_category_and_offer_does_not_force_us(fake_db):
    await onboard._set_category_and_offer({
        "external_product_id": "anuko_32",
        "category_kind": "haircare",
        "market": "KR",
    })
    # last execute = the catalog_offers UPDATE; market must be the product's, not 'US'
    offer_sql, offer_params = fake_db.calls[-1]
    assert "catalog_offers" in offer_sql
    assert offer_params["market"] == "KR"
    assert "market='US'" not in offer_sql  # the old hardcode is gone


async def test_set_category_and_offer_defaults_market_us(fake_db):
    await onboard._set_category_and_offer({
        "external_product_id": "x1",
        "category_kind": "skincare",
    })
    offer_params = fake_db.calls[-1][1]
    assert offer_params["market"] == "US"


async def test_resolve_pdp_scope_single_seller_is_merchant_owned(monkeypatch):
    # seller_count=1 -> classify() returns 'merchant_owned' (a resolved scope),
    # so the mirrored row can clear the entity_unresolved serving gate.
    db = _FakeDB(fetch_one_result={"seller_count": 1})
    monkeypatch.setattr(onboard, "database", db)
    await onboard._resolve_pdp_scope({"external_product_id": "anuko_32"})
    update_sql, params = db.calls[-1]
    assert "catalog_products" in update_sql and "pdp_scope" in update_sql
    assert params["s"] == "merchant_owned"
    assert "pdp_scope='unverified'" in update_sql  # only promotes unresolved rows


async def test_resolve_pdp_scope_multi_seller_is_canonical(monkeypatch):
    db = _FakeDB(fetch_one_result={"seller_count": 3})
    monkeypatch.setattr(onboard, "database", db)
    await onboard._resolve_pdp_scope({"external_product_id": "anuko_32"})
    assert db.calls[-1][1]["s"] == "multi_merchant_canonical"
