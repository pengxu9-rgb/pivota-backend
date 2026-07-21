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
    # balance defaults to plenty of credits on a paid tier so the metered path
    # (used >= free limit) succeeds unless a test overrides it.
    state = {"used": 0, "balance": {"credits": 100000, "plan_tier": "growth"}}
    started: list = []
    completed: list = []
    enqueued: list = []
    brand_calls: list = []
    sku_intel_calls: list = []
    credit_ops: list = []

    async def fake_count(*, merchant_id, subject_type):
        return state["used"]

    async def fake_get_balance(merchant_id):
        return state["balance"]

    import services.credit_consumption_service as _ccs_mod

    async def fake_consume(merchant_id, operation_type, idempotency_key, *,
                           probes=None, credits=None, usd_cogs=None, conn=None):
        credit_ops.append({"kind": "consume", "merchant_id": merchant_id,
                           "op": operation_type, "credits": credits,
                           "key": idempotency_key})
        return {"credits": credits, "category": "audit"}

    async def fake_refund(merchant_id, operation_type, credits, source_event_id, *,
                          usd_cogs=0, conn=None):
        credit_ops.append({"kind": "refund", "credits": credits,
                           "key": source_event_id})
        return {"credits": credits}

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

    async def fake_enqueue(*, merchant_id, product_keys, subject_type="merchant",
                           idempotency_key=None, requested_by_user_id=None,
                           request_options_jsonb=None):
        enqueued.append({
            "merchant_id": merchant_id, "subject_type": subject_type,
            "product_keys": product_keys, "idempotency_key": idempotency_key,
            "request_options_jsonb": request_options_jsonb,
        })
        return "run-url-1", False

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
    monkeypatch.setattr(mar, "enqueue_audit_run_with_replay", fake_enqueue)
    monkeypatch.setattr(mar, "record_audit_run_completed", fake_completed)
    monkeypatch.setattr(mar, "run_brand_report", fake_brand_report)
    monkeypatch.setattr(mar, "run_wedge_hero_sku_intelligence", fake_sku_intelligence)
    monkeypatch.setattr(bdcs, "fetch_curated_audit_product", fake_fetch)
    monkeypatch.setattr(atc, "audit_telemetry", fake_telemetry)
    monkeypatch.setattr(mar, "_FREE_URL_AUDITS_PER_MERCHANT", 2)
    monkeypatch.setattr(mar, "_WEDGE_MAX_RUNS", 2)
    monkeypatch.setattr(mar, "get_balance", fake_get_balance)
    monkeypatch.setattr(_ccs_mod, "consume", fake_consume)
    monkeypatch.setattr(_ccs_mod, "refund", fake_refund)

    app = FastAPI()
    app.include_router(mar.router)
    app.dependency_overrides[auth_module.get_current_merchant] = lambda: "merch-A"
    c = TestClient(app)
    c.state, c.started, c.completed, c.brand_calls, c.sku_intel_calls = (
        state, started, completed, brand_calls, sku_intel_calls,
    )
    c.credit_ops = credit_ops
    c.enqueued = enqueued
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
    assert body["tier"] == "url_per_sku"
    # Immediately-known fields are echoed so the UI can render context.
    assert [p["pdp_url"] for p in body["audited_products"]] == _BODY["product_urls"]
    assert [p["raw_title"] for p in body["audited_products"]] == [
        "[Bundle] Product A, 2 pack",
        "[Bundle] Product B, 2 pack",
    ]
    assert body["methodology"]["products_audited"] == 2
    assert body["methodology"]["queries_per_product"] == mar._WEDGE_PROMPTS_PER_SKU
    assert body["free_audits_remaining"] == 1
    # Enqueued on the durable worker (not the bare-asyncio runner) with the
    # wedge marker + per_sku launch + synthetic products (NOT raw URLs as keys).
    enq = client.enqueued[-1]
    assert enq["subject_type"] == "merchant_url"
    assert all(k.startswith("urlwedge:") for k in enq["product_keys"])
    launch = enq["request_options_jsonb"]["launch"]
    assert launch["audit_mode"] == "per_sku"
    # Stub merchant is growth (paid) -> gets the paid provider set (gemini + chatgpt).
    assert launch["providers"] == mar._WEDGE_PROVIDERS + mar._WEDGE_PAID_PROVIDERS
    assert [s["pdp_url"] for s in launch["synthetic_products"]] == _BODY["product_urls"]
    # No custom prompts by default.
    assert launch["custom_prompts"] == []


