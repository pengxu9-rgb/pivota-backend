from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from fastapi import BackgroundTasks, HTTPException


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


CREATED_AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


class _TestAgentContext:
    agent_id = "agent_ws14"
    agent_name = "WS14 Agent"
    allowed_merchants = None
    session_id = "session_ctx_ws14"

    def can_access_merchant(self, merchant_id: str) -> bool:
        return True


class _PaymentAction:
    def model_dump(self) -> dict[str, Any]:
        return {
            "type": "stripe_client_secret",
            "client_secret": "cs_ws14",
            "url": None,
            "public_key": None,
            "raw": None,
        }


class _Dumpable:
    def __init__(self, payload: dict[str, Any]):
        self._payload = payload
        self.status = payload.get("status")

    def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return dict(self._payload)


def _build_order_request() -> SimpleNamespace:
    return SimpleNamespace(
        merchant_id="merch_ws14",
        customer_email="buyer@example.com",
        customer_name=None,
        quote_id="quote_ws14",
        brief_id=None,
        brief_schema_version=None,
        discount_codes=None,
        selected_delivery_option=None,
        items=[
            SimpleNamespace(
                product_id="prod_ws14",
                product_title="WS14 Product",
                variant_id="var_ws14",
                quantity=1,
                metadata={
                    "platform": "shopify",
                    "platform_product_id": "gid://shopify/Product/14",
                },
            )
        ],
        shipping_address=SimpleNamespace(
            name="Test Buyer",
            address_line1="1 Test St",
            address_line2=None,
            city="San Francisco",
            state="CA",
            postal_code="94107",
            country="US",
            phone=None,
        ),
        currency="USD",
        agent_session_id="session_ws14",
        metadata={
            "pricing_quote": {
                "quote_id": "quote_ws14",
                "quote_hash_sha256": "hash_ws14",
                "currency": "USD",
            },
            "decision_layer": {
                "decision_id": "decision_ws14",
                "content_key": "content_ws14",
                "catalog_offer_id": "offer_ws14",
            },
        },
        preferred_psp=None,
        selected_payment_offer_id="payment_offer_ws14",
        payment_method_evidence=None,
        idempotency_key="idem_ws14",
    )


def _order_response(order_request: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        order_id="ord_ws14",
        merchant_id=order_request.merchant_id,
        customer_email=order_request.customer_email,
        items=order_request.items,
        shipping_address=order_request.shipping_address,
        subtotal=Decimal("10.00"),
        shipping_fee=Decimal("0.00"),
        tax=Decimal("0.00"),
        total=Decimal("10.00"),
        currency="USD",
        status="pending",
        payment_status="unpaid",
        fulfillment_status=None,
        payment_intent_id="pi_ws14",
        client_secret="cs_ws14",
        psp="stripe",
        payment_action=_PaymentAction(),
        shopify_order_id=None,
        tracking_number=None,
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
        paid_at=None,
        shipped_at=None,
        agent_session_id=order_request.agent_session_id,
        metadata=order_request.metadata,
    )


def _quote_snapshot(order_request: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        quote_id=order_request.quote_id,
        merchant_id=order_request.merchant_id,
        snapshot_json={
            "platform": "shopify",
            "engine": "shopify",
            "engine_ref": "shop_ws14",
            "currency": "USD",
            "pricing": {"total": "10.00"},
            "line_items": [],
        },
        engine="shopify",
        engine_ref="shop_ws14",
        expires_at=CREATED_AT,
    )


