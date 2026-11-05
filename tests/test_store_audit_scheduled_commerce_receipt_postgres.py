"""A scheduled Store Audit commerce re-probe can record a completed journey.

    createdb pivota_dialect_check
    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        pytest tests/test_store_audit_scheduled_commerce_receipt_postgres.py

2026-11-04, the selectors' first real night: every journey the commerce worker completes carries a
cart section (gateway src/services/commerceStorefrontAudit.js; only a timeout or network error omits
it), and the receipt route refuses cart evidence on a run with no product_key (409
PRODUCT_NOT_RESOLVED, inside the receipt transaction, so nothing is kept). The scheduled selector
enqueued every run with product_key=None, so a scheduled probe could record only its timeouts: one
completed journey was answered 409, discarded, and its retry timed out.

This file runs the real selector, Store Readiness route, claim and receipt against Postgres (the
receipt is one transaction; a refused write must leave nothing), with catalog_products built from the
model so the catalog branch of the probe identity runs for real: a databases Postgres Record has no
dict .get, which the catalog branch used to call. The receipt is shaped like the worker's
completed-journey output: platform, checkout, cart verified, six passed steps.

Never point this at prod: it writes.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs the Postgres DATABASE_URL supplied by postgres-dialect-gate",
)

RUN = uuid.uuid4().hex[:8]
MERCHANT = f"sacr_merchant_{RUN}"
# Store Readiness refuses reserved TLDs (.example/.test); a random-hex .com name, never fetched.
DOMAIN = f"sacr-{RUN}-store.com"
ENDPOINT = f"https://{DOMAIN}/"
PRODUCT_URL = f"https://{DOMAIN}/products/sacr-hero"
CATALOG_KEY = f"sacr_product_{RUN}"
KEY = "sacr-test-key"
WORKER = f"sacr-worker-{RUN}"


@pytest.fixture
async def store_audit_db(monkeypatch):
    from db.audit_evidence import ensure_audit_evidence_tables
    from db.catalog import catalog_products
    from db.database import database
    from db.merchant_audit_runs import ensure_merchant_audit_runs_table
    from tests.model_schema import ensure_model_tables

    for name, value in {
        "STORE_AUDIT_COMMERCE_REPROBE_SCHEDULER_ENABLED": "true",
        "STORE_AUDIT_COMMERCE_REPROBE_ARMED": "true",
        "STORE_AUDIT_COMMERCE_PROBE_RECEIPT_ENABLED": "true",
        "STORE_AUDIT_COMMERCE_PROBE_INTERNAL_KEY": KEY,
    }.items():
        monkeypatch.setenv(name, value)
    await database.connect()
    # The modules' own DDL, so the tables cannot drift from what the routes write. Both are
    # created here, before the receipt transaction calls them (DDL inside it is a no-op then).
    # catalog_products from the MODEL: a narrow hand-written one would poison later gate files.
    await ensure_audit_evidence_tables()
    await ensure_merchant_audit_runs_table()
    await ensure_model_tables([catalog_products])
    try:
        yield database
    finally:
        for table in ("evidence_items", "verification_runs", "execution_routes", "merchant_audit_runs", "catalog_products"):
            await database.execute(f"DELETE FROM {table} WHERE merchant_id = :m", {"m": MERCHANT})
        await database.disconnect()


async def _catalog_product(db) -> None:
    # Synced without a trailing slash; Store Readiness is submitted with one, below.
    await db.execute(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, canonical_url) "
        "VALUES (:k, :m, 'shopify', :k, 'sacr hero', :u)",
        {"k": CATALOG_KEY, "m": MERCHANT, "u": PRODUCT_URL + "?variant=1"},
    )


async def _storefront_route(*, endpoint: str = ENDPOINT, subject_type: str = "merchant") -> str:
    from db.audit_evidence import upsert_execution_route
    from db.merchant_audit_runs import record_audit_run_started

    run_id = await record_audit_run_started(merchant_id=MERCHANT, product_keys=[], subject_type=subject_type)
    assert run_id
    route = await upsert_execution_route(
        normalized_domain=DOMAIN, route_kind="storefront", endpoint=endpoint,
        merchant_id=MERCHANT, audit_run_id=run_id,
    )
    assert route and route.get("merchant_id") == MERCHANT
    return run_id


def _only_this_merchant(monkeypatch):
    # The real candidate query, narrowed to this file's merchant: the gate's database is shared,
    # and another file's storefront routes must not be enqueued from here.
    from jobs import scheduled_commerce_checkout_reprobe_job as reprobe_job

    real = reprobe_job.list_merchant_storefront_candidates

    async def candidates(**_kwargs):
        return [row for row in await real(limit=100) if row.get("merchant_id") == MERCHANT]

    monkeypatch.setattr(reprobe_job, "list_merchant_storefront_candidates", candidates)
    return reprobe_job


async def _claim(db):
    from fastapi import Response

    from routes.store_audit_commerce_probe_internal import CommerceProbeClaimRequest, claim_commerce_probe

    # The claim takes the oldest pending commerce run in the whole (shared) database. Refuse rather
    # than claim another file's row.
    others = await db.fetch_val(
        "SELECT count(*) FROM verification_runs WHERE verifier_id = 'commerce_checkout_probe' "
        "AND status IN ('pending', 'claimed') AND merchant_id IS DISTINCT FROM :m",
        {"m": MERCHANT},
    )
    if others:
        pytest.fail(f"{others} other commerce run(s) pending in the shared database; not claiming theirs")
    claim = await claim_commerce_probe(CommerceProbeClaimRequest(worker_id=WORKER), Response(), x_internal_key=KEY)
    assert claim is not None and getattr(claim, "merchant_id", None) == MERCHANT, claim
    return claim


def _completed_journey(claim):
    from routes.store_audit_commerce_probe_internal import CommerceProbeReceipt

    return CommerceProbeReceipt.model_validate({
        "audit_run_id": claim.audit_run_id,
        "verification_run_id": claim.verification_run_id,
        "worker_id": WORKER,
        "probe_id": claim.probe_id,
        "verifier_id": "commerce_checkout_probe",
        "verification_status": "succeeded",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "platform": {"platform": "shopify", "checkout_provider": "shopify"},
        "checkout": {"status": "guest_route_detected"},
        "cart": {"status": "verified", "quantity": 1},
        "steps": [
            {"step": "storefront_access", "status": "passed", "reason": "storefront_loaded"},
            {"step": "product_search", "status": "passed", "reason": "search_result_found"},
            {"step": "product_detail", "status": "passed", "reason": "pdp_confirmed"},
            {"step": "add_to_cart", "status": "passed", "reason": "cart_item_added"},
            {"step": "shipping_address", "status": "passed", "reason": "address_fields_filled"},
            {"step": "checkout", "status": "passed", "reason": "checkout_reached"},
        ],
    })


async def _run(db):
    return await db.fetch_one(
        "SELECT verify_id, product_key, status FROM verification_runs WHERE merchant_id = :m", {"m": MERCHANT},
    )


async def test_a_scheduled_probe_records_its_completed_journey(store_audit_db, monkeypatch):
    from routes.store_audit_commerce_probe_internal import receive_commerce_probe_receipt
    from services.store_audit_probe_identity import probe_url

    # The route's audit run is a Store Readiness run, as in prod (Store Readiness writes
    # last_audit_run_id): a scheduled receipt must not rewrite it.
    parent_run = await _storefront_route(subject_type="store_readiness")
    reprobe_job = _only_this_merchant(monkeypatch)

    summary = await reprobe_job.run_scheduled_commerce_checkout_reprobes()
    assert summary["enqueued"] == 1, summary

    run = await _run(store_audit_db)
    # No catalog product sits at the storefront root, so the key is the URL-probe identity.
    import hashlib
    expected = "url_probe:" + hashlib.sha256(f"{MERCHANT}|{probe_url(ENDPOINT)}".encode()).hexdigest()[:24]
    assert run["product_key"] == expected

    claim = await _claim(store_audit_db)
    assert claim.product_key == run["product_key"]
    response = await receive_commerce_probe_receipt(_completed_journey(claim), x_internal_key=KEY)

    assert response.verification_status == "succeeded"
    assert (await _run(store_audit_db))["status"] == "succeeded"
    rows = await store_audit_db.fetch_all(
        "SELECT evidence_type, product_key FROM evidence_items WHERE merchant_id = :m ORDER BY evidence_type",
        {"m": MERCHANT},
    )
    assert [(r["evidence_type"], r["product_key"]) for r in rows] == [
        ("commerce_cartability", run["product_key"]),
        ("commerce_checkout_route", None),
        ("commerce_platform", None),
    ]
    parent = await store_audit_db.fetch_one(
        "SELECT status, report_jsonb FROM merchant_audit_runs WHERE run_id = :r", {"r": parent_run},
    )
    assert parent["status"] == "running" and parent["report_jsonb"] is None


async def test_a_scheduled_route_on_a_catalog_product_is_keyed_to_that_product(store_audit_db, monkeypatch):
    # The catalog branch: before the fix, a matching row made the identity call raise (Record has no
    # .get), so this merchant was counted failed and never enqueued.
    await _catalog_product(store_audit_db)
    await _storefront_route(endpoint=PRODUCT_URL + "/")
    reprobe_job = _only_this_merchant(monkeypatch)

    summary = await reprobe_job.run_scheduled_commerce_checkout_reprobes()

    assert summary["enqueued"] == 1 and summary["failed"] == 0, summary
    assert (await _run(store_audit_db))["product_key"] == CATALOG_KEY


async def test_store_readiness_keys_a_catalog_product_url_the_same_way(store_audit_db, monkeypatch):
    # Store Readiness POST used to 500 here, after upserting the route, for any URL in the catalog.
    # Submitted with a trailing slash and a query: the same product the selector keys on.
    from routes import store_readiness as sr

    async def owns(_merchant_id, _domain):
        return True

    monkeypatch.setattr(sr, "merchant_owns_domain", owns)
    await _catalog_product(store_audit_db)

    response = await sr.run_store_readiness(
        sr.StoreReadinessRunRequest(product_url=PRODUCT_URL + "/?utm_source=sacr"), merchant_id=MERCHANT,
    )

    assert response.state == "pending"
    assert (await _run(store_audit_db))["product_key"] == CATALOG_KEY


async def test_a_run_without_a_product_key_still_refuses_cart_evidence_and_keeps_nothing(store_audit_db, caplog):
    # The failure mode the selector no longer produces, kept as the control: a refused receipt
    # rolls back the platform/checkout rows it wrote first, and its code is now in the log.
    from fastapi import HTTPException

    from db.audit_evidence import VERIFIER_COMMERCE_CHECKOUT_PROBE, enqueue_verification_run
    from routes.store_audit_commerce_probe_internal import receive_commerce_probe_receipt

    await _storefront_route()
    route = await store_audit_db.fetch_one(
        "SELECT execution_route_id, last_audit_run_id FROM execution_routes WHERE merchant_id = :m", {"m": MERCHANT},
    )
    verify_id = await enqueue_verification_run(
        audit_run_id=str(route["last_audit_run_id"]), merchant_id=MERCHANT,
        execution_route_id=str(route["execution_route_id"]),
        verifier_id=VERIFIER_COMMERCE_CHECKOUT_PROBE, max_retries=1,
    )
    assert verify_id
    claim = await _claim(store_audit_db)
    assert claim.product_key is None
    caplog.set_level(logging.WARNING, logger="routes.store_audit_commerce_probe_internal")

    with pytest.raises(HTTPException) as refused:
        await receive_commerce_probe_receipt(_completed_journey(claim), x_internal_key=KEY)

    assert refused.value.status_code == 409
    assert refused.value.detail == {"error": "PRODUCT_NOT_RESOLVED"}
    assert f"code=PRODUCT_NOT_RESOLVED verify={verify_id[:8]}" in caplog.text
    kept = await store_audit_db.fetch_val(
        "SELECT count(*) FROM evidence_items WHERE merchant_id = :m", {"m": MERCHANT},
    )
    assert kept == 0


async def test_a_store_readiness_probe_still_completes_its_own_run(store_audit_db, monkeypatch):
    # The other side of the parent-run rule: Store Readiness enqueues with its own idempotency key,
    # and its receipt must complete its audit run. Ties the enqueue to the receipt end to end.
    from routes import store_readiness as sr
    from routes.store_audit_commerce_probe_internal import receive_commerce_probe_receipt

    async def owns(_merchant_id, _domain):
        return True

    monkeypatch.setattr(sr, "merchant_owns_domain", owns)
    started = await sr.run_store_readiness(sr.StoreReadinessRunRequest(product_url=PRODUCT_URL), merchant_id=MERCHANT)
    claim = await _claim(store_audit_db)
    assert claim.audit_run_id == started.audit_run_id

    response = await receive_commerce_probe_receipt(_completed_journey(claim), x_internal_key=KEY)

    assert response.verification_status == "succeeded"
    parent = await store_audit_db.fetch_one(
        "SELECT status, subject_type, report_jsonb FROM merchant_audit_runs WHERE run_id = :r", {"r": started.audit_run_id},
    )
    assert parent["subject_type"] == "store_readiness"
    assert parent["status"] == "succeeded"
    assert parent["report_jsonb"] is not None and "store_readiness" in str(parent["report_jsonb"])


async def test_a_suppressed_catalog_row_is_never_the_key(store_audit_db):
    from services.store_audit_probe_identity import probe_product_key, probe_url

    import hashlib

    # Only a suppressed row at the URL: no catalog key.
    await store_audit_db.execute(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, canonical_url, "
        "suppression_reason, updated_at) VALUES (:k, :m, 'shopify', :k, 'sacr suppressed', :u, 'non_product_listing', now())",
        {"k": f"{CATALOG_KEY}_suppressed", "m": MERCHANT, "u": PRODUCT_URL},
    )
    expected = "url_probe:" + hashlib.sha256(f"{MERCHANT}|{probe_url(PRODUCT_URL)}".encode()).hexdigest()[:24]
    assert await probe_product_key(merchant_id=MERCHANT, url=PRODUCT_URL) == expected

    # An older live row at the same URL wins over the newer suppressed one.
    await store_audit_db.execute(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, canonical_url, "
        "updated_at) VALUES (:k, :m, 'shopify', :k, 'sacr live', :u, now() - interval '2 days')",
        {"k": CATALOG_KEY, "m": MERCHANT, "u": PRODUCT_URL},
    )
    assert await probe_product_key(merchant_id=MERCHANT, url=PRODUCT_URL + "/") == CATALOG_KEY

    # Two live rows at one URL: the most recently updated one.
    await store_audit_db.execute(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, canonical_url, "
        "updated_at) VALUES (:k, :m, 'shopify', :k, 'sacr live newer', :u, now() - interval '1 day')",
        {"k": f"{CATALOG_KEY}_newer", "m": MERCHANT, "u": PRODUCT_URL + "?variant=2"},
    )
    assert await probe_product_key(merchant_id=MERCHANT, url=PRODUCT_URL) == f"{CATALOG_KEY}_newer"
