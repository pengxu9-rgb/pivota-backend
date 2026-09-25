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


async def _run(
    store,
    enrichment,
    *,
    platform="shopify",
    graphql_return=None,
    graphql_side_effect=None,
    token="tok",
    stores_side_effect=None,
    actor_kind=None,
):
    from services.shopify_content_writeback import publish_content_to_store
    if graphql_side_effect is not None:
        gql = AsyncMock(side_effect=graphql_side_effect)
    else:
        gql = AsyncMock(return_value=graphql_return if graphql_return is not None else _OK_GRAPHQL)
    stores_mock = (
        AsyncMock(side_effect=stores_side_effect)
        if stores_side_effect is not None
        else AsyncMock(return_value=([store] if store else []))
    )
    with patch("services.merchant_store_service.get_merchant_active_stores", new=stores_mock), \
         patch("services.shopify_access_token_service.resolve_shopify_admin_access_token",
               new=AsyncMock(return_value=(token, {}))), \
         patch("services.shopify_graphql_client.shopify_admin_graphql", new=gql):
        # actor_kind is REQUIRED by publish_content_to_store: the only write to a
        # merchant's live store must not inherit "approved" from an omitted argument.
        # These cases exercise the human-asked path; the model path has its own tests
        # in tests/services/test_merchant_write_guardrails_wiring.py.
        from services.merchant_write_guardrails import ACTOR_HUMAN
        res = await publish_content_to_store(
            merchant_id="m1", platform=platform, platform_product_id="p1",
            enrichment=enrichment,
            actor_kind=actor_kind if actor_kind is not None else ACTOR_HUMAN)
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


# --- the REAL re-consent path: a missing-scope token returns HTTP 403, which
# shopify_admin_graphql RAISES (not a 200-with-userError). This is what the
# Chydan canary hits first (its App-B token predates write_products). ---------


@pytest.mark.asyncio
async def test_needs_write_products_on_http_403_exception():
    res, gql = await _run(
        _store(content_writeback_status="enabled"), _enr(),
        graphql_side_effect=RuntimeError("Shopify GraphQL HTTP 403"),
    )
    assert res["status"] == "needs_write_products"
    gql.assert_awaited_once()


@pytest.mark.asyncio
async def test_error_on_http_500_exception():
    res, gql = await _run(
        _store(content_writeback_status="enabled"), _enr(),
        graphql_side_effect=RuntimeError("Shopify GraphQL HTTP 500"),
    )
    assert res["status"] == "error"  # a non-denied failure is an error, not re-consent
    gql.assert_awaited_once()


def test_looks_like_access_denied_classification():
    from services.shopify_content_writeback import _looks_like_access_denied
    assert _looks_like_access_denied("Shopify GraphQL HTTP 403") is True
    assert _looks_like_access_denied("401 Unauthorized") is True
    assert _looks_like_access_denied("ACCESS_DENIED: app not approved") is True
    assert _looks_like_access_denied("not approved for write_products") is True
    assert _looks_like_access_denied("Shopify GraphQL HTTP 500") is False
    assert _looks_like_access_denied("") is False


@pytest.mark.asyncio
async def test_error_when_no_admin_token():
    res, gql = await _run(_store(content_writeback_status="enabled"), _enr(), token=None)
    assert res["status"] == "error"
    assert res["message"] == "no_admin_token"
    gql.assert_not_awaited()  # no write attempted without a token


@pytest.mark.asyncio
async def test_error_on_unsupported_platform():
    # A non-shopify store passes the gate (expected_platform == its own platform)
    # but the write path only supports Shopify -> fail closed, no call.
    res, gql = await _run(
        _store(platform="wix", content_writeback_status="enabled"), _enr(),
        platform="wix",
    )
    assert res["status"] == "error"
    assert res["message"] == "unsupported_platform:wix"
    gql.assert_not_awaited()


@pytest.mark.asyncio
async def test_error_when_store_lookup_fails():
    res, gql = await _run(
        _store(content_writeback_status="enabled"), _enr(),
        stores_side_effect=Exception("db down"),
    )
    assert res["status"] == "error"
    assert res["message"] == "store_lookup_failed"
    gql.assert_not_awaited()
