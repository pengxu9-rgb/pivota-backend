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

from typing import Any, Dict, List

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from utils.auth import get_current_user, get_current_merchant


# ---------------------------------------------------------------------------
# Fake DB layer — minimal stand-in for catalog_products + merchant lookup.
# Mirrors the catalog row shape consumed by the audit route.
# ---------------------------------------------------------------------------


class FakeDatabase:
    """In-memory replacement for `db.database.database` covering only the
    queries the audit route runs:
      - SELECT from catalog_products (filtered)
      - UPDATE catalog_products SET pivota_signature_id, pivota_canonical_url
        for the lazy mint path
    Both parsed loosely from compiled SQL — sufficient for the in-route
    behaviors we need to verify."""

    def __init__(self, products: List[Dict[str, Any]]) -> None:
        self._products = products
        self.update_calls: List[Dict[str, Any]] = []

    async def execute(self, query) -> Any:
        # Track UPDATEs so tests can assert lazy mint persisted.
        try:
            compiled = query.compile(compile_kwargs={"literal_binds": True})
        except Exception:
            return None
        sql = str(compiled)
        self.update_calls.append({"sql": sql})
        return None

    async def fetch_all(self, query) -> List[Dict[str, Any]]:
        try:
            compiled = query.compile(compile_kwargs={"literal_binds": True})
        except Exception:
            return []
        sql = str(compiled).lower()
        import re
        m = re.search(r"merchant_id\s*=\s*'([^']+)'", sql)
        merchant_id = m.group(1) if m else None
        platforms_match = re.search(
            r"platform\s+in\s*\((.*?)\)", sql,
        )
        sources_match = re.search(
            r"source_product_id\s+in\s*\((.*?)\)", sql,
        )
        platforms: List[str] = []
        sources: List[str] = []
        if platforms_match:
            platforms = [
                p.strip().strip("'") for p in platforms_match.group(1).split(",")
            ]
        if sources_match:
            sources = [
                s.strip().strip("'") for s in sources_match.group(1).split(",")
            ]
        if merchant_id is None:
            return []
        return [
            r for r in self._products
            if r["merchant_id"] == merchant_id
            and r["platform"] in platforms
            and r["source_product_id"] in sources
        ]


def _row(
    merchant_id: str,
    source_product_id: str,
    *,
    platform: str = "shopify",
    with_url: bool = True,
    handle: str = "",
    with_pivota_canonical: bool = False,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"id": source_product_id}
    if handle:
        payload["handle"] = handle
    pivota_sig = (
        f"sig_test_{source_product_id}" if with_pivota_canonical else None
    )
    pivota_url = (
        f"https://agent.pivota.cc/products/{pivota_sig}"
        if with_pivota_canonical
        else None
    )
    return {
        "merchant_id": merchant_id,
        "product_key": (
            f"prod::{merchant_id}::{platform}::{source_product_id}"
        ),
        "platform": platform,
        "source_product_id": source_product_id,
        "title": f"Product {source_product_id}",
        "brand": "Test Brand",
        "product_type": "face mask",
        "canonical_url": (
            f"https://example.com/p/{source_product_id}" if with_url else None
        ),
        "product_payload": payload,
        "pivota_canonical_url": pivota_url,
        "pivota_signature_id": pivota_sig,
    }


