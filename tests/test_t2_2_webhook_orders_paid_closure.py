"""T2-2 — the Shopify `orders/paid` webhook branch that closes external conversions.

Drives the real `handle_shopify_webhook` handler and asserts:
  - note_attributes.pivota_click_id present  → close_external_order_conversion called
    with the parsed order id / GMV / currency;
  - note_attributes absent                   → NOT called, order-event logging intact;
  - HMAC verification fails (production)      → 401, closure never reached.

The closure primitive itself is unit-tested in test_t2_2_external_conversion_closure.
Here we only prove the wiring + the HMAC gate.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db.database as db_database  # noqa: E402
import routes.webhook_routes as wr  # noqa: E402
from fastapi import BackgroundTasks, HTTPException  # noqa: E402


class _FakeHeaders:
    def __init__(self, headers: Dict[str, str]) -> None:
        self._h = {k.lower(): v for k, v in headers.items()}

    def get(self, key: str, default: Any = None) -> Any:
        return self._h.get(key.lower(), default)


class _FakeRequest:
    def __init__(self, body: bytes, headers: Optional[Dict[str, str]] = None) -> None:
        self._body = body
        self.headers = _FakeHeaders(headers or {})

    async def body(self) -> bytes:
        return self._body


class _FakeOrdersDB:
    async def fetch_one(self, query: Any, values: Any = None):
        return None  # no Pivota orders row for an external purchase

    async def execute(self, query: Any, values: Any = None) -> int:
        return 0


def _install_common_stubs(monkeypatch, *, stores: List[Dict[str, Any]], close_spy, log_spy):
    async def _get_onboarding(merchant_id: str):
        return {"merchant_id": merchant_id, "mcp_shop_domain": "teststore.myshopify.com"}

    async def _get_stores(merchant_id: str):
        return stores

    async def _ingest(**kwargs: Any):
        return (False, None)

    monkeypatch.setattr(wr, "get_merchant_onboarding", _get_onboarding)
    monkeypatch.setattr(wr, "get_merchant_active_stores", _get_stores)
    monkeypatch.setattr(wr, "ingest_shopify_webhook", _ingest)
    monkeypatch.setattr(wr, "record_shopify_webhook", lambda *a, **k: None)
    monkeypatch.setattr(wr, "log_order_event", log_spy)
    monkeypatch.setattr(wr, "close_external_order_conversion", close_spy)
    monkeypatch.setattr(db_database, "database", _FakeOrdersDB())


def _paid_payload(note_attributes: Any) -> bytes:
    body: Dict[str, Any] = {
        "id": 6600123,
        "name": "#1042",
        "financial_status": "paid",
        "total_price": "59.90",
        "currency": "USD",
    }
    if note_attributes is not None:
        body["note_attributes"] = note_attributes
    return json.dumps(body).encode()


@pytest.mark.asyncio
async def test_orders_paid_with_click_attr_closes_conversion(monkeypatch):
    calls: List[Dict[str, Any]] = []

    async def close_spy(**kwargs: Any):
        calls.append(kwargs)
        return {"replayed": False}

    async def log_spy(**kwargs: Any):
        return None

    _install_common_stubs(monkeypatch, stores=[], close_spy=close_spy, log_spy=log_spy)

    payload = _paid_payload([{"name": "pivota_click_id", "value": "clk_live1"}])
    resp = await wr.handle_shopify_webhook(
        merchant_id="merch_test",
        request=_FakeRequest(payload),
        background_tasks=BackgroundTasks(),
        x_shopify_hmac_sha256="whatever",  # non-production → signature not enforced
        x_shopify_topic="orders/paid",
        x_shopify_shop_domain="teststore.myshopify.com",
    )
    assert resp["status"] == "success"
    assert len(calls) == 1
    call = calls[0]
    assert call["merchant_id"] == "merch_test"
    assert call["click_id"] == "clk_live1"
    assert call["external_order_id"] == "6600123"
    assert call["gross_amount_cents"] == 5990
    assert call["currency"] == "USD"


@pytest.mark.asyncio
async def test_orders_paid_without_click_attr_does_not_close(monkeypatch):
    calls: List[Dict[str, Any]] = []
    logs: List[Dict[str, Any]] = []

    async def close_spy(**kwargs: Any):
        calls.append(kwargs)
        return {"replayed": False}

    async def log_spy(**kwargs: Any):
        logs.append(kwargs)
        return None

    _install_common_stubs(monkeypatch, stores=[], close_spy=close_spy, log_spy=log_spy)

    payload = _paid_payload([{"name": "gift_message", "value": "enjoy"}])
    resp = await wr.handle_shopify_webhook(
        merchant_id="merch_test",
        request=_FakeRequest(payload),
        background_tasks=BackgroundTasks(),
        x_shopify_hmac_sha256="whatever",
        x_shopify_topic="orders/paid",
        x_shopify_shop_domain="teststore.myshopify.com",
    )
    assert resp["status"] == "success"
    assert calls == []  # no attribution edge attempted
    # existing order-event logging still happened
    assert any(l.get("event_type") == "shopify_order_webhook" for l in logs)


@pytest.mark.asyncio
async def test_orders_create_does_not_close_conversion(monkeypatch):
    # orders/create precedes payment — must not mint a converted edge.
    calls: List[Dict[str, Any]] = []

    async def close_spy(**kwargs: Any):
        calls.append(kwargs)
        return {"replayed": False}

    async def log_spy(**kwargs: Any):
        return None

    _install_common_stubs(monkeypatch, stores=[], close_spy=close_spy, log_spy=log_spy)

    payload = _paid_payload([{"name": "pivota_click_id", "value": "clk_live1"}])
    await wr.handle_shopify_webhook(
        merchant_id="merch_test",
        request=_FakeRequest(payload),
        background_tasks=BackgroundTasks(),
        x_shopify_hmac_sha256="whatever",
        x_shopify_topic="orders/create",
        x_shopify_shop_domain="teststore.myshopify.com",
    )
    assert calls == []


@pytest.mark.asyncio
async def test_invalid_hmac_in_production_rejects_and_never_closes(monkeypatch):
    calls: List[Dict[str, Any]] = []

    async def close_spy(**kwargs: Any):
        calls.append(kwargs)
        return {"replayed": False}

    async def log_spy(**kwargs: Any):
        return None

    # Force the production code path so HMAC is strictly enforced.
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "deadbeef")
    stores = [{
        "platform": "shopify",
        "domain": "teststore.myshopify.com",
        "api_credentials": {"webhook_secret": "s3cret-shared"},
    }]
    _install_common_stubs(monkeypatch, stores=stores, close_spy=close_spy, log_spy=log_spy)

    payload = _paid_payload([{"name": "pivota_click_id", "value": "clk_live1"}])
    with pytest.raises(HTTPException) as exc:
        await wr.handle_shopify_webhook(
            merchant_id="merch_test",
            request=_FakeRequest(payload),
            background_tasks=BackgroundTasks(),
            x_shopify_hmac_sha256="not-a-valid-signature",
            x_shopify_topic="orders/paid",
            x_shopify_shop_domain="teststore.myshopify.com",
        )
    assert exc.value.status_code == 401
    assert calls == []  # closure never reached — it is behind the HMAC gate