def _install_agent_create_order_harness(
    monkeypatch: pytest.MonkeyPatch,
    *,
    store_result: Any = None,
    store_exception: Optional[BaseException] = None,
    idempotency_value: Optional[dict[str, Any]] = None,
    replay_result: Optional[dict[str, Any]] = None,
    replay_exception: Optional[BaseException] = None,
) -> tuple[Any, dict[str, Any]]:
    import db.agent_product_events as product_events_module
    import db.orders as orders_module
    import mvp.events as mvp_events
    import mvp.governance as governance_module
    import mvp.ledger_events as ledger_events
    import mvp.offer as offer_module
    import routes.agent_api as module
    import services.agent_decision_event_store as decision_event_store
    import services.agent_governance as agent_governance_module
    import services.agent_webhook_service as webhook_service
    import services.pcs_fact_ingest as pcs_fact_ingest
    import services.quote_first_enforcement as quote_first_enforcement
    import services.quote_service as quote_service
    import services.shopify_policy_service as shopify_policy_service

    calls: dict[str, Any] = {"steps": []}

    if store_result is None:
        store_result = {"platform": "shopify", "store_id": "store_ws14"}

    async def fake_validate_request_compat(*_args: Any, **_kwargs: Any) -> None:
        calls["steps"].append("validate_request_compat")

    async def fake_should_require_quote_for_order_create(*, merchant_id: str):
        calls["steps"].append("should_require_quote_for_order_create")
        return False, {"merchant_id": merchant_id}

    async def fake_get_primary_store(_merchant_id: str):
        calls["steps"].append("get_primary_store")
        if store_exception is not None:
            raise store_exception
        return store_result

    async def fake_load_replayable_agent_order_create_response(_order_request: Any):
        calls["steps"].append("load_replayable")
        calls["replay_idempotency_key"] = getattr(_order_request, "idempotency_key", None)
        if replay_exception is not None:
            raise replay_exception
        return replay_result

    async def fake_cache_agent_order_create_response_best_effort(
        idempotency_key: Optional[str],
        response: dict[str, Any],
    ) -> None:
        calls["cached_replay"] = {"idempotency_key": idempotency_key, "response": response}

    async def fake_quote_load(self: Any, *, quote_id: str):
        calls["steps"].append(f"quote_load:{quote_id}")
        return _quote_snapshot(calls["order_request"])

    async def fake_get_latest_policy_hashes(_merchant_id: str):
        calls["steps"].append("get_latest_policy_hashes")
        return []

    async def fake_create_new_order(order_request: Any, background_tasks: Any, **kwargs: Any):
        calls["steps"].append("create_new_order")
        calls["create_new_order_kwargs"] = kwargs
        return _order_response(order_request)

    async def fake_get_order(order_id: str):
        calls["steps"].append(f"get_order:{order_id}")
        return {"order_id": order_id, "metadata": calls["order_request"].metadata}

    async def fake_append_internal_fact_best_effort(**_kwargs: Any) -> None:
        calls["append_internal_fact"] = True

    async def fake_log_agent_request(*_args: Any, **_kwargs: Any) -> None:
        calls["log_agent_request"] = True

    async def fake_log_product_events(_events: list[dict[str, Any]]) -> None:
        calls["log_product_events"] = True

    async def fake_record_checkout_decision(**kwargs: Any) -> None:
        calls["record_checkout_decision"] = kwargs

    async def fake_record_response(*_args: Any, **_kwargs: Any) -> None:
        calls["record_response"] = True

    async def fake_emit_agent_webhook_event(*_args: Any, **_kwargs: Any) -> None:
        calls["webhook"] = True

    class FakePostgresIdempotencyStore:
        async def get(self, *, scope: str, key: str):
            calls["steps"].append("idempotency_get")
            calls["idempotency_get"] = {"scope": scope, "key": key}
            if idempotency_value is None:
                return None
            return SimpleNamespace(value=idempotency_value)

    monkeypatch.setattr(module, "validate_request_compat", fake_validate_request_compat)
    monkeypatch.setattr(module, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(
        module,
        "_load_replayable_agent_order_create_response",
        fake_load_replayable_agent_order_create_response,
    )
    monkeypatch.setattr(
        module,
        "_cache_agent_order_create_response_best_effort",
        fake_cache_agent_order_create_response_best_effort,
    )
    monkeypatch.setattr(module, "log_agent_request", fake_log_agent_request)
    monkeypatch.setattr(
        module,
        "_enqueue_agent_create_order_background_task",
        lambda *args, **_kwargs: None,
    )
    monkeypatch.setattr(module.order_routes_module, "create_new_order", fake_create_new_order)
    monkeypatch.setattr(orders_module, "get_order", fake_get_order)
    monkeypatch.setattr(product_events_module, "log_product_events", fake_log_product_events)
    monkeypatch.setattr(pcs_fact_ingest, "append_internal_fact_best_effort", fake_append_internal_fact_best_effort)
    monkeypatch.setattr(decision_event_store, "record_checkout_decision", fake_record_checkout_decision)
    monkeypatch.setattr(
        governance_module.governance,
        "evaluate",
        lambda _policy_input: SimpleNamespace(decision="allow", reason_codes=[], risk_tier="low"),
    )
    monkeypatch.setattr(agent_governance_module.agent_governance, "record_response", fake_record_response)
    monkeypatch.setattr(
        quote_first_enforcement,
        "should_require_quote_for_order_create",
        fake_should_require_quote_for_order_create,
    )
    monkeypatch.setattr(quote_service.QuoteService, "load_active_quote_or_raise", fake_quote_load)
    monkeypatch.setattr(shopify_policy_service, "get_latest_policy_hashes", fake_get_latest_policy_hashes)
    monkeypatch.setattr(module, "_AGENT_ORDER_IDEMPOTENCY_STORE", FakePostgresIdempotencyStore())
    monkeypatch.setattr(
        offer_module,
        "build_offers_from_quote",
        lambda **_kwargs: [_Dumpable({"offer_id": "offer_ws14"})],
    )
    monkeypatch.setattr(
        offer_module,
        "preflight_offers",
        lambda **_kwargs: [_Dumpable({"status": "pass"})],
    )
    monkeypatch.setattr(mvp_events, "emit_best_effort", lambda **_kwargs: None)
    monkeypatch.setattr(ledger_events, "emit_ledger_event_best_effort", lambda **_kwargs: None)
    monkeypatch.setattr(webhook_service, "emit_agent_webhook_event", fake_emit_agent_webhook_event)

    return module, calls


async def _call_agent_create_order(module: Any, order_request: SimpleNamespace) -> dict[str, Any]:
    return await module.agent_create_order(
        order_request,
        BackgroundTasks(),
        request=None,
        context=_TestAgentContext(),
        agent_user=None,
        x_buyer_ref=None,
    )


@pytest.mark.asyncio
async def test_agent_create_order_parallel_pre_create_reads_success(monkeypatch: pytest.MonkeyPatch) -> None:
    module, calls = _install_agent_create_order_harness(monkeypatch)
    order_request = _build_order_request()
    calls["order_request"] = order_request

    response = await _call_agent_create_order(module, order_request)

    assert response["order_id"] == "ord_ws14"
    assert calls["idempotency_get"] == {"scope": "order_create", "key": "idem_ws14"}
    assert calls["replay_idempotency_key"] == "idem_ws14"
    assert "create_new_order" in calls["steps"]
    assert calls["create_new_order_kwargs"]["precomputed_store_info"] == {
        "platform": "shopify",
        "store_id": "store_ws14",
    }


@pytest.mark.asyncio
async def test_agent_create_order_parallel_pre_create_reads_idempotency_hit_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cached_response = {
        "status": "success",
        "order_id": "ord_cached_ws14",
        "merchant_id": "merch_ws14",
    }
    module, calls = _install_agent_create_order_harness(
        monkeypatch,
        idempotency_value=cached_response,
        replay_exception=RuntimeError("replay should be ignored after idempotency hit"),
    )
    order_request = _build_order_request()
    calls["order_request"] = order_request

    response = await _call_agent_create_order(module, order_request)

    assert response == cached_response
    assert "load_replayable" in calls["steps"]
    assert "create_new_order" not in calls["steps"]


@pytest.mark.asyncio
async def test_agent_create_order_parallel_pre_create_reads_prioritizes_store_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_error = HTTPException(status_code=418, detail="store-boom")
    replay_error = HTTPException(status_code=409, detail="replay-boom")
    module, calls = _install_agent_create_order_harness(
        monkeypatch,
        store_exception=store_error,
        replay_exception=replay_error,
    )
    order_request = _build_order_request()
    calls["order_request"] = order_request

    with pytest.raises(HTTPException) as exc_info:
        await _call_agent_create_order(module, order_request)

    assert exc_info.value.status_code == 418
    assert exc_info.value.detail == "store-boom"


@pytest.mark.asyncio
async def test_agent_create_order_parallel_pre_create_reads_raises_replay_exception_after_prior_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replay_error = HTTPException(status_code=409, detail="replay-boom")
    module, calls = _install_agent_create_order_harness(
        monkeypatch,
        replay_exception=replay_error,
    )
    order_request = _build_order_request()
    calls["order_request"] = order_request

    with pytest.raises(HTTPException) as exc_info:
        await _call_agent_create_order(module, order_request)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "replay-boom"
    assert calls["idempotency_get"] == {"scope": "order_create", "key": "idem_ws14"}
