"""Phase A wedge — POST /api/merchant-center/audit/url-readiness (ASYNC).

Merchant-CURATED + fire-and-poll: the merchant gives us their site + up to 5
product URLs; we fetch each, then kick the audit off in the BACKGROUND and
return a run_id immediately (grounded probes can take minutes). The client
polls GET /url-readiness/{run_id}. This file covers the three pieces:
  - POST: cap / fetch / validation / brand context / 202 "running" shape
  - _run_wedge_audit_background: runs the report + honesty scrub + persists
  - GET: polls the run (running / succeeded / failed / not-owned)
"""
from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import routes.merchant_audit_routes as mar
import services.audit_telemetry_context as atc
import services.bd_cold_start_service as bdcs
from utils import auth as auth_module


def _canned_report():
    # Includes the canned industry_context + buried hedge the wedge must scrub.
    return {
        "merchant_name": "Merch",
        "industry_context": {"market_size_billions_usd": 6500},
        "aggregate": {
            "avg_visibility": 40, "avg_attribution": 10,
            "avg_category_visibility": 55,
            "brand_verdict_label": "INVISIBLE",
            "brand_verdict_explanation": (
                "None of 3 buyer-intent queries cited your url. Gemini "
                "grounded its answers in third-party sources including "
                "iherb.com. We did not verify whether those sources mention "
                "your brand or products. Possible causes are covered by the "
                "action items below."
            ),
        },
        "per_product": [
            {"verdict": {"label": "INVISIBLE"},
             "industry_context": {"blurb": "canned"},
             "upstream_status": {"is_real": True}}
        ],
    }


@pytest.fixture
def client(monkeypatch):
    state = {"used": 0}
    started: list = []
    completed: list = []
    brand_calls: list = []
    sku_intel_calls: list = []

    async def fake_count(*, merchant_id, subject_type):
        return state["used"]

    async def fake_onboarding(merchant_id):
        return {"store_url": "https://merch.example", "business_name": "Merch"}

    async def fake_fetch(pdp_url):
        if not pdp_url.startswith("http"):
            return None, f"{pdp_url!r} is not a valid URL"
        handle = pdp_url.rstrip("/").rsplit("/", 1)[-1]
        return (
            {"title": f"Product {handle.upper()}",
             "raw_title": f"[Bundle] Product {handle.upper()}, 2 pack",
             "pdp_url": pdp_url, "vendor": "Merch",
             "product_type": "Supplements"},
            None,
        )

    async def fake_started(*, merchant_id, product_keys, subject_type="merchant"):
        started.append({"merchant_id": merchant_id, "subject_type": subject_type,
                        "product_keys": product_keys})
        return "run-url-1"

    async def fake_completed(*, run_id, status, **kw):
        completed.append({"run_id": run_id, "status": status, **kw})

    async def fake_brand_report(**kwargs):
        brand_calls.append(kwargs)
        return _canned_report()

    async def fake_sku_intelligence(**kwargs):
        sku_intel_calls.append(kwargs)
        return {
            "hero_sku": {
                "title": (kwargs.get("hero_product") or {}).get("title"),
                "pdp_url": (kwargs.get("hero_product") or {}).get("pdp_url"),
                "vendor": (kwargs.get("hero_product") or {}).get("vendor"),
            },
            "headline": "Nobody owns `test lane` yet, and your product is exactly that - own it.",
            "intent_ladder": {},
            "top_open_lanes": [{
                "query": "test lane",
                "first_move": "Add a PDP section + FAQ for this lane",
            }],
            "substitution_alert": {"present": False},
            "prompt_matrix": [],
            "demand_state_summary": "open lane detected",
            "coverage": {},
            "is_empty": False,
        }

    @asynccontextmanager
    async def fake_telemetry(*, run_id, merchant_id):
        yield

    # Don't actually launch the background task in POST tests — close the
    # coroutine so it doesn't run (or warn). The runner is tested directly
    # below. Patch the indirection, NOT global asyncio.create_task (which
    # pytest-asyncio itself uses to drive the async tests).
    def fake_schedule(coro):
        coro.close()

    monkeypatch.setattr(mar, "_schedule_wedge_audit", fake_schedule)
    monkeypatch.setattr(mar, "count_runs_for_merchant_by_subject", fake_count)
    monkeypatch.setattr(mar, "get_merchant_onboarding", fake_onboarding)
    monkeypatch.setattr(mar, "record_audit_run_started", fake_started)
    monkeypatch.setattr(mar, "record_audit_run_completed", fake_completed)
    monkeypatch.setattr(mar, "run_brand_report", fake_brand_report)
    monkeypatch.setattr(mar, "run_wedge_hero_sku_intelligence", fake_sku_intelligence)
    monkeypatch.setattr(bdcs, "fetch_curated_audit_product", fake_fetch)
    monkeypatch.setattr(atc, "audit_telemetry", fake_telemetry)
    monkeypatch.setattr(mar, "_FREE_URL_AUDITS_PER_MERCHANT", 2)
    monkeypatch.setattr(mar, "_WEDGE_MAX_RUNS", 2)

    app = FastAPI()
    app.include_router(mar.router)
    app.dependency_overrides[auth_module.get_current_merchant] = lambda: "merch-A"
    c = TestClient(app)
    c.state, c.started, c.completed, c.brand_calls, c.sku_intel_calls = (
        state, started, completed, brand_calls, sku_intel_calls,
    )
    return c


