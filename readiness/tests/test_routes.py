from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from middleware.error_handler import ErrorHandlerMiddleware
from readiness.flags import DEFAULT_ALPHA_MERCHANT_ID
from readiness.order_sync import InMemoryReadinessJournal
from readiness.tests.conftest import build_live_shopify_products, build_review_summaries, load_real_merchant_fixture


def _install_live_source_mocks(monkeypatch, *, psp_enabled: bool):
    from readiness.sources import shopify_live

    fixture = load_real_merchant_fixture()
    live_products = build_live_shopify_products()

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

    async def fake_fetch_live_products(_merchant_id: str, _shop_domain: str, _access_token: str):
        return live_products, None

    async def fake_load_product_review_summaries(**_kwargs):
        return build_review_summaries()

    monkeypatch.setattr(shopify_live, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(shopify_live, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(shopify_live, "_get_shopify_config_for_merchant", fake_get_shopify_cfg)
    monkeypatch.setattr(shopify_live, "get_cached_products", fake_get_cached_products)
    monkeypatch.setattr(shopify_live, "_fetch_active_psp_config", fake_get_active_psp)
    monkeypatch.setattr(shopify_live, "_fetch_live_products", fake_fetch_live_products)
    monkeypatch.setattr(shopify_live, "load_product_review_summaries", fake_load_product_review_summaries)


def _build_test_client(monkeypatch, *, psp_enabled: bool, include_error_handler: bool = False) -> TestClient:
    monkeypatch.setenv("FEATURE_READINESS_REAL_MERCHANT_ALPHA", "true")
    monkeypatch.setenv("FEATURE_READINESS_SOURCE_OF_TRUTH_V1", "true")
    monkeypatch.setenv("FEATURE_READINESS_CANONICAL_CHECKOUT_ALPHA", "true")
    monkeypatch.setenv("FEATURE_READINESS_PAYMENT_BRIDGE_ALPHA", "true")
    monkeypatch.setenv("FEATURE_READINESS_PAYMENT_INTENT_ALPHA", "true")
    monkeypatch.setenv("READINESS_ALLOW_UNAUTHED_DEV", "true")
    monkeypatch.setenv("READINESS_ALPHA_MERCHANT_ID", DEFAULT_ALPHA_MERCHANT_ID)

    _install_live_source_mocks(monkeypatch, psp_enabled=psp_enabled)

    from readiness import service as readiness_service
    from readiness import order_sync as readiness_order_sync
    from routes.readiness_internal import router as readiness_router

    readiness_order_sync._default_journal = InMemoryReadinessJournal()
    app = FastAPI()
    if include_error_handler:
        app.add_middleware(ErrorHandlerMiddleware)
    app.include_router(readiness_router)
    return TestClient(app)


def test_real_merchant_report_and_export(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True)

    report = client.get(f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/report?channel=ucp")
    assert report.status_code == 200
    report_json = report.json()
    assert report_json["merchant_alpha_mode"] == "real_merchant_alpha"
    assert report_json["capability_status"]["checkout"] == "ready"
    assert report_json["capability_status"]["reviews_confidence"] == "ready"
    assert report_json["merchant_id"] == DEFAULT_ALPHA_MERCHANT_ID
    assert report_json["products"][0]["reviews"]["review_count"] == 27

    export = client.get(f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/exports/ucp")
    assert export.status_code == 200
    export_json = export.json()
    assert export_json["merchant_alpha_mode"] == "real_merchant_alpha"
    assert export_json["capability_status"]["checkout"] == "ready"
    assert export_json["capability_status"]["reviews_confidence"] == "ready"
    assert len(export_json["offers"]) == 3
    offer_variant_ids = {offer["variant_id"] for offer in export_json["offers"]}
    assert "431000000001" in offer_variant_ids
    assert "431000000002" in offer_variant_ids
    assert "431000000003" in offer_variant_ids
    assert "431000000004" not in offer_variant_ids
    assert all(offer["reviews"]["has_reviews"] is True for offer in export_json["offers"])


def test_real_merchant_summary_report_and_export(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True)

    report = client.get(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/report?channel=ucp&summary_only=true&sample_limit=2"
    )
    assert report.status_code == 200
    report_json = report.json()
    assert report_json["response_mode"] == "summary"
    assert report_json["products"] == []
    assert report_json["summary"]["product_count"] == 2
    assert report_json["summary"]["variant_count"] == 4
    assert report_json["summary"]["ready_variant_count"] == 3
    assert report_json["summary"]["blocked_variant_count"] == 1
    assert report_json["summary"]["sample_limit"] == 2
    assert len(report_json["summary"]["ready_variant_ids_sample"]) == 2
    assert report_json["summary"]["blocked_variant_ids_sample"] == ["431000000004"]

    export = client.get(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/exports/ucp?summary_only=true&sample_limit=2"
    )
    assert export.status_code == 200
    export_json = export.json()
    assert export_json["response_mode"] == "summary"
    assert export_json["offers"] == []
    assert export_json["summary"]["offer_count"] == 3
    assert export_json["summary"]["review_backed_offer_count"] == 3
    assert export_json["summary"]["sample_limit"] == 2
    assert len(export_json["summary"]["offer_ids_sample"]) == 2


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


def test_checkout_blocked_error_code_is_preserved_with_error_handler(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=False, include_error_handler=True)

    response = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout",
        json={"variant_id": "431000000001", "quantity": 1},
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "VARIANT_NOT_READY_FOR_CHECKOUT"
    assert body["error"]["details"]["code"] == "VARIANT_NOT_READY_FOR_CHECKOUT"
    assert "merchant_checkout_capability_missing" in body["error"]["details"]["blockers"]


def test_report_unsupported_channel_error_code_is_preserved(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True, include_error_handler=True)

    response = client.get(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/report?channel=google"
    )

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "UNSUPPORTED_CHANNEL"
    assert body["error"]["details"]["code"] == "UNSUPPORTED_CHANNEL"
    assert body["error"]["details"]["channel"] == "google"


def test_report_unsupported_merchant_error_code_is_preserved(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True, include_error_handler=True)

    response = client.get("/internal/readiness/merchants/not-supported-merchant/report?channel=ucp")

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "READINESS_MERCHANT_UNSUPPORTED"
    assert body["error"]["details"]["code"] == "READINESS_MERCHANT_UNSUPPORTED"
    assert "supported_merchants" in body["error"]["details"]


def test_checkout_variant_not_found_error_code_is_preserved(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True, include_error_handler=True)

    response = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout",
        json={"variant_id": "does-not-exist", "quantity": 1},
    )

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "VARIANT_NOT_FOUND"
    assert body["error"]["details"]["code"] == "VARIANT_NOT_FOUND"
    assert body["error"]["details"]["variant_id"] == "does-not-exist"


def test_checkout_session_not_found_error_code_is_preserved(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True, include_error_handler=True)

    response = client.get("/internal/readiness/checkout-sessions/rdchk_missing")

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "CHECKOUT_NOT_FOUND"
    assert body["error"]["details"]["code"] == "CHECKOUT_NOT_FOUND"
    assert body["error"]["details"]["checkout_id"] == "rdchk_missing"


def test_payment_bridge_not_found_error_code_is_preserved(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True, include_error_handler=True)

    response = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout-sessions/rdchk_missing/payment-bridge",
        json={"payment_reference": "pi_missing"},
    )

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "CHECKOUT_NOT_FOUND"
    assert body["error"]["details"]["code"] == "CHECKOUT_NOT_FOUND"
    assert body["error"]["details"]["checkout_id"] == "rdchk_missing"


def test_payment_intent_not_found_error_code_is_preserved(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True, include_error_handler=True)

    response = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout-sessions/rdchk_missing/payment-intent",
        json={},
    )

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "CHECKOUT_NOT_FOUND"
    assert body["error"]["details"]["code"] == "CHECKOUT_NOT_FOUND"
    assert body["error"]["details"]["checkout_id"] == "rdchk_missing"


def test_order_sync_audit_not_found_error_code_is_preserved(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True, include_error_handler=True)

    response = client.get(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/order-sync-audit/rdchk_missing"
    )

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "CHECKOUT_NOT_FOUND"
    assert body["error"]["details"]["code"] == "CHECKOUT_NOT_FOUND"
    assert body["error"]["details"]["checkout_id"] == "rdchk_missing"


def test_order_sync_not_found_error_code_is_preserved(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True, include_error_handler=True)

    response = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/order-sync/rdchk_missing",
        json={"replay": False},
    )

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "CHECKOUT_NOT_FOUND"
    assert body["error"]["details"]["code"] == "CHECKOUT_NOT_FOUND"
    assert body["error"]["details"]["checkout_id"] == "rdchk_missing"