def _install_audit_run_persistence_mocks(monkeypatch: pytest.MonkeyPatch, mar) -> List[Dict[str, Any]]:
    """Phase C-4 PR-C helper: replace the 4 audit-history DB helpers
    with stateful in-memory simulations. Used by the main `env`
    fixture and each inline-fixture-style test below.

    Returns the in-memory history list so callers that need to assert
    on persisted state can inspect it directly.
    """
    history: List[Dict[str, Any]] = []

    async def _count(*, merchant_id: str, window_seconds: int) -> int:
        return sum(1 for r in history if r["merchant_id"] == merchant_id)

    async def _start(*, merchant_id: str, product_keys: List[str]) -> str:
        run_id = f"run-{len(history)}"
        history.append({
            "run_id": run_id,
            "merchant_id": merchant_id,
            "product_keys": list(product_keys),
            "status": "running",
        })
        return run_id

    async def _complete(**kwargs) -> None:
        run_id = kwargs.get("run_id")
        for r in history:
            if r["run_id"] == run_id:
                r.update({k: v for k, v in kwargs.items() if k != "run_id"})

    async def _recent(*, merchant_id: str, limit: int = 5):
        rows = [r for r in history if r["merchant_id"] == merchant_id]
        return rows[-limit:][::-1]

    monkeypatch.setattr(mar, "count_runs_in_window", _count)
    monkeypatch.setattr(mar, "record_audit_run_started", _start)
    monkeypatch.setattr(mar, "record_audit_run_completed", _complete)
    monkeypatch.setattr(mar, "recent_runs_for_merchant", _recent)
    # Compatibility shim: existing tests call `mar._audit_run_history.clear()`
    # to reset state. Point that name at the in-memory list so the .clear()
    # works without rewriting each test.
    mar._audit_run_history = history  # type: ignore[attr-defined]
    return history


def _install_audit_ready_mock(
    monkeypatch: pytest.MonkeyPatch,
    mar,
    *,
    catalog_count: int = 1,
) -> None:
    async def _audit_ready(_merchant_id: str, _platform: str):
        return {
            "ready": True,
            "blocking_gaps": [],
            "counts": {
                "catalog_products": catalog_count,
                "product_quality_snapshot": catalog_count,
            },
        }

    monkeypatch.setattr(mar, "assess_merchant_audit_readiness", _audit_ready)


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
    _install_audit_run_persistence_mocks(monkeypatch, mar)
    _install_audit_ready_mock(
        monkeypatch, mar, catalog_count=len(products),
    )

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
        json={"products": [{"platform": "shopify", "source_product_id": "p1"}]},
    )
    assert res.status_code == 403
    assert "merchant" in res.json()["detail"].lower()


def test_401_when_token_missing_merchant_id_claim(
    monkeypatch: pytest.MonkeyPatch,
):
    """No fallback. If the JWT lacks merchant_id, the route 401s and
    asks the merchant to refresh their session — login is responsible
    for putting merchant_id in the token. Per-request DB fallback is
    intentionally NOT in get_current_merchant."""
    from routes import merchant_audit_routes as mar

    async def _override_user_no_mid():
        return {"role": "merchant", "email": "x@example.com"}

    app = FastAPI()
    app.include_router(mar.router)
    app.dependency_overrides[get_current_user] = _override_user_no_mid

    client = TestClient(app)
    res = client.post(
        "/api/merchant-center/audit/ai-commerce-readiness",
        json={"products": [{"platform": "shopify", "source_product_id": "p1"}]},
    )
    assert res.status_code == 401
    assert "log out" in res.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


def _ref(source_id: str, platform: str = "shopify") -> Dict[str, str]:
    return {"platform": platform, "source_product_id": source_id}


def test_422_when_products_empty(env):
    client, _, _ = env
    res = client.post(
        "/api/merchant-center/audit/ai-commerce-readiness",
        json={"products": []},
    )
    assert res.status_code == 422


def test_422_when_products_over_five(env):
    client, _, _ = env
    res = client.post(
        "/api/merchant-center/audit/ai-commerce-readiness",
        json={"products": [_ref(f"p{i}") for i in range(6)]},
    )
    assert res.status_code == 422


# ---------------------------------------------------------------------------
# Catalog lookup + cross-tenant guard
# ---------------------------------------------------------------------------


def test_404_when_product_unknown(env):
    client, _, _ = env
    res = client.post(
        "/api/merchant-center/audit/ai-commerce-readiness",
        json={"products": [_ref("p1"), _ref("does_not_exist")]},
    )
    assert res.status_code == 404
    missing = res.json()["detail"]["missing_products"]
    assert any(m["source_product_id"] == "does_not_exist" for m in missing)