_URL = "/api/merchant-center/audit/url-readiness"
_BODY = {
    "product_urls": [
        "https://merch.example/products/a",
        "https://merch.example/products/b",
    ],
}


# --- POST: kickoff -----------------------------------------------------------
def test_post_returns_running_with_run_id(client):
    client.state["used"] = 0
    res = client.post(_URL, json=_BODY)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "running"
    assert body["run_id"] == "run-url-1"
    assert body["brand_report"] is None  # not ready yet
    assert body["tier"] == "url_wedge"
    # Immediately-known fields are echoed so the UI can render context.
    assert [p["pdp_url"] for p in body["audited_products"]] == _BODY["product_urls"]
    assert [p["raw_title"] for p in body["audited_products"]] == [
        "[Bundle] Product A, 2 pack",
        "[Bundle] Product B, 2 pack",
    ]
    assert body["methodology"]["products_audited"] == 2
    assert body["methodology"]["queries_per_product"] == 2
    assert body["free_audits_remaining"] == 1
    # Run recorded with the wedge marker + the provided URLs before kickoff.
    assert client.started[0]["subject_type"] == "merchant_url"
    assert client.started[0]["product_keys"] == _BODY["product_urls"]


def test_post_vendor_fallback_when_absent(client, monkeypatch):
    client.state["used"] = 0
    captured_runs: list = []

    def capture_background_run(**kwargs):
        captured_runs.append(kwargs)

        async def noop():
            return None

        return noop()

    async def fetch_without_vendor(pdp_url):
        handle = pdp_url.rstrip("/").rsplit("/", 1)[-1]
        return (
            {"title": f"Product {handle.upper()}",
             "raw_title": f"[Bundle] Product {handle.upper()}, 2 pack",
             "pdp_url": pdp_url,
             "product_type": "Supplements"},
            None,
        )

    monkeypatch.setattr(mar, "_run_wedge_audit_background", capture_background_run)
    monkeypatch.setattr(bdcs, "fetch_curated_audit_product", fetch_without_vendor)

    body = client.post(_URL, json=_BODY).json()
    assert [p["title"] for p in body["audited_products"]] == [
        "Product A", "Product B",
    ]
    assert [p["vendor"] for p in body["audited_products"]] == ["Merch", "Merch"]
    assert [p["title"] for p in captured_runs[-1]["audit_products"]] == [
        "Product A", "Product B",
    ]
    assert [p["vendor"] for p in captured_runs[-1]["audit_products"]] == [
        "Merch", "Merch",
    ]

    async def fetch_with_vendor(pdp_url):
        handle = pdp_url.rstrip("/").rsplit("/", 1)[-1]
        return (
            {"title": f"Product {handle.upper()}",
             "raw_title": f"[Bundle] Product {handle.upper()}, 2 pack",
             "pdp_url": pdp_url,
             "vendor": "Fetched Brand",
             "product_type": "Supplements"},
            None,
        )

    monkeypatch.setattr(bdcs, "fetch_curated_audit_product", fetch_with_vendor)
    body = client.post(_URL, json=_BODY).json()
    assert [p["title"] for p in body["audited_products"]] == [
        "Product A", "Product B",
    ]
    assert [p["vendor"] for p in body["audited_products"]] == [
        "Fetched Brand", "Fetched Brand",
    ]
    assert [p["vendor"] for p in captured_runs[-1]["audit_products"]] == [
        "Fetched Brand", "Fetched Brand",
    ]


