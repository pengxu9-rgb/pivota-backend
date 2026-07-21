"""A-F1.1 (funnel plan) — external-seed /r tokens carry stable click identity.

The external-seed card builders in routes/agent_api.py and
routes/agent_sdk_fixed.py previously minted tokens whose ctx held only
source/seed ids: at /r time record_surface_event minted a THROWAWAY click id
with NULL merchant, and the destination lacked utm_content (the WooCommerce
order-side join key) — clicks from those surfaces could never bind to a
conversion. Covers, for BOTH builders (decoding the real signed token):
  - ctx carries pvt_click_id (clk_…), pvt_surface, tool, join_mode=referral_only;
  - the SAME click id rides the destination as pvt_click_id= and utm_content=;
  - stored seller_ref/seed_kind + anchor merchant (from attached_product_key)
    thread through when present, and are ABSENT for legacy seeds (ADR-009
    no-fallback: never invent identity at mint time).
"""

from __future__ import annotations

from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

import pytest

from services.outbound_links_service import parse_and_verify_redirect_token  # noqa: E402


class _FakeReq:
    base_url = "https://api.pivota.cc/"


def _seed_row(**over: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "id": "seed_af11",
        "external_product_id": "ext_p1",
        "market": "US",
        "tool": "find_products",
        "utm_template": None,
        "destination_url": "https://brand.example/products/serum",
        "canonical_url": "https://brand.example/products/serum",
        "domain": "brand.example",
        "title": "Test Serum",
        "image_url": None,
        "price_amount": 25.0,
        "price_currency": "USD",
        "availability": "in_stock",
        "seed_data": {"snapshot": {"title": "Test Serum"}},
        # Canonical STORAGE form (double-colon), prod::merchant::platform::pid —
        # this is what the column actually holds (IDENTITY_REFERENCE §2). The
        # earlier fixture used the never-persisted pipe form and masked the
        # delimiter bug.
        "attached_product_key": "prod::merch_anchor::shopify::prod_1",
        "seller_ref": "merch_obs_brand_example",
        "seed_kind": "cross",
    }
    base.update(over)
    return base


def _decode(redirect_url: str) -> Dict[str, Any]:
    token = parse_qs(urlparse(redirect_url).query)["token"][0]
    return parse_and_verify_redirect_token(token)


def _dest_params(payload: Dict[str, Any]) -> Dict[str, list]:
    return parse_qs(urlparse(payload["dest"]).query)


async def _build(module, seed_row: Dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Optional[Dict[str, Any]]:
    # The referral-runtime readiness gate is DB-backed and not the subject
    # under test — stub it open so the token mint itself is exercised.
    async def _gate_open(row, **kwargs):
        return (False, None)

    monkeypatch.setattr(module, "should_block_external_referral_runtime", _gate_open)
    return await module._build_external_seed_product(
        req=_FakeReq(),
        seed_row=seed_row,
        allowed_domains=["brand.example"],
    )


@pytest.mark.parametrize("module_path", ["routes.agent_api", "routes.agent_sdk_fixed"])
@pytest.mark.asyncio
async def test_token_ctx_carries_stable_click_identity(module_path: str, monkeypatch: pytest.MonkeyPatch):
    import importlib

    module = importlib.import_module(module_path)
    product = await _build(module, _seed_row(), monkeypatch)
    assert product is not None, f"{module_path} builder returned None"

    payload = _decode(product["external_redirect_url"])
    ctx = payload["ctx"]

    # stable click id, surface, join mode
    click_id = ctx.get("pvt_click_id")
    assert click_id and click_id.startswith("clk_")
    assert ctx.get("pvt_surface") == "find_products"
    assert ctx.get("tool") == "find_products"
    assert ctx.get("join_mode") == "referral_only"
    # legacy keys preserved
    assert ctx.get("source") == "external_seed"
    assert ctx.get("external_seed_id") == "seed_af11"

    # the SAME id rides the destination as both referral carriers
    params = _dest_params(payload)
    assert params.get("pvt_click_id") == [click_id]
    assert params.get("utm_content") == [click_id]

    # stored identity threads through
    assert ctx.get("merchant_id") == "merch_anchor"
    assert ctx.get("seller_ref") == "merch_obs_brand_example"
    assert ctx.get("seed_kind") == "cross"


@pytest.mark.parametrize("module_path", ["routes.agent_api", "routes.agent_sdk_fixed"])
@pytest.mark.asyncio
async def test_legacy_seed_threads_no_invented_identity(module_path: str, monkeypatch: pytest.MonkeyPatch):
    """ADR-009 no-fallback: a legacy seed (no seller_ref/seed_kind, no
    attachment) still gets a stable click id, but NO merchant/seller keys."""
    import importlib

    module = importlib.import_module(module_path)
    product = await _build(
        module,
        _seed_row(attached_product_key=None, seller_ref=None, seed_kind=None),
        monkeypatch,
    )
    assert product is not None

    payload = _decode(product["external_redirect_url"])
    ctx = payload["ctx"]
    assert ctx.get("pvt_click_id", "").startswith("clk_")
    assert "merchant_id" not in ctx
    assert "seller_ref" not in ctx
    assert "seed_kind" not in ctx


@pytest.mark.parametrize("module_path", ["routes.agent_api", "routes.agent_sdk_fixed"])
@pytest.mark.asyncio
async def test_anchor_from_pipe_transport_form_also_parses(module_path: str, monkeypatch: pytest.MonkeyPatch):
    """The canonical extractor also defends the never-persisted pipe transport
    form, so a legacy pipe key still yields the anchor merchant."""
    import importlib

    module = importlib.import_module(module_path)
    product = await _build(
        module, _seed_row(attached_product_key="merch_anchor|shopify|prod_1"), monkeypatch
    )
    ctx = _decode(product["external_redirect_url"])["ctx"]
    assert ctx.get("merchant_id") == "merch_anchor"


@pytest.mark.parametrize("module_path", ["routes.agent_api", "routes.agent_sdk_fixed"])
@pytest.mark.asyncio
async def test_each_mint_gets_a_fresh_click_id(module_path: str, monkeypatch: pytest.MonkeyPatch):
    """Click ids must be per-mint (per card render), not shared."""
    import importlib

    module = importlib.import_module(module_path)
    p1 = await _build(module, _seed_row(), monkeypatch)
    p2 = await _build(module, _seed_row(), monkeypatch)
    c1 = _decode(p1["external_redirect_url"])["ctx"]["pvt_click_id"]
    c2 = _decode(p2["external_redirect_url"])["ctx"]["pvt_click_id"]
    assert c1 != c2


def test_gateway_identity_parses_double_colon_storage_form():
    """The gateway's external-seed identity (the highest-traffic surface) shares
    the delimiter fix: the double-colon STORAGE form must yield the anchor
    merchant/platform/product, not None (the old pipe-only parse dropped them)."""
    import routes.agent_shop_gateway as gw

    ident = gw._external_seed_redirect_identity(
        row={"attached_product_key": "prod::merch_anchor::shopify::123", "domain": "brand.example"},
        seed_data={},
    )
    assert ident["merchant_id"] == "merch_anchor"
    assert ident["platform"] == "shopify"
    assert ident["product_id"] == "prod::merch_anchor::shopify::123"

    # standalone (no attached key) → no invented merchant (honest referral)
    standalone = gw._external_seed_redirect_identity(
        row={"attached_product_key": None, "domain": "brand.example"}, seed_data={}
    )
    assert standalone["merchant_id"] is None
