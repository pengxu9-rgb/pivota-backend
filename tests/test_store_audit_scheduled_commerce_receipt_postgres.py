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

This file runs the real selector, claim and receipt against Postgres (the receipt is one
transaction; a refused write must leave nothing). The receipt is shaped like the worker's
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
DOMAIN = f"sacr-{RUN}.example"
ENDPOINT = f"https://{DOMAIN}/"
KEY = "sacr-test-key"
WORKER = f"sacr-worker-{RUN}"


@pytest.fixture
async def store_audit_db(monkeypatch):
    from db.audit_evidence import ensure_audit_evidence_tables
    from db.database import database
    from db.merchant_audit_runs import ensure_merchant_audit_runs_table

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
    await ensure_audit_evidence_tables()
    await ensure_merchant_audit_runs_table()
    try:
        yield database
    finally:
        for table in ("evidence_items", "verification_runs", "execution_routes", "merchant_audit_runs"):
            await database.execute(f"DELETE FROM {table} WHERE merchant_id = :m", {"m": MERCHANT})
        await database.disconnect()


async def _storefront_route() -> None:
    from db.audit_evidence import upsert_execution_route
    from db.merchant_audit_runs import record_audit_run_started

    run_id = await record_audit_run_started(merchant_id=MERCHANT, product_keys=[])
    assert run_id
    route = await upsert_execution_route(
        normalized_domain=DOMAIN, route_kind="storefront", endpoint=ENDPOINT,
        merchant_id=MERCHANT, audit_run_id=run_id,
    )
    assert route and route.get("merchant_id") == MERCHANT


def _only_this_merchant(monkeypatch):
    # The real candidate query, narrowed to this file's merchant: the gate's database is shared,
    # and another file's storefront routes must not be enqueued from here.
    from jobs import scheduled_commerce_checkout_reprobe_job as reprobe_job

    real = reprobe_job.list_merchant_storefront_candidates

    async def candidates(**_kwargs):
        return [row for row in await real(limit=100) if row.get("merchant_id") == MERCHANT]

    monkeypatch.setattr(reprobe_job, "list_merchant_storefront_candidates", candidates)
    return reprobe_job


async def _claim():
    from fastapi import Response

    from routes.store_audit_commerce_probe_internal import CommerceProbeClaimRequest, claim_commerce_probe

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


async def test_a_scheduled_probe_records_its_completed_journey(store_audit_db, monkeypatch):
    from routes.store_audit_commerce_probe_internal import receive_commerce_probe_receipt

    await _storefront_route()
    reprobe_job = _only_this_merchant(monkeypatch)

    summary = await reprobe_job.run_scheduled_commerce_checkout_reprobes()
    assert summary["enqueued"] == 1, summary

    run = await store_audit_db.fetch_one(
        "SELECT verify_id, product_key FROM verification_runs WHERE merchant_id = :m", {"m": MERCHANT},
    )
    # No catalog product sits at the storefront root, so the key is the URL-probe identity.
    assert str(run["product_key"]).startswith("url_probe:")

    claim = await _claim()
    assert claim.product_key == run["product_key"]
    response = await receive_commerce_probe_receipt(_completed_journey(claim), x_internal_key=KEY)

    assert response.verification_status == "succeeded"
    status = await store_audit_db.fetch_val(
        "SELECT status FROM verification_runs WHERE verify_id = :v", {"v": str(run["verify_id"])},
    )
    assert status == "succeeded"
    rows = await store_audit_db.fetch_all(
        "SELECT evidence_type, product_key FROM evidence_items WHERE merchant_id = :m ORDER BY evidence_type",
        {"m": MERCHANT},
    )
    assert [(r["evidence_type"], r["product_key"]) for r in rows] == [
        ("commerce_cartability", run["product_key"]),
        ("commerce_checkout_route", None),
        ("commerce_platform", None),
    ]


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
    claim = await _claim()
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
