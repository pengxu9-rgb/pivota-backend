from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.chdir(REPO_ROOT)

from main import app


@pytest.mark.asyncio
async def test_public_order_resume_returns_resumable_payment_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.accounts_orders_api as route_module

    recorded: list[tuple[str, str, str]] = []

    async def fake_count_recent_public_lookup_by_ip(_ip: str) -> int:
        return 0

    async def fake_count_recent_public_lookup_by_key(_email: str, _order_id: str) -> int:
        return 0

    async def fake_record_public_lookup(ip: str, email: str, order_id: str) -> None:
        recorded.append((ip, email, order_id))

    async def fake_fetch_one(_query, _values=None):
        return {
            "order_id": "ORD_RESUME_1",
            "merchant_id": "merch_1",
            "customer_email": "buyer@example.com",
            "currency": "USD",
            "status": "pending",
            "payment_status": "pending",
            "fulfillment_status": None,
            "total": 9.53,
            "created_at": datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 4, 21, 12, 1, tzinfo=timezone.utc),
            "shipping_address": {
                "name": "Buyer Example",
                "address_line1": "1 Test St",
                "city": "San Francisco",
                "province": "CA",
                "country": "US",
                "postal_code": "94105",
            },
            "items": [
                {
                    "product_id": "prod_1",
                    "variant_id": "var_1",
                    "offer_id": "offer_1",
                    "quantity": 1,
                    "sku": "SKU-1",
                }
            ],
            "metadata": {
                "stripe_refund_status": {
                    "provider": "stripe",
                    "refund_id": "re_test_refund",
                    "status": "pending",
                    "pending_reason": "processing",
                    "currency": "USD",
                    "reference_status": "pending",
                    "reference_type": "acquirer_reference_number",
                    "tracking_reference_kind": "ARN",
                    "observed_at": "2026-04-21T12:00:00+00:00",
                },
                "stripe_refund_statuses": {
                    "re_test_refund": {
                        "provider": "stripe",
                        "refund_id": "re_test_refund",
                        "status": "pending",
                        "pending_reason": "processing",
                        "currency": "USD",
                        "reference_status": "pending",
                        "reference_type": "acquirer_reference_number",
                        "tracking_reference_kind": "ARN",
                        "observed_at": "2026-04-21T12:00:00+00:00",
                    }
                },
                "pricing_quote": {
                    "line_items": [
                        {
                            "product_id": "prod_1",
                            "variant_id": "var_1",
                            "unit_price_effective": "1.53",
                        }
                    ],
                    "quote_id": "q_resume_1",
                    "currency": "USD",
                    "pricing": {
                        "subtotal": "1.53",
                        "discount_total": "0.16",
                        "shipping_fee": "8.00",
                        "tax": "0.00",
                        "total": "9.37",
                    },
                }
            },
            "psp_used": "stripe",
            "payment_intent_id": "pi_123",
            "client_secret": "cs_test_secret",
        }

    async def fake_build_resumable_payment_payload(order_data, *, payment_status: str):
        assert order_data["order_id"] == "ORD_RESUME_1"
        assert payment_status == "pending"
        return {
            "psp": "stripe",
            "payment_intent_id": "pi_123",
            "payment_action": {"type": "stripe_client_secret"},
            "status": "pending",
        }

    async def fake_load_order_item_display_context(*, merchant_id, product_id):
        assert merchant_id == "merch_1"
        assert product_id == "prod_1"
        return {
            "title": "Resume Item",
            "image_url": "https://example.com/item.png",
        }

    monkeypatch.setattr(
        route_module,
        "count_recent_public_lookup_by_ip",
        fake_count_recent_public_lookup_by_ip,
    )
    monkeypatch.setattr(
        route_module,
        "count_recent_public_lookup_by_key",
        fake_count_recent_public_lookup_by_key,
    )
    monkeypatch.setattr(route_module, "record_public_lookup", fake_record_public_lookup)
    monkeypatch.setattr(route_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(
        route_module,
        "_build_resumable_payment_payload",
        fake_build_resumable_payment_payload,
    )
    monkeypatch.setattr(
        route_module,
        "_load_order_item_display_context",
        fake_load_order_item_display_context,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/accounts/public/order-resume",
            params={"order_id": "ORD_RESUME_1", "email": "buyer@example.com"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["order"]["order_id"] == "ORD_RESUME_1"
    assert body["order"]["shipping_address"]["city"] == "San Francisco"
    assert body["pricing_quote"] == {
        "quote_id": "q_resume_1",
        "currency": "USD",
        "pricing": {
            "subtotal": "1.53",
            "discount_total": "0.16",
            "shipping_fee": "8.00",
            "tax": "0.00",
            "total": "9.37",
        },
    }
    assert body["items"] == [
        {
            "product_id": "prod_1",
            "variant_id": "var_1",
            "offer_id": "offer_1",
            "title": "Resume Item",
            "quantity": 1,
            "unit_price_minor": 153,
            "subtotal_minor": 153,
            "sku": "SKU-1",
            "merchant_id": "merch_1",
            "image_url": "https://example.com/item.png",
        }
    ]
    assert body["payment"]["current"]["payment_action"]["type"] == "stripe_client_secret"
    assert body["refund"]["psp"]["provider"] == "stripe"
    assert body["refund"]["psp"]["latest"]["refund_id"] == "re_test_refund"
    assert body["refund"]["psp"]["latest"]["pending_reason"] == "processing"
    assert body["refund"]["psp"]["latest"]["tracking_reference_kind"] == "ARN"
    assert body["customer"]["email"] == "buyer@example.com"
    assert recorded == [("127.0.0.1", "buyer@example.com", "ORD_RESUME_1")]


@pytest.mark.asyncio
async def test_public_order_resume_hides_order_on_email_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.accounts_orders_api as route_module

    async def fake_count_recent_public_lookup_by_ip(_ip: str) -> int:
        return 0

    async def fake_count_recent_public_lookup_by_key(_email: str, _order_id: str) -> int:
        return 0

    async def fake_fetch_one(_query, _values=None):
        return {
            "order_id": "ORD_RESUME_2",
            "merchant_id": "merch_1",
            "customer_email": "someone-else@example.com",
        }

    monkeypatch.setattr(
        route_module,
        "count_recent_public_lookup_by_ip",
        fake_count_recent_public_lookup_by_ip,
    )
    monkeypatch.setattr(
        route_module,
        "count_recent_public_lookup_by_key",
        fake_count_recent_public_lookup_by_key,
    )
    monkeypatch.setattr(route_module.database, "fetch_one", fake_fetch_one)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/accounts/public/order-resume",
            params={"order_id": "ORD_RESUME_2", "email": "buyer@example.com"},
        )

    assert resp.status_code == 404
    assert resp.json()["detail"]["error"]["code"] == "NOT_FOUND"
