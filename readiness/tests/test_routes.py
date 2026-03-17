from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from readiness.flags import DEFAULT_ALPHA_MERCHANT_ID
from readiness.order_sync import InMemoryReadinessJournal
from readiness.tests.conftest import load_real_merchant_fixture


def _install_live_source_mocks(monkeypatch, *, psp_enabled: bool):
    from readiness.sources import shopify_live

    fixture = load_real_merchant_fixture()

    async def fake_get_merchant_onboarding(_merchant_id: str):
        return {"merchant_id": fixture["merchant_id"], "business_name": fixture["merchant_name"]}

    async def fake_get_primary_store(_merchant_id: str):
        return fixture["store"]

    async def fake_get_shopify_cfg(_merchant_id: str):
        return fixture["shopify_config"]

    async def fake_get_cached_products(*, merchant_id: str, platform: str, include_expired: bool = False):
        assert merchant_id == fixture["merchant_id"]
        assert platform == "shopify"
        rows = deepcopy(fixture["products_cache_rows"])
        now = datetime.now(timezone.utc).replace(microsecond=0)
        rows[0]["cached_at"] = now.isoformat().replace("+00:00", "Z")
        rows[0]["expires_at"] = (now + timedelta(days=7)).isoformat().replace("+00:00", "Z")
        rows[1]["cached_at"] = (now - timedelta(days=2)).isoformat().replace("+00:00", "Z")
        rows[1]["expires_at"] = (now - timedelta(days=1)).isoformat().replace("+00:00", "Z")
        return rows

    async def fake_get_active_psp(_merchant_id: str):
        return fixture["merchant_psp"] if psp_enabled else None

    monkeypatch.setattr(shopify_live, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(shopify_live, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(shopify_live, "_get_shopify_config_for_merchant", fake_get_shopify_cfg)
    monkeypatch.setattr(shopify_live, "get_cached_products", fake_get_cached_products)
    monkeypatch.setattr(shopify_live, "_fetch_active_psp_config", fake_get_active_psp)


def _build_test_client(monkeypatch, *, psp_enabled: bool) -> TestClient:
    monkeypatch.setenv("FEATURE_READINESS_REAL_MERCHANT_ALPHA", "true")
    monkeypatch.setenv("FEATURE_READINESS_SOURCE_OF_TRUTH_V1", "true")
    monkeypatch.setenv("FEATURE_READINESS_CANONICAL_CHECKOUT_ALPHA", "true")
    monkeypatch.setenv("READINESS_ALLOW_UNAUTHED_DEV", "true")
    monkeypatch.setenv("READINESS_ALPHA_MERCHANT_ID", DEFAULT_ALPHA_MERCHANT_ID)

    _install_live_source_mocks(monkeypatch, psp_enabled=psp_enabled)

    from readiness import service as readiness_service
    from readiness import order_sync as readiness_order_sync
    from routes.readiness_internal import router as readiness_router

    readiness_order_sync._default_journal = InMemoryReadinessJournal()
    app = FastAPI()
    app.include_router(readiness_router)
    return TestClient(app)


def test_real_merchant_report_and_export(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True)

    report = client.get(f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/report?channel=ucp")
    assert report.status_code == 200
    report_json = report.json()
    assert report_json["merchant_alpha_mode"] == "real_merchant_alpha"
    assert report_json["capability_status"]["checkout"] == "ready"
    assert report_json["merchant_id"] == DEFAULT_ALPHA_MERCHANT_ID

    export = client.get(f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/exports/ucp")
    assert export.status_code == 200
    export_json = export.json()
    assert export_json["merchant_alpha_mode"] == "real_merchant_alpha"
    assert export_json["capability_status"]["checkout"] == "ready"
    assert len(export_json["offers"]) == 2
    offer_variant_ids = {offer["variant_id"] for offer in export_json["offers"]}
    assert "431000000001" in offer_variant_ids
    assert "431000000002" in offer_variant_ids
    assert "431000000003" not in offer_variant_ids


def test_checkout_blocked_when_capability_missing(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=False)

    response = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout",
        json={"variant_id": "431000000001", "quantity": 1},
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "VARIANT_NOT_READY_FOR_CHECKOUT"
    assert "merchant_checkout_capability_missing" in detail["blockers"]


def test_real_merchant_checkout_and_order_sync_are_idempotent(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True)

    from readiness import service as readiness_service

    order_state = {"shopify_order_id": None}

    async def fake_create_order(_order_data):
        return "ORD_ALPHA_1"

    async def fake_get_order(_order_id: str):
        return {"order_id": "ORD_ALPHA_1", "shopify_order_id": order_state["shopify_order_id"]}

    async def fake_update_fulfillment_info(order_id: str, shopify_order_id=None, **_kwargs):
        assert order_id == "ORD_ALPHA_1"
        order_state["shopify_order_id"] = shopify_order_id
        return True

    async def fake_create_shopify_order_for_checkout(**_kwargs):
        return {
            "ok": True,
            "shopify_order_id": "9001002003",
            "shopify_order_name": "#1003",
            "shopify_order_url": "https://alpha-beauty-demo.myshopify.com/admin/orders/9001002003",
        }

    monkeypatch.setattr(readiness_service, "create_order", fake_create_order)
    monkeypatch.setattr(readiness_service, "get_order", fake_get_order)
    monkeypatch.setattr(readiness_service, "update_fulfillment_info", fake_update_fulfillment_info)
    monkeypatch.setattr(readiness_service, "_create_shopify_order_for_checkout", fake_create_shopify_order_for_checkout)

    checkout = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout",
        json={
            "variant_id": "431000000001",
            "quantity": 2,
            "idempotency_key": "idem-alpha-1",
            "buyer_email": "buyer@example.com",
            "customer_name": "Alpha Buyer",
            "shipping_address": {
                "name": "Alpha Buyer",
                "address_line1": "1 Orchard Road",
                "city": "Singapore",
                "postal_code": "238823",
                "country": "SG"
            }
        },
    )
    assert checkout.status_code == 200
    checkout_json = checkout.json()
    assert checkout_json["payment_mode"] == "merchant_native_alpha"

    sync_1 = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/order-sync/{checkout_json['checkout_id']}",
        json={"replay": False},
    )
    assert sync_1.status_code == 200
    sync_1_json = sync_1.json()
    assert sync_1_json["order_id"] == "ORD_ALPHA_1"
    assert sync_1_json["status"] == "state_synced"
    event_types = [event["event_type"] for event in sync_1_json["events"]]
    assert "payment_capability_verified" in event_types
    assert "order_created" in event_types
    assert "order_forwarded_to_merchant" in event_types
    assert "state_synced" in event_types

    sync_2 = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/order-sync/{checkout_json['checkout_id']}",
        json={"replay": True},
    )
    assert sync_2.status_code == 200
    sync_2_json = sync_2.json()
    assert sync_2_json["status"] == "state_synced"
    assert sync_2_json["replayed"] is True
