from datetime import datetime, timezone

from jobs import scheduled_commerce_checkout_reprobe_job as reprobe_job
from jobs.scheduled_commerce_checkout_reprobe_job import merchant_reprobe_idempotency_key


def test_scheduler_requires_explicit_armed_gate(monkeypatch):
    monkeypatch.setenv("STORE_AUDIT_COMMERCE_REPROBE_SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("STORE_AUDIT_COMMERCE_PROBE_RECEIPT_ENABLED", "true")
    monkeypatch.setenv("STORE_AUDIT_COMMERCE_PROBE_INTERNAL_KEY", "dedicated-key")
    monkeypatch.delenv("STORE_AUDIT_COMMERCE_REPROBE_ARMED", raising=False)

    assert reprobe_job._enabled() is False


def test_idempotency_is_merchant_scoped_not_sku_or_endpoint_scoped():
    at = datetime(2026, 8, 24, 10, tzinfo=timezone.utc)
    assert merchant_reprobe_idempotency_key(
        merchant_id="merchant:jolse", scheduled_at=at,
    ) == merchant_reprobe_idempotency_key(
        merchant_id="merchant:jolse", scheduled_at=at.replace(hour=23),
    )
    assert merchant_reprobe_idempotency_key(
        merchant_id="merchant:jolse", scheduled_at=at,
    ) != merchant_reprobe_idempotency_key(
        merchant_id="merchant:other", scheduled_at=at,
    )


def test_scheduler_uses_merchant_scoped_inflight_guard():
    source = open("jobs/scheduled_commerce_checkout_reprobe_job.py", encoding="utf-8").read()
    assert "has_in_flight_verification_for_merchant" in source
    assert "merchant_id=merchant_id" in source


async def test_scheduled_run_is_enqueued_with_the_probe_product_key(monkeypatch):
    # The receipt route 409s cart evidence on a run with no product_key, and the worker sends a
    # cart section on every journey it completes: before 2026-11-04 the selector enqueued
    # product_key=None, so a scheduled probe could record only its timeouts.
    from db import audit_evidence as ae
    from services import store_audit_probe_identity as identity

    for name, value in {
        "STORE_AUDIT_COMMERCE_REPROBE_SCHEDULER_ENABLED": "true",
        "STORE_AUDIT_COMMERCE_REPROBE_ARMED": "true",
        "STORE_AUDIT_COMMERCE_PROBE_RECEIPT_ENABLED": "true",
        "STORE_AUDIT_COMMERCE_PROBE_INTERNAL_KEY": "dedicated-key",
    }.items():
        monkeypatch.setenv(name, value)
    route = {
        "merchant_id": "merchant:jolse", "execution_route_id": "route-1",
        "last_audit_run_id": "audit-1", "endpoint_normalized": "https://jolse.example/",
    }

    async def candidates(**_kwargs):
        return [route]

    async def no_evidence(**_kwargs):
        return []

    async def nothing_in_flight(**_kwargs):
        return False

    keyed = []

    async def product_key(*, merchant_id, url):
        keyed.append((merchant_id, url))
        return "url_probe:abc"

    enqueued = []

    async def enqueue(**kwargs):
        enqueued.append(kwargs)
        return "verify-1"

    monkeypatch.setattr(reprobe_job, "list_merchant_storefront_candidates", candidates)
    monkeypatch.setattr(ae, "fetch_active_commerce_evidence", no_evidence)
    monkeypatch.setattr(ae, "has_in_flight_verification_for_merchant", nothing_in_flight)
    monkeypatch.setattr(ae, "enqueue_verification_run", enqueue)
    monkeypatch.setattr(identity, "probe_product_key", product_key)

    summary = await reprobe_job.run_scheduled_commerce_checkout_reprobes()

    assert summary["enqueued"] == 1
    # Keyed on the URL the worker is handed (the claim's target_url is the route's endpoint).
    assert keyed == [("merchant:jolse", "https://jolse.example/")]
    assert enqueued[0]["product_key"] == "url_probe:abc"
    assert enqueued[0]["execution_route_id"] == "route-1"
