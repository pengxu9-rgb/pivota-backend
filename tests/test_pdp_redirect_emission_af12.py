"""A-F1.2 (funnel plan) — get_product_detail emits an attributed /r link.

The PDP builder previously returned a card with NO external_redirect_url, so a
buyer landing on a product-detail card had no attributed path to checkout. It
now runs the same _attach_connected_product_redirects post-pass find_products
uses. This asserts the wiring end-to-end: a connected Shopify PDP card comes
back carrying a decodable /r token (cart_permalink join for a numeric variant).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import routes.agent_shop_gateway as gw  # noqa: E402
import services.merchant_store_service as mss  # noqa: E402
from models.standard_product import StandardProduct  # noqa: E402
from services.outbound_links_service import parse_and_verify_redirect_token  # noqa: E402


@pytest.mark.asyncio
async def test_pdp_card_carries_attributed_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    product = StandardProduct(
        id="9990001",
        product_id="9990001",
        merchant_id="merch_conn",
        platform="shopify",
        title="Connected Serum",
        price=18.0,
        currency="USD",
        handle="connected-serum",
    )
    # variant with a numeric id → cart_permalink join
    from models.standard_product import StandardProductVariant

    product.variants = [StandardProductVariant(id="46000000001", title="Default", price=18.0)]

    async def fake_load(product_id, *, merchant_id):
        return product

    async def fake_stores(merchant_id: str) -> List[Dict[str, Any]]:
        return [{"platform": "shopify", "domain": "conn-store.myshopify.com", "status": "active"}]

    async def _noop_enrich(*a, **k):
        return None

    from fastapi import BackgroundTasks

    monkeypatch.setattr(gw, "_load_product_by_id", fake_load)
    monkeypatch.setattr(mss, "get_merchant_active_stores", fake_stores)
    monkeypatch.setattr(gw, "enrich_product_detail_with_payment_offers", _noop_enrich)
    monkeypatch.setattr(gw, "_reviews_enabled", lambda: False)

    result = await gw._handle_get_product_detail(
        gw.ProductRef(merchant_id="merch_conn", product_id="9990001"),
        BackgroundTasks(),
    )

    card = result["product"]
    url = card.get("external_redirect_url")
    assert url and "/r?token=" in url
    token = parse_qs(urlparse(url).query)["token"][0]
    payload = parse_and_verify_redirect_token(token)
    assert "conn-store.myshopify.com/cart/46000000001:1" in payload["dest"]
    ctx = payload.get("ctx") or {}
    assert ctx.get("merchant_id") == "merch_conn"
    assert ctx.get("join_mode") == "cart_permalink"


@pytest.mark.asyncio
async def test_pdp_emission_is_fail_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    """A store-lookup failure must not break the PDP response."""
    product = StandardProduct(
        id="p1", product_id="p1", merchant_id="merch_conn", platform="shopify",
        title="X", price=1.0, currency="USD", handle="x",
    )

    async def fake_load(product_id, *, merchant_id):
        return product

    async def boom(merchant_id: str):
        raise RuntimeError("db down")

    async def _noop_enrich(*a, **k):
        return None

    from fastapi import BackgroundTasks

    monkeypatch.setattr(gw, "_load_product_by_id", fake_load)
    monkeypatch.setattr(mss, "get_merchant_active_stores", boom)
    monkeypatch.setattr(gw, "enrich_product_detail_with_payment_offers", _noop_enrich)
    monkeypatch.setattr(gw, "_reviews_enabled", lambda: False)

    result = await gw._handle_get_product_detail(
        gw.ProductRef(merchant_id="merch_conn", product_id="p1"),
        BackgroundTasks(),
    )
    # PDP still returns the product; just no redirect (no derivable destination)
    assert result["product"]["id"] == "p1"
    assert not result["product"].get("external_redirect_url")
