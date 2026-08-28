import time
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient


from main import app


KNOWN_MERCHANT_ID = "merch_efbc46b4619cfbdf"
KNOWN_PRODUCTS = {
    "9886499864904": "The Ordinary Niacinamide 10% + Zinc 1%",
    "9886500749640": "Winona Soothing Repair Serum",
}


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _build_product_cache_row(product_id: str, title: str) -> Dict[str, Any]:
    return {
        "merchant_id": KNOWN_MERCHANT_ID,
        "platform": "shopify",
        "platform_product_id": product_id,
        "product_data": {
            "id": product_id,
            "product_id": product_id,
            "title": title,
            "currency": "USD",
            "price": 18.9,
            "inventory_quantity": 18,
            "in_stock": True,
            "merchant_name": "Pivota Demo Store",
            "variants": [
                {
                    "id": f"{product_id}_v1",
                    "variant_id": f"{product_id}_v1",
                    "sku": f"SKU_{product_id}",
                    "price": 18.9,
                    "inventory_quantity": 18,
                }
            ],
        },
    }


def test_products_search_delegates_to_optimized_handler(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    import routes.agent_api as agent_api

    async def fake_search(**kwargs):
        return {
            "status": "success",
            "products": [{"id": "p_1", "product_id": "p_1", "merchant_id": KNOWN_MERCHANT_ID}],
            "pagination": {"total_count": 1, "limit": 20, "offset": 0, "page": 1, "total_pages": 1, "has_more": False},
        }

    monkeypatch.setattr(agent_api, "agent_search_products", fake_search)

    latencies_ms: List[float] = []
    for _ in range(30):
        t0 = time.perf_counter()
        res = client.get(
            "/agent/v1/products/search?merchant_id=merch_efbc46b4619cfbdf&query=niacinamide&limit=20&offset=0",
            headers={"X-API-Key": "test-api-key"},
        )
        latencies_ms.append((time.perf_counter() - t0) * 1000.0)
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "success"
        assert body.get("metadata", {}).get("source") == "agent_sdk_fixed_delegate"
        assert body.get("metadata", {}).get("reason_code") == "ok"

    # Stability target: no timeout/500 for repeated calls.
    assert len(latencies_ms) == 30


def test_products_resolve_hits_known_products(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    import routes.agent_api as agent_api

    async def fake_search(**kwargs):
        raise AssertionError("exact resolve path should not call agent_search_products for direct product_id hits")

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        values = values or {}

        if "FROM products_cache" in q and "platform_product_id = ANY(:pid_aliases)" in q:
            pid_aliases = set(values.get("pid_aliases") or [])
            out = []
            for pid, title in KNOWN_PRODUCTS.items():
                if pid in pid_aliases:
                    out.append(_build_product_cache_row(pid, title))
            return out

        if "FROM product_group_members" in q:
            pids = set(values.get("pids") or values.get("platform_product_ids") or [])
            for pid in KNOWN_PRODUCTS.keys():
                if pid in pids:
                    return [
                        {
                            "product_group_id": f"pg_{pid}",
                            "merchant_id": KNOWN_MERCHANT_ID,
                            "platform": "shopify",
                            "platform_product_id": pid,
                            "is_primary": True,
                        }
                    ]
            return []

        return []

    monkeypatch.setattr(agent_api.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_api, "agent_search_products", fake_search)

    for pid in KNOWN_PRODUCTS.keys():
        res = client.get(
            f"/agent/v1/products/resolve?merchant_id={KNOWN_MERCHANT_ID}&product_id={pid}&limit=10",
            headers={"X-API-Key": "test-api-key"},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "success"
        assert body.get("resolved") is True
        assert body.get("reason_code") == "OK"
        assert body["candidate_count"] > 0
        # ADR-009 decision 1 (no-fallback): grouped products key on pg — the
        # group lookup runs even on the exact-identifier fast path.
        assert body.get("canonical_ref") == f"pg:pg_{pid}"
        assert body.get("canonical_identity_status") == "resolved"
        assert body.get("canonical_product_group_id") == f"pg_{pid}"
        assert body.get("metadata", {}).get("reason_code") == "OK"
        sources = body.get("metadata", {}).get("sources") or []
        assert any(s.get("source") == "products_cache_exact" and s.get("ok") is True for s in sources)
        assert any(s.get("source") == "product_group_members" and s.get("ok") is True for s in sources)
        assert all("latency_ms" in s for s in sources)


def _iter_strings(obj):
    """Yield every string anywhere inside a JSON-shaped payload."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _iter_strings(k)
            yield from _iter_strings(v)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            yield from _iter_strings(item)


def test_products_resolve_ungrouped_product_is_honest_absent_never_pc(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    """ADR-009 ratified decision 1 (no-fallback), mirrored from
    tests/test_offers_resolve.py: a product with NO product_group_members row
    resolves with canonical_ref None and canonical_identity_status ==
    'no_canonical_identity' — and NOTHING anywhere in the response carries a
    merchant-scoped `pc:` ref (the removed fallback must not resurface)."""
    import routes.agent_api as agent_api

    pid = "9886499864904"

    async def fake_search(**kwargs):
        raise AssertionError("exact resolve path should not call agent_search_products for direct product_id hits")

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        values = values or {}
        if "FROM products_cache" in q and "platform_product_id = ANY(:pid_aliases)" in q:
            if pid in set(values.get("pid_aliases") or []):
                return [_build_product_cache_row(pid, KNOWN_PRODUCTS[pid])]
            return []
        if "FROM product_group_members" in q:
            return []  # pg-NULL product: no membership row anywhere
        return []

    monkeypatch.setattr(agent_api.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_api, "agent_search_products", fake_search)

    res = client.get(
        f"/agent/v1/products/resolve?merchant_id={KNOWN_MERCHANT_ID}&product_id={pid}&limit=10",
        headers={"X-API-Key": "test-api-key"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    assert body.get("resolved") is True, "the candidate itself is still served — only the canonical ref is absent"
    assert body["candidate_count"] > 0

    assert body.get("canonical_ref") is None
    assert body.get("canonical_identity_status") == "no_canonical_identity"
    assert body.get("canonical_product_group_id") is None
    # canonical_product stays a best-effort merchant-level pointer.
    canonical_product = body.get("canonical_product") or {}
    assert canonical_product.get("product_id") == pid
    assert canonical_product.get("product_group_id") is None

    # The banned fallback must not resurface ANYWHERE in the response — not
    # in candidates, canonical_product, sources, or any nested string.
    pc_strings = [s for s in _iter_strings(body) if s.startswith("pc:")]
    assert pc_strings == [], f"merchant-scoped pc: refs must never be minted: {pc_strings}"


def test_products_resolve_reports_db_ambiguous_param_and_skips_global_fallback(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    import routes.agent_api as agent_api

    class AmbiguousParameterError(Exception):
        pass

    search_calls: List[Dict[str, Any]] = []

    async def fake_search(**kwargs):
        search_calls.append({"merchant_id": kwargs.get("merchant_id")})
        return {"status": "success", "products": []}

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM products_cache" in q and "platform_product_id = ANY(:pid_aliases)" in q:
            raise AmbiguousParameterError("could not determine data type of parameter $1")
        if "FROM product_group_members" in q:
            return []
        return []

    monkeypatch.setattr(agent_api, "agent_search_products", fake_search)
    monkeypatch.setattr(agent_api.database, "fetch_all", fake_fetch_all)

    res = client.get(
        f"/agent/v1/products/resolve?merchant_id={KNOWN_MERCHANT_ID}&product_id=9886499864904&limit=10",
        headers={"X-API-Key": "test-api-key"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    assert body.get("resolved") is False
    assert body["candidate_count"] == 0
    assert body.get("reason_code") == "DB_ERROR"
    assert body.get("metadata", {}).get("reason_code") == "DB_ERROR"
    assert len(search_calls) == 1
    assert search_calls[0]["merchant_id"] == KNOWN_MERCHANT_ID

    sources = body.get("metadata", {}).get("sources") or []
    cache_source = next((s for s in sources if s.get("source") == "products_cache_exact"), {})
    assert cache_source.get("status") == "error"
    assert cache_source.get("reason_code") == "db_ambiguous_param"
    assert cache_source.get("reason") == "DB_ERROR"
    assert cache_source.get("query") == "products_cache_exact"
    assert not any(s.get("source") == "agent_search_global" for s in sources)


def test_offers_resolve_maps_to_canonical_and_internal_offers(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    import routes.agent_shop_gateway as gateway

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        values = values or {}

        if "FROM external_product_seeds" in q:
            return []

        if "FROM products_cache" in q and "platform_product_id = ANY(:pid_aliases)" in q:
            pid_aliases = set(values.get("pid_aliases") or [])
            out = []
            for pid, title in KNOWN_PRODUCTS.items():
                if pid in pid_aliases:
                    out.append(_build_product_cache_row(pid, title))
            return out

        if "FROM product_group_members" in q:
            candidates = set(values.get("platform_product_ids") or [])
            for pid in KNOWN_PRODUCTS.keys():
                if pid in candidates:
                    return [
                        {
                            "product_group_id": f"pg_{pid}",
                            "merchant_id": KNOWN_MERCHANT_ID,
                            "platform": "shopify",
                            "platform_product_id": pid,
                            "is_primary": True,
                        },
                        {
                            "product_group_id": f"pg_{pid}",
                            "merchant_id": "merch_alt_seller",
                            "platform": "shopify",
                            "platform_product_id": pid,
                            "is_primary": False,
                        },
                    ]
            return []

        if "FROM products_cache" in q and "merchant_id = ANY(:merchant_ids)" in q:
            member_mids = values.get("merchant_ids") or []
            member_pids = values.get("platform_product_ids") or []
            out = []
            for pid in member_pids:
                if pid in KNOWN_PRODUCTS:
                    for mid in member_mids:
                        row = _build_product_cache_row(pid, KNOWN_PRODUCTS[pid])
                        row["merchant_id"] = mid
                        row["product_data"]["merchant_name"] = "Alt Seller" if mid != KNOWN_MERCHANT_ID else "Pivota Demo Store"
                        out.append(row)
            return out

        return []

    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)

    pid = "9886499864904"
    res = client.post(
        "/agent/shop/v1/invoke",
        json={
            "operation": "offers.resolve",
            "payload": {"product": {"product_id": pid, "merchant_id": KNOWN_MERCHANT_ID}, "limit": 10},
            "metadata": {"source": "creator-agent-ui", "merchant_id": KNOWN_MERCHANT_ID},
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    assert body["offers_count"] > 0
    assert (body.get("canonical_product_ref") or "").startswith("pg:")
    assert body.get("mapping", {}).get("candidates")
    assert body.get("metadata", {}).get("reason_code") == "OK"
    assert any(s.get("source") == "products_cache" for s in (body.get("metadata", {}).get("sources") or []))


def test_offers_resolve_reports_db_ambiguous_param(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    import routes.agent_shop_gateway as gateway

    class AmbiguousParameterError(Exception):
        pass

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM external_product_seeds" in q:
            return []
        if "FROM products_cache" in q and "platform_product_id = ANY(:pid_aliases)" in q:
            raise AmbiguousParameterError("ambiguous parameter type")
        if "FROM product_group_members" in q:
            return []
        return []

    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)

    res = client.post(
        "/agent/shop/v1/invoke",
        json={
            "operation": "offers.resolve",
            "payload": {"product": {"product_id": "9886499864904", "merchant_id": KNOWN_MERCHANT_ID}, "limit": 10},
            "metadata": {"source": "creator-agent-ui", "merchant_id": KNOWN_MERCHANT_ID},
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    assert body["offers_count"] == 0
    assert body.get("metadata", {}).get("reason_code") == "DB_ERROR"
    sources = body.get("metadata", {}).get("sources") or []
    cache_source = next((s for s in sources if s.get("source") == "products_cache"), {})
    assert cache_source.get("status") == "error"
    assert cache_source.get("reason_code") == "db_ambiguous_param"
    assert cache_source.get("reason") == "DB_ERROR"
    assert cache_source.get("query") == "products_cache_by_alias"


def test_offers_resolve_maps_sku_to_internal_product(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    import routes.agent_shop_gateway as gateway

    target_pid = "9886499864904"
    target_sku = f"{target_pid}_v1"

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        values = values or {}
        if "FROM external_product_seeds" in q:
            return []
        if "FROM products_cache" in q and "jsonb_array_elements" in q:
            if target_sku in set(values.get("sku_aliases") or []):
                return [_build_product_cache_row(target_pid, KNOWN_PRODUCTS[target_pid])]
            return []
        if "FROM products_cache" in q and "platform_product_id = ANY(:pid_aliases)" in q:
            return []
        if "FROM product_group_members" in q:
            return [
                {
                    "product_group_id": f"pg_{target_pid}",
                    "merchant_id": KNOWN_MERCHANT_ID,
                    "platform": "shopify",
                    "platform_product_id": target_pid,
                    "is_primary": True,
                }
            ]
        if "FROM products_cache" in q and "merchant_id = ANY(:merchant_ids)" in q:
            return [_build_product_cache_row(target_pid, KNOWN_PRODUCTS[target_pid])]
        return []

    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)

    res = client.post(
        "/agent/shop/v1/invoke",
        json={
            "operation": "offers.resolve",
            "payload": {"product": {"sku_id": target_sku, "merchant_id": KNOWN_MERCHANT_ID}, "limit": 10},
            "metadata": {"source": "creator-agent-ui", "merchant_id": KNOWN_MERCHANT_ID},
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body.get("status") == "success"
    assert body.get("offers_count", 0) > 0
    assert body.get("metadata", {}).get("reason_code") == "OK"
    first_offer = (body.get("offers") or [])[0]
    assert first_offer.get("purchase_route") == "internal_checkout"
    items = first_offer.get("internal_checkout_items") or []
    assert items and items[0].get("product_id") == target_pid


def test_offers_resolve_stability_30_calls(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    from middleware.rate_limiter import RateLimitMiddleware
    import routes.agent_shop_gateway as gateway

    # This is a burst/stability assertion, not a rate-limit integration test.
    # The full sweep reuses the global FastAPI app and legitimately consumes
    # most of its anonymous ceiling before this late-running test. Start this
    # test with an isolated in-memory global bucket so its 30 calls measure the
    # resolver instead of suite-order leakage; production limits are unchanged.
    if app.middleware_stack is None:
        app.middleware_stack = app.build_middleware_stack()
    middleware = app.middleware_stack
    while middleware is not None and not isinstance(middleware, RateLimitMiddleware):
        middleware = getattr(middleware, "app", None)
    assert middleware is not None, "RateLimitMiddleware not found in app stack"
    monkeypatch.setattr(middleware, "redis", None)
    monkeypatch.delitem(middleware._anon_store, "global", raising=False)

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        values = values or {}
        if "FROM external_product_seeds" in q:
            return []
        if "FROM products_cache" in q and "platform_product_id = ANY(:pid_aliases)" in q:
            pid_aliases = set(values.get("pid_aliases") or [])
            if "9886500749640" in pid_aliases:
                return [_build_product_cache_row("9886500749640", KNOWN_PRODUCTS["9886500749640"])]
            return []
        if "FROM product_group_members" in q:
            pids = values.get("platform_product_ids") or []
            if "9886500749640" in pids:
                return [
                    {
                        "product_group_id": "pg_9886500749640",
                        "merchant_id": KNOWN_MERCHANT_ID,
                        "platform": "shopify",
                        "platform_product_id": "9886500749640",
                        "is_primary": True,
                    }
                ]
            return []
        if "FROM products_cache" in q and "merchant_id = ANY(:merchant_ids)" in q:
            return [_build_product_cache_row("9886500749640", KNOWN_PRODUCTS["9886500749640"])]
        return []

    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)

    for _ in range(30):
        res = client.post(
            "/agent/shop/v1/invoke",
            json={
                "operation": "offers.resolve",
                "payload": {"product": {"product_id": "9886500749640", "merchant_id": KNOWN_MERCHANT_ID}, "limit": 10},
                "metadata": {"source": "creator-agent-ui", "merchant_id": KNOWN_MERCHANT_ID},
            },
        )
        assert res.status_code == 200
        body = res.json()
        assert body.get("status") == "success"
        assert body.get("offers_count", 0) > 0
        assert body.get("metadata", {}).get("reason_code") == "OK"
