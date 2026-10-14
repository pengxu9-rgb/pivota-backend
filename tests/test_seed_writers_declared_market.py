"""The seed writers file a seed under a DECLARED registry market, never one inferred or defaulted.

ADR-026 decision 1: never infer a market from a currency. The storefront backfill did exactly that
(EUR-priced storefront -> a market no registry lists; SGD -> SG; anything else -> US), and every seed
writer read a missing market as US. Each writer now takes the market the request declares and refuses
a missing one, one the registry does not list, and one the registry does not ingest (an `off` market).

Every test goes through the real route on a TestClient; only the database and the network are faked.
The storefront backfill runs its real candidate builder and its real upsert against fake products_cache
rows, so the market on the written row is the one the route decided, not one a test handed in.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from utils.auth import get_current_employee

MERCHANT = "merch_decl"
DOMAIN = "store.example"


@pytest.fixture
def client():
    import routes.employee_products as ep

    async def override_employee():
        return {"employee_id": "emp_test", "email": "test@example.com"}

    app = FastAPI()
    app.include_router(ep.router)
    app.dependency_overrides[get_current_employee] = override_employee
    return TestClient(app)


# ----------------------------------------------------------------- registry facts the tests lean on


def test_the_markets_these_tests_use_have_the_registry_states_they_assume():
    from services import market_registry

    assert market_registry.market_or_none("EU-DE") is None, "the market the inference invented is unlisted"
    assert market_registry.market("FR")["ingest"] is False, "FR is an off market"
    assert {"US", "SG", "AU"} <= set(market_registry.ingest_markets())


# ----------------------------------------------------------------- storefront backfill


def _cache_row(pid: str, currency: str) -> Dict[str, Any]:
    return {
        "merchant_id": MERCHANT,
        "platform": "shopify",
        "platform_product_id": pid,
        "product_data": {
            "id": pid,
            "title": f"Serum {pid}",
            "handle": f"serum-{pid}",
            "vendor": "Brand",
            "currency": currency,
            "price": 30.0,
            "variants": [{"id": f"{pid}01", "variant_id": f"{pid}01", "price": 30.0, "inventory_quantity": 5}],
        },
        "cached_at": None,
        "expires_at": None,
    }


class _StorefrontDb:
    """products_cache rows for the candidate builder; seed lookups, writes and supersedes recorded."""

    def __init__(self, cache_rows: List[Dict[str, Any]], existing: Optional[Dict[str, Any]] = None):
        self.cache_rows = cache_rows
        self.existing = existing
        self.cache_reads = 0
        self.writes: List[Dict[str, Any]] = []

    async def fetch_all(self, query, values=None):
        assert "products_cache" in query
        self.cache_reads += 1
        return list(self.cache_rows)

    async def fetch_one(self, query, values=None):
        assert "external_product_seeds" in query
        return self.existing

    async def execute_seed(self, query, values):
        self.writes.append({"sql": query, **values})

    async def execute(self, query, values=None):
        return None


def _install_storefront(monkeypatch, db: _StorefrontDb) -> None:
    import routes.employee_products as ep

    monkeypatch.setattr(ep, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(ep, "get_merchant_referral_domains", AsyncMock(return_value=[DOMAIN]))
    monkeypatch.setattr(ep.database, "fetch_all", db.fetch_all)
    monkeypatch.setattr(ep.database, "fetch_one", db.fetch_one)
    monkeypatch.setattr(ep.database, "execute", db.execute)
    monkeypatch.setattr(ep, "_execute_seed_data_stmt", db.execute_seed)
    monkeypatch.setattr(ep, "_derive_seed_seller_columns", AsyncMock(return_value=(None, None)))


def _backfill(client: TestClient, **body):
    return client.post(
        "/employee/products/external-seeds/backfill-storefront",
        json={"merchant_id": MERCHANT, "dry_run": False, **body},
    )


@pytest.mark.parametrize(
    "body,detail",
    [
        ({}, "MARKET_REQUIRED"),
        ({"market": "  "}, "MARKET_REQUIRED"),
        ({"market": "EU-DE"}, "UNKNOWN_MARKET:EU-DE"),
        ({"market": "fr"}, "MARKET_NOT_INGESTED:FR"),
    ],
)
@pytest.mark.parametrize("dry_run", [True, False])
def test_a_storefront_backfill_without_an_ingestible_declared_market_is_refused_before_any_read(
    monkeypatch, client, body, detail, dry_run
):
    # EUR prices: what the old inference turned into the unlisted 'EU-DE' market.
    db = _StorefrontDb([_cache_row("1", "EUR"), _cache_row("2", "EUR")])
    _install_storefront(monkeypatch, db)

    res = _backfill(client, dry_run=dry_run, **body)

    assert res.status_code == 400, res.text
    assert res.json()["detail"] == detail
    assert db.cache_reads == 0 and db.writes == []


@pytest.mark.parametrize("currency", ["EUR", "SGD", "USD"])
def test_a_storefront_backfill_files_new_seeds_under_the_declared_market_whatever_they_are_priced_in(
    monkeypatch, client, currency
):
    db = _StorefrontDb([_cache_row("1", currency), _cache_row("2", currency)])
    _install_storefront(monkeypatch, db)

    res = _backfill(client, market="au")

    assert res.status_code == 200, res.text
    payload = res.json()
    assert payload["market"] == "AU" and payload["created"] == 2
    inserts = [w for w in db.writes if w["sql"].strip().upper().startswith("INSERT")]
    assert [w["market"] for w in inserts] == ["AU", "AU"]
    assert [w["price_currency"] for w in inserts] == [currency, currency], "the currency is stored as read"
    assert {item["market"] for item in payload["items"]} == {"AU"}


def test_a_storefront_backfill_dry_run_reports_the_declared_market(monkeypatch, client):
    db = _StorefrontDb([_cache_row("1", "SGD")])
    _install_storefront(monkeypatch, db)

    res = _backfill(client, market="US", dry_run=True)

    assert res.status_code == 200, res.text
    assert res.json()["market"] == "US", "not SG off the SGD price"
    assert db.writes == []


def test_a_storefront_backfill_refreshes_an_existing_seed_without_re_filing_it(monkeypatch, client):
    existing = {
        "id": "eps_old",
        "market": "JP",
        "attached_product_key": f"{MERCHANT}|shopify|1",
        "canonical_url": f"https://{DOMAIN}/products/serum-1",
        "destination_url": f"https://{DOMAIN}/products/serum-1",
        "seed_data": {},
    }
    db = _StorefrontDb([_cache_row("1", "USD")], existing=existing)
    _install_storefront(monkeypatch, db)

    res = _backfill(client, market="US")

    assert res.status_code == 200, res.text
    (update,) = db.writes
    assert update["sql"].strip().upper().startswith("UPDATE")
    assert "market" not in update, "the update binds no market"
    set_clause = update["sql"].split("WHERE")[0]
    assert "market" not in set_clause, "the update never moves the seed to another market partition"
    (item,) = res.json()["items"]
    assert (item["action"], item["market"]) == ("updated", "JP")


# ----------------------------------------------------------------- create (and the attached-link door)


def _install_create(monkeypatch) -> List[Dict[str, Any]]:
    import routes.employee_products as ep

    writes: List[Dict[str, Any]] = []

    async def execute_seed(query, values):
        writes.append({"sql": query, **values})

    snapshot = SimpleNamespace(
        canonical_url=None, domain=DOMAIN, title="Serum", image_url=None,
        price_amount=30.0, price_currency="EUR", availability="in_stock", evidence={},
    )
    monkeypatch.setattr(ep, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(ep, "resolve_external_offer", AsyncMock(return_value=snapshot))
    monkeypatch.setattr(ep.database, "fetch_all", AsyncMock(return_value=[]))
    monkeypatch.setattr(ep.database, "execute", AsyncMock(return_value=None))
    monkeypatch.setattr(ep, "_execute_seed_data_stmt", execute_seed)
    monkeypatch.setattr(ep, "_derive_seed_seller_columns", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(ep, "_make_redirect_url", AsyncMock(return_value="https://r.example/r?token=t"))
    return writes


@pytest.mark.parametrize(
    "path,extra,detail",
    [
        ("/employee/products/external-seeds", {}, "MARKET_REQUIRED"),
        ("/employee/products/external-seeds", {"market": "EU-DE"}, "UNKNOWN_MARKET:EU-DE"),
        ("/employee/products/external-seeds", {"market": "FR"}, "MARKET_NOT_INGESTED:FR"),
        ("/employee/products/m|shopify|1/external-links", {}, "MARKET_REQUIRED"),
        ("/employee/products/m|shopify|1/external-links", {"market": "EU-DE"}, "UNKNOWN_MARKET:EU-DE"),
    ],
)
def test_creating_a_seed_without_an_ingestible_declared_market_is_refused(monkeypatch, client, path, extra, detail):
    writes = _install_create(monkeypatch)

    res = client.post(path, json={"destination_url": f"https://{DOMAIN}/products/serum-1", **extra})

    assert res.status_code == 400, res.text
    assert res.json()["detail"] == detail
    assert writes == []


def test_creating_a_seed_files_it_under_the_declared_market(monkeypatch, client):
    writes = _install_create(monkeypatch)

    res = client.post(
        "/employee/products/external-seeds",
        json={"destination_url": f"https://{DOMAIN}/products/serum-1", "market": " sg "},
    )

    assert res.status_code == 200, res.text
    (insert,) = writes
    assert insert["market"] == "SG"


# ----------------------------------------------------------------- edit


@pytest.mark.parametrize(
    "market,detail",
    [("", "MARKET_REQUIRED"), ("EU-DE", "UNKNOWN_MARKET:EU-DE"), ("FR", "MARKET_NOT_INGESTED:FR")],
)
def test_editing_a_seed_into_a_missing_or_unknown_market_is_refused(monkeypatch, client, market, detail):
    import routes.employee_products as ep

    writes: List[Dict[str, Any]] = []

    async def execute_seed(query, values):
        writes.append({"sql": query, **values})

    row = {"id": "eps_1", "market": "US", "tool": "*", "canonical_url": f"https://{DOMAIN}/p", "seed_data": {}}
    monkeypatch.setattr(ep, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(ep.database, "fetch_one", AsyncMock(return_value=row))
    monkeypatch.setattr(ep, "_execute_seed_data_stmt", execute_seed)

    res = client.patch("/employee/products/external-seeds/eps_1", json={"market": market})

    assert res.status_code == 400, res.text
    assert res.json()["detail"] == detail
    assert writes == []


# ----------------------------------------------------------------- CSV import (both sheet shapes)


def _install_csv(monkeypatch) -> List[Dict[str, Any]]:
    import routes.employee_products as ep

    writes: List[Dict[str, Any]] = []

    async def execute_seed(query, values):
        writes.append({"sql": query, **values})

    monkeypatch.setattr(ep, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(ep.database, "fetch_one", AsyncMock(return_value=None))
    monkeypatch.setattr(ep.database, "fetch_all", AsyncMock(return_value=[]))
    monkeypatch.setattr(ep.database, "execute", AsyncMock(return_value=None))
    monkeypatch.setattr(ep, "_execute_seed_data_stmt", execute_seed)
    monkeypatch.setattr(ep, "_derive_seed_seller_columns", AsyncMock(return_value=(None, None)))
    return writes


def _import(client: TestClient, csv_text: str, query: str = ""):
    return client.post(
        f"/employee/products/external-seeds/import-csv?mode=upsert{query}",
        content=csv_text,
        headers={"Content-Type": "text/csv"},
    )


_PLAIN = "destination_url,market,price_currency\n"
_CATALOG = "product_url,market,sku,price,currency,product_title\n"


@pytest.mark.parametrize(
    "sheet,row_market,detail",
    [
        (_PLAIN, "", "MARKET_REQUIRED"),
        (_PLAIN, "EU-DE", "UNKNOWN_MARKET:EU-DE"),
        (_PLAIN, "FR", "MARKET_NOT_INGESTED:FR"),
        (_CATALOG, "", "MARKET_REQUIRED"),
        (_CATALOG, "EU-DE", "UNKNOWN_MARKET:EU-DE"),
    ],
)
def test_a_csv_row_with_no_ingestible_market_is_refused_and_never_defaults_to_us(
    monkeypatch, client, sheet, row_market, detail
):
    writes = _install_csv(monkeypatch)
    row = (
        f"https://{DOMAIN}/p/1,{row_market},EUR\n"
        if sheet == _PLAIN
        else f"https://{DOMAIN}/p/1,{row_market},SKU1,30,EUR,Serum\n"
    )

    res = _import(client, sheet + row)  # no sheet-wide market either

    assert res.status_code == 200, res.text
    payload = res.json()
    assert payload["created"] == 0
    assert len(payload["errors"]) == 1 and payload["errors"][0].endswith(detail), payload["errors"]
    assert writes == []


@pytest.mark.parametrize("market,detail", [("EU-DE", "UNKNOWN_MARKET:EU-DE"), ("FR", "MARKET_NOT_INGESTED:FR")])
def test_a_csv_import_declaring_an_unknown_sheet_market_is_refused_whole(monkeypatch, client, market, detail):
    writes = _install_csv(monkeypatch)

    res = _import(client, _PLAIN + f"https://{DOMAIN}/p/1,,\n", f"&market={market}")

    assert res.status_code == 400, res.text
    assert res.json()["detail"] == detail
    assert writes == []


def test_a_csv_row_files_under_its_own_market_or_the_declared_sheet_market(monkeypatch, client):
    writes = _install_csv(monkeypatch)
    sheet = _PLAIN + f"https://{DOMAIN}/p/1,SG,SGD\n" + f"https://{DOMAIN}/p/2,,\n"

    res = _import(client, sheet, "&market=us")

    assert res.status_code == 200, res.text
    assert res.json()["errors"] == []
    assert sorted((w["destination_url"], w["market"]) for w in writes) == [
        (f"https://{DOMAIN}/p/1", "SG"),
        (f"https://{DOMAIN}/p/2", "US"),
    ]


def test_a_csv_catalog_sheet_files_its_rows_under_their_declared_market(monkeypatch, client):
    writes = _install_csv(monkeypatch)
    sheet = _CATALOG + f"https://{DOMAIN}/p/1,AU,SKU1,30,AUD,Serum\n"

    res = _import(client, sheet)

    assert res.status_code == 200, res.text
    assert res.json()["errors"] == []
    (insert,) = writes
    assert insert["market"] == "AU"