def test_order_sync_audit_route_returns_service_payload(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True)

    from readiness import service as readiness_service

    async def fake_build_order_sync_audit(merchant_id: str, checkout_id: str, *, sample_limit: int = 10):
        assert merchant_id == DEFAULT_ALPHA_MERCHANT_ID
        assert checkout_id == "rdchk_alpha_1"
        assert sample_limit == 7
        return {
            "merchant_id": merchant_id,
            "checkout_id": checkout_id,
            "merchant_alpha_mode": "real_merchant_alpha",
            "sync_signals": {
                "merchant_writeback": {"status": "ready"},
                "webhook_ingest": {"status": "pending"},
            },
        }

    monkeypatch.setattr(readiness_service, "build_order_sync_audit", fake_build_order_sync_audit)

    response = client.get(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/order-sync-audit/rdchk_alpha_1?sample_limit=7"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["checkout_id"] == "rdchk_alpha_1"
    assert body["sync_signals"]["merchant_writeback"]["status"] == "ready"
    assert body["sync_signals"]["webhook_ingest"]["status"] == "pending"


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


def test_payment_bridge_requires_local_order(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True, include_error_handler=True)

    checkout = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout",
        json={
            "variant_id": "431000000001",
            "quantity": 1,
            "idempotency_key": "idem-alpha-no-order",
            "buyer_email": "buyer@example.com",
            "customer_name": "Alpha Buyer",
            "shipping_address": {
                "name": "Alpha Buyer",
                "address_line1": "1 Orchard Road",
                "city": "Singapore",
                "postal_code": "238823",
                "country": "SG",
            },
        },
    )
    checkout_id = checkout.json()["checkout_id"]

    response = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout-sessions/{checkout_id}/payment-bridge",
        json={"payment_reference": "pi_alpha_missing_order"},
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "CHECKOUT_ORDER_NOT_CREATED"
    assert body["error"]["details"]["checkout_id"] == checkout_id


