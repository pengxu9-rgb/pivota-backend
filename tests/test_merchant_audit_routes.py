"""
HTTP-level tests for the merchant self-service AI Commerce Readiness
audit route at POST /api/merchant-center/audit/ai-commerce-readiness.

Coverage:
  - 401: no token (auth dep returns 403/401 from get_current_user upstream;
    we test the merchant-role gate specifically)
  - 403: token role != "merchant"
  - 422: product_keys empty / > 5 (Pydantic length validation)
  - 404: any product_key in the list isn't owned by this merchant
  - cross-tenant guard: product_key exists globally but belongs to a
    different merchant → 404 (the WHERE merchant_id=current filter
    silently misses)
  - 422: a selected product has no canonical_url in catalog (audit needs
    a buyer-facing URL)
  - 429: per-merchant rate limit (2 audits / 24h) exhausted
  - happy path: mocked run_brand_report returns a brand-level dict;
    route returns it under {brand_report, rate_limit_remaining}
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

import pytest

os.environ.setdefault(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres"
)

from fastapi import FastAPI
from fastapi.testclient import TestClient

from utils.auth import get_current_user, get_current_merchant


# ---------------------------------------------------------------------------
# Fake DB layer — minimal stand-in for catalog_products + merchant lookup.
# Mirrors the catalog row shape consumed by the audit route.
# ---------------------------------------------------------------------------


class FakeDatabase:
    """In-memory replacement for `db.database.database` covering only the
    one query the audit route runs (catalog_products SELECT)."""

    def __init__(self, products: List[Dict[str, Any]]) -> None:
        # products: list of dict rows (merchant_id + product_key + title +
        # brand + product_type + canonical_url).
        self._products = products

    async def fetch_all(self, query) -> List[Dict[str, Any]]:
        # We don't run the actual SQLAlchemy query — pull merchant_id +
        # product_keys from the compiled WHERE clause.
        try:
            compiled = query.compile(compile_kwargs={"literal_binds": True})
        except Exception:
            return []
        sql = str(compiled).lower()
        # Crude but sufficient parse — extract the literals after merchant_id =
        # and the IN(...) tuple.
        import re
        m = re.search(r"merchant_id\s*=\s*'([^']+)'", sql)
        merchant_id = m.group(1) if m else None
        keys_match = re.search(r"product_key\s+in\s*\((.*?)\)", sql)
        keys: List[str] = []
        if keys_match:
            keys = [
                k.strip().strip("'") for k in keys_match.group(1).split(",")
            ]
        if merchant_id is None:
            return []
        return [
            r for r in self._products
            if r["merchant_id"] == merchant_id
            and r["product_key"] in keys
        ]


def _row(merchant_id: str, key: str, *, with_url: bool = True) -> Dict[str, Any]:
    return {
        "merchant_id": merchant_id,
        "product_key": key,
        "title": f"Product {key}",
        "brand": "Test Brand",
        "product_type": "face mask",
        "canonical_url": f"https://example.com/p/{key}" if with_url else None,
    }


# ---------------------------------------------------------------------------
# Fixture: a small FastAPI app with just the merchant audit router +
# overridden auth + fake DB + mocked run_brand_report.
# ---------------------------------------------------------------------------


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    from routes import merchant_audit_routes as mar

    # In-memory product fixtures: 3 products owned by merch_self,
    # 1 product owned by merch_other (cross-tenant guard test).
    products = [
        _row("merch_self", "p1"),
        _row("merch_self", "p2"),
        _row("merch_self", "p_no_url", with_url=False),
        _row("merch_other", "p_other"),
    ]

    monkeypatch.setattr(mar, "database", FakeDatabase(products))

    async def _fake_get_merchant_onboarding(_mid: str):
        return {
            "merchant_id": "merch_self",
            "business_name": "Test Merchant Inc",
            "store_url": "test-merchant.example.com",
        }

    monkeypatch.setattr(
        mar, "get_merchant_onboarding", _fake_get_merchant_onboarding,
    )

    captured_brand_report_calls: List[Dict[str, Any]] = []

    async def _fake_run_brand_report(**kwargs):
        captured_brand_report_calls.append(kwargs)
        products = kwargs.get("products") or []
        return {
            "merchant_name": kwargs.get("merchant_name"),
            "merchant_domain": kwargs.get("merchant_domain"),
            "timestamp": "2026-05-07T00:00:00+00:00",
            "provider": kwargs.get("provider"),
            "per_product": [
                {
                    "merchant_pdp_url": p["pdp_url"],
                    "verdict": {
                        "label": "VISIBLE VIA RETAILERS",
                        "visibility_score": 0,
                        "attribution_score": 0,
                        "category_visibility_score": 100,
                    },
                }
                for p in products
            ],
            "aggregate": {
                "avg_visibility": 0,
                "avg_attribution": 0,
                "avg_category_visibility": 100,
                "brand_verdict_label": "VISIBLE VIA RETAILERS",
                "brand_verdict_explanation": "test",
                "products_count": len(products),
                "products_succeeded": len(products),
                "products_failed": 0,
            },
            "cross_product_competitors": [],
            "failed": [],
        }

    monkeypatch.setattr(mar, "run_brand_report", _fake_run_brand_report)

    # Reset the in-memory rate-limit history between tests.
    mar._audit_run_history.clear()

    # Override merchant auth — default to a "merch_self" merchant token.
    async def _override_merchant() -> str:
        return "merch_self"

    app = FastAPI()
    app.include_router(mar.router)
    app.dependency_overrides[get_current_merchant] = _override_merchant

    return TestClient(app), captured_brand_report_calls, mar


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------


def test_403_when_token_role_is_not_merchant(monkeypatch: pytest.MonkeyPatch):
    """get_current_merchant 403s when the JWT carries a non-merchant role
    (e.g. an employee token used against a merchant-only route)."""
    from routes import merchant_audit_routes as mar

    # Don't override get_current_merchant — instead override
    # get_current_user (its upstream dep) to return a non-merchant role,
    # so the real get_current_merchant runs its 403 branch.
    async def _override_user_as_employee():
        return {"role": "employee", "email": "e@example.com"}

    app = FastAPI()
    app.include_router(mar.router)
    app.dependency_overrides[get_current_user] = _override_user_as_employee

    client = TestClient(app)
    res = client.post(
        "/api/merchant-center/audit/ai-commerce-readiness",
        json={"product_keys": ["p1"]},
    )
    assert res.status_code == 403
    assert "merchant" in res.json()["detail"].lower()


def test_400_when_token_missing_merchant_id(monkeypatch: pytest.MonkeyPatch):
    from routes import merchant_audit_routes as mar

    async def _override_user_no_mid():
        return {"role": "merchant", "email": "x@example.com"}

    app = FastAPI()
    app.include_router(mar.router)
    app.dependency_overrides[get_current_user] = _override_user_no_mid

    client = TestClient(app)
    res = client.post(
        "/api/merchant-center/audit/ai-commerce-readiness",
        json={"product_keys": ["p1"]},
    )
    assert res.status_code == 400


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


def test_422_when_product_keys_empty(env):
    client, _, _ = env
    res = client.post(
        "/api/merchant-center/audit/ai-commerce-readiness",
        json={"product_keys": []},
    )
    assert res.status_code == 422


def test_422_when_product_keys_over_five(env):
    client, _, _ = env
    res = client.post(
        "/api/merchant-center/audit/ai-commerce-readiness",
        json={"product_keys": ["p1", "p2", "p3", "p4", "p5", "p6"]},
    )
    assert res.status_code == 422


# ---------------------------------------------------------------------------
# Catalog lookup + cross-tenant guard
# ---------------------------------------------------------------------------


def test_404_when_product_key_unknown(env):
    client, _, _ = env
    res = client.post(
        "/api/merchant-center/audit/ai-commerce-readiness",
        json={"product_keys": ["p1", "does_not_exist"]},
    )
    assert res.status_code == 404
    assert "does_not_exist" in res.json()["detail"]["missing_product_keys"]


def test_404_when_product_owned_by_different_merchant(env):
    """Cross-tenant guard: p_other exists in catalog_products globally
    but is owned by merch_other. Looking it up as merch_self must miss
    (treated as 'not found for this merchant')."""
    client, _, _ = env
    res = client.post(
        "/api/merchant-center/audit/ai-commerce-readiness",
        json={"product_keys": ["p_other"]},
    )
    assert res.status_code == 404
    assert "p_other" in res.json()["detail"]["missing_product_keys"]


def test_422_when_product_has_no_canonical_url(env):
    """An audit needs a buyer-facing URL to score attribution against —
    if the catalog row has no canonical_url, we 422 with a clear
    message telling the merchant to run SKU Match first."""
    client, _, _ = env
    res = client.post(
        "/api/merchant-center/audit/ai-commerce-readiness",
        json={"product_keys": ["p_no_url"]},
    )
    assert res.status_code == 422
    assert "p_no_url" in res.json()["detail"]["product_keys_missing_canonical_url"]


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_429_when_audit_quota_exhausted(env):
    client, _, _ = env
    # First two audits succeed
    for _ in range(2):
        res = client.post(
            "/api/merchant-center/audit/ai-commerce-readiness",
            json={"product_keys": ["p1"]},
        )
        assert res.status_code == 200, res.text
    # Third hits the cap
    res = client.post(
        "/api/merchant-center/audit/ai-commerce-readiness",
        json={"product_keys": ["p1"]},
    )
    assert res.status_code == 429
    detail = res.json()["detail"]
    assert detail["limit"] == 2
    assert "next_reset_in_seconds" in detail


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_returns_brand_report_with_per_product_array(env):
    client, captured_calls, _ = env
    res = client.post(
        "/api/merchant-center/audit/ai-commerce-readiness",
        json={"product_keys": ["p1", "p2"], "max_runs": 2},
    )
    assert res.status_code == 200
    body = res.json()
    assert "brand_report" in body
    assert "rate_limit_remaining" in body
    assert body["rate_limit_remaining"] == 1  # 2 max - 1 used
    aggregate = body["brand_report"]["aggregate"]
    assert aggregate["products_count"] == 2
    per_product = body["brand_report"]["per_product"]
    assert len(per_product) == 2
    # run_brand_report was called with the right shape
    assert len(captured_calls) == 1
    call = captured_calls[0]
    assert call["merchant_name"] == "Test Merchant Inc"
    assert call["merchant_domain"] == "test-merchant.example.com"
    assert call["max_runs"] == 2
    assert call["provider"] == "gemini"
    products = call["products"]
    assert len(products) == 2
    # Vendor / type / pdp_url come from catalog_products, not from
    # the request body — confirms cross-tenant safety.
    assert all(p["vendor"] == "Test Brand" for p in products)
    assert all(
        p["pdp_url"] in {"https://example.com/p/p1", "https://example.com/p/p2"}
        for p in products
    )
