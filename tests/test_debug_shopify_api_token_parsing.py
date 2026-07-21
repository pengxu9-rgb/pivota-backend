import pytest


@pytest.mark.asyncio
async def test_debug_shopify_api_extracts_access_token_from_json(monkeypatch):
    import httpx

    from routes import debug_shopify_api

    async def fake_fetch_one(_query: str, _values: dict):
        return {
            "domain": "shop.myshopify.com",
            "api_key": '{"access_token":"shpat_good"}',
            "status": "active",
            "connected_at": None,
        }

    monkeypatch.setattr(debug_shopify_api.database, "fetch_one", fake_fetch_one)

    observed = {}

    class DummyResponse:
        def __init__(self, status_code: int):
            self.status_code = status_code
            self.text = ""

        def json(self):
            return {"products": [{"id": 1}, {"id": 2}]}

    async def fake_get(self, _url: str, **kwargs):
        headers = kwargs.get("headers") or {}
        observed["token"] = headers.get("X-Shopify-Access-Token")
        return DummyResponse(200)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get, raising=True)

    out = await debug_shopify_api.test_shopify_api("merch_1")
    assert out["status"] == "success"
    assert observed["token"] == "shpat_good"

