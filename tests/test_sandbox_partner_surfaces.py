"""S6: two surfaces that open outside production are shut in the partner sandbox, and ONLY there.

1. The Checkout.com webhook (routes/payment_routes.py) with no CHECKOUT_WEBHOOK_SECRET falls through
   unsigned outside production and reaches mark_order_paid. The sandbox refuses it like production.
2. The full internal OpenAPI map (main.docs_viewer_allowed) and the HTML doc pages
   (main.require_docs_ui_enabled) are open outside production. The sandbox guards them like
   production: admin key for the spec, no doc pages.

Staging, development and test keep today's answers: each sandbox test has its staging twin.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import httpx
import pytest
from fastapi import HTTPException

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)

import main  # noqa: E402
from tests.test_sandbox_guard import _clean_platform_env  # noqa: E402,F401 (autouse: no platform markers)

ADMIN_KEY = "admin-key-for-the-test-0123456789"


@pytest.fixture
def recorded(monkeypatch) -> List[Dict[str, Any]]:
    import routes.payment_routes as payment_routes
    import services.webhook_service as webhook_module

    events: List[Dict[str, Any]] = []

    async def fake_record_webhook_event(**kwargs: Any) -> int:
        events.append(kwargs)
        return 1

    async def no_finalize(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("an unsigned event reached the payment finalizer")

    monkeypatch.setattr(webhook_module.WebhookService, "record_webhook_event", staticmethod(fake_record_webhook_event))
    monkeypatch.setattr(payment_routes, "finalize_payment_success", no_finalize)
    monkeypatch.delenv("CHECKOUT_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    return events


async def _post_unsigned(payload: Dict[str, Any]) -> httpx.Response:
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/api/payments/webhooks/checkout", json=payload)


_MISSING_REF = {"type": "payment_captured", "data": {"id": "evt_s6", "approved": True}}
_PAID = {"type": "payment_captured", "data": {"id": "evt_s6_paid", "reference": "ord_s6", "approved": True}}


# --- 1. the unsigned checkout webhook -------------------------------------------------------------


@pytest.mark.parametrize("markers", [{}, {"K_SERVICE": "web"}, {"CLOUD_RUN_JOB": "j"}])
def test_an_unsigned_checkout_webhook_is_fatal_in_the_sandbox(monkeypatch, markers):
    from routes.payment_routes import _unsigned_webhook_is_fatal

    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.setenv("PIVOTA_ENV", "sandbox")
    for key, value in markers.items():
        monkeypatch.setenv(key, value)
    assert _unsigned_webhook_is_fatal() is True


@pytest.mark.parametrize("label", ["staging", "development", "test"])
def test_outside_the_sandbox_the_answer_is_unchanged(monkeypatch, label):
    from routes.payment_routes import _unsigned_webhook_is_fatal

    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.setenv("PIVOTA_ENV", label)
    assert _unsigned_webhook_is_fatal() is False


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [_MISSING_REF, _PAID], ids=["missing_ref", "paid"])
async def test_the_sandbox_refuses_an_unsigned_event_before_reading_it(monkeypatch, recorded, payload):
    monkeypatch.setenv("PIVOTA_ENV", "sandbox")
    resp = await _post_unsigned(payload)
    assert resp.status_code == 503
    assert resp.json()["detail"] == "webhook_secret_not_configured"
    assert [e["status"] for e in recorded] == ["failed"]
    assert recorded[0]["signature_verified"] is False


@pytest.mark.asyncio
async def test_staging_still_falls_through_as_before(monkeypatch, recorded):
    monkeypatch.setenv("PIVOTA_ENV", "staging")
    resp = await _post_unsigned(_MISSING_REF)
    assert resp.status_code == 200
    assert resp.json()["reason"] == "missing_order_reference"
    assert [e["status"] for e in recorded] == ["ignored"]


@pytest.mark.asyncio
async def test_with_a_secret_the_sandbox_needs_the_signature_like_everywhere(monkeypatch, recorded):
    monkeypatch.setenv("PIVOTA_ENV", "sandbox")
    monkeypatch.setenv("CHECKOUT_WEBHOOK_SECRET", "whsec_s6_test")
    resp = await _post_unsigned(_PAID)
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Missing signature"


# --- 2. the internal OpenAPI map and the doc pages ------------------------------------------------


@pytest.fixture
def admin_key(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", ADMIN_KEY)
    monkeypatch.delenv("PROMOTIONS_ADMIN_KEY", raising=False)


def test_the_sandbox_shows_the_internal_spec_only_with_the_admin_key(monkeypatch, admin_key):
    monkeypatch.setenv("PIVOTA_ENV", "sandbox")
    assert main.docs_viewer_allowed(None) is False
    assert main.docs_viewer_allowed("") is False
    assert main.docs_viewer_allowed("wrong") is False
    assert main.docs_viewer_allowed(ADMIN_KEY) is True


def test_the_sandbox_with_no_admin_key_configured_shows_it_to_nobody(monkeypatch):
    monkeypatch.setenv("PIVOTA_ENV", "sandbox")
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    monkeypatch.delenv("PROMOTIONS_ADMIN_KEY", raising=False)
    assert main.docs_viewer_allowed(None) is False
    assert main.docs_viewer_allowed("") is False


@pytest.mark.parametrize("label", ["staging", "development", "test"])
def test_outside_the_sandbox_the_spec_stays_open_as_before(monkeypatch, admin_key, label):
    monkeypatch.setenv("PIVOTA_ENV", label)
    assert main.docs_viewer_allowed(None) is True


def test_production_is_unchanged(monkeypatch, admin_key):
    monkeypatch.setenv("PIVOTA_ENV", "production")
    assert main.docs_viewer_allowed(None) is False
    assert main.docs_viewer_allowed(ADMIN_KEY) is True


@pytest.mark.asyncio
async def test_the_sandbox_serves_no_doc_pages(monkeypatch):
    monkeypatch.setenv("PIVOTA_ENV", "sandbox")
    with pytest.raises(HTTPException) as excinfo:
        await main.require_docs_ui_enabled()
    assert excinfo.value.status_code == 404
    monkeypatch.setenv("PIVOTA_ENV", "staging")
    assert await main.require_docs_ui_enabled() is None


@pytest.mark.asyncio
async def test_over_http_the_sandbox_redirects_an_anonymous_spec_read_and_hides_the_pages(monkeypatch, admin_key):
    monkeypatch.setenv("PIVOTA_ENV", "sandbox")
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        anonymous = await client.get("/openapi.json")
        keyed = await client.get("/openapi.json", headers={"X-ADMIN-KEY": ADMIN_KEY})
        pages = [await client.get(path) for path in ("/docs", "/redoc")]
    assert anonymous.status_code == 307
    assert anonymous.headers["location"].endswith("/agent/docs/openapi.json")
    assert keyed.status_code == 200 and "paths" in keyed.json()
    assert [p.status_code for p in pages] == [404, 404]
