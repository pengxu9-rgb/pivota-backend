"""GET /agent/v1/products/{merchant}/{SKU-*} must not fabricate a product (FIX_PLAN D-10).

The route used to answer ANY `SKU-` id with a made-up "Platform Order Product" at 10.00 USD with stock
999, before looking anything up. An unknown SKU id now gets the same 404 as any other unknown id, and a
SKU id the catalog does hold is served from the catalog like any other.
"""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from main import app


# The fabricated product's marks: its title, and its stock of 999. Checked on the parsed answer, value by value, never
# as a substring of the whole body: the error envelope's timestamp (microseconds such as ".383999") and request id are
# free text that can carry "999" on any run.
FABRICATED_TITLE = "Platform Order Product"
FABRICATED_NUMBER = 999
ENVELOPE_FREE_TEXT = frozenset({"timestamp", "request_id"})


def fabrication_marks(payload, path="$"):
    """Every place in a parsed JSON answer that carries a mark of the fabricated product."""
    marks = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in ENVELOPE_FREE_TEXT and isinstance(value, str):
                continue
            here = f"{path}.{key}"
            if isinstance(value, (dict, list)):
                marks += fabrication_marks(value, here)
                continue
            if isinstance(value, str) and FABRICATED_TITLE in value:
                marks.append(here)
            elif not isinstance(value, bool) and isinstance(value, (int, float)) and value == FABRICATED_NUMBER:
                marks.append(here)
            elif isinstance(value, str) and value.strip() == str(FABRICATED_NUMBER):
                marks.append(here)
            elif isinstance(value, str) and (key == "sku" or key == "id" or key.endswith("_id")) and str(FABRICATED_NUMBER) in value:
                marks.append(here)
    elif isinstance(payload, list):
        for i, value in enumerate(payload):
            here = f"{path}[{i}]"
            if isinstance(value, (dict, list)):
                marks += fabrication_marks(value, here)
            elif (isinstance(value, str) and (FABRICATED_TITLE in value or value.strip() == str(FABRICATED_NUMBER))) or (
                not isinstance(value, bool) and isinstance(value, (int, float)) and value == FABRICATED_NUMBER
            ):
                marks.append(here)
    return marks


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def lookups(monkeypatch: pytest.MonkeyPatch):
    import routes.agent_api as agent_api_module

    calls = {"cache": [], "upstream": []}
    found = {}

    async def fake_cache(*, merchant_id, product_ref, include_expired=False, stale_cutoff=None):
        calls["cache"].append((merchant_id, product_ref, include_expired))
        return None if include_expired else found.get(product_ref)

    async def fake_upstream(*, merchant_id, product_ref, context):
        calls["upstream"].append((merchant_id, product_ref))
        return {"product": None, "query_source": None}

    monkeypatch.setattr(agent_api_module, "_load_cached_product_for_agent_detail", fake_cache)
    monkeypatch.setattr(agent_api_module, "_load_upstream_product_for_agent_detail", fake_upstream)
    monkeypatch.setattr(agent_api_module, "_context_can_access_merchant", lambda _ctx, _mid: True)
    monkeypatch.setattr(agent_api_module, "log_agent_request", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_api_module, "AGENT_PRODUCT_DETAIL_STALE_FALLBACK_ENABLED", True)
    monkeypatch.setattr(agent_api_module, "AGENT_PRODUCT_DETAIL_STALE_MAX_AGE_HOURS", 336)
    return calls, found


@pytest.mark.parametrize("product_id", ["SKU-12345", "SKU-ABC-1", "SKU-"])
def test_an_unknown_sku_id_is_the_normal_not_found(client, lookups, product_id):
    calls, _ = lookups
    response = client.get(f"/agent/v1/products/merch_test/{product_id}", headers={"X-API-Key": "test-api-key"})
    assert response.status_code == 404
    assert response.json().get("detail") == "Product not found"
    assert fabrication_marks(response.json()) == []
    # It took the ordinary lookup: fresh cache, then upstream, then the bounded stale cache.
    assert calls["cache"] == [("merch_test", product_id, False), ("merch_test", product_id, True)]
    assert calls["upstream"] == [("merch_test", product_id)]


def test_a_sku_id_the_catalog_holds_is_served_from_the_catalog(client, lookups):
    _, found = lookups
    found["SKU-777"] = {"id": "SKU-777", "product_id": "SKU-777", "title": "Real Serum", "price": 24.0, "currency": "EUR"}
    response = client.get("/agent/v1/products/merch_test/SKU-777", headers={"X-API-Key": "test-api-key"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["metadata"]["detail_source"] == "fresh_cache"
    assert payload["product"]["title"] == "Real Serum"
    assert payload["product"]["price"] == 24.0
    assert payload["product"].get("stock") != 999
    assert fabrication_marks(payload) == []


def test_the_fabrication_check_reads_values_not_the_body_text():
    # The product the route used to make up is caught wherever it lands.
    made_up = {
        "status": "success",
        "product": {
            "id": "SKU-12345", "title": "Platform Order Product SKU-12345", "price": 10.0, "currency": "USD",
            "stock": 999, "available": True,
            "variants": [{"id": "SKU-12345", "title": "Default", "price": 10.0, "sku": "SKU-12345", "available": True}],
        },
    }
    assert fabrication_marks(made_up) == ["$.product.title", "$.product.stock"]
    assert fabrication_marks({"offers": [{"offer_id": "of_999x"}], "product": {"variants": [{"sku": "SKU-999"}]}}) == [
        "$.offers[0].offer_id", "$.product.variants[0].sku",
    ]
    assert fabrication_marks({"items": [999, "999"]}) == ["$.items[0]", "$.items[1]"]
    # A 404 envelope whose clock or request id happens to read 999 is not a fabricated product.
    envelope = {
        "status": "error",
        "error": {"code": "NOT_FOUND", "timestamp": "2026-11-18T00:28:44.383999Z", "request_id": "4aa1610c-999-42aa"},
        "detail": "Product not found",
    }
    assert fabrication_marks(envelope) == []