def test_custom_prompts_dedupe_into_launch(client):
    client.state["used"] = 0
    res = client.post(_URL, json={
        **_BODY,
        "custom_prompts": [
            "best collagen jelly for glow",
            "  best collagen jelly for glow  ",  # dup (trim/case)
            "vitamin c gummies for travel",
            "",  # blank dropped
        ],
    })
    assert res.status_code == 200, res.text
    launch = client.enqueued[-1]["request_options_jsonb"]["launch"]
    # Probed once brand-level; deduped + trimmed; blanks dropped.
    assert launch["custom_prompts"] == [
        "best collagen jelly for glow",
        "vitamin c gummies for travel",
    ]


# --- POST: per-SKU merchant prompts (custom_prompts_by_url) -------------------
def test_custom_prompts_by_url_rekeyed_to_synthetic_sku(client):
    client.state["used"] = 0
    res = client.post(_URL, json={
        **_BODY,
        "custom_prompts_by_url": {
            "https://merch.example/products/b": [
                "  collagen jelly for red-eye flights ",
                "collagen jelly for red-eye flights",  # dup (trim/case)
                "",  # blank dropped
            ],
        },
    })
    assert res.status_code == 200, res.text
    launch = client.enqueued[-1]["request_options_jsonb"]["launch"]
    by_sku = launch["custom_prompts_by_sku"]
    # Re-keyed from the submitted URL to the minted synthetic sku_key of
    # EXACTLY that product (products/b is the second synthetic product).
    sku_b = launch["synthetic_products"][1]["sku_key"]
    assert launch["synthetic_products"][1]["pdp_url"].endswith("/products/b")
    assert by_sku == {sku_b: ["collagen jelly for red-eye flights"]}
    # Brand-level slots unaffected.
    assert launch["custom_prompts"] == []


def test_custom_prompts_by_url_unknown_key_422(client):
    client.state["used"] = 0
    res = client.post(_URL, json={
        **_BODY,
        "custom_prompts_by_url": {
            "https://elsewhere.example/products/z": ["some prompt"],
        },
    })
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "custom_prompts_url_mismatch"
    assert client.enqueued == []


def test_custom_prompts_by_url_per_product_cap_422(client, monkeypatch):
    monkeypatch.setattr(mar, "_WEDGE_CUSTOM_PROMPTS_PER_SKU", 2)
    client.state["used"] = 0
    res = client.post(_URL, json={
        **_BODY,
        "custom_prompts_by_url": {
            "https://merch.example/products/a": ["p one", "p two", "p three"],
        },
    })
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "custom_prompts_per_product_cap"
    assert client.enqueued == []


def test_custom_prompts_by_url_total_cap_422(client, monkeypatch):
    monkeypatch.setattr(mar, "_WEDGE_CUSTOM_PROMPTS_PER_SKU", 2)
    monkeypatch.setattr(mar, "_WEDGE_CUSTOM_PROMPTS_TOTAL", 3)
    client.state["used"] = 0
    res = client.post(_URL, json={
        **_BODY,
        "custom_prompts_by_url": {
            "https://merch.example/products/a": ["a one", "a two"],
            "https://merch.example/products/b": ["b one", "b two"],
        },
    })
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "custom_prompts_total_cap"
    assert client.enqueued == []