def test_payment_intent_requires_local_order(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True, include_error_handler=True)

    checkout = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout",
        json={
            "variant_id": "431000000001",
            "quantity": 1,
            "idempotency_key": "idem-alpha-payment-intent-no-order",
            "buyer_email": "buyer@example.com",
            "customer_name": "Alpha Buyer",
            "shipping_address": {
                "name": "Alpha Buyer",
                "address_line1": "1 Orchard Road",
                "city": "Singapore",
                "postal_code": "238823",
                "country": "SG",
            },
        },
    )
    checkout_id = checkout.json()["checkout_id"]

    response = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout-sessions/{checkout_id}/payment-intent",
        json={},
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "CHECKOUT_ORDER_NOT_CREATED"
    assert body["error"]["details"]["checkout_id"] == checkout_id


def test_real_merchant_payment_bridge_marks_order_paid_and_syncs_transaction(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True)

    from readiness import service as readiness_service

    order_state = {
        "order_id": "ORD_ALPHA_PAID",
        "shopify_order_id": None,
        "status": "pending",
        "payment_status": "unpaid",
        "payment_intent_id": None,
        "psp_used": "stripe",
        "total": 29.0,
        "currency": "USD",
        "total_refunded": 0,
    }
    payment_updates = []
    bridged_events = []

    async def fake_create_order(_order_data):
        return "ORD_ALPHA_PAID"

    async def fake_get_order(_order_id: str):
        return dict(order_state)

    async def fake_update_fulfillment_info(order_id: str, shopify_order_id=None, **_kwargs):
        assert order_id == "ORD_ALPHA_PAID"
        order_state["shopify_order_id"] = shopify_order_id
        return True

    async def fake_create_shopify_order_for_checkout(**_kwargs):
        return {
            "ok": True,
            "shopify_order_id": "9001888999",
            "shopify_order_name": "#1888",
            "shopify_order_url": "https://alpha-beauty-demo.myshopify.com/admin/orders/9001888999",
        }

    async def fake_update_payment_info(order_id: str, payment_intent_id: str, client_secret: str, payment_status: str = "processing", psp_used=None):
        assert order_id == "ORD_ALPHA_PAID"
        payment_updates.append(
            {
                "payment_intent_id": payment_intent_id,
                "client_secret": client_secret,
                "payment_status": payment_status,
                "psp_used": psp_used,
            }
        )
        order_state["payment_intent_id"] = payment_intent_id
        order_state["payment_status"] = payment_status
        order_state["psp_used"] = psp_used
        return True

    async def fake_mark_order_paid(order_id: str):
        assert order_id == "ORD_ALPHA_PAID"
        order_state["status"] = "paid"
        order_state["payment_status"] = "paid"
        return True

    async def fake_log_order_event(**kwargs):
        bridged_events.append(kwargs)

    async def fake_ensure_external_payment_transaction_best_effort(**kwargs):
        assert kwargs["shopify_order_id"] == "9001888999"
        assert kwargs["external_payment_ref"] == "pi_alpha_bridge_1"
        return {"ok": True, "created": True, "transaction_id": "txn_alpha_bridge_1"}

    monkeypatch.setattr(readiness_service, "create_order", fake_create_order)
    monkeypatch.setattr(readiness_service, "get_order", fake_get_order)
    monkeypatch.setattr(readiness_service, "update_fulfillment_info", fake_update_fulfillment_info)
    monkeypatch.setattr(readiness_service, "_create_shopify_order_for_checkout", fake_create_shopify_order_for_checkout)
    monkeypatch.setattr(readiness_service, "update_payment_info", fake_update_payment_info)
    monkeypatch.setattr(readiness_service, "mark_order_paid", fake_mark_order_paid)
    monkeypatch.setattr(readiness_service, "log_order_event", fake_log_order_event)
    monkeypatch.setattr(readiness_service, "ensure_external_payment_transaction_best_effort", fake_ensure_external_payment_transaction_best_effort)

    checkout = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout",
        json={
            "variant_id": "431000000001",
            "quantity": 1,
            "idempotency_key": "idem-alpha-paid-bridge",
            "buyer_email": "buyer@example.com",
            "customer_name": "Alpha Buyer",
            "shipping_address": {
                "name": "Alpha Buyer",
                "address_line1": "1 Orchard Road",
                "city": "Singapore",
                "postal_code": "238823",
                "country": "SG",
            },
        },
    )
    checkout_id = checkout.json()["checkout_id"]

    sync = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/order-sync/{checkout_id}",
        json={"replay": False},
    )
    assert sync.status_code == 200
    assert sync.json()["status"] == "state_synced"

    bridge = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout-sessions/{checkout_id}/payment-bridge",
        json={
            "payment_reference": "pi_alpha_bridge_1",
            "psp_used": "stripe",
            "source": "operator_canary_bridge",
        },
    )
    assert bridge.status_code == 200
    bridge_json = bridge.json()
    assert bridge_json["order_id"] == "ORD_ALPHA_PAID"
    assert bridge_json["payment_status"] == "paid"
    assert bridge_json["payment_reference"] == "pi_alpha_bridge_1"
    assert bridge_json["psp_used"] == "stripe"
    assert bridge_json["transaction_sync"]["ok"] is True
    assert bridge_json["replayed"] is False

    assert payment_updates[0]["payment_intent_id"] == "pi_alpha_bridge_1"
    assert payment_updates[0]["payment_status"] == "paid"
    assert bridged_events[0]["event_type"] == "readiness_payment_bridged"

    checkout_view = client.get(f"/internal/readiness/checkout-sessions/{checkout_id}")
    assert checkout_view.status_code == 200
    payload = checkout_view.json()["checkout"]["session_payload"]
    assert payload["payment_reference"] == "pi_alpha_bridge_1"
    assert payload["payment_psp_used"] == "stripe"


