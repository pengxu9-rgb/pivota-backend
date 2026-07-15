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
        return {"shop_domain": "alpha-beauty-demo.myshopify.com", "api_version": "2025-10"}

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


@pytest.mark.asyncio
async def test_probe_return_eligibility_for_checkout_marks_likely_eligible(monkeypatch):
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
    await journal.update_checkout_session(checkout.checkout_id, order_id="ORD_RETURN_ELIGIBLE_1")

    async def fake_get_order(order_id: str):
        assert order_id == "ORD_RETURN_ELIGIBLE_1"
        return {
            "order_id": order_id,
            "shopify_order_id": "7001002005",
            "status": "open",
            "payment_status": "paid",
            "fulfillment_status": "fulfilled",
        }

    async def fake_get_primary_store(_merchant_id: str):
        return {
            "platform": "shopify",
            "domain": "alpha-beauty-demo.myshopify.com",
            "store_id": "store_alpha_1",
            "api_key": '{"access_token":"tok_alpha"}',
        }

    async def fake_get_shopify_cfg(_merchant_id: str):
        return {"shop_domain": "alpha-beauty-demo.myshopify.com", "api_version": "2025-10"}

    async def fake_resolve_token(**_kwargs):
        return "tok_alpha", {"refreshed": False}

    async def fake_probe_shopify_return_eligibility_best_effort(**kwargs):
        assert kwargs["shopify_order_id"] == "7001002005"
        return {
            "ok": True,
            "shopify_order": {
                "id": 7001002005,
                "financial_status": "paid",
                "fulfillment_status": "fulfilled",
            },
            "order_probe": {"returnStatus": "NO_RETURN"},
            "existing_returns": [],
            "return_capabilities": {
                "queryroot_returnable_fulfillments_available": True,
                "queryroot_returnable_fulfillment_available": True,
                "order_return_status_available": True,
                "order_returns_available": True,
            },
            "schema_diag": {"order_returnish_fields": ["returnStatus", "returns"]},
        }

    async def fake_build_order_sync_audit(merchant_id: str, checkout_id: str, *, sample_limit: int = 10):
        assert merchant_id == "merch_efbc46b4619cfbdf"
        assert checkout_id == checkout.checkout_id
        assert sample_limit == 4
        return {
            "checkout_id": checkout_id,
            "order_state": {
                "status": "open",
                "payment_status": "paid",
                "fulfillment_status": "fulfilled",
                "shopify_order_id": "7001002005",
            },
            "evidence": {"return_records": []},
            "sync_signals": {"return_sync": {"status": "not_observed"}},
        }

    monkeypatch.setattr(readiness_service, "get_default_journal", lambda: journal)
    monkeypatch.setattr(readiness_service, "get_order", fake_get_order)
    monkeypatch.setattr(readiness_service, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(readiness_service, "_get_shopify_config_for_merchant", fake_get_shopify_cfg)
    monkeypatch.setattr(readiness_service, "resolve_shopify_admin_access_token", fake_resolve_token)
    monkeypatch.setattr(
        readiness_service,
        "probe_shopify_return_eligibility_best_effort",
        fake_probe_shopify_return_eligibility_best_effort,
    )
    monkeypatch.setattr(readiness_service, "build_order_sync_audit", fake_build_order_sync_audit)

    result = await readiness_service.probe_return_eligibility_for_checkout(
        "merch_efbc46b4619cfbdf",
        checkout.checkout_id,
        sample_limit=4,
    )

    assert result["eligibility"]["status"] == "likely_eligible"
    assert result["eligibility"]["blockers"] == []
    assert result["eligibility"]["resolved_payment_status"] == "paid"
    assert result["eligibility"]["resolved_fulfillment_status"] == "fulfilled"


@pytest.mark.asyncio
async def test_probe_return_eligibility_for_checkout_flags_unfulfilled_or_refunded(monkeypatch):
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
    await journal.update_checkout_session(checkout.checkout_id, order_id="ORD_RETURN_ELIGIBLE_2")

    async def fake_get_order(order_id: str):
        assert order_id == "ORD_RETURN_ELIGIBLE_2"
        return {
            "order_id": order_id,
            "shopify_order_id": "7001002006",
            "status": "open",
            "payment_status": "refunded",
            "fulfillment_status": "processing",
        }

    async def fake_get_primary_store(_merchant_id: str):
        return {
            "platform": "shopify",
            "domain": "alpha-beauty-demo.myshopify.com",
            "store_id": "store_alpha_1",
            "api_key": '{"access_token":"tok_alpha"}',
        }

    async def fake_get_shopify_cfg(_merchant_id: str):
        return {"shop_domain": "alpha-beauty-demo.myshopify.com", "api_version": "2025-10"}

    async def fake_resolve_token(**_kwargs):
        return "tok_alpha", {"refreshed": False}

    async def fake_probe_shopify_return_eligibility_best_effort(**_kwargs):
        return {
            "ok": True,
            "shopify_order": {
                "id": 7001002006,
                "financial_status": "refunded",
                "fulfillment_status": "unfulfilled",
            },
            "order_probe": {"returnStatus": "NO_RETURN"},
            "existing_returns": [],
            "return_capabilities": {
                "queryroot_returnable_fulfillments_available": True,
                "queryroot_returnable_fulfillment_available": True,
                "order_return_status_available": True,
                "order_returns_available": True,
            },
            "schema_diag": {"order_returnish_fields": ["returnStatus", "returns"]},
        }

    async def fake_build_order_sync_audit(_merchant_id: str, checkout_id: str, *, sample_limit: int = 10):
        assert checkout_id == checkout.checkout_id
        assert sample_limit == 10
        return {
            "checkout_id": checkout_id,
            "order_state": {
                "status": "open",
                "payment_status": "refunded",
                "fulfillment_status": "processing",
                "shopify_order_id": "7001002006",
            },
            "evidence": {"return_records": []},
            "sync_signals": {"return_sync": {"status": "not_observed"}},
        }

    monkeypatch.setattr(readiness_service, "get_default_journal", lambda: journal)
    monkeypatch.setattr(readiness_service, "get_order", fake_get_order)
    monkeypatch.setattr(readiness_service, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(readiness_service, "_get_shopify_config_for_merchant", fake_get_shopify_cfg)
    monkeypatch.setattr(readiness_service, "resolve_shopify_admin_access_token", fake_resolve_token)
    monkeypatch.setattr(
        readiness_service,
        "probe_shopify_return_eligibility_best_effort",
        fake_probe_shopify_return_eligibility_best_effort,
    )
    monkeypatch.setattr(readiness_service, "build_order_sync_audit", fake_build_order_sync_audit)

    result = await readiness_service.probe_return_eligibility_for_checkout(
        "merch_efbc46b4619cfbdf",
        checkout.checkout_id,
    )

    assert result["eligibility"]["status"] == "not_ready"
    assert "order_already_refunded" in result["eligibility"]["blockers"]
    assert "order_not_fulfilled" in result["eligibility"]["blockers"]


@pytest.mark.asyncio
async def test_probe_return_eligibility_for_checkout_treats_shipped_as_fulfilled(monkeypatch):
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
    await journal.update_checkout_session(checkout.checkout_id, order_id="ORD_RETURN_ELIGIBLE_3")

    async def fake_get_order(order_id: str):
        assert order_id == "ORD_RETURN_ELIGIBLE_3"
        return {
            "order_id": order_id,
            "shopify_order_id": "7001002007",
            "status": "completed",
            "payment_status": "paid",
            "fulfillment_status": "shipped",
        }

    async def fake_get_primary_store(_merchant_id: str):
        return {
            "platform": "shopify",
            "domain": "alpha-beauty-demo.myshopify.com",
            "store_id": "store_alpha_1",
            "api_key": '{"access_token":"tok_alpha"}',
        }

    async def fake_get_shopify_cfg(_merchant_id: str):
        return {"shop_domain": "alpha-beauty-demo.myshopify.com", "api_version": "2025-10"}

    async def fake_resolve_token(**_kwargs):
        return "tok_alpha", {"refreshed": False}

    async def fake_probe_shopify_return_eligibility_best_effort(**_kwargs):
        return {
            "ok": True,
            "shopify_order": {
                "id": 7001002007,
                "financial_status": "paid",
                "fulfillment_status": "fulfilled",
            },
            "order_probe": {"returnStatus": "NO_RETURN"},
            "existing_returns": [],
            "return_capabilities": {
                "queryroot_returnable_fulfillments_available": True,
                "queryroot_returnable_fulfillment_available": True,
                "order_return_status_available": True,
                "order_returns_available": True,
            },
            "schema_diag": {"order_returnish_fields": ["returnStatus", "returns"]},
        }

    async def fake_build_order_sync_audit(_merchant_id: str, checkout_id: str, *, sample_limit: int = 10):
        assert checkout_id == checkout.checkout_id
        assert sample_limit == 10
        return {
            "checkout_id": checkout_id,
            "order_state": {
                "status": "completed",
                "payment_status": "paid",
                "fulfillment_status": "shipped",
                "shopify_order_id": "7001002007",
            },
            "evidence": {"return_records": []},
            "sync_signals": {"return_sync": {"status": "not_observed"}},
        }

    monkeypatch.setattr(readiness_service, "get_default_journal", lambda: journal)
    monkeypatch.setattr(readiness_service, "get_order", fake_get_order)
    monkeypatch.setattr(readiness_service, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(readiness_service, "_get_shopify_config_for_merchant", fake_get_shopify_cfg)
    monkeypatch.setattr(readiness_service, "resolve_shopify_admin_access_token", fake_resolve_token)
    monkeypatch.setattr(
        readiness_service,
        "probe_shopify_return_eligibility_best_effort",
        fake_probe_shopify_return_eligibility_best_effort,
    )
    monkeypatch.setattr(readiness_service, "build_order_sync_audit", fake_build_order_sync_audit)

    result = await readiness_service.probe_return_eligibility_for_checkout(
        "merch_efbc46b4619cfbdf",
        checkout.checkout_id,
    )

    assert result["eligibility"]["status"] == "likely_eligible"
    assert result["eligibility"]["blockers"] == []
    assert result["eligibility"]["resolved_fulfillment_status"] == "shipped"
