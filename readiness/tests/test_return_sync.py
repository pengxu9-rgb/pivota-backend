from __future__ import annotations

import pytest

from readiness.order_sync import InMemoryReadinessJournal


@pytest.mark.asyncio
async def test_sync_returns_for_checkout_runs_shopify_sync_and_audit(monkeypatch):
    from readiness import service as readiness_service

    journal = InMemoryReadinessJournal()
    checkout = await journal.create_checkout_session(
        merchant_id="merch_efbc46b4619cfbdf",
        channel="ucp",
        variant_id="431000000001",
        quantity=1,
        payment_mode="merchant_native_alpha",
        session_payload={"merchant_alpha_mode": "real_merchant_alpha"},
        continue_url=None,
        idempotency_key=None,
    )
    await journal.update_checkout_session(checkout.checkout_id, order_id="ORD_RETURN_1")

    async def fake_get_order(order_id: str):
        assert order_id == "ORD_RETURN_1"
        return {
            "order_id": order_id,
            "shopify_order_id": "7001002003",
            "status": "open",
            "payment_status": "paid",
        }

    async def fake_get_primary_store(merchant_id: str):
        assert merchant_id == "merch_efbc46b4619cfbdf"
        return {
            "platform": "shopify",
            "domain": "alpha-beauty-demo.myshopify.com",
            "store_id": "store_alpha_1",
            "api_key": '{"access_token":"tok_alpha"}',
        }

    async def fake_get_shopify_cfg(_merchant_id: str):
        return {"shop_domain": "alpha-beauty-demo.myshopify.com", "api_version": "2024-07"}

    async def fake_resolve_token(**_kwargs):
        return "tok_alpha", {"refreshed": False}

    async def fake_sync_returns_best_effort(**kwargs):
        assert kwargs["merchant_id"] == "merch_efbc46b4619cfbdf"
        assert kwargs["shop_domain"] == "alpha-beauty-demo.myshopify.com"
        assert kwargs["access_token"] == "tok_alpha"
        assert kwargs["api_version"] == "2025-01"
        assert kwargs["limit"] == 15
        return {"ok": True, "fetched": 1, "upserted": 1}

    async def fake_build_order_sync_audit(merchant_id: str, checkout_id: str, *, sample_limit: int = 10):
        assert merchant_id == "merch_efbc46b4619cfbdf"
        assert checkout_id == checkout.checkout_id
        assert sample_limit == 4
        return {"checkout_id": checkout_id, "sync_signals": {"return_sync": {"status": "ready"}}}

    monkeypatch.setattr(readiness_service, "get_default_journal", lambda: journal)
    monkeypatch.setattr(readiness_service, "get_order", fake_get_order)
    monkeypatch.setattr(readiness_service, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(readiness_service, "_get_shopify_config_for_merchant", fake_get_shopify_cfg)
    monkeypatch.setattr(readiness_service, "resolve_shopify_admin_access_token", fake_resolve_token)
    monkeypatch.setattr(readiness_service, "sync_shopify_returns_best_effort", fake_sync_returns_best_effort)
    monkeypatch.setattr(readiness_service, "build_order_sync_audit", fake_build_order_sync_audit)

    result = await readiness_service.sync_returns_for_checkout(
        "merch_efbc46b4619cfbdf",
        checkout.checkout_id,
        api_version="2025-01",
        limit=15,
        sample_limit=4,
    )

    assert result["checkout"].checkout_id == checkout.checkout_id
    assert result["order"]["order_id"] == "ORD_RETURN_1"
    assert result["return_sync_result"]["ok"] is True
    assert result["audit"]["sync_signals"]["return_sync"]["status"] == "ready"


@pytest.mark.asyncio
async def test_sync_returns_for_checkout_requires_shopify_store(monkeypatch):
    from readiness import service as readiness_service

    journal = InMemoryReadinessJournal()
    checkout = await journal.create_checkout_session(
        merchant_id="merch_efbc46b4619cfbdf",
        channel="ucp",
        variant_id="431000000001",
        quantity=1,
        payment_mode="merchant_native_alpha",
        session_payload={"merchant_alpha_mode": "real_merchant_alpha"},
        continue_url=None,
        idempotency_key=None,
    )
    await journal.update_checkout_session(checkout.checkout_id, order_id="ORD_RETURN_2")

    async def fake_get_order(_order_id: str):
        return {"order_id": "ORD_RETURN_2", "shopify_order_id": "7001002004"}

    async def fake_get_primary_store(_merchant_id: str):
        return None

    async def fake_get_shopify_cfg(_merchant_id: str):
        return {}

    monkeypatch.setattr(readiness_service, "get_default_journal", lambda: journal)
    monkeypatch.setattr(readiness_service, "get_order", fake_get_order)
    monkeypatch.setattr(readiness_service, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(readiness_service, "_get_shopify_config_for_merchant", fake_get_shopify_cfg)

    with pytest.raises(ValueError) as exc_info:
        await readiness_service.sync_returns_for_checkout(
            "merch_efbc46b4619cfbdf",
            checkout.checkout_id,
        )

    detail = exc_info.value.args[0]
    assert detail["code"] == "CHECKOUT_RETURN_SYNC_UNAVAILABLE"
