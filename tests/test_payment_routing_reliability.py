from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _route_config(max_retries: int = 2) -> dict:
    return {
        "route_id": "route_test",
        "max_retries": max_retries,
        "timeout_ms": 200,
        "psp_priority": [
            {"psp": "stripe", "priority": 1},
            {"psp": "adyen", "priority": 2},
            {"psp": "paypal", "priority": 3},
        ],
    }


def _build_service(monkeypatch: pytest.MonkeyPatch):
    import services.payment_routing_service as prs

    service = prs.PaymentRoutingService(database=SimpleNamespace())
    monkeypatch.setattr(service, "_log_payment_attempt", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_update_payment_attempt", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "update_route_metrics", AsyncMock(return_value=None))
    return prs, service


@pytest.mark.asyncio
async def test_payment_routing_v2_enforces_attempt_cap_and_no_backjump(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prs, service = _build_service(monkeypatch)
    monkeypatch.setattr(prs, "PAYMENT_ROUTING_V2_ENABLED", True)
    monkeypatch.setattr(prs, "PAYMENT_ROUTING_V2_ALLOWLIST", set())
    monkeypatch.setattr(prs, "PAYMENT_ROUTING_MAX_ATTEMPTS_TOTAL", 2)
    monkeypatch.setattr(prs, "PAYMENT_ROUTING_COOLDOWN_SECONDS", 0.01)

    async def fake_select_psp(agent_id: str, merchant_id: str | None, amount: float, currency: str):
        return "stripe", _route_config(max_retries=5)

    calls: list[str] = []

    async def fail_all(psp_name: str, payment_request: dict):
        calls.append(psp_name)
        raise Exception("connection refused")

    monkeypatch.setattr(service, "select_psp", fake_select_psp)
    monkeypatch.setattr(service, "_execute_with_psp", fail_all)

    result = await service.execute_with_failover(
        payment_request={"order_id": "ord_1", "amount": 10.0, "currency": "USD"},
        agent_id="agent_1",
        merchant_id="merchant_1",
    )

    assert result["success"] is False
    assert result["attempts"] == 2
    assert result["attempts_limit"] == 2
    assert calls == ["stripe", "adyen"]
    assert len(calls) == len(set(calls))
    assert result["visited_psps"] == ["adyen", "stripe"]
    assert result["last_psp"] == "adyen"


@pytest.mark.asyncio
async def test_payment_routing_v2_business_decline_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prs, service = _build_service(monkeypatch)
    monkeypatch.setattr(prs, "PAYMENT_ROUTING_V2_ENABLED", True)
    monkeypatch.setattr(prs, "PAYMENT_ROUTING_V2_ALLOWLIST", set())
    monkeypatch.setattr(prs, "PAYMENT_ROUTING_MAX_ATTEMPTS_TOTAL", 3)
    monkeypatch.setattr(prs, "PAYMENT_ROUTING_COOLDOWN_SECONDS", 0.01)

    async def fake_select_psp(agent_id: str, merchant_id: str | None, amount: float, currency: str):
        return "stripe", _route_config(max_retries=5)

    calls: list[str] = []

    async def decline(psp_name: str, payment_request: dict):
        calls.append(psp_name)
        raise Exception("card_declined")

    monkeypatch.setattr(service, "select_psp", fake_select_psp)
    monkeypatch.setattr(service, "_execute_with_psp", decline)

    result = await service.execute_with_failover(
        payment_request={"order_id": "ord_2", "amount": 20.0, "currency": "USD"},
        agent_id="agent_2",
        merchant_id="merchant_2",
    )

    assert result["success"] is False
    assert result["attempts"] == 1
    assert calls == ["stripe"]
    assert result["visited_psps"] == ["stripe"]
    assert result["last_psp"] == "stripe"


@pytest.mark.asyncio
async def test_payment_routing_v2_skips_open_circuit_psp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prs, service = _build_service(monkeypatch)
    monkeypatch.setattr(prs, "PAYMENT_ROUTING_V2_ENABLED", True)
    monkeypatch.setattr(prs, "PAYMENT_ROUTING_V2_ALLOWLIST", set())

    async def fake_select_psp(agent_id: str, merchant_id: str | None, amount: float, currency: str):
        return "stripe", _route_config(max_retries=2)

    calls: list[str] = []

    async def psp_execute(psp_name: str, payment_request: dict):
        calls.append(psp_name)
        if psp_name == "adyen":
            return {"transaction_id": "tx_1", "status": "success"}
        raise Exception("unexpected")

    monkeypatch.setattr(service, "select_psp", fake_select_psp)
    monkeypatch.setattr(service, "_execute_with_psp", psp_execute)

    service._v2_circuit_open_until["stripe"] = time.monotonic() + 30.0

    result = await service.execute_with_failover(
        payment_request={"order_id": "ord_3", "amount": 30.0, "currency": "USD"},
        agent_id="agent_3",
        merchant_id="merchant_3",
    )

    assert result["success"] is True
    assert result["psp_used"] == "adyen"
    assert result["attempt_number"] == 1
    assert calls == ["adyen"]
    assert "stripe" not in result["visited_psps"]


@pytest.mark.asyncio
async def test_payment_routing_legacy_path_remains_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prs, service = _build_service(monkeypatch)
    monkeypatch.setattr(prs, "PAYMENT_ROUTING_V2_ENABLED", False)

    async def fake_select_psp(agent_id: str, merchant_id: str | None, amount: float, currency: str):
        return "stripe", {
            "route_id": "route_test",
            "max_retries": 1,
            "timeout_ms": 200,
            "psp_priority": [
                {"psp": "stripe", "priority": 1},
                {"psp": "adyen", "priority": 2},
            ],
        }

    calls: list[str] = []

    async def fail_then_success(psp_name: str, payment_request: dict):
        calls.append(psp_name)
        if psp_name == "stripe":
            raise Exception("connection reset")
        return {"transaction_id": "tx_legacy", "status": "success"}

    monkeypatch.setattr(service, "select_psp", fake_select_psp)
    monkeypatch.setattr(service, "_execute_with_psp", fail_then_success)

    result = await service.execute_with_failover(
        payment_request={"order_id": "ord_4", "amount": 40.0, "currency": "USD"},
        agent_id="agent_4",
        merchant_id="merchant_4",
    )

    assert result["success"] is True
    assert result["psp_used"] == "adyen"
    assert result["attempt_number"] == 2
    assert calls == ["stripe", "adyen"]


@pytest.mark.asyncio
async def test_payment_attempt_logging_omits_internal_trusted_agent_id() -> None:
    import services.payment_routing_service as prs

    captured: dict = {}

    async def fake_execute(query: str, values: dict):
        captured.update(values)

    service = prs.PaymentRoutingService(database=SimpleNamespace(execute=fake_execute))

    await service._log_payment_attempt(
        attempt_id="att_test",
        order_id="ord_test",
        route_id=None,
        agent_id="agent_internal_trusted_abc123",
        psp_name="stripe",
        attempt_number=1,
        status="pending",
        amount=10.0,
        currency="USD",
    )

    assert captured["agent_id"] is None


def test_multi_psp_attempt_logging_omits_internal_trusted_agent_id() -> None:
    from adapters.multi_psp_orchestrator import _payment_attempt_agent_id

    assert _payment_attempt_agent_id("agent_internal_trusted_abc123") is None
    assert _payment_attempt_agent_id("") is None
    assert _payment_attempt_agent_id("agent_real") == "agent_real"
