from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres")
os.chdir(REPO_ROOT)

from main import app


@pytest.mark.asyncio
async def test_shopify_access_scopes_preflight_returns_live_scope_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.shopify_promotions_sync_api as route_module

    class _Cfg:
        shop_domain = "example.myshopify.com"

    async def fake_get_cfg(_merchant_id: str):
        return _Cfg()

    async def fake_fetch_scopes(_cfg, **_kwargs):
        return ["read_products", "read_discounts", "write_discounts", "read_customers"]

    monkeypatch.setenv("PROMOTIONS_ADMIN_KEY", "test_admin_key")
    monkeypatch.setattr(route_module, "get_shopify_config_for_merchant", fake_get_cfg)
    monkeypatch.setattr(route_module, "_fetch_access_scopes_for_config", fake_fetch_scopes)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/agent/internal/shopify/promotions/preflight/merch_1/access-scopes",
            headers={"X-ADMIN-KEY": "test_admin_key"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["shop_domain"] == "example.myshopify.com"
    assert body["access_scopes"] == [
        "read_products",
        "read_discounts",
        "write_discounts",
        "read_customers",
    ]
    assert body["has_read_discounts"] is True
    assert body["has_write_discounts"] is True
    assert body["has_read_customers"] is True
