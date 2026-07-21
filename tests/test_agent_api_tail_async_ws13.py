from __future__ import annotations

import asyncio
import json
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
    agent_id = "agent_ws13"
    agent_name = "WS13 Agent"
    allowed_merchants = None
    session_id = "session_ctx_ws13"

    def can_access_merchant(self, merchant_id: str) -> bool:
        return True


class _PaymentAction:
    def model_dump(self) -> dict[str, Any]:
        return {
            "type": "stripe_client_secret",
            "client_secret": "cs_ws13",
            "url": None,
            "public_key": None,
            "raw": None,
        }


async def _drain_background_tasks() -> None:
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def _build_order_request() -> SimpleNamespace:
    return SimpleNamespace(
        merchant_id="merch_ws13",
        customer_email="buyer@example.com",
        customer_name=None,
        quote_id="quote_ws13",
        brief_id=None,
        brief_schema_version=None,
        discount_codes=None,
        selected_delivery_option=None,
        items=[
            SimpleNamespace(
                product_id="prod_ws13",
                product_title="WS13 Product",
                variant_id="var_ws13",
                quantity=1,
                metadata={
                    "platform": "shopify",
                    "platform_product_id": "gid://shopify/Product/13",
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
        agent_session_id="session_ws13",
        metadata={
            "pricing_quote": {
                "quote_id": "quote_ws13",
                "quote_hash_sha256": "hash_ws13",
                "currency": "USD",
            },
            "decision_layer": {
                "decision_id": "decision_ws13",
                "content_key": "content_ws13",
                "catalog_offer_id": "offer_ws13",
            },
        },
        preferred_psp=None,
        selected_payment_offer_id="payment_offer_ws13",
        payment_method_evidence=None,
        idempotency_key="idem_ws13",
    )


def _order_response(order_request: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        order_id="ord_ws13",
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
        payment_intent_id="pi_ws13",
        client_secret="cs_ws13",
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


def _install_agent_create_order_harness(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_step: Optional[str] = None,
    create_exception: Optional[BaseException] = None,
):
    import db.agent_product_events as product_events_module
    import db.orders as orders_module
    import mvp.events as mvp_events
    import mvp.ledger_events as ledger_events
    import routes.agent_api as module
    import routes.order_routes as order_routes_module
    import services.agent_decision_event_store as decision_event_store
    import services.agent_governance as governance_module
    import services.pcs_fact_ingest as pcs_fact_ingest
    import services.quote_first_enforcement as quote_first_enforcement
    import services.quote_service as quote_service
    import services.shopify_policy_service as shopify_policy_service

    calls: dict[str, Any] = {}
    warnings: list[str] = []

    def capture_warning(message: Any, *args: Any, **_kwargs: Any) -> None:
        try:
            warnings.append(str(message) % args if args else str(message))
        except Exception:
            warnings.append(str(message))

    async def maybe_fail(step: str) -> None:
        calls[step] = calls.get(step, 0) + 1
        if fail_step == step:
            raise RuntimeError(f"boom-{step}")

    async def fake_validate_request_compat(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def fake_should_require_quote_for_order_create(*, merchant_id: str):
        return False, {"merchant_id": merchant_id}

    async def fake_get_primary_store(_merchant_id: str):
        return {"platform": "shopify", "store_id": "store_ws13"}

    async def fake_load_replayable_agent_order_create_response(_order_request: Any):
        return None

    async def fake_quote_load(self: Any, *, quote_id: str):
        raise RuntimeError(f"skip preflight quote load for {quote_id}")

    async def fake_get_latest_policy_hashes(_merchant_id: str):
        return []

    async def fake_create_new_order(order_request: Any, background_tasks: Any, **_kwargs: Any):
        if create_exception is not None:
            raise create_exception
        return _order_response(order_request)

    async def fake_get_order(order_id: str):
        calls["get_order"] = calls.get("get_order", 0) + 1
        return {"order_id": order_id, "metadata": {}}

    async def fake_append_internal_fact_best_effort(**_kwargs: Any) -> None:
        await maybe_fail("append_internal_fact_best_effort")

    async def fake_log_agent_request(*_args: Any, **kwargs: Any) -> None:
        status_code = kwargs.get("status_code")
        if status_code == 200:
            step = "log_agent_request.success"
        elif status_code == 500:
            step = "log_agent_request.exception"
        else:
            step = "log_agent_request.http_error"
        await maybe_fail(step)

    async def fake_log_product_events(_events: list[dict[str, Any]]) -> None:
        await maybe_fail("log_product_events")

    async def fake_cache_agent_order_create_response_best_effort(
        _idempotency_key: Optional[str],
        _response: dict[str, Any],
    ) -> None:
        await maybe_fail("_cache_agent_order_create_response_best_effort")

    async def fake_record_checkout_decision(**kwargs: Any) -> None:
        calls["record_checkout_decision_payload"] = kwargs
        await maybe_fail("record_checkout_decision")

    async def fake_record_response(*_args: Any, **_kwargs: Any) -> None:
        await maybe_fail("agent_governance.record_response")

    class FakePostgresIdempotencyStore:
        async def get(self, *, scope: str, key: str):
            calls["idempotency_get"] = {"scope": scope, "key": key}
            return None

        async def put(self, *, scope: str, key: str, value: dict[str, Any]):
            calls["idempotency_put"] = {"scope": scope, "key": key, "value": value}
            return None

    monkeypatch.setattr(module.logger, "warning", capture_warning)
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
    monkeypatch.setattr(module, "log_product_events", fake_log_product_events)
    monkeypatch.setattr(module, "get_order", fake_get_order)
    monkeypatch.setattr(order_routes_module, "create_new_order", fake_create_new_order)
    monkeypatch.setattr(orders_module, "get_order", fake_get_order)
    monkeypatch.setattr(pcs_fact_ingest, "append_internal_fact_best_effort", fake_append_internal_fact_best_effort)
    monkeypatch.setattr(product_events_module, "log_product_events", fake_log_product_events)
    monkeypatch.setattr(decision_event_store, "record_checkout_decision", fake_record_checkout_decision)
    monkeypatch.setattr(governance_module.agent_governance, "record_response", fake_record_response)
    monkeypatch.setattr(
        quote_first_enforcement,
        "should_require_quote_for_order_create",
        fake_should_require_quote_for_order_create,
    )
    monkeypatch.setattr(quote_service.QuoteService, "load_active_quote_or_raise", fake_quote_load)
    monkeypatch.setattr(shopify_policy_service, "get_latest_policy_hashes", fake_get_latest_policy_hashes)
    monkeypatch.setattr(module, "_AGENT_ORDER_IDEMPOTENCY_STORE", FakePostgresIdempotencyStore())
    monkeypatch.setattr(mvp_events, "emit_best_effort", lambda **_kwargs: None)
    monkeypatch.setattr(ledger_events, "emit_ledger_event_best_effort", lambda **_kwargs: None)

    return module, calls, warnings


async def _call_agent_create_order(
    module: Any,
    *,
    order_request: Optional[SimpleNamespace] = None,
) -> dict[str, Any]:
    return await module.agent_create_order(
        order_request or _build_order_request(),
        BackgroundTasks(),
        request=None,
        context=_TestAgentContext(),
        agent_user=None,
        x_buyer_ref=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fail_step",
    [
        "append_internal_fact_best_effort",
        "log_agent_request.success",
        "log_product_events",
        "_cache_agent_order_create_response_best_effort",
        "agent_governance.record_response",
    ],
)
async def test_agent_create_order_tail_background_failures_return_success(
    monkeypatch: pytest.MonkeyPatch,
    fail_step: str,
) -> None:
    module, calls, warnings = _install_agent_create_order_harness(
        monkeypatch,
        fail_step=fail_step,
    )

    response = await _call_agent_create_order(module)
    await _drain_background_tasks()

    assert response["status"] == "success"
    assert response["order_id"] == "ord_ws13"
    assert calls[fail_step] == 1
    assert any(f"background {fail_step} failed: boom-{fail_step}" in warning for warning in warnings)


@pytest.mark.asyncio
async def test_agent_create_order_success_response_shape_matches_ws13_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, _calls, _warnings = _install_agent_create_order_harness(monkeypatch)

    response = await _call_agent_create_order(module)
    await _drain_background_tasks()

    assert json.dumps(response, separators=(",", ":"), ensure_ascii=False).encode() == (
        b'{"status":"success","order_id":"ord_ws13","merchant_id":"merch_ws13",'
        b'"total":"10.00","total_amount":10.0,"currency":"USD",'
        b'"presentment_currency":"USD","charge_currency":"USD","settlement_currency":null,'
        b'"payment":{"psp":"stripe","client_secret":"cs_ws13","payment_intent_id":"pi_ws13",'
        b'"payment_action":{"type":"stripe_client_secret","client_secret":"cs_ws13","url":null,'
        b'"public_key":null,"raw":null},"instructions":"Use client_secret for Stripe payment confirmation"},'
        b'"tracking":{"agent_session_id":"session_ws13","created_at":"2026-01-02T03:04:05+00:00"},'
        b'"commerce_path":"pivota_direct_quote_first",'
        b'"execution_policy":{"commerce_path":"pivota_direct_quote_first","platform":"shopify",'
        b'"surface":"public_agent_purchase","allows_pivota_order":true,"allows_psp_creation":true,'
        b'"requires_live_quote":true,"allows_external_redirect":false,"legacy_or_fallback":false,'
        b'"validation_authority":"pivota_live_quote","execution_policy_version":"2026-04-29.v1",'
        b'"reason":"shopify_live_quote_and_final_revalidation_required"},'
        b'"legacy_or_fallback":false,"validation_authority":"pivota_live_quote"}'
    )


@pytest.mark.asyncio
async def test_agent_create_order_record_checkout_decision_still_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, calls, _warnings = _install_agent_create_order_harness(monkeypatch)

    response = await _call_agent_create_order(module)
    await _drain_background_tasks()

    assert response["status"] == "success"
    assert calls["record_checkout_decision"] == 1
    assert calls["record_checkout_decision_payload"]["order_id"] == "ord_ws13"
    assert calls["record_checkout_decision_payload"]["purchase_route"] == "pivota_direct_quote_first"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("create_exception", "fail_step", "expected_status"),
    [
        (HTTPException(status_code=409, detail={"error": "QUOTE_MISMATCH"}), "log_agent_request.http_error", 409),
        (RuntimeError("create failed"), "log_agent_request.exception", 500),
    ],
)
async def test_agent_create_order_error_logging_background_failures_do_not_mask_response(
    monkeypatch: pytest.MonkeyPatch,
    create_exception: BaseException,
    fail_step: str,
    expected_status: int,
) -> None:
    module, calls, warnings = _install_agent_create_order_harness(
        monkeypatch,
        fail_step=fail_step,
        create_exception=create_exception,
    )

    with pytest.raises(HTTPException) as exc_info:
        await _call_agent_create_order(module)
    await _drain_background_tasks()

    assert exc_info.value.status_code == expected_status
    assert calls[fail_step] == 1
    assert any(f"background {fail_step} failed: boom-{fail_step}" in warning for warning in warnings)
