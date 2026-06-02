"""Phase A: free URL-audit wedge — POST /api/merchant-center/audit/url-readiness.

Merchant-CURATED model: the merchant gives us their site + up to 5 product URLs
(their hero SKUs); we FETCH each for clean data and audit exactly those — no
auto-discovery. Free for the first N per merchant, then 402. The wedge also
post-processes the report for honesty (strips canned industry_context + the
"we did not verify…" hedge) and ships an upfront methodology disclosure.
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
    brand_calls: list = []

    async def fake_count(*, merchant_id, subject_type):
        return state["used"]

    async def fake_onboarding(merchant_id):
        return {"store_url": "https://merch.example", "business_name": "Merch"}

    async def fake_fetch(pdp_url):
        # Resolve every https URL to a clean product; flag anything else.
        if not pdp_url.startswith("http"):
            return None, f"{pdp_url!r} is not a valid URL"
        handle = pdp_url.rstrip("/").rsplit("/", 1)[-1]
        return (
            {
                "title": f"Product {handle.upper()}",
                "pdp_url": pdp_url,
                "vendor": "Merch",
                "product_type": "Supplements",
            },
            None,
        )

    async def fake_started(*, merchant_id, product_keys, subject_type="merchant"):
        started.append(
            {"merchant_id": merchant_id, "subject_type": subject_type,
             "product_keys": product_keys}
        )
        return "run-url-1"

    async def fake_completed(*, run_id, status, **kw):
        completed.append({"run_id": run_id, "status": status, **kw})

    async def fake_brand_report(**kwargs):
        brand_calls.append(kwargs)
        return {
            "merchant_name": kwargs.get("merchant_name"),
            # Canned template + buried hedge the wedge must scrub out.
            "industry_context": {"market_size_billions_usd": 6500},
            "aggregate": {
                "avg_visibility": 40, "avg_attribution": 10,
                "avg_category_visibility": 55,
                "brand_verdict_label": "INVISIBLE",
                "brand_verdict_explanation": (
                    "None of 3 buyer-intent queries cited your url. Gemini "
                    "grounded its answers in third-party sources including "
                    "iherb.com. We did not verify whether those sources "
                    "mention your brand or products. Possible causes are "
                    "covered by the action items below."
                ),
            },
            "per_product": [
                {"verdict": {"label": "INVISIBLE"},
                 "industry_context": {"blurb": "canned"},
                 "explanation": (
                     "Across the queries we tested, your URL did not appear "
                     "in any grounded source we did not verify."
                 ),
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
    monkeypatch.setattr(bdcs, "fetch_curated_audit_product", fake_fetch)
    monkeypatch.setattr(atc, "audit_telemetry", fake_telemetry)

    app = FastAPI()
    app.include_router(mar.router)
    app.dependency_overrides[auth_module.get_current_merchant] = lambda: "merch-A"
    c = TestClient(app)
    c.state, c.started, c.completed, c.brand_calls = (
        state, started, completed, brand_calls,
    )
    return c


_URL = "/api/merchant-center/audit/url-readiness"
_BODY = {
    "product_urls": [
        "https://merch.example/products/a",
        "https://merch.example/products/b",
    ],
}


def test_curated_happy_path(client):
    client.state["used"] = 0
    res = client.post(_URL, json=_BODY)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["tier"] == "url_wedge"
    assert body["free_audits_used"] == 1
    assert body["free_audits_remaining"] == 1
    # Audits EXACTLY the two URLs the merchant provided — no discovery.
    assert [p["pdp_url"] for p in body["audited_products"]] == _BODY["product_urls"]
    assert body["methodology"]["model"] == "merchant_curated"
    assert body["methodology"]["products_audited"] == 2
    assert body["methodology"]["products_requested"] == 2
    assert body["methodology"]["queries_per_product"] == 3
    assert body["methodology"]["unresolved_urls"] == []
    # The fetched products (not crawled) flow into the brand report.
    call = client.brand_calls[0]
    assert [p["pdp_url"] for p in call["products"]] == _BODY["product_urls"]
    assert call["products"][0]["title"] == "Product A"
    # Recorded with the wedge marker + the provided URLs as product_keys.
    assert client.started[0]["subject_type"] == "merchant_url"
    assert client.started[0]["product_keys"] == _BODY["product_urls"]
    assert client.completed[-1]["status"] == "succeeded"


def test_honesty_scrub_strips_industry_context_and_hedge(client):
    client.state["used"] = 0
    body = client.post(_URL, json=_BODY).json()
    report = body["brand_report"]
    # Canned industry_context removed at every level.
    assert "industry_context" not in report
    assert "industry_context" not in report["per_product"][0]
    # The "we did not verify…" hedge is gone; the actionable remainder stays.
    expl = report["aggregate"]["brand_verdict_explanation"]
    assert "did not verify" not in expl
    assert "Possible causes are covered by the action items below." in expl
    assert "iherb.com" in expl  # real evidence kept
    assert "did not verify" not in report["per_product"][0]["explanation"]
    # The limitation is stated ONCE, upfront, in methodology instead.
    limitations = " ".join(body["methodology"]["limitations"])
    assert "not found in this sample" in limitations
    assert "verified" in limitations


def test_free_cap_blocks_at_limit(client):
    client.state["used"] = 2
    res = client.post(_URL, json=_BODY)
    assert res.status_code == 402
    assert res.json()["detail"]["code"] == "free_audit_limit_reached"
    # Blocked before any fetch/LLM/record work.
    assert client.started == []
    assert client.brand_calls == []


def test_partial_resolution_audits_what_resolved(client, monkeypatch):
    client.state["used"] = 0

    async def half_fetch(pdp_url):
        if pdp_url.endswith("/bad"):
            return None, "couldn't read a product from that URL"
        return (
            {"title": "Good", "pdp_url": pdp_url,
             "vendor": "Merch", "product_type": "X"},
            None,
        )

    monkeypatch.setattr(bdcs, "fetch_curated_audit_product", half_fetch)
    res = client.post(_URL, json={
        "product_urls": [
            "https://merch.example/products/good",
            "https://merch.example/products/bad",
        ],
    })
    assert res.status_code == 200, res.text
    body = res.json()
    assert len(body["audited_products"]) == 1
    assert body["methodology"]["products_audited"] == 1
    assert body["methodology"]["products_requested"] == 2
    assert body["methodology"]["unresolved_urls"][0]["url"].endswith("/bad")


def test_all_unresolved_422(client, monkeypatch):
    client.state["used"] = 0

    async def none_fetch(pdp_url):
        return None, "couldn't read a product"

    monkeypatch.setattr(bdcs, "fetch_curated_audit_product", none_fetch)
    res = client.post(_URL, json=_BODY)
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "no_products_resolved"
    assert client.started == []  # no run recorded when nothing resolves


def test_website_defaults_to_store_url(client):
    client.state["used"] = 0
    body = client.post(_URL, json=_BODY).json()
    assert body["audited_url"] == "https://merch.example"  # from store_url


def test_explicit_brand_and_website_override(client):
    client.state["used"] = 0
    res = client.post(_URL, json={
        **_BODY,
        "website": "https://other.example",
        "brand": "BB Lab",
    })
    body = res.json()
    assert body["audited_url"] == "https://other.example"
    # Merchant-supplied brand wins as the report's merchant_name.
    assert client.brand_calls[0]["merchant_name"] == "BB Lab"


def test_product_urls_required(client):
    client.state["used"] = 0
    assert client.post(_URL, json={}).status_code == 422  # missing product_urls
    assert client.post(_URL, json={"product_urls": []}).status_code == 422
    too_many = [f"https://m.example/p/{i}" for i in range(6)]
    assert client.post(_URL, json={"product_urls": too_many}).status_code == 422
