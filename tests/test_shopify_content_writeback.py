"""publish_content_to_store — the gated app-owned-metafield write. All deps
mocked (store, token, GraphQL); the gate (store_content_writeback_context) runs
for real off the store dict. Asserts the safety invariants: blocked unless opted
in, never writes body_html, ACCESS_DENIED -> needs re-consent."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


def _store(**kw):
    base = {
        "store_id": "s1", "platform": "shopify", "status": "active",
        "domain": "shop.myshopify.com", "api_key_raw": "{}",
    }
    base.update(kw)
    return base


def _enr(**kw):
    base = {
        "title_override": "T", "summary_short": "S",
        "description_markdown": "D" * 50, "bullet_points": ["a"],
    }
    base.update(kw)
    return base


_OK_GRAPHQL = {"metafieldsSet": {"metafields": [{"id": "gid://1", "namespace": "pivota", "key": "ai_pdp"}], "userErrors": []}}


async def _run(store, enrichment, *, graphql_return=None, token="tok"):
    from services.shopify_content_writeback import publish_content_to_store
    gql = AsyncMock(return_value=graphql_return if graphql_return is not None else _OK_GRAPHQL)
    with patch("services.merchant_store_service.get_merchant_active_stores",
               new=AsyncMock(return_value=([store] if store else []))), \
         patch("services.shopify_access_token_service.resolve_shopify_admin_access_token",
               new=AsyncMock(return_value=(token, {}))), \
         patch("services.shopify_graphql_client.shopify_admin_graphql", new=gql):
        res = await publish_content_to_store(
            merchant_id="m1", platform="shopify", platform_product_id="p1",
            enrichment=enrichment)
    return res, gql


@pytest.mark.asyncio
async def test_blocked_when_store_disabled():
    res, gql = await _run(_store(content_writeback_status="disabled"), _enr())
    assert res["status"] == "blocked"
    gql.assert_not_awaited()  # never reaches the write when the gate is closed


@pytest.mark.asyncio
async def test_written_when_enabled_and_never_touches_body_html():
    res, gql = await _run(_store(content_writeback_status="enabled"), _enr())
    assert res["status"] == "written"
    assert res["metafield"] == {"namespace": "pivota", "key": "ai_pdp"}
    gql.assert_awaited_once()
    sent = gql.await_args.kwargs["variables"]["metafields"][0]
    assert sent["ownerId"] == "gid://shopify/Product/p1"
    assert sent["namespace"] == "pivota" and sent["key"] == "ai_pdp"
    # SAFETY: an app-owned metafield only — body_html is never in the payload.
    assert "body_html" not in str(gql.await_args.kwargs)


@pytest.mark.asyncio
async def test_no_copy_when_enrichment_empty():
    res, gql = await _run(
        _store(content_writeback_status="enabled"),
        {"title_override": "", "summary_short": "", "description_markdown": "", "bullet_points": []},
    )
    assert res["status"] == "no_copy"
    gql.assert_not_awaited()


@pytest.mark.asyncio
async def test_needs_write_products_on_access_denied():
    denied = {"metafieldsSet": {"metafields": [], "userErrors": [
        {"field": None, "message": "not approved", "code": "ACCESS_DENIED"}]}}
    res, gql = await _run(_store(content_writeback_status="enabled"), _enr(), graphql_return=denied)
    assert res["status"] == "needs_write_products"


@pytest.mark.asyncio
async def test_store_missing():
    res, gql = await _run(None, _enr())
    assert res["status"] == "store_missing"
    gql.assert_not_awaited()


@pytest.mark.asyncio
async def test_canary_blocks_other_product():
    store = _store(content_writeback_status="canary", content_writeback_canary_product_id="p_other")
    res, gql = await _run(store, _enr())  # writing p1, canary is p_other
    assert res["status"] == "blocked"
    assert res["blocker"] == "canary_product_mismatch"
    gql.assert_not_awaited()