def test_free_cap_blocks_at_limit(client):
    client.state["used"] = 2
    res = client.post(_URL, json=_BODY)
    assert res.status_code == 402
    assert res.json()["detail"]["code"] == "free_audit_limit_reached"
    assert client.started == []  # blocked before any fetch/record work


def test_cap_lifted_when_disabled(client, monkeypatch):
    monkeypatch.setattr(mar, "_FREE_URL_AUDITS_PER_MERCHANT", 0)
    client.state["used"] = 99
    body = client.post(_URL, json=_BODY).json()
    assert body["status"] == "running"
    assert body["free_audits_allowed"] is None
    assert body["free_audits_remaining"] is None
    assert body["free_audits_used"] == 100


def test_partial_resolution_audits_what_resolved(client, monkeypatch):
    client.state["used"] = 0

    async def half_fetch(pdp_url):
        if pdp_url.endswith("/bad"):
            return None, "couldn't read a product from that URL"
        return ({"title": "Good", "pdp_url": pdp_url,
                 "vendor": "Merch", "product_type": "X"}, None)

    monkeypatch.setattr(bdcs, "fetch_curated_audit_product", half_fetch)
    body = client.post(_URL, json={
        "product_urls": ["https://merch.example/products/good",
                         "https://merch.example/products/bad"],
    }).json()
    assert len(body["audited_products"]) == 1
    assert body["methodology"]["products_audited"] == 1
    assert body["methodology"]["unresolved_urls"][0]["url"].endswith("/bad")


def test_all_unresolved_422(client, monkeypatch):
    client.state["used"] = 0

    async def none_fetch(pdp_url):
        return None, "couldn't read a product"

    monkeypatch.setattr(bdcs, "fetch_curated_audit_product", none_fetch)
    res = client.post(_URL, json=_BODY)
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "no_products_resolved"
    assert client.started == []


def test_website_defaults_to_store_url(client):
    client.state["used"] = 0
    body = client.post(_URL, json=_BODY).json()
    assert body["audited_url"] == "https://merch.example"


def test_product_urls_required(client):
    client.state["used"] = 0
    assert client.post(_URL, json={}).status_code == 422
    assert client.post(_URL, json={"product_urls": []}).status_code == 422
    too_many = [f"https://m.example/p/{i}" for i in range(6)]
    assert client.post(_URL, json={"product_urls": too_many}).status_code == 422


