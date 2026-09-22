"""Product/variant detail for observed external sellers, served by the gateway
(services/agent_product_detail_gateway_proxy.py).

What must hold:
* flag OFF (the default), or an onboarded merchant: the endpoint is exactly what it was;
* flag ON + a `merch_obs_*` seller: the caller's reference is resolved to ONE Pivota id, the
  gateway is asked with the caller's own key, and the answer comes back in this endpoint's
  contract -- every shade, with its SKU, price and currency; stock counts null, never 0;
* a reference that resolves to nothing, or to two products, is a 404, never a guess;
* a failed forward is a visible error, never the local path (which has no such merchant);
* a request that already went through a proxy is not forwarded again.
"""

from __future__ import annotations

from typing import Any, Dict, List

import httpx
import pytest

import routes.agent_products as agent_products
from main import app
from routes.agent_auth import get_agent_context
from services import agent_product_detail_gateway_proxy as detail_proxy
from services.agent_search_gateway_proxy import HOP_HEADER

MID = "merch_obs_88382424262f3e0f"
SIG = "sig_b978d922cbcb2f0c04ccf2e1a1e3b558"

# The gateway's get_product_detail answer for the Meitu gloss, trimmed from the live 2026-09-22 read.
GATEWAY_PRODUCT = {
    "product_id": SIG, "merchant_id": MID, "title": "LIP-PRESSION Metal Serum Gloss", "brand": "JUNGSAEMMOOL",
    "category": "Lip Gloss", "product_type": "Lip Gloss", "category_path": ["beauty", "makeup", "lip", "gloss"],
    "description": "A lip gloss with rich colour density.", "currency": "SGD", "source": "external_seed",
    "image_urls": ["https://jsmbeauty.sg/a.jpg", "https://jsmbeauty.sg/b.jpg"],
    "destination_url": "https://jsmbeauty.sg/products/lip-pression-metal-serum-gloss",
    "canonical_url": f"https://agent.pivota.cc/products/{SIG}", "pivota_signature_id": SIG,
    "default_variant_id": "50856826536257",
    "variants": [
        {"variant_id": "50856826536257", "sku_id": "32168999", "title": "Core Drop",
         "options": [{"name": "Color", "value": "Core Drop"}],
         "price": {"current": {"amount": 30, "currency": "SGD"}}, "availability": {"in_stock": True}},
        {"variant_id": "50865870831937", "sku_id": "32128937", "title": "Chai Tea",
         "options": [{"name": "Color", "value": "Chai Tea"}],
         "price": {"current": {"amount": 30, "currency": "SGD"}}, "availability": {"in_stock": False}},
    ],
}
CONTRACT_KEYS = {"id", "merchant_id", "currency", "title", "description", "vendor", "product_type",
                 "variants", "options", "images", "tags"}


class _Ctx:
    def __init__(self) -> None:
        self.agent_id = "agent_meitu"
        self.agent_name = "test"
        self.request = None
        self.allowed_merchants = None

    def can_access_merchant(self, merchant_id: str) -> bool:
        return self.allowed_merchants is None or merchant_id in self.allowed_merchants


