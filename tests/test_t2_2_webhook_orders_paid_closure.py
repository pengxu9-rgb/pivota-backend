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
    # ADR-009 §D3 wiring: the handler forwards the SAME (canonicalized) shop domain
    # it authenticated as the converting store-of-record for the seller-mismatch guard.
    assert call["converting_shop_domain"] == "teststore.myshopify.com"


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
@pytest.mark.parametrize(
    "deployed_shape",
    [
        {"RAILWAY_ENVIRONMENT": "production"},
        {"RAILWAY_ENVIRONMENT": "staging"},
        {"K_SERVICE": "pivota-backend", "PIVOTA_ENV": "production"},
        {"K_SERVICE": "pivota-backend", "PIVOTA_ENV": "staging"},
        {"K_SERVICE": "pivota-backend"},
    ],
    ids=["railway_prod", "railway_staging", "cloud_run_prod",
         "cloud_run_staging", "cloud_run_unresolved"],
)
async def test_invalid_hmac_in_production_rejects_and_never_closes(
    monkeypatch, deployed_shape
):
    calls: List[Dict[str, Any]] = []

    async def close_spy(**kwargs: Any):
        calls.append(kwargs)
        return {"replayed": False}

    async def log_spy(**kwargs: Any):
        return None

    # Force the deployed code path so HMAC is strictly enforced.
    #
    # This used to set RAILWAY_GIT_COMMIT_SHA, the legacy "am I deployed"
    # idiom. That is build metadata about a commit, not proof of a deployment
    # (a developer .env carries it), so config.platform no longer treats it as
    # a platform marker. Parametrised over every DEPLOYED shape instead —
    # including both STAGING ones, which is what pins is_deployed() rather than
    # is_production() as the rule: staging has always enforced Shopify HMAC.
    for _k in ("RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME", "K_SERVICE",
               "PIVOTA_ENV", "PIVOTA_PLATFORM", "ENVIRONMENT", "APP_ENV"):
        monkeypatch.delenv(_k, raising=False)
    for _k, _v in deployed_shape.items():
        monkeypatch.setenv(_k, _v)
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