# --- background runner -------------------------------------------------------
@pytest.mark.asyncio
async def test_background_runner_persists_scrubbed_result(client):
    # client fixture wired the mocks; drive the runner directly.
    base_payload = {
        "audit_run_id": "run-url-1", "audited_url": "https://merch.example",
        "tier": "url_wedge", "audited_products": [],
        "methodology": {"model": "merchant_curated"},
        "free_audits_allowed": 2, "free_audits_used": 1,
        "free_audits_remaining": 1,
    }
    await mar._run_wedge_audit_background(
        run_id="run-url-1", merchant_id="merch-A", merchant_name="BB Lab",
        merchant_domain="bblab.shop",
        audit_products=[{"title": "P", "pdp_url": "https://m/p/1",
                         "vendor": "BB Lab", "product_type": "X"}],
        base_payload=base_payload,
    )
    # Wedge opts into bounded concurrency + parallel scan modes.
    call = client.brand_calls[0]
    assert call["coverage_profile"] == "gemini_deepseek"
    assert call["product_concurrency"] == 1
    assert call["parallel_scan_modes"] is True
    assert call["max_runs"] == 2
    # Persisted as succeeded with the FULL payload in report_jsonb.
    done = client.completed[-1]
    assert done["status"] == "succeeded"
    payload = done["report_jsonb"]
    assert payload["status"] == "succeeded"
    assert payload["tier"] == "url_wedge"
    report = payload["brand_report"]
    assert payload["sku_intelligence"]["headline"].startswith("Nobody owns")
    assert client.sku_intel_calls[0]["coverage_profile"] == "gemini_deepseek"
    assert client.sku_intel_calls[0]["prompts_per_sku"] == 14
    assert client.sku_intel_calls[0]["hero_product"]["title"] == "P"
    # Honesty scrub applied in the runner.
    assert "industry_context" not in report
    assert "industry_context" not in report["per_product"][0]
    expl = report["aggregate"]["brand_verdict_explanation"]
    assert "did not verify" not in expl
    assert "iherb.com" in expl


@pytest.mark.asyncio
async def test_background_runner_selects_first_product_with_attributes(client, monkeypatch):
    seen: list = []

    async def fake_sku_intelligence(**kwargs):
        seen.append(kwargs)
        return {"is_empty": False, "headline": "ok"}

    monkeypatch.setattr(mar, "run_wedge_hero_sku_intelligence", fake_sku_intelligence)

    await mar._run_wedge_audit_background(
        run_id="run-url-1",
        merchant_id="merch-A",
        merchant_name="BB Lab",
        merchant_domain="bblab.shop",
        audit_products=[
            {"title": "No Attrs", "pdp_url": "https://m/p/1", "vendor": "BB Lab"},
            {
                "title": "Hero Attrs",
                "pdp_url": "https://m/p/2",
                "vendor": "BB Lab",
                "attributes_raw": {"tags": ["halal"]},
            },
        ],
        base_payload={},
    )

    assert seen[0]["hero_product"]["title"] == "Hero Attrs"
    assert seen[0]["hero_product"]["_wedge_hero_index"] == 1
    payload = client.completed[-1]["report_jsonb"]
    assert "brand_report" in payload
    assert payload["sku_intelligence"] == {"is_empty": False, "headline": "ok"}


