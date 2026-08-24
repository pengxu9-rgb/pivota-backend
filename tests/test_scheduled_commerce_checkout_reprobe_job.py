from datetime import datetime, timezone

from jobs.scheduled_commerce_checkout_reprobe_job import merchant_reprobe_idempotency_key


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
