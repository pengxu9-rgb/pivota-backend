"""WS-A increment 2: the merchant 'Sync products' action ingests catalog.

The visible merchant sync button used to write ONLY products_cache, never the
catalog — so a merchant's own action left catalog tables empty and the first v3
audit came back blocked. This wires it to also kick a catalog ingest, which then
enqueues the quality backfill (WS-A.1).

The ingest is ENQUEUED, not run here. It used to be handed to FastAPI's
`BackgroundTasks`, which runs it in the API process after the response is
already sent — unsupervised, unretried, and lost on a revision swap, with the
only trace of a failure being `catalog_sync_jobs.status='failed'` in the
database (2026-08-29). This test drives the real handler with mocked
dependencies and asserts the handler leaves behind a pollable job row and runs
NOTHING itself.
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


async def test_merchant_sync_enqueues_catalog_ingest_and_returns_job_id(monkeypatch) -> None:
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

    ran: List[str] = []

    async def _run_catalog_sync_job(job_id):  # must NOT be reached from the request
        ran.append(job_id)
        return {"job_id": job_id, "status": "completed"}

    monkeypatch.setattr(msc.database, "fetch_one", _fetch_one)
    monkeypatch.setattr(msc.database, "execute", _execute)
    monkeypatch.setattr(msc, "resolve_shopify_admin_access_token", _resolve_token)
    monkeypatch.setattr(adapters.ShopifyProductAdapter, "fetch_products", staticmethod(_fetch_products))
    monkeypatch.setattr(dbproducts, "upsert_product_cache", _upsert_cache)
    monkeypatch.setattr(css, "create_catalog_sync_job", _create_catalog_sync_job)
    monkeypatch.setattr(css, "run_catalog_sync_job", _run_catalog_sync_job)

    resp = await msc.merchant_sync_shopify_products(
        request=msc.ShopifySyncRequest(merchant_id="m1"),
        current_user={"role": "merchant", "merchant_id": "m1"},
    )

    # A catalog-sync job was created for the merchant...
    assert created and created[0]["merchant_id"] == "m1"
    assert created[0]["connector"] == "shopify"

    # ...and the request ran NO part of the ingest itself. This is the whole
    # point of the change: the work belongs to the out-of-band drain tick
    # (services.catalog_sync_drain), which retries and survives this process.
    assert ran == [], f"the request ran the ingest in-process: {ran}"

    # The caller gets a HANDLE, so the outcome is pollable rather than assumed
    # from a 200. Before this, `catalog_ingest_queued: true` was all they got —
    # returned identically whether the ingest later succeeded or failed.
    assert resp["data"]["catalog_ingest_queued"] is True
    assert resp["data"]["catalog_ingest_job_id"] == "cj_test"
    assert resp["data"]["catalog_ingest_status_url"] == "/v1/catalog/sync/jobs/cj_test"


async def test_merchant_sync_handler_takes_no_background_tasks(monkeypatch) -> None:
    """The handler must not be able to schedule post-response work at all.

    Pinned on the SIGNATURE rather than on behaviour: FastAPI injects
    `BackgroundTasks` purely from the annotation, so re-adding the parameter is
    the one edit that silently makes `add_task` available again here.
    """
    import inspect

    params = inspect.signature(msc.merchant_sync_shopify_products).parameters
    annotations = [str(p.annotation) for p in params.values()]
    assert not any("BackgroundTasks" in a for a in annotations), annotations