def test_custom_prompts_by_url_billed_per_probe(client, monkeypatch):
    """Metered path: per-SKU merchant prompts count into per_provider_probes
    exactly like the prompts the worker will actually run."""
    captured = {}

    def fake_estimate(pairs):
        captured["pairs"] = pairs
        return 42, 1.5

    import services.credit_consumption_service as _ccs_mod
    monkeypatch.setattr(_ccs_mod, "estimate_probe_credits", fake_estimate)
    client.state["used"] = 5  # past the free allowance -> metered
    res = client.post(_URL, json={
        **_BODY,
        "custom_prompts": ["brand slot prompt"],
        "custom_prompts_by_url": {
            "https://merch.example/products/a": ["niche a"],
            "https://merch.example/products/b": ["niche b"],
        },
    })
    assert res.status_code == 200, res.text
    expected_per_provider = (
        2 * mar._WEDGE_PROMPTS_PER_SKU  # 2 resolved products
        + 1  # brand-level slot
        + 2  # per-SKU merchant prompts
    )
    assert [p[1] for p in captured["pairs"]] == [
        expected_per_provider for _ in captured["pairs"]
    ]


def test_custom_prompt_too_long_422(client):
    client.state["used"] = 0
    res = client.post(_URL, json={
        **_BODY,
        "custom_prompts_by_url": {
            "https://merch.example/products/a": ["x" * 301],
        },
    })
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "custom_prompt_too_long"
    assert client.enqueued == []


def test_custom_prompts_url_alias_merge_respects_per_product_cap(client, monkeypatch):
    """Two case-variant URLs mint the SAME synthetic sku_key (the key digest
    lowercases pdp_url) — their prompts merge, and the merged list must not
    exceed the per-product cap via the alias loophole."""
    monkeypatch.setattr(mar, "_WEDGE_CUSTOM_PROMPTS_PER_SKU", 2)
    client.state["used"] = 0
    res = client.post(_URL, json={
        "product_urls": [
            "https://merch.example/products/a",
            "https://merch.example/products/A",
        ],
        "custom_prompts_by_url": {
            "https://merch.example/products/a": ["p one", "p two"],
            "https://merch.example/products/A": ["p three", "p four"],
        },
    })
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "custom_prompts_per_product_cap"


def test_pinned_basis_prices_reprobed_customs(client, monkeypatch):
    """Review round (billing drift): a pinned re-run reprobes the prior run's
    FULL pinned set (auto + previously pinned merchant prompts), so pricing
    uses the recovered pinned set — not the static planned budget — plus only
    the NEW prompts not already pinned."""
    captured = {}

    def fake_estimate(pairs):
        captured["pairs"] = pairs
        return 42, 1.5

    import services.credit_consumption_service as _ccs_mod
    monkeypatch.setattr(_ccs_mod, "estimate_probe_credits", fake_estimate)

    pinned_a = [f"auto prompt {i}" for i in range(14)] + [
        "already pinned niche", "second pinned niche",
    ]

    async def fake_pinned(merchant_id, sku_keys, max_reports=3):
        # Product A has a 16-spec pinned basis; product B has none (first run).
        return {sku_keys[0]: [q.lower() for q in pinned_a]}

    monkeypatch.setattr(mar, "_pinned_selected_queries_by_sku", fake_pinned)
    client.state["used"] = 5  # metered
    res = client.post(_URL, json={
        **_BODY,
        "custom_prompts_by_url": {
            # One already in A's pinned set (free — it reprobes anyway),
            # one genuinely new (priced).
            "https://merch.example/products/a": [
                "Already Pinned Niche", "brand new niche",
            ],
        },
    })
    assert res.status_code == 200, res.text
    # A: 16 pinned + 1 new custom; B: planned budget (no basis).
    expected = 16 + 1 + mar._WEDGE_PROMPTS_PER_SKU
    assert [p[1] for p in captured["pairs"]] == [
        expected for _ in captured["pairs"]
    ]