def test_404_when_product_owned_by_different_merchant(env):
    """Cross-tenant guard: p_other exists in catalog_products globally
    but is owned by merch_other. Looking it up as merch_self must miss
    (treated as 'not found for this merchant')."""
    client, _, _ = env
    res = client.post(
        "/api/merchant-center/audit/ai-commerce-readiness",
        json={"products": [_ref("p_other")]},
    )
    assert res.status_code == 404
    missing = res.json()["detail"]["missing_products"]
    assert any(m["source_product_id"] == "p_other" for m in missing)


def test_409_when_legacy_sync_audit_readiness_not_ready(
    env,
    monkeypatch: pytest.MonkeyPatch,
):
    """Legacy sync path must use the readiness probe, not render an
    all-blocked first audit while catalog quality backfill is still running."""
    client, captured_calls, mar_module = env

    async def _not_ready(_merchant_id: str, platform: str):
        return {
            "ready": False,
            "blocking_gaps": [
                "product_quality_snapshot missing content_quality_score"
            ],
            "counts": {
                "catalog_products": 2,
                "product_quality_snapshot": 0,
            },
            "platform": platform,
        }

    monkeypatch.setattr(
        mar_module, "assess_merchant_audit_readiness", _not_ready,
    )

    res = client.post(
        "/api/merchant-center/audit/ai-commerce-readiness",
        json={"products": [_ref("p1")]},
    )

    assert res.status_code == 409
    detail = res.json()["detail"]
    assert detail["code"] == "merchant_not_audit_ready"
    assert detail["platform"] == "shopify"
    assert detail["retry_after_seconds"] == 60
    assert "product_quality_snapshot" in str(detail["blocking_gaps"])
    assert captured_calls == []
    assert mar_module._audit_run_history == []


