import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

MERCHANT_ID = "merch_web"
STORE_ID = "store_web_1"
PLATFORM = "woocommerce"
ORIGIN = "https://shop.example.com"
SIGNING_SECRET = "collector-test-secret-that-is-long-enough-12345"


@pytest.fixture(autouse=True)
def collector_signing_secret(monkeypatch):
    monkeypatch.setenv("MERCHANT_WEB_COLLECTOR_SIGNING_SECRET", SIGNING_SECRET)


def _claims_token(*, origin=ORIGIN, now=None):
    from services.merchant_web_collector_service import issue_web_collector_token

    return issue_web_collector_token(
        merchant_id=MERCHANT_ID,
        store_id=STORE_ID,
        platform=PLATFORM,
        allowed_origins=[origin],
        now=now,
    )["token"]


def _event(**overrides):
    payload = {
        "event_id": "web_evt_1",
        "event_type": "product.viewed",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "session_id": "ses_123",
        "visitor_id": "vis_123",
        "canonical_product_id": "prod_123",
        "click_id": "clk_123456",
        "source_channel": "chatgpt",
        "metadata": {"quantity": 1},
    }
    payload.update(overrides)
    return payload


def _app(current_user=None):
    from routes.merchant_events import router
    from utils.auth import get_current_user

    app = FastAPI()
    app.include_router(router)
    if current_user is not None:
        async def fake_current_user():
            return current_user

        app.dependency_overrides[get_current_user] = fake_current_user
    return app


def test_token_is_bound_to_origin_and_domain_separated():
    from services.merchant_web_collector_service import (
        WebCollectorError,
        verify_web_collector_token,
    )

    token = _claims_token()
    claims = verify_web_collector_token(token, request_origin=ORIGIN)

    assert claims["merchant_id"] == MERCHANT_ID
    assert claims["store_id"] == STORE_ID
    assert claims["platform"] == PLATFORM
    with pytest.raises(WebCollectorError) as wrong_origin:
        verify_web_collector_token(token, request_origin="https://attacker.example")
    assert wrong_origin.value.status_code == 403


def test_expired_or_tampered_token_is_rejected():
    from services.merchant_web_collector_service import (
        WebCollectorError,
        verify_web_collector_token,
    )

    expired = _claims_token(now=datetime.now(timezone.utc) - timedelta(days=100))
    with pytest.raises(WebCollectorError) as expired_error:
        verify_web_collector_token(expired, request_origin=ORIGIN)
    assert expired_error.value.status_code == 401

    token = _claims_token()
    parts = token.split(".")
    replacement = "a" if parts[2][0] != "a" else "b"
    parts[2] = replacement + parts[2][1:]
    with pytest.raises(WebCollectorError) as tampered_error:
        verify_web_collector_token(".".join(parts), request_origin=ORIGIN)
    assert tampered_error.value.status_code == 401


@pytest.mark.parametrize(
    "origin,expected",
    [
        ("SHOP.EXAMPLE.COM", "https://shop.example.com"),
        ("https://shop.example.com:443/", "https://shop.example.com"),
        ("http://localhost:3000", "http://localhost:3000"),
    ],
)
def test_origin_normalization(origin, expected):
    from services.merchant_web_collector_service import normalize_collector_origin

    assert normalize_collector_origin(origin) == expected


@pytest.mark.parametrize(
    "origin",
    [
        "http://shop.example.com",
        "https://user:pass@shop.example.com",
        "https://shop.example.com/path",
        "https://shop.example.com?token=bad",
    ],
)
def test_unsafe_install_origins_are_rejected(origin):
    from services.merchant_web_collector_service import normalize_collector_origin

    with pytest.raises(ValueError):
        normalize_collector_origin(origin)


def test_web_batch_injects_token_scope_and_preserves_stitching():
    from services.merchant_web_collector_service import build_web_collector_batch

    claims = {
        "merchant_id": MERCHANT_ID,
        "store_id": STORE_ID,
        "platform": PLATFORM,
    }
    batch = build_web_collector_batch(
        {"collector_token": "public", "events": [_event()]},
        claims=claims,
    )

    event = batch.events[0]
    assert event.platform == PLATFORM
    assert event.store_id == STORE_ID
    assert event.source == "universal_web_collector"
    assert event.session_id == "ses_123"
    assert event.click_id == "clk_123456"
    assert event.amount_cents is None


@pytest.mark.parametrize(
    "event_type",
    [
        "payment.succeeded",
        "payment.failed",
        "order.created",
        "order.paid",
        "order.cancelled",
        "refund.succeeded",
        "return.completed",
    ],
)
def test_public_collector_cannot_manufacture_authoritative_events(event_type):
    from services.merchant_web_collector_service import (
        WebCollectorError,
        build_web_collector_batch,
    )

    with pytest.raises(WebCollectorError) as error:
        build_web_collector_batch(
            {
                "collector_token": "public",
                "events": [_event(event_type=event_type)],
            },
            claims={"merchant_id": MERCHANT_ID, "store_id": STORE_ID, "platform": PLATFORM},
        )
    assert error.value.status_code == 422


