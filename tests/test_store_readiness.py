from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from routes import store_readiness as sr
from routes.store_readiness import StoreReadinessRunRequest, _project, _target


def test_target_strips_query_and_rejects_non_https():
    assert _target("https://www.example.com/products/hero?variant=123#reviews") == (
        "example.com",
        "https://www.example.com/products/hero",
    )
    with pytest.raises(HTTPException) as error:
        _target("http://example.com/products/hero")
    assert error.value.status_code == 422


def test_project_keeps_each_journey_step_and_requires_critical_steps_for_ready():
    checked = datetime(2026, 9, 21, tzinfo=timezone.utc)
    result = _project({
        "status": "succeeded",
        "audit_run_id": "audit-1",
        "verify_id": "verify-1",
        "completed_at": checked,
        "evidence_jsonb": {
            "steps": [
                {"step": "storefront_access", "status": "passed", "reason": "storefront_loaded"},
                {"step": "product_search", "status": "passed", "reason": "search_result_found"},
                {"step": "product_detail", "status": "passed", "reason": "pdp_confirmed"},
                {"step": "add_to_cart", "status": "passed", "reason": "cart_item_added"},
                {"step": "shipping_address", "status": "passed", "reason": "address_fields_filled"},
                {"step": "checkout", "status": "passed", "reason": "checkout_reached"},
            ],
        },
    })
    assert result.state == "complete"
    assert result.overall == "ready"
    assert [step.step for step in result.steps] == [
        "storefront_access", "product_search", "product_detail",
        "add_to_cart", "shipping_address", "checkout",
    ]
    assert result.steps[1].status == "passed"


def test_project_does_not_turn_legacy_empty_success_into_ready():
    result = _project({
        "status": "succeeded",
        "audit_run_id": "audit-1",
        "verify_id": "verify-1",
        "evidence_jsonb": {},
    })
    assert result.overall == "attention"
    assert all(step.status == "not_run" for step in result.steps)


@pytest.mark.asyncio
async def test_run_accepts_owned_direct_product_url_without_catalog_sync(monkeypatch):
    monkeypatch.setenv("STORE_AUDIT_COMMERCE_PROBE_RECEIPT_ENABLED", "true")

    async def owns(_merchant_id, _domain):
        return True

    async def route(**_kwargs):
        return {"execution_route_id": "route-1", "merchant_id": "merchant-1"}

    async def not_in_flight(**_kwargs):
        return False

    async def identity(**_kwargs):
        return "url_probe:abc"

    async def start(**_kwargs):
        return "audit-1"

    async def enqueue(**_kwargs):
        return "verify-1"

    monkeypatch.setattr(sr, "merchant_owns_domain", owns)
    monkeypatch.setattr(sr, "upsert_execution_route", route)
    monkeypatch.setattr(sr, "has_in_flight_verification_for_route", not_in_flight)
    monkeypatch.setattr(sr, "_product_identity", identity)
    monkeypatch.setattr(sr, "record_audit_run_started", start)
    monkeypatch.setattr(sr, "enqueue_verification_run", enqueue)

    response = await sr.run_store_readiness(
        StoreReadinessRunRequest(
            product_url="https://shop.example.com/products/hero?variant=123",
        ),
        merchant_id="merchant-1",
    )
    assert response.state == "pending"
    assert response.audit_run_id == "audit-1"
    assert response.verification_run_id == "verify-1"
    assert all(step.status == "pending" for step in response.steps)
