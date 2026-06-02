"""T1a: free URL-audit wedge — POST /api/merchant-center/audit/url-readiness.
Audits a merchant's storefront by discovery (no catalog sync), free for the
first N per merchant, then 402.
"""
from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import routes.merchant_audit_routes as mar
import services.audit_telemetry_context as atc
import services.bd_cold_start_service as bdcs
from utils import auth as auth_module


@pytest.fixture
def client(monkeypatch):
    state = {"used": 0}
    started: list = []
    completed: list = []

    async def fake_count(*, merchant_id, subject_type):
        return state["used"]

    async def fake_onboarding(merchant_id):
        return {"store_url": "https://merch.example", "business_name": "Merch"}

    async def fake_discover(url, *, max_products=3, market="US", persist=False):
        return {
            "merchant_name": "Merch",
            "merchant_domain": "https://merch.example",
            "discovery_method": "shopify_native",
            "products_discovered_total": 4,
            "coverage": {"ok": True},
            "products": [
                {"pdp_url": f"{url}/products/a", "title": "A"},
                {"pdp_url": f"{url}/products/b", "title": "B"},
            ],
        }

    async def fake_started(*, merchant_id, product_keys, subject_type="merchant"):
        started.append(
            {"merchant_id": merchant_id, "subject_type": subject_type,
             "product_keys": product_keys}
        )
        return "run-url-1"

    async def fake_completed(*, run_id, status, **kw):
        completed.append({"run_id": run_id, "status": status, **kw})

    async def fake_brand_report(**kwargs):
        return {
            "aggregate": {
                "avg_visibility": 40, "avg_attribution": 10,
                "avg_category_visibility": 55,
            },
            "per_product": [
                {"verdict": {"label": "VISIBLE"},
                 "upstream_status": {"is_real": True}}
            ],
        }

    @asynccontextmanager
    async def fake_telemetry(*, run_id, merchant_id):
        yield

    monkeypatch.setattr(mar, "count_runs_for_merchant_by_subject", fake_count)
    monkeypatch.setattr(mar, "get_merchant_onboarding", fake_onboarding)
    monkeypatch.setattr(mar, "record_audit_run_started", fake_started)
    monkeypatch.setattr(mar, "record_audit_run_completed", fake_completed)
    monkeypatch.setattr(mar, "run_brand_report", fake_brand_report)
    monkeypatch.setattr(bdcs, "discover_products_for_audit", fake_discover)
    monkeypatch.setattr(atc, "audit_telemetry", fake_telemetry)

    app = FastAPI()
    app.include_router(mar.router)
    app.dependency_overrides[auth_module.get_current_merchant] = lambda: "merch-A"
    c = TestClient(app)
    c.state, c.started, c.completed = state, started, completed
    return c


_URL = "/api/merchant-center/audit/url-readiness"


def test_url_audit_happy_path(client):
    client.state["used"] = 0
    res = client.post(_URL, json={})
    assert res.status_code == 200
    body = res.json()
    assert body["tier"] == "url_wedge"
    assert body["audited_url"] == "https://merch.example"  # from store_url
    assert body["free_audits_used"] == 1
    assert body["free_audits_remaining"] == 1
    assert body["brand_report"]["aggregate"]["avg_visibility"] == 40
    assert body["discovery"]["products_audited"] == 2
    # recorded with the wedge marker + completed succeeded
    assert client.started[0]["subject_type"] == "merchant_url"
    assert client.completed[-1]["status"] == "succeeded"


def test_free_cap_blocks_at_limit(client):
    client.state["used"] = 2
    res = client.post(_URL, json={})
    assert res.status_code == 402
    assert res.json()["detail"]["code"] == "free_audit_limit_reached"
    # blocked before any crawl/LLM/record work
    assert client.started == []


def test_explicit_url_override(client):
    client.state["used"] = 0
    res = client.post(_URL, json={"url": "https://other.example"})
    assert res.status_code == 200
    assert res.json()["audited_url"] == "https://other.example"


def test_no_store_url_422(client, monkeypatch):
    client.state["used"] = 0

    async def empty_onboarding(merchant_id):
        return {}

    monkeypatch.setattr(mar, "get_merchant_onboarding", empty_onboarding)
    res = client.post(_URL, json={})
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "no_store_url"


def test_discovery_failure_422(client, monkeypatch):
    client.state["used"] = 0

    async def boom(url, *, max_products=3, market="US", persist=False):
        raise bdcs.BrandDiscoveryError("could not reach storefront")

    monkeypatch.setattr(bdcs, "discover_products_for_audit", boom)
    res = client.post(_URL, json={})
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "discovery_failed"
    assert client.started == []  # no run recorded on discovery failure