def test_public_collector_cannot_report_money_or_cross_store_ids():
    from services.merchant_web_collector_service import (
        WebCollectorError,
        build_web_collector_batch,
    )

    claims = {"merchant_id": MERCHANT_ID, "store_id": STORE_ID, "platform": PLATFORM}
    for event in (
        _event(amount_cents=999, currency="USD"),
        _event(store_id="some_other_store"),
        _event(platform="shopify"),
        _event(order_id="order_123"),
        _event(refund_id="refund_123"),
        _event(return_id="return_123"),
        _event(buyer_id="buyer@example.com"),
    ):
        with pytest.raises(WebCollectorError):
            build_web_collector_batch(
                {"collector_token": "public", "events": [event]},
                claims=claims,
            )


def test_web_collector_rejects_stale_and_future_events():
    from services.merchant_web_collector_service import (
        WebCollectorError,
        build_web_collector_batch,
    )

    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    claims = {"merchant_id": MERCHANT_ID, "store_id": STORE_ID, "platform": PLATFORM}
    for occurred_at in (
        now - timedelta(days=8),
        now + timedelta(minutes=6),
    ):
        with pytest.raises(WebCollectorError):
            build_web_collector_batch(
                {
                    "collector_token": "public",
                    "events": [_event(occurred_at=occurred_at.isoformat())],
                },
                claims=claims,
                now=now,
            )


def test_web_collector_route_accepts_origin_bound_batch(monkeypatch):
    from routes import merchant_events as route

    calls = []

    class FakeDatabase:
        async def fetch_one(self, query, values):
            assert values["store_id"] == STORE_ID
            return {
                "store_id": STORE_ID,
                "merchant_id": MERCHANT_ID,
                "platform": PLATFORM,
                "domain": ORIGIN,
                "status": "active",
            }

    async def fake_ingest(**kwargs):
        calls.append(kwargs)
        return {"accepted": 1, "duplicates": 0, "events": []}

    monkeypatch.setattr(route, "database", FakeDatabase())
    monkeypatch.setattr(route, "ingest_merchant_event_batch", fake_ingest)
    response = TestClient(_app()).post(
        "/merchant-events/v1/web/batch",
        content=json.dumps(
            {"collector_token": _claims_token(), "events": [_event()]},
            separators=(",", ":"),
        ),
        headers={"Content-Type": "text/plain", "Origin": ORIGIN},
    )

    assert response.status_code == 200, response.text
    assert response.json()["accepted"] == 1
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert len(calls) == 1
    assert calls[0]["agent_identity_confidence"] == "browser_observed"
    event = calls[0]["batch"].events[0]
    assert calls[0]["merchant_id"] == MERCHANT_ID
    assert event.store_id == STORE_ID
    assert event.platform == PLATFORM
    assert event.source == "universal_web_collector"


def test_web_collector_route_rejects_missing_origin_before_store_lookup(monkeypatch):
    from routes import merchant_events as route

    class NeverDatabase:
        async def fetch_one(self, *_args, **_kwargs):
            raise AssertionError("store lookup must not run without an authenticated origin")

    monkeypatch.setattr(route, "database", NeverDatabase())
    response = TestClient(_app()).post(
        "/merchant-events/v1/web/batch",
        content=json.dumps({"collector_token": _claims_token(), "events": [_event()]}),
        headers={"Content-Type": "text/plain"},
    )

    assert response.status_code == 403


def test_install_token_is_tenant_scoped_and_returns_pending_consent_snippet(monkeypatch):
    from routes import merchant_events as route

    class FakeDatabase:
        async def fetch_one(self, _query, _values):
            return {
                "store_id": STORE_ID,
                "merchant_id": MERCHANT_ID,
                "platform": PLATFORM,
                "domain": "shop.example.com",
                "status": "active",
            }

    monkeypatch.setattr(route, "database", FakeDatabase())
    current_user = {"role": "merchant", "merchant_id": MERCHANT_ID}
    response = TestClient(_app(current_user)).post(
        "/merchant-events/v1/web/install-token",
        json={"store_id": STORE_ID},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["allowed_origins"] == [ORIGIN]
    assert payload["collector_token"] not in payload["script_src"]
    assert 'data-pivota-consent="pending"' in payload["install_snippet"]

    denied = TestClient(_app({"role": "merchant", "merchant_id": "other"})).post(
        "/merchant-events/v1/web/install-token",
        json={"store_id": STORE_ID},
    )
    assert denied.status_code == 403


def test_collector_asset_is_javascript_and_exposes_consent_gated_api():
    response = TestClient(_app()).get("/merchant-events/v1/collector.js")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/javascript")
    assert "setConsent" in response.text
    assert "universal_web_collector" not in response.text
    assert "payment.succeeded" not in response.text
    assert "window.PivotaCommerce" in response.text
