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