def test_refresh_run_prices_planned_budget_not_pinned(client, monkeypatch):
    """refresh=True regenerates the basis — pricing must NOT read the pinned
    set (the scan is skipped entirely)."""
    captured = {}

    def fake_estimate(pairs):
        captured["pairs"] = pairs
        return 42, 1.5

    async def fake_pinned(merchant_id, sku_keys, max_reports=3):
        raise AssertionError("pinned-basis scan must be skipped on refresh")

    import services.credit_consumption_service as _ccs_mod
    monkeypatch.setattr(_ccs_mod, "estimate_probe_credits", fake_estimate)
    monkeypatch.setattr(mar, "_pinned_selected_queries_by_sku", fake_pinned)
    client.state["used"] = 5
    res = client.post(_URL, json={**_BODY, "refresh": True})
    assert res.status_code == 200, res.text
    expected = 2 * mar._WEDGE_PROMPTS_PER_SKU
    assert [p[1] for p in captured["pairs"]] == [
        expected for _ in captured["pairs"]
    ]


def test_idempotency_key_varies_with_custom_prompts(client):
    """Review round: re-POSTing the same URLs with DIFFERENT prompts must not
    replay the in-flight run (which would silently drop the new prompts).
    Same URLs + same prompts still dedupe; no-customs keys are unchanged."""
    client.state["used"] = 0
    body_a = {
        **_BODY,
        "custom_prompts_by_url": {
            "https://merch.example/products/a": ["niche one"],
        },
    }
    assert client.post(_URL, json=body_a).status_code == 200
    assert client.post(_URL, json=body_a).status_code == 200
    body_b = {
        **_BODY,
        "custom_prompts_by_url": {
            "https://merch.example/products/a": ["a different niche"],
        },
    }
    assert client.post(_URL, json=body_b).status_code == 200
    assert client.post(_URL, json=_BODY).status_code == 200  # no customs
    keys = [e["idempotency_key"] for e in client.enqueued]
    assert keys[0] == keys[1], "identical requests must share a dedup key"
    assert keys[2] != keys[0], "different prompts must mint a new key"
    assert keys[3] != keys[0], "customs key differs from the no-customs key"


def test_custom_prompts_on_unresolved_url_not_launched(client):
    """Prompts keyed to a URL that fails to resolve are neither probed nor
    billed — the URL itself is already reported in `unresolved`."""
    client.state["used"] = 0
    res = client.post(_URL, json={
        "product_urls": ["https://merch.example/products/a", "not-a-url"],
        "custom_prompts_by_url": {"not-a-url": ["orphan prompt"]},
    })
    assert res.status_code == 200, res.text
    launch = client.enqueued[-1]["request_options_jsonb"]["launch"]
    assert launch["custom_prompts_by_sku"] == {}
    assert [u["url"] for u in res.json()["methodology"]["unresolved_urls"]] == [
        "not-a-url"
    ]


