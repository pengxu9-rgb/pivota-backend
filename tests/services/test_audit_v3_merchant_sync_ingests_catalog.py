"""WS-A increment 2: the merchant 'Sync products' action ingests catalog.

The visible merchant sync button used to write ONLY products_cache, never the
catalog — so a merchant's own action left catalog tables empty and the first v3
audit came back blocked. This wires it to also kick a catalog ingest (in the
background, to keep the response fast); run_catalog_sync_job then enqueues the
quality backfill (WS-A.1). This test drives the real handler with mocked
dependencies and asserts a catalog-sync background task is scheduled.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import routes.merchant_store_connections as msc
import db.products as dbproducts
import services.catalog_sync_service as css
import adapters.product_adapters as adapters


class _FakeBG:
    def __init__(self) -> None:
        self.tasks: List[Dict[str, Any]] = []

    def add_task(self, func, *args, **kwargs) -> None:
        self.tasks.append({"func": func, "args": args, "kwargs": kwargs})


async def test_merchant_sync_backgrounds_catalog_ingest(monkeypatch) -> None:
    # One row satisfies both the store query and the credentials query (the
    # handler subscripts cred_row["api_key"] etc.; resolve_token is mocked).
    store = {"store_id": "st1", "platform": "shopify", "domain": "ownist.myshopify.com",
             "status": "active", "access_token": "tok",
             "api_key": "k", "api_secret": "s", "shop_domain": "ownist.myshopify.com"}

    async def _fetch_one(*_a, **_k):
        return dict(store)

    async def _execute(*_a, **_k):
        return None

    async def _resolve_token(*_a, **_k):
        return ("tok", {})

    async def _fetch_products(*_a, **_k):
        return ([], None, None)  # empty page -> loop ends immediately

    async def _upsert_cache(*_a, **_k):
        return None

    created: List[Dict[str, Any]] = []

    async def _create_catalog_sync_job(*, merchant_id, connector, mode, scope=None, requested_by=None):
        created.append({"merchant_id": merchant_id, "connector": connector, "requested_by": requested_by})
        return {"job_id": "cj_test"}

    async def _run_catalog_sync_job(job_id):  # sentinel target for the bg task
        return {"job_id": job_id, "status": "completed"}

    monkeypatch.setattr(msc.database, "fetch_one", _fetch_one)
    monkeypatch.setattr(msc.database, "execute", _execute)
    monkeypatch.setattr(msc, "resolve_shopify_admin_access_token", _resolve_token)
    monkeypatch.setattr(adapters.ShopifyProductAdapter, "fetch_products", staticmethod(_fetch_products))
    monkeypatch.setattr(dbproducts, "upsert_product_cache", _upsert_cache)
    monkeypatch.setattr(css, "create_catalog_sync_job", _create_catalog_sync_job)
    monkeypatch.setattr(css, "run_catalog_sync_job", _run_catalog_sync_job)

    bg = _FakeBG()
    resp = await msc.merchant_sync_shopify_products(
        request=msc.ShopifySyncRequest(merchant_id="m1"),
        background_tasks=bg,
        current_user={"role": "merchant", "merchant_id": "m1"},
    )

    # A catalog-sync job was created for the merchant...
    assert created and created[0]["merchant_id"] == "m1"
    assert created[0]["connector"] == "shopify"
    # ...and scheduled as a BACKGROUND task (fast response), targeting run_catalog_sync_job.
    assert any(t["func"] is _run_catalog_sync_job and t["args"] == ("cj_test",) for t in bg.tasks)
    assert resp["data"]["catalog_ingest_queued"] is True
