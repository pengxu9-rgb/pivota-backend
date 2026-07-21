from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient


from main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_agent_get_product_uses_stale_cache_fallback(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    import routes.agent_api as agent_api_module

    calls = []

    async def fake_load_cached_product_for_agent_detail(
        *,
        merchant_id: str,
        product_ref: str,
        include_expired: bool = False,
        stale_cutoff=None,
    ):
        calls.append(
            {
                "merchant_id": merchant_id,
                "product_ref": product_ref,
                "include_expired": include_expired,
                "has_stale_cutoff": stale_cutoff is not None,
            }
        )
        if include_expired:
            return {
                "id": "9886500127048",
                "product_id": "9886500127048",
                "title": "IPSA Time Reset Aqua",
                "price": 45.0,
                "currency": "USD",
            }
        return None

    monkeypatch.setattr(
        agent_api_module,
        "_load_cached_product_for_agent_detail",
        fake_load_cached_product_for_agent_detail,
    )
    monkeypatch.setattr(
        agent_api_module,
        "_load_upstream_product_for_agent_detail",
        AsyncMock(return_value={"product": None, "query_source": None}),
    )
    monkeypatch.setattr(agent_api_module, "_context_can_access_merchant", lambda _ctx, _mid: True)
    monkeypatch.setattr(agent_api_module, "log_agent_request", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_api_module, "AGENT_PRODUCT_DETAIL_STALE_FALLBACK_ENABLED", True)
    monkeypatch.setattr(agent_api_module, "AGENT_PRODUCT_DETAIL_STALE_MAX_AGE_HOURS", 336)

    response = client.get(
        "/agent/v1/products/merch_test/9886500127048",
        headers={"X-API-Key": "test-api-key"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["product"]["product_id"] == "9886500127048"
    assert payload.get("metadata", {}).get("detail_source") == "stale_cache"
    assert payload.get("metadata", {}).get("cache_source") == "products_cache_stale_fallback"
    assert len(calls) == 2
    assert calls[0]["include_expired"] is False
    assert calls[1]["include_expired"] is True
    assert calls[1]["has_stale_cutoff"] is True


def test_agent_get_product_without_stale_fallback_returns_404(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    import routes.agent_api as agent_api_module

    calls = []

    async def fake_load_cached_product_for_agent_detail(
        *,
        merchant_id: str,
        product_ref: str,
        include_expired: bool = False,
        stale_cutoff=None,
    ):
        calls.append(include_expired)
        return None

    monkeypatch.setattr(
        agent_api_module,
        "_load_cached_product_for_agent_detail",
        fake_load_cached_product_for_agent_detail,
    )
    monkeypatch.setattr(
        agent_api_module,
        "_load_upstream_product_for_agent_detail",
        AsyncMock(return_value={"product": None, "query_source": None}),
    )
    monkeypatch.setattr(agent_api_module, "_context_can_access_merchant", lambda _ctx, _mid: True)
    monkeypatch.setattr(agent_api_module, "AGENT_PRODUCT_DETAIL_STALE_FALLBACK_ENABLED", False)

    response = client.get(
        "/agent/v1/products/merch_test/does-not-exist",
        headers={"X-API-Key": "test-api-key"},
    )
    assert response.status_code == 404
    assert response.json().get("detail") == "Product not found"
    assert calls == [False]


def test_agent_get_product_prefers_upstream_before_stale(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    import routes.agent_api as agent_api_module

    async def fake_load_cached_product_for_agent_detail(
        *,
        merchant_id: str,
        product_ref: str,
        include_expired: bool = False,
        stale_cutoff=None,
    ):
        return None

    monkeypatch.setattr(
        agent_api_module,
        "_load_cached_product_for_agent_detail",
        fake_load_cached_product_for_agent_detail,
    )
    monkeypatch.setattr(
        agent_api_module,
        "_load_upstream_product_for_agent_detail",
        AsyncMock(
            return_value={
                "product": {
                    "id": "9886500127048",
                    "product_id": "9886500127048",
                    "title": "IPSA Time Reset Aqua",
                    "price": 45.0,
                    "currency": "USD",
                },
                "query_source": "realtime",
            }
        ),
    )
    monkeypatch.setattr(agent_api_module, "_context_can_access_merchant", lambda _ctx, _mid: True)
    monkeypatch.setattr(agent_api_module, "log_agent_request", AsyncMock(return_value=None))

    response = client.get(
        "/agent/v1/products/merch_test/9886500127048",
        headers={"X-API-Key": "test-api-key"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["product"]["product_id"] == "9886500127048"
    assert payload.get("metadata", {}).get("detail_source") == "upstream"


def test_normalize_product_detail_payload_backfills_variant_price() -> None:
    import routes.agent_api as agent_api_module

    normalized = agent_api_module._normalize_product_detail_payload(
        {
            "id": "9886500127048",
            "title": "IPSA Time Reset Aqua",
            "price": None,
            "variants": [
                {"id": "variant_1", "price": "43.5"},
            ],
        },
        fallback_product_id="9886500127048",
        merchant_id="merch_test",
    )

    assert normalized["price"] == 43.5
    assert normalized["product_id"] == "9886500127048"
    assert normalized["merchant_id"] == "merch_test"


def test_normalize_product_detail_payload_keeps_price_null_when_missing() -> None:
    import routes.agent_api as agent_api_module

    normalized = agent_api_module._normalize_product_detail_payload(
        {
            "id": "9886500749640",
            "title": "Winona Soothing Repair Serum",
            "variants": [],
        },
        fallback_product_id="9886500749640",
    )

    assert normalized["price"] is None
    assert normalized["product_id"] == "9886500749640"