def test_real_merchant_payment_intent_creation_is_idempotent(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True)

    from adapters.psp_adapter import PaymentIntent
    from readiness import service as readiness_service

    order_state = {
        "order_id": "ORD_ALPHA_INTENT",
        "shopify_order_id": None,
        "status": "pending",
        "payment_status": "unpaid",
        "payment_intent_id": None,
        "client_secret": None,
        "psp_used": None,
        "total": 29.0,
        "currency": "USD",
        "total_refunded": 0,
    }
    create_calls = []

    async def fake_create_order(_order_data):
        return "ORD_ALPHA_INTENT"

    async def fake_get_order(_order_id: str):
        return dict(order_state)

    async def fake_update_fulfillment_info(order_id: str, shopify_order_id=None, **_kwargs):
        assert order_id == "ORD_ALPHA_INTENT"
        order_state["shopify_order_id"] = shopify_order_id
        return True

    async def fake_create_shopify_order_for_checkout(**_kwargs):
        return {
            "ok": True,
            "shopify_order_id": "9001777000",
            "shopify_order_name": "#1777",
            "shopify_order_url": "https://alpha-beauty-demo.myshopify.com/admin/orders/9001777000",
        }

    async def fake_update_payment_info(order_id: str, payment_intent_id: str, client_secret: str, payment_status: str = "processing", psp_used=None):
        assert order_id == "ORD_ALPHA_INTENT"
        order_state["payment_intent_id"] = payment_intent_id
        order_state["client_secret"] = client_secret
        order_state["payment_status"] = payment_status
        order_state["psp_used"] = psp_used
        return True

    async def fake_log_order_event(**_kwargs):
        return None

    async def fake_create_payment_with_failover(*, merchant_id: str, amount, currency: str, metadata, preferred_psps=None):
        create_calls.append(
            {
                "merchant_id": merchant_id,
                "amount": str(amount),
                "currency": currency,
                "metadata": metadata,
                "preferred_psps": preferred_psps,
            }
        )
        return (
            True,
            PaymentIntent(
                id="pi_alpha_intent_1",
                client_secret="cs_alpha_intent_1",
                amount=2900,
                currency="USD",
                status="requires_action",
                psp_type="stripe",
                raw_response={},
            ),
            None,
            "stripe",
        )

    monkeypatch.setattr(readiness_service, "create_order", fake_create_order)
    monkeypatch.setattr(readiness_service, "get_order", fake_get_order)
    monkeypatch.setattr(readiness_service, "update_fulfillment_info", fake_update_fulfillment_info)
    monkeypatch.setattr(readiness_service, "_create_shopify_order_for_checkout", fake_create_shopify_order_for_checkout)
    monkeypatch.setattr(readiness_service, "update_payment_info", fake_update_payment_info)
    monkeypatch.setattr(readiness_service, "log_order_event", fake_log_order_event)
    monkeypatch.setattr(readiness_service, "create_payment_with_failover", fake_create_payment_with_failover)

    checkout = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout",
        json={
            "variant_id": "431000000001",
            "quantity": 1,
            "idempotency_key": "idem-alpha-payment-intent",
            "buyer_email": "buyer@example.com",
            "customer_name": "Alpha Buyer",
            "shipping_address": {
                "name": "Alpha Buyer",
                "address_line1": "1 Orchard Road",
                "city": "Singapore",
                "postal_code": "238823",
                "country": "SG",
            },
        },
    )
    checkout_id = checkout.json()["checkout_id"]

    sync = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/order-sync/{checkout_id}",
        json={"replay": False},
    )
    assert sync.status_code == 200

    intent_1 = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout-sessions/{checkout_id}/payment-intent",
        json={"preferred_psps": ["stripe"]},
    )
    assert intent_1.status_code == 200
    intent_1_json = intent_1.json()
    assert intent_1_json["payment_intent_id"] == "pi_alpha_intent_1"
    assert intent_1_json["payment_intent_status"] == "requires_action"
    assert intent_1_json["payment_status"] == "awaiting_payment"
    assert intent_1_json["payment_action"]["type"] == "stripe_client_secret"
    assert intent_1_json["bridged_to_paid"] is False
    assert intent_1_json["replayed"] is False
    assert create_calls[0]["preferred_psps"] == ["stripe"]

    intent_2 = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout-sessions/{checkout_id}/payment-intent",
        json={"preferred_psps": ["stripe"]},
    )
    assert intent_2.status_code == 200
    intent_2_json = intent_2.json()
    assert intent_2_json["payment_intent_id"] == "pi_alpha_intent_1"
    assert intent_2_json["replayed"] is True
    assert len(create_calls) == 1

    checkout_view = client.get(f"/internal/readiness/checkout-sessions/{checkout_id}")
    assert checkout_view.status_code == 200
    payload = checkout_view.json()["checkout"]["session_payload"]
    assert payload["payment_intent_id"] == "pi_alpha_intent_1"
    assert payload["payment_intent_status"] == "requires_action"