class _FakeDB:
    """Answers the module's two lookups from a small table; records what it was asked."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.product_rows: Dict[str, List[str]] = {SIG: [SIG], "jungsaemmool-lip-pression-metal-serum-gloss": [SIG],
                                                   "two-products": [SIG, "sig_other"]}
        self.variant_rows: Dict[str, List[str]] = {"50856826536257": [SIG], "32168999": [SIG]}

    async def fetch_all(self, sql: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        self.calls.append(params)
        assert params["merchant_id"] == MID, "every lookup is scoped to the requested merchant"
        sigs = self.variant_rows.get(params["variant_id"], []) if "variant_id" in params else self.product_rows.get(params["ref"], [])
        return [{"pivota_signature_id": sig} for sig in sigs]


@pytest.fixture
def endpoint(monkeypatch: pytest.MonkeyPatch):
    ctx = _Ctx()
    db = _FakeDB()
    onboarding_calls: List[str] = []

    async def no_log(**kwargs: Any) -> None:
        return None

    async def onboarding(merchant_id: str):
        onboarding_calls.append(merchant_id)
        return None  # an observed seller is never onboarded: the local path 404s

    monkeypatch.setattr(agent_products, "database", db)
    monkeypatch.setattr(agent_products, "log_agent_request", no_log)
    monkeypatch.setattr(agent_products, "get_merchant_onboarding", onboarding)
    monkeypatch.setattr(agent_products.settings, "pivota_agent_internal_url", "http://gw.test")
    app.dependency_overrides[get_agent_context] = lambda: ctx
    yield ctx, db, onboarding_calls
    app.dependency_overrides.pop(get_agent_context, None)


def _mock_gateway(monkeypatch: pytest.MonkeyPatch, handler) -> List[httpx.Request]:
    seen: List[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    monkeypatch.setattr(detail_proxy, "_get_client", lambda: client)
    return seen


async def _get(path: str, headers: Dict[str, str] | None = None) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api.test") as client:
        return await client.get(path, headers={"X-API-Key": "ak_caller", **(headers or {})})


def _ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"product": GATEWAY_PRODUCT})


# --- the switch ---------------------------------------------------------------------------

def test_off_unless_explicitly_on_and_only_for_observed_sellers() -> None:
    assert detail_proxy.enabled_for(MID, {}, {}) == (False, "flag_off")
    on = {detail_proxy.FLAG: "on"}
    assert detail_proxy.enabled_for(MID, {}, on) == (True, "enabled")
    for merchant in ("merch_shopify_00d4a720d67d96c5dcba", "external_seed", "", None):
        assert detail_proxy.enabled_for(merchant, {}, on)[0] is False, merchant
    assert detail_proxy.enabled_for(MID, {HOP_HEADER: "1"}, on) == (False, "already_proxied")


@pytest.mark.asyncio
async def test_flag_off_the_endpoint_is_exactly_what_it_was(monkeypatch: pytest.MonkeyPatch, endpoint) -> None:
    _, db, onboarding_calls = endpoint
    monkeypatch.delenv(detail_proxy.FLAG, raising=False)
    seen = _mock_gateway(monkeypatch, _ok)
    resp = await _get(f"/agent/v1/products/merchants/{MID}/product/{SIG}")
    assert resp.status_code == 404
    assert seen == [] and db.calls == []
    assert onboarding_calls == [MID], "the local path ran"


# --- product detail -------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("ref", [SIG, "jungsaemmool-lip-pression-metal-serum-gloss"])
async def test_product_detail_is_served_by_the_gateway_in_this_contract(monkeypatch: pytest.MonkeyPatch, endpoint, ref: str) -> None:
    _, _, onboarding_calls = endpoint
    monkeypatch.setenv(detail_proxy.FLAG, "on")
    seen = _mock_gateway(monkeypatch, _ok)
    resp = await _get(f"/agent/v1/products/merchants/{MID}/product/{ref}")

    assert resp.status_code == 200
    assert onboarding_calls == [], "the local path does not run"
    assert len(seen) == 1
    sent = seen[0]
    assert str(sent.url) == "http://gw.test/agent/shop/v1/invoke"
    import json as _json
    assert _json.loads(sent.content) == {"operation": "get_product_detail",
                                         "payload": {"product": {"merchant_id": MID, "product_id": SIG}}}
    assert sent.headers["x-agent-api-key"] == "ak_caller", "the caller's own key, never a service credential"
    assert sent.headers[HOP_HEADER] == "1"

    body = resp.json()
    assert body["status"] == "success"
    product = body["product"]
    assert CONTRACT_KEYS <= set(product)
    assert (product["id"], product["merchant_id"], product["currency"]) == (SIG, MID, "SGD")
    assert (product["title"], product["vendor"]) == ("LIP-PRESSION Metal Serum Gloss", "JUNGSAEMMOOL")
    assert product["images"] == GATEWAY_PRODUCT["image_urls"]
    assert product["destination_url"] == GATEWAY_PRODUCT["destination_url"]
    assert product["served_by"] == "gateway"
    assert [(v["variant_id"], v["sku"], v["price"], v["currency"], v["available"]) for v in product["variants"]] == [
        ("50856826536257", "32168999", 30.0, "SGD", True),
        ("50865870831937", "32128937", 30.0, "SGD", False),
    ]
    assert all(v["inventory_quantity"] is None for v in product["variants"]), "an unknown count is null, never 0"
    assert product["options"] == [{"name": "Color", "values": ["Core Drop", "Chai Tea"]}]


@pytest.mark.asyncio
@pytest.mark.parametrize("ref,code", [("no-such-product", "DETAIL_NOT_FOUND"), ("two-products", "DETAIL_AMBIGUOUS")])
async def test_an_unresolvable_reference_is_a_404_never_a_guess(monkeypatch: pytest.MonkeyPatch, endpoint, ref: str, code: str) -> None:
    _, _, onboarding_calls = endpoint
    monkeypatch.setenv(detail_proxy.FLAG, "on")
    seen = _mock_gateway(monkeypatch, _ok)
    resp = await _get(f"/agent/v1/products/merchants/{MID}/product/{ref}")
    assert resp.status_code == 404
    assert resp.headers["x-error-code"] == code
    assert seen == [] and onboarding_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("gateway,status,code", [
    (lambda r: httpx.Response(404, json={"error": {"code": "NO_MERCHANT_OFFER"}}), 404, "GATEWAY_NO_MERCHANT_OFFER"),
    (lambda r: httpx.Response(503, json={}), 503, "GATEWAY_HTTP_503"),
    (lambda r: httpx.Response(500, json={}), 502, "GATEWAY_HTTP_500"),
])
async def test_a_failed_forward_is_a_visible_error_never_the_local_path(monkeypatch: pytest.MonkeyPatch, endpoint, gateway, status: int, code: str) -> None:
    _, _, onboarding_calls = endpoint
    monkeypatch.setenv(detail_proxy.FLAG, "on")
    _mock_gateway(monkeypatch, gateway)
    resp = await _get(f"/agent/v1/products/merchants/{MID}/product/{SIG}")
    assert resp.status_code == status
    assert resp.headers["x-error-code"] == code
    assert onboarding_calls == []


@pytest.mark.asyncio
async def test_a_proxied_request_is_not_forwarded_again(monkeypatch: pytest.MonkeyPatch, endpoint) -> None:
    _, _, onboarding_calls = endpoint
    monkeypatch.setenv(detail_proxy.FLAG, "on")
    seen = _mock_gateway(monkeypatch, _ok)
    resp = await _get(f"/agent/v1/products/merchants/{MID}/product/{SIG}", headers={HOP_HEADER: "1"})
    assert seen == []
    assert onboarding_calls == [MID] and resp.status_code == 404


@pytest.mark.asyncio
async def test_a_restricted_agent_is_still_refused_first(monkeypatch: pytest.MonkeyPatch, endpoint) -> None:
    ctx, db, _ = endpoint
    monkeypatch.setenv(detail_proxy.FLAG, "on")
    ctx.allowed_merchants = ["merch_other"]
    seen = _mock_gateway(monkeypatch, _ok)
    resp = await _get(f"/agent/v1/products/merchants/{MID}/product/{SIG}")
    assert resp.status_code == 403
    assert seen == [] and db.calls == []


# --- variant detail -------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["50856826536257", "32168999"])
async def test_variant_detail_resolves_the_variant_then_serves_the_product(monkeypatch: pytest.MonkeyPatch, endpoint, variant: str) -> None:
    _, db, onboarding_calls = endpoint
    monkeypatch.setenv(detail_proxy.FLAG, "on")
    seen = _mock_gateway(monkeypatch, _ok)
    resp = await _get(f"/agent/v1/products/merchants/{MID}/variant/{variant}")

    assert resp.status_code == 200
    assert onboarding_calls == []
    assert db.calls[-1]["variant_id"] == variant
    import json as _json
    assert _json.loads(seen[0].content)["payload"]["product"] == {"merchant_id": MID, "product_id": SIG, "sku_id": variant}
    body = resp.json()
    product = body["product"]
    assert product["id"] == SIG and body["selected_variant_id"] == variant
    assert product["variants"][0]["sku"] == "32168999"


@pytest.mark.asyncio
async def test_variant_detail_for_an_unknown_variant_is_a_404(monkeypatch: pytest.MonkeyPatch, endpoint) -> None:
    monkeypatch.setenv(detail_proxy.FLAG, "on")
    seen = _mock_gateway(monkeypatch, _ok)
    resp = await _get(f"/agent/v1/products/merchants/{MID}/variant/999")
    assert resp.status_code == 404 and seen == []


# --- the lookup's own SQL shape --------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_handle_is_matched_only_as_the_exact_external_key_shape() -> None:
    db = _FakeDB()
    await detail_proxy.resolve_signature(db, MID, product_ref="lip_gloss%")
    assert db.calls[-1]["ext_ref"] == "ext:lip\\_gloss\\%::%", "LIKE metacharacters in a caller's id are escaped"
    sig, why = await detail_proxy.resolve_signature(db, MID, product_ref=SIG)
    assert (sig, why) == (SIG, "sig")


@pytest.mark.asyncio
async def test_a_connected_merchants_variant_detail_still_runs_its_own_path(monkeypatch: pytest.MonkeyPatch, endpoint) -> None:
    """The variant route calls get_product_details internally for a connected non-Shopify store.
    That call must carry the request through (it now takes `req`), with the flag on."""
    monkeypatch.setenv(detail_proxy.FLAG, "on")
    seen = _mock_gateway(monkeypatch, _ok)

    async def onboarded(merchant_id: str):
        return {"merchant_id": merchant_id, "status": "active"}

    async def store(merchant_id: str):
        return {"platform": "wix", "store_id": "s1"}

    async def cache_row(**kwargs: Any):
        return {"product_data": {"product_id": kwargs["platform_product_id"], "title": "Wix Balm", "price": 12,
                                 "currency": "USD", "inventory_quantity": 3}, "cached_at": None}

    async def none(*args: Any, **kwargs: Any):
        return None

    monkeypatch.setattr(agent_products, "get_merchant_onboarding", onboarded)
    monkeypatch.setattr(agent_products.merchant_store_service, "get_primary_store", store)
    monkeypatch.setattr(agent_products, "get_product_cache_row", cache_row)
    monkeypatch.setattr(agent_products, "_redis_get_json", none)
    monkeypatch.setattr(agent_products, "_redis_set_json", none)
    monkeypatch.setattr(agent_products, "enrich_product_detail_with_payment_offers", none)
    monkeypatch.setattr(agent_products, "build_agent_push_projection_from_standard_product", lambda *a, **k: {})

    resp = await _get("/agent/v1/products/merchants/merch_wix_1/variant/v_123")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["product"]["title"] == "Wix Balm" and body["selected_variant_id"] == "v_123"
    assert "served_by" not in body["product"]
    assert seen == [], "a connected merchant is never forwarded"