@pytest.mark.asyncio
async def test_background_runner_degrades_when_sku_intelligence_raises(client, monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("hero sku failed")

    monkeypatch.setattr(mar, "run_wedge_hero_sku_intelligence", boom)

    await mar._run_wedge_audit_background(
        run_id="run-url-1",
        merchant_id="merch-A",
        merchant_name="BB Lab",
        merchant_domain="bblab.shop",
        audit_products=[{"title": "P", "pdp_url": "https://m/p/1"}],
        base_payload={},
    )

    done = client.completed[-1]
    assert done["status"] == "succeeded"
    payload = done["report_jsonb"]
    assert "brand_report" in payload
    assert payload["sku_intelligence"]["is_empty"] is True
    assert "hero sku failed" in payload["sku_intelligence"]["error_note"]


@pytest.mark.asyncio
async def test_background_runner_records_failure(client, monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("upstream exploded")

    monkeypatch.setattr(mar, "run_brand_report", boom)
    await mar._run_wedge_audit_background(
        run_id="run-url-1", merchant_id="merch-A", merchant_name="BB Lab",
        merchant_domain="bblab.shop",
        audit_products=[{"title": "P", "pdp_url": "https://m/p/1"}],
        base_payload={},
    )
    done = client.completed[-1]
    assert done["status"] == "failed"
    assert "upstream exploded" in done["error_message"]


# --- GET: poll ---------------------------------------------------------------
def _get_client(monkeypatch, row):
    completed: list = []

    async def fake_fetch_run(*, run_id):
        return row

    async def fake_completed(*, run_id, status, **kw):
        completed.append({"run_id": run_id, "status": status, **kw})

    monkeypatch.setattr(mar, "fetch_audit_run_by_id", fake_fetch_run)
    monkeypatch.setattr(mar, "record_audit_run_completed", fake_completed)
    monkeypatch.setattr(mar, "_WEDGE_RUN_STALE_TTL_S", 900)
    app = FastAPI()
    app.include_router(mar.router)
    app.dependency_overrides[auth_module.get_current_merchant] = lambda: "merch-A"
    c = TestClient(app)
    c.completed = completed
    return c


def test_get_running(monkeypatch):
    c = _get_client(monkeypatch, {
        "merchant_id": "merch-A", "subject_type": "merchant_url",
        "status": "running", "report_jsonb": None,
    })
    body = c.get(f"{_URL}/run-url-1").json()
    assert body == {"status": "running", "run_id": "run-url-1"}
    assert c.completed == []


def test_get_running_fresh_requested_at_not_stale(monkeypatch):
    requested_at = datetime.now(timezone.utc).isoformat()
    c = _get_client(monkeypatch, {
        "merchant_id": "merch-A", "subject_type": "merchant_url",
        "status": "running", "report_jsonb": None,
        "requested_at": requested_at,
    })
    body = c.get(f"{_URL}/run-url-1").json()
    assert body == {"status": "running", "run_id": "run-url-1"}
    assert c.completed == []


def test_get_running_stale_requested_at_fails(monkeypatch):
    requested_at = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    c = _get_client(monkeypatch, {
        "merchant_id": "merch-A", "subject_type": "merchant_url",
        "status": "running", "report_jsonb": None,
        "requested_at": requested_at,
    })
    body = c.get(f"{_URL}/run-url-1").json()
    assert body == {
        "status": "failed",
        "run_id": "run-url-1",
        "error": "This audit didn't finish in time — please re-run.",
    }
    assert c.completed == [{
        "run_id": "run-url-1",
        "status": "failed",
        "error_message": "audit_timed_out_stale",
    }]


def test_get_succeeded_returns_payload(monkeypatch):
    payload = {"status": "succeeded", "run_id": "run-url-1",
               "tier": "url_wedge", "brand_report": {"aggregate": {}}}
    c = _get_client(monkeypatch, {
        "merchant_id": "merch-A", "subject_type": "merchant_url",
        "status": "succeeded", "report_jsonb": payload,
    })
    assert c.get(f"{_URL}/run-url-1").json() == payload


def test_get_failed_maps_mock_fallback(monkeypatch):
    c = _get_client(monkeypatch, {
        "merchant_id": "merch-A", "subject_type": "merchant_url",
        "status": "failed", "error_message": "upstream_mock_fallback",
    })
    body = c.get(f"{_URL}/run-url-1").json()
    assert body["status"] == "failed"
    assert "fallback data" in body["error"]


def test_get_scoped_to_merchant_and_subject(monkeypatch):
    # other merchant's run → 404
    c = _get_client(monkeypatch, {
        "merchant_id": "OTHER", "subject_type": "merchant_url",
        "status": "succeeded", "report_jsonb": {},
    })
    assert c.get(f"{_URL}/run-url-1").status_code == 404
    # a synced (non-wedge) run → 404
    c2 = _get_client(monkeypatch, {
        "merchant_id": "merch-A", "subject_type": "merchant",
        "status": "succeeded", "report_jsonb": {},
    })
    assert c2.get(f"{_URL}/run-url-1").status_code == 404


def test_get_missing_run_404(monkeypatch):
    c = _get_client(monkeypatch, None)
    assert c.get(f"{_URL}/run-url-1").status_code == 404