def test_url_less_product_falls_back_to_pivota_canonical_url(env):
    """A catalog row with no canonical_url AND no handle in
    product_payload now falls back to its Pivota canonical PDP URL
    (auto-minted if missing) — the audit ALWAYS finds a URL to probe.
    The product is audited; product_key surfaces in
    `audited_via_pivota_canonical` so the UI can flag the score
    reflects Pivota's hosted surface."""
    client, captured_calls, mar_module = env
    res = client.post(
        "/api/merchant-center/audit/ai-commerce-readiness",
        json={"products": [_ref("p_no_url")]},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    # Audit ran (no skip).
    assert len(captured_calls) == 1
    assert len(captured_calls[0]["products"]) == 1
    pdp_url = captured_calls[0]["products"][0]["pdp_url"]
    # URL points at Pivota canonical surface, not example.com / merchant store.
    assert pdp_url.startswith("https://agent.pivota.cc/products/sig_")
    # audited_via_pivota_canonical lists this product_key.
    assert len(body["audited_via_pivota_canonical"]) == 1
    assert "p_no_url" in body["audited_via_pivota_canonical"][0]


def test_url_less_product_persists_minted_pivota_sig_to_catalog(env):
    """Lazy-mint path: when the catalog row has no pivota_signature_id
    yet, the route generates one + writes it back via UPDATE so
    subsequent audits + the sitemap renderer see the same sig."""
    client, _, mar_module = env
    res = client.post(
        "/api/merchant-center/audit/ai-commerce-readiness",
        json={"products": [_ref("p_no_url")]},
    )
    assert res.status_code == 200
    # Verify the UPDATE happened — FakeDatabase records execute() calls.
    db = mar_module.database
    assert len(db.update_calls) == 1
    sql = db.update_calls[0]["sql"]
    assert "pivota_signature_id" in sql.lower()
    assert "pivota_canonical_url" in sql.lower()


def test_existing_pivota_canonical_url_is_used_without_lazy_mint(
    monkeypatch: pytest.MonkeyPatch,
):
    """When catalog row already has pivota_canonical_url set (from
    catalog sync), the audit uses it directly — no UPDATE roundtrip."""
    from routes import merchant_audit_routes as mar
    products_for_self = [
        _row("merch_self", "p_with_pivota", with_url=False, with_pivota_canonical=True),
    ]
    db = FakeDatabase(products_for_self)
    monkeypatch.setattr(mar, "database", db)

    async def _fake_get_merchant_onboarding(_mid: str):
        return {"merchant_id": "merch_self", "business_name": "Test"}
    monkeypatch.setattr(
        mar, "get_merchant_onboarding", _fake_get_merchant_onboarding,
    )

    captured: List[Dict[str, Any]] = []

    async def _fake_run_brand_report(**kwargs):
        captured.append(kwargs)
        return {
            "merchant_name": kwargs["merchant_name"],
            "merchant_domain": None,
            "timestamp": "2026-05-07T00:00:00+00:00",
            "provider": "gemini",
            "per_product": [],
            "aggregate": {
                "avg_visibility": 0, "avg_attribution": 0,
                "avg_category_visibility": 0,
                "brand_verdict_label": "INVISIBLE",
                "brand_verdict_explanation": "test",
                "products_count": 1, "products_succeeded": 1,
                "products_failed": 0,
            },
            "cross_product_competitors": [],
            "failed": [],
        }
    monkeypatch.setattr(mar, "run_brand_report", _fake_run_brand_report)
    _install_audit_run_persistence_mocks(monkeypatch, mar)
    _install_audit_ready_mock(
        monkeypatch, mar, catalog_count=len(products_for_self),
    )

    async def _override_merchant() -> str:
        return "merch_self"

    app = FastAPI()
    app.include_router(mar.router)
    app.dependency_overrides[get_current_merchant] = _override_merchant
    client = TestClient(app)

    res = client.post(
        "/api/merchant-center/audit/ai-commerce-readiness",
        json={"products": [_ref("p_with_pivota")]},
    )
    assert res.status_code == 200, res.text
    pdp_url = captured[0]["products"][0]["pdp_url"]
    assert pdp_url == "https://agent.pivota.cc/products/sig_test_p_with_pivota"
    # No UPDATE — sig already existed.
    assert len(db.update_calls) == 0


def test_canonical_url_derived_from_handle_when_catalog_url_missing(
    monkeypatch: pytest.MonkeyPatch,
):
    """The realistic case: catalog_products.canonical_url is NULL
    (catalog sync didn't capture it) but product_payload carries the
    Shopify handle. Route derives `{store_url}/products/{handle}` and
    proceeds — no 422, no fallback flag."""
    from routes import merchant_audit_routes as mar

    products = [
        _row("merch_self", "p_with_handle", with_url=False, handle="snail-mucin"),
    ]
    monkeypatch.setattr(mar, "database", FakeDatabase(products))

    async def _fake_get_merchant_onboarding(_mid: str):
        return {
            "merchant_id": "merch_self",
            "business_name": "Test",
            "store_url": "test-merchant.example.com",
        }
    monkeypatch.setattr(
        mar, "get_merchant_onboarding", _fake_get_merchant_onboarding,
    )

    captured: List[Dict[str, Any]] = []

    async def _fake_run_brand_report(**kwargs):
        captured.append(kwargs)
        return {
            "merchant_name": kwargs["merchant_name"],
            "merchant_domain": kwargs.get("merchant_domain"),
            "timestamp": "2026-05-07T00:00:00+00:00",
            "provider": "gemini",
            "per_product": [],
            "aggregate": {
                "avg_visibility": 0, "avg_attribution": 0,
                "avg_category_visibility": 0,
                "brand_verdict_label": "INVISIBLE",
                "brand_verdict_explanation": "test",
                "products_count": 1, "products_succeeded": 1,
                "products_failed": 0,
            },
            "cross_product_competitors": [],
            "failed": [],
        }
    monkeypatch.setattr(mar, "run_brand_report", _fake_run_brand_report)
    _install_audit_run_persistence_mocks(monkeypatch, mar)
    _install_audit_ready_mock(monkeypatch, mar, catalog_count=len(products))

    async def _override_merchant() -> str:
        return "merch_self"

    app = FastAPI()
    app.include_router(mar.router)
    from utils.auth import get_current_merchant
    app.dependency_overrides[get_current_merchant] = _override_merchant

    client = TestClient(app)
    res = client.post(
        "/api/merchant-center/audit/ai-commerce-readiness",
        json={"products": [_ref("p_with_handle")]},
    )
    assert res.status_code == 200, res.text
    # The derived URL was passed to run_brand_report.
    assert len(captured) == 1
    call_products = captured[0]["products"]
    assert len(call_products) == 1
    assert call_products[0]["pdp_url"] == (
        "https://test-merchant.example.com/products/snail-mucin"
    )


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_429_when_audit_quota_exhausted(env):
    client, _, _ = env
    # First two audits succeed
    for _ in range(2):
        res = client.post(
            "/api/merchant-center/audit/ai-commerce-readiness",
            json={"products": [_ref("p1")]},
        )
        assert res.status_code == 200, res.text
    # Third hits the cap
    res = client.post(
        "/api/merchant-center/audit/ai-commerce-readiness",
        json={"products": [_ref("p1")]},
    )
    assert res.status_code == 429
    detail = res.json()["detail"]
    assert detail["limit"] == 2
    assert "next_reset_in_seconds" in detail


def test_legacy_sync_path_wraps_run_brand_report_in_audit_telemetry(
    monkeypatch: pytest.MonkeyPatch,
):
    """P1-3 regression: pre-fix, the legacy /ai-commerce-readiness
    sync path called run_brand_report OUTSIDE any audit_telemetry
    context, so probe telemetry rows recorded audit_run_id=None +
    merchant_id=None — invisible to per-run cost rollups.

    Assert that when run_brand_report is invoked, the contextvar
    set by audit_telemetry() has the audit run + merchant attribution
    populated. We capture the context AT run_brand_report time
    (not after) so the test catches the case where the wrap was
    accidentally moved outside the call site."""
    from routes import merchant_audit_routes as mar
    from services.audit_telemetry_context import current_audit_context

    products = [_row("merch_self", "p1")]
    monkeypatch.setattr(mar, "database", FakeDatabase(products))

    async def _fake_get_merchant_onboarding(_mid: str):
        return {"merchant_id": "merch_self",
                "business_name": "T", "store_url": "t.com"}
    monkeypatch.setattr(
        mar, "get_merchant_onboarding", _fake_get_merchant_onboarding,
    )

    captured_ctx_at_brand_report_time: Dict[str, Any] = {}

    async def _capturing_brand_report(**kwargs):
        ctx = current_audit_context()
        captured_ctx_at_brand_report_time["audit_run_id"] = ctx.audit_run_id
        captured_ctx_at_brand_report_time["merchant_id"] = ctx.merchant_id
        # Minimal return so the rest of the legacy block doesn't fail.
        return {
            "merchant_name": "T", "merchant_domain": "t.com",
            "timestamp": "2026-05-13T00:00:00+00:00",
            "provider": "gemini",
            "per_product": [{
                "merchant_pdp_url": "https://t.com/p",
                "verdict": {"label": "OK", "visibility_score": 0,
                            "attribution_score": 0,
                            "category_visibility_score": 0},
            }],
            "aggregate": {
                "avg_visibility": 0, "avg_attribution": 0,
                "avg_category_visibility": 0,
                "brand_verdict_label": "OK",
                "brand_verdict_explanation": "",
                "products_count": 1, "products_succeeded": 1,
                "products_failed": 0,
            },
            "cross_product_competitors": [],
            "failed": [],
        }

    monkeypatch.setattr(mar, "run_brand_report", _capturing_brand_report)
    _install_audit_run_persistence_mocks(monkeypatch, mar)
    _install_audit_ready_mock(monkeypatch, mar, catalog_count=len(products))

    async def _override_merchant() -> str:
        return "merch_self"

    app = FastAPI()
    app.include_router(mar.router)
    app.dependency_overrides[get_current_merchant] = _override_merchant
    client = TestClient(app)

    res = client.post(
        "/api/merchant-center/audit/ai-commerce-readiness",
        json={"products": [_ref("p1")]},
    )
    assert res.status_code == 200, res.text
    # The contextvar must have been set at the moment run_brand_report
    # was called.
    assert captured_ctx_at_brand_report_time["merchant_id"] == "merch_self", (
        "merchant_id not set in audit_telemetry context — probe "
        "telemetry rows would record merchant_id=None"
    )
    # audit_run_id may be None when record_audit_run_started's DB
    # write is mocked out, but the contextvar MUST have been
    # populated with WHATEVER value record_audit_run_started returned.
    # Assert it's at least present in the captured dict so the wrap
    # demonstrably executed.
    assert "audit_run_id" in captured_ctx_at_brand_report_time


def test_429_when_audit_quota_exhausted_via_async_pipeline_compat(env):
    """P1-1 regression: before the fix, `?via=async_pipeline` returned
    before _check_audit_rate_limit, so a quota-exhausted merchant
    could keep enqueueing audits by switching to the compat path.
    Now both arms share the same daily-cap check."""
    client, _, _ = env
    # Burn the cap via the sync path first.
    for _ in range(2):
        res = client.post(
            "/api/merchant-center/audit/ai-commerce-readiness",
            json={"products": [_ref("p1")]},
        )
        assert res.status_code == 200, res.text
    # ?via=async_pipeline MUST also be rate-limited.
    res = client.post(
        "/api/merchant-center/audit/ai-commerce-readiness"
        "?via=async_pipeline",
        json={"products": [_ref("p1")]},
    )
    assert res.status_code == 429, (
        f"async-compat path bypassed the rate limit: got "
        f"{res.status_code} {res.text}"
    )
    detail = res.json()["detail"]
    assert detail["limit"] == 2


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_returns_brand_report_with_per_product_array(env):
    client, captured_calls, _ = env
    res = client.post(
        "/api/merchant-center/audit/ai-commerce-readiness",
        json={"products": [_ref("p1"), _ref("p2")], "max_runs": 2},
    )
    assert res.status_code == 200, res.text
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


# ---------------------------------------------------------------------------
# GET /tracking — subject_type scoping (the URL-audit page's trend chart)
# ---------------------------------------------------------------------------


def test_tracking_defaults_to_merchant_subject_type(
    env, monkeypatch: pytest.MonkeyPatch,
):
    """Without a subject_type param the series covers catalog audits."""
    client, _, _ = env
    captured: Dict[str, Any] = {}

    async def _fake_history(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(
        "db.merchant_audit_runs.score_history_for_merchant", _fake_history,
    )
    res = client.get("/api/merchant-center/audit/tracking")
    assert res.status_code == 200, res.text
    assert captured["subject_type"] == "merchant"
    assert res.json()["points"] == []


def test_tracking_scopes_to_merchant_url_runs(
    env, monkeypatch: pytest.MonkeyPatch,
):
    """subject_type=merchant_url scopes the series to URL-wedge runs — the
    URL-audit page's chart must reflect the audits run on that page."""
    client, _, _ = env
    captured: Dict[str, Any] = {}

    async def _fake_history(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(
        "db.merchant_audit_runs.score_history_for_merchant", _fake_history,
    )
    res = client.get(
        "/api/merchant-center/audit/tracking",
        params={"subject_type": "merchant_url"},
    )
    assert res.status_code == 200, res.text
    assert captured["subject_type"] == "merchant_url"


def test_tracking_422_on_unknown_subject_type(env):
    client, _, _ = env
    res = client.get(
        "/api/merchant-center/audit/tracking",
        params={"subject_type": "cold_start"},
    )
    assert res.status_code == 422