def test_post_vendor_fallback_when_absent(client, monkeypatch):
    client.state["used"] = 0

    async def fetch_without_vendor(pdp_url):
        handle = pdp_url.rstrip("/").rsplit("/", 1)[-1]
        return (
            {"title": f"Product {handle.upper()}",
             "raw_title": f"[Bundle] Product {handle.upper()}, 2 pack",
             "pdp_url": pdp_url,
             "product_type": "Supplements"},
            None,
        )

    monkeypatch.setattr(bdcs, "fetch_curated_audit_product", fetch_without_vendor)

    body = client.post(_URL, json=_BODY).json()
    assert [p["title"] for p in body["audited_products"]] == [
        "Product A", "Product B",
    ]
    assert [p["vendor"] for p in body["audited_products"]] == ["Merch", "Merch"]
    # The vendor fallback flows into the synthetic products handed to the worker.
    syn = client.enqueued[-1]["request_options_jsonb"]["launch"]["synthetic_products"]
    assert [s["title"] for s in syn] == ["Product A", "Product B"]
    assert [s["vendor"] for s in syn] == [
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
    syn2 = client.enqueued[-1]["request_options_jsonb"]["launch"]["synthetic_products"]
    assert [s["vendor"] for s in syn2] == [
        "Fetched Brand", "Fetched Brand",
    ]


def test_over_free_limit_meters_credits_instead_of_blocking(client):
    # The bug fix: a credited merchant past the free cap is METERED, not 402'd.
    client.state["used"] = 2
    client.state["balance"] = {"credits": 100000, "plan_tier": "growth"}
    res = client.post(_URL, json=_BODY)
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "running"
    assert body["billing_mode"] == "credits"
    assert body["credits_charged"] > 0
    # Free count is NOT consumed by a credit-metered run.
    assert body["free_audits_used"] == 2
    assert body["free_audits_remaining"] == 0
    # Exactly one debit, idempotent on the deterministic key; no refund.
    consumes = [o for o in client.credit_ops if o["kind"] == "consume"]
    assert len(consumes) == 1
    assert consumes[0]["op"] == "audit"
    assert consumes[0]["key"].startswith("url_wedge:")
    assert consumes[0]["credits"] == body["credits_charged"]
    assert not [o for o in client.credit_ops if o["kind"] == "refund"]


def test_over_free_limit_insufficient_credits_free_tier_402(client):
    # Free-tier merchant with no credits past the cap still gets a clear 402.
    client.state["used"] = 2
    client.state["balance"] = {"credits": 0, "plan_tier": "free"}
    res = client.post(_URL, json=_BODY)
    assert res.status_code == 402
    detail = res.json()["detail"]
    assert detail["code"] == "insufficient_credits"
    assert detail["required"] > 0
    assert detail["available"] == 0
    assert client.started == []  # blocked before any run is recorded
    assert not client.credit_ops  # nothing debited


def test_paid_tier_over_free_proceeds_even_if_balance_low(client):
    # Paid tier may run on overage even when the live balance reads low.
    client.state["used"] = 5
    client.state["balance"] = {"credits": 0, "plan_tier": "growth"}
    res = client.post(_URL, json=_BODY)
    assert res.status_code == 200
    assert res.json()["billing_mode"] == "credits"
    assert [o for o in client.credit_ops if o["kind"] == "consume"]


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
    # Schema ceiling is the PAID cap (20); beyond it is a 422 regardless of tier.
    over_schema_cap = [f"https://m.example/p/{i}" for i in range(21)]
    assert client.post(_URL, json={"product_urls": over_schema_cap}).status_code == 422


def test_product_urls_free_tier_cap_is_5(client):
    # The 5-URL cap is tier-enforced in the handler: free plans 422 with an
    # upgrade path, paid plans (the fixture default, "growth") go up to 20.
    client.state["used"] = 0
    client.state["balance"] = {"credits": 0, "plan_tier": "free"}
    six = [f"https://m.example/p/{i}" for i in range(6)]
    res = client.post(_URL, json={"product_urls": six})
    assert res.status_code == 422
    detail = res.json()["detail"]
    assert detail["code"] == "product_cap_exceeded"
    assert detail["cap"] == 5
    assert detail["upgrade_path"]


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
    assert call["coverage_profile"] == "pilot_gemini"
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
    # Hero SKU adds grounded ChatGPT; the brand report stays Gemini-only
    # primary for cost.
    assert client.sku_intel_calls[0]["coverage_profile"] == "us_shopper"
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


def test_get_running_old_request_does_NOT_inline_fail(monkeypatch):
    # Behavior change: URL audits run on the durable worker and legitimately
    # run several minutes. The GET no longer inline-fails an old-but-running
    # run (that split-brained a healthy, actively-leased worker run). Truly
    # abandoned runs are reaped by fail_abandoned_runs() instead.
    requested_at = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    c = _get_client(monkeypatch, {
        "merchant_id": "merch-A", "subject_type": "merchant_url",
        "status": "running", "report_jsonb": None,
        "requested_at": requested_at,
    })
    body = c.get(f"{_URL}/run-url-1").json()
    assert body == {"status": "running", "run_id": "run-url-1"}
    assert c.completed == []  # GET must NOT write a terminal status


def test_get_succeeded_reshapes_per_sku_report(monkeypatch):
    # report_jsonb is the per_sku brand_report; the GET reshapes it into the
    # URL-audit envelope and flags catalog dims unavailable (connect-store funnel).
    per_sku_report = {"per_sku_reports": [{"sku_key": "urlwedge:x", "scores": {}}],
                      "brand_rollup": {"where_you_can_win": {"targets": []}},
                      "merchant_narrative": {"headline_story": "You're findable but not recommended."},
                      "authority_map": {"skus": []}}
    c = _get_client(monkeypatch, {
        "run_id": "run-url-1",
        "merchant_id": "merch-A", "subject_type": "merchant_url",
        "status": "succeeded", "report_jsonb": per_sku_report,
        "partial_result_jsonb": {"launch": {"wedge_base_payload": {
            "tier": "url_per_sku", "audited_url": "https://m.example",
            "audited_products": [{"pdp_url": "https://m/p/1"}],
        }}},
    })
    body = c.get(f"{_URL}/run-url-1").json()
    assert body["status"] == "succeeded"
    assert body["run_id"] == "run-url-1"
    assert body["tier"] == "url_per_sku"
    assert body["catalog_dimensions_available"] is False
    assert body["per_sku_reports"] == per_sku_report["per_sku_reports"]
    assert body["where_you_can_win"] == {"targets": []}
    # The merchant-grade narrative (insight layer) is lifted for rendering.
    assert body["merchant_narrative"]["headline_story"].startswith("You're findable")
    # Echoed base-payload fields are merged through.
    assert body["audited_url"] == "https://m.example"


def test_get_succeeded_methodology_reports_measured_coverage(monkeypatch):
    # The persisted methodology carries the PLANNED budget; the reshape must
    # overwrite it with what the providers ACTUALLY ran (real per-model counts +
    # real model names), so the header stops claiming a static "14 (Gemini)".
    per_sku_report = {
        "per_sku_reports": [
            {
                "sku_key": "urlwedge:x",
                "scores": {},
                "citation_by_provider": {
                    "gemini": {"score": 40, "prompts": 7},
                    "chatgpt": {"score": 60, "prompts": 10},
                    # A failed provider must NOT count toward coverage.
                    "deepseek": {"status": "probe_failed", "prompts": 0},
                },
            }
        ],
    }
    c = _get_client(monkeypatch, {
        "run_id": "run-url-1",
        "merchant_id": "merch-A", "subject_type": "merchant_url",
        "status": "succeeded", "report_jsonb": per_sku_report,
        "partial_result_jsonb": {"launch": {"wedge_base_payload": {
            "tier": "url_per_sku",
            "methodology": {
                "products_audited": 1,
                # The static planned budget that used to leak into the header.
                "queries_per_product": mar._WEDGE_PROMPTS_PER_SKU,
                "what_we_checked": "... (Gemini grounded search) ...",
            },
        }}},
    })
    m = c.get(f"{_URL}/run-url-1").json()["methodology"]
    # Measured, not the static 14: the fullest single-model coverage (10).
    assert m["queries_per_product"] == 10
    # Planned budget preserved for reference.
    assert m["queries_per_product_target"] == mar._WEDGE_PROMPTS_PER_SKU
    # Real per-model run counts + real model list (failed provider excluded).
    assert m["prompts_by_provider"] == {"gemini": 7, "chatgpt": 10}
    assert m["providers_ran"] == ["chatgpt", "gemini"]
    # The copy names the models that actually ran — no lone "Gemini" claim.
    assert "ChatGPT" in m["what_we_checked"] and "Gemini" in m["what_we_checked"]
    assert "10 AI shopping-agent buyer-intent queries" in m["what_we_checked"]
    # Backend owns the display-ready header strings (frontend renders verbatim),
    # in natural engine order (Gemini first), not raw alphabetical id order.
    assert m["grounded_search_label"] == "Gemini + ChatGPT grounded search"
    assert m["provider_run_summary"] == "Gemini 7 · ChatGPT 10"


def test_get_succeeded_all_providers_failed_marks_coverage_unavailable(monkeypatch):
    # A run that "succeeds" but every provider came back failed / coverage-
    # unavailable must NOT show the static planned "14 (Gemini)" — it reports
    # coverage_unavailable so the header prompts a re-run instead of fabricating.
    per_sku_report = {
        "per_sku_reports": [
            {
                "sku_key": "urlwedge:x",
                "scores": {},
                "citation_by_provider": {
                    "gemini": {"status": "probe_failed", "prompts": 0},
                    "chatgpt": {
                        "coverage_unavailable": True,
                        "score": None,
                        "prompts": 0,
                    },
                },
            }
        ],
    }
    c = _get_client(monkeypatch, {
        "run_id": "run-url-1",
        "merchant_id": "merch-A", "subject_type": "merchant_url",
        "status": "succeeded", "report_jsonb": per_sku_report,
        "partial_result_jsonb": {"launch": {"wedge_base_payload": {
            "tier": "url_per_sku",
            "methodology": {
                "products_audited": 1,
                "queries_per_product": mar._WEDGE_PROMPTS_PER_SKU,
                # Production shape: base_payload always carries the PLANNED label
                # from the launch set — it must NOT survive into an all-failed run.
                "grounded_search_label": "Gemini + ChatGPT grounded search",
                "what_we_checked": "... (Gemini grounded search) ...",
            },
        }}},
    })
    m = c.get(f"{_URL}/run-url-1").json()["methodology"]
    assert m["coverage_unavailable"] is True
    assert m["queries_per_product"] == 0
    assert m["providers_ran"] == []
    assert m["queries_per_product_target"] == mar._WEDGE_PROMPTS_PER_SKU
    assert "Gemini grounded search" not in m["what_we_checked"]
    # The stale planned label must be cleared, not left contradicting the body.
    assert not m.get("grounded_search_label")
    assert not m.get("provider_run_summary")


def test_get_succeeded_legacy_report_keeps_planned_methodology(monkeypatch):
    # No per-provider signal at all (legacy report shape) → we can't measure,
    # so leave the planned methodology untouched (don't wrongly zero a real run).
    per_sku_report = {"per_sku_reports": [{"sku_key": "urlwedge:x", "scores": {}}]}
    c = _get_client(monkeypatch, {
        "run_id": "run-url-1",
        "merchant_id": "merch-A", "subject_type": "merchant_url",
        "status": "succeeded", "report_jsonb": per_sku_report,
        "partial_result_jsonb": {"launch": {"wedge_base_payload": {
            "tier": "url_per_sku",
            "methodology": {
                "products_audited": 1,
                "queries_per_product": mar._WEDGE_PROMPTS_PER_SKU,
            },
        }}},
    })
    m = c.get(f"{_URL}/run-url-1").json()["methodology"]
    assert m["queries_per_product"] == mar._WEDGE_PROMPTS_PER_SKU
    assert "coverage_unavailable" not in m
    assert "providers_ran" not in m


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


def test_depth_config_in_launch(client):
    # Depth: the URL audit launches with enough prompts to run the niche/longtail
    # lanes (>=14) and an answer-quality verify pass — not the old thin 8-prompt,
    # no-verify pass.
    client.state["used"] = 0
    res = client.post(_URL, json=_BODY)
    assert res.status_code == 200, res.text
    launch = client.enqueued[-1]["request_options_jsonb"]["launch"]
    assert launch["prompts_per_sku"] >= 14
    assert launch["verify_providers"] == mar._WEDGE_VERIFY_PROVIDERS
    assert launch["verify_providers"]  # non-empty (verify enabled)


def test_free_tier_stays_gemini_only(client):
    # A FREE-tier merchant doesn't get the paid (ChatGPT) provider — the wedge
    # stays Gemini-only to bound the absorbed cost.
    client.state["used"] = 0
    client.state["balance"] = {"credits": 0, "plan_tier": "free"}
    res = client.post(_URL, json=_BODY)
    assert res.status_code == 200, res.text
    launch = client.enqueued[-1]["request_options_jsonb"]["launch"]
    assert launch["providers"] == mar._WEDGE_PROVIDERS
    assert "chatgpt" not in launch["providers"]


def test_retail_channel_url_uses_brand_site_as_first_party(client):
    # When the pasted URL is a retailer (different host than the brand site),
    # first-party citation must be measured against the BRAND site, not the
    # retailer — so canonical_url points at the brand site, pdp_url stays the
    # pasted retailer page, and the retailer host is flagged.
    client.state["used"] = 0
    res = client.post(_URL, json={
        "product_urls": ["https://global.oliveyoung.com/products/anuko-x"],
        "website": "https://anuko.com",
        "brand": "Anuko",
    })
    assert res.status_code == 200, res.text
    syn = client.enqueued[-1]["request_options_jsonb"]["launch"]["synthetic_products"][0]
    assert syn["pdp_url"] == "https://global.oliveyoung.com/products/anuko-x"
    assert syn["canonical_url"] == "https://anuko.com"
    assert syn["retail_channel_host"] == "global.oliveyoung.com"
    # Retail page = referring context, not the product's own data source.
    assert syn["product_data_source"] == "retail_channel"
    # …and the source is surfaced per product in the response envelope.
    ap = client.enqueued[-1]["request_options_jsonb"]["launch"]["wedge_base_payload"]["audited_products"][0]
    assert ap["data_source"] == "retail_channel"
    assert ap["retail_channel_host"] == "global.oliveyoung.com"


def test_own_site_url_keeps_product_page_as_canonical(client):
    # When the pasted URL is on the brand's own site, canonical stays the
    # product page (not the homepage) — no retail-channel rewrite.
    client.state["used"] = 0
    res = client.post(_URL, json={
        "product_urls": ["https://merch.example/products/a"],
        "website": "https://merch.example",
    })
    assert res.status_code == 200, res.text
    syn = client.enqueued[-1]["request_options_jsonb"]["launch"]["synthetic_products"][0]
    assert syn["canonical_url"] == "https://merch.example/products/a"
    assert syn["retail_channel_host"] is None
    # Own page = first-party product data source.
    assert syn["product_data_source"] == "own_pdp"
    ap = client.enqueued[-1]["request_options_jsonb"]["launch"]["wedge_base_payload"]["audited_products"][0]
    assert ap["data_source"] == "own_pdp"


def test_get_succeeded_includes_merchant_context(monkeypatch):
    # The page needs tier + connection state to stop showing the free-sample /
    # connect-store funnel to a subscribed, connected merchant.
    import services.merchant_integration_state as mis

    async def fake_bal(mid):
        return {"plan_tier": "growth", "credits": 5000}

    async def fake_state(mid):
        return {"store_platform_integrated": True}

    monkeypatch.setattr(mar, "get_balance", fake_bal)
    monkeypatch.setattr(mis, "get_integration_state", fake_state)
    c = _get_client(monkeypatch, {
        "run_id": "r", "merchant_id": "merch-A", "subject_type": "merchant_url",
        "status": "succeeded", "report_jsonb": {"per_sku_reports": []},
        "partial_result_jsonb": {"launch": {}},
    })
    mc = c.get(f"{_URL}/r").json()["merchant_context"]
    assert mc["is_paid"] is True
    assert mc["plan_tier"] == "growth"
    assert mc["store_connected"] is True