def test_order_sync_replay_reconciles_cancelled_order_state(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True)

    from readiness import service as readiness_service

    order_state = {
        "shopify_order_id": None,
        "status": "pending",
        "payment_status": "unpaid",
        "total_refunded": 0,
    }

    async def fake_create_order(_order_data):
        return "ORD_ALPHA_CANCEL"

    async def fake_get_order(_order_id: str):
        return {
            "order_id": "ORD_ALPHA_CANCEL",
            "shopify_order_id": order_state["shopify_order_id"],
            "status": order_state["status"],
            "payment_status": order_state["payment_status"],
            "total_refunded": order_state["total_refunded"],
        }

    async def fake_update_fulfillment_info(order_id: str, shopify_order_id=None, **_kwargs):
        assert order_id == "ORD_ALPHA_CANCEL"
        order_state["shopify_order_id"] = shopify_order_id
        return True

    async def fake_create_shopify_order_for_checkout(**_kwargs):
        return {
            "ok": True,
            "shopify_order_id": "9001002999",
            "shopify_order_name": "#1099",
            "shopify_order_url": "https://alpha-beauty-demo.myshopify.com/admin/orders/9001002999",
        }

    monkeypatch.setattr(readiness_service, "create_order", fake_create_order)
    monkeypatch.setattr(readiness_service, "get_order", fake_get_order)
    monkeypatch.setattr(readiness_service, "update_fulfillment_info", fake_update_fulfillment_info)
    monkeypatch.setattr(readiness_service, "_create_shopify_order_for_checkout", fake_create_shopify_order_for_checkout)

    checkout = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout",
        json={
            "variant_id": "431000000001",
            "quantity": 1,
            "idempotency_key": "idem-alpha-cancel",
            "buyer_email": "buyer@example.com",
            "customer_name": "Alpha Buyer",
            "shipping_address": {
                "name": "Alpha Buyer",
                "address_line1": "1 Orchard Road",
                "city": "Singapore",
                "postal_code": "238823",
                "country": "SG",
            },
        },
    )
    checkout_id = checkout.json()["checkout_id"]

    sync_1 = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/order-sync/{checkout_id}",
        json={"replay": False},
    )
    assert sync_1.status_code == 200
    assert sync_1.json()["status"] == "state_synced"

    order_state["status"] = "cancelled"

    sync_2 = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/order-sync/{checkout_id}",
        json={"replay": True},
    )
    assert sync_2.status_code == 200
    sync_2_json = sync_2.json()
    assert sync_2_json["status"] == "cancelled"
    assert sync_2_json["replayed"] is True
    event_types = [event["event_type"] for event in sync_2_json["events"]]
    assert "merchant_cancellation_observed" in event_types
