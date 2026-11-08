from __future__ import annotations

import asyncio
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
        raise prs.PaymentNotSentError("connect failed")

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
            # Typed "never sent", so failing over is safe. A transport error such as
            # "connection reset" may follow a write and must NOT fail over (tests below).
            raise prs.PaymentNotSentError("connect failed")
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


# --- An unknown outcome is never failed over -------------------------------------------------
# A timeout, a transport error after the request may have been sent, a 5xx, or an unclassified
# error can mean the first PSP accepted the payment. Trying the next PSP would charge twice. These
# attempts stop and report `outcome_unknown` with our reference, to be reconciled at that PSP.


def _two_psp_route(timeout_ms: int = 200) -> dict:
    return {
        "route_id": "route_test",
        "max_retries": 2,
        "timeout_ms": timeout_ms,
        "psp_priority": [
            {"psp": "stripe", "priority": 1},
            {"psp": "adyen", "priority": 2},
        ],
    }


def _set_mode(monkeypatch: pytest.MonkeyPatch, prs, *, v2: bool) -> None:
    monkeypatch.setattr(prs, "PAYMENT_ROUTING_V2_ENABLED", v2)
    monkeypatch.setattr(prs, "PAYMENT_ROUTING_V2_ALLOWLIST", set())
    monkeypatch.setattr(prs, "PAYMENT_ROUTING_MAX_ATTEMPTS_TOTAL", 3)
    monkeypatch.setattr(prs, "PAYMENT_ROUTING_COOLDOWN_SECONDS", 0.01)


def _route(service, monkeypatch: pytest.MonkeyPatch, route: dict) -> None:
    async def fake_select_psp(agent_id, merchant_id, amount, currency):
        return "stripe", route

    monkeypatch.setattr(service, "select_psp", fake_select_psp)


@pytest.mark.asyncio
async def test_v2_timeout_after_the_psp_accepted_does_not_fail_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prs, service = _build_service(monkeypatch)
    _set_mode(monkeypatch, prs, v2=True)
    _route(service, monkeypatch, _two_psp_route(timeout_ms=50))

    accepted: list[str] = []
    calls: list[str] = []
    never = asyncio.Event()

    async def accept_then_hang(psp_name: str, payment_request: dict):
        calls.append(psp_name)
        accepted.append(psp_name)  # the PSP has the payment; its answer never arrives in time
        await never.wait()
        return {"transaction_id": "tx_late"}

    monkeypatch.setattr(service, "_execute_with_psp", accept_then_hang)

    result = await service.execute_with_failover(
        payment_request={"order_id": "ord_t1", "amount": 10.0, "currency": "USD"},
        agent_id="agent_t1",
        merchant_id="merchant_t1",
    )

    assert calls == ["stripe"], "a second PSP was called after a maybe-write"
    assert accepted == ["stripe"]
    assert result["success"] is False
    assert result["status"] == "outcome_unknown"
    assert result["outcome_unknown"] is True
    assert result["psp_used"] == "stripe"
    assert result["reference"] == "ord_t1"
    assert result["visited_psps"] == ["stripe"]
    statuses = [c.kwargs.get("status") for c in service._update_payment_attempt.await_args_list]
    assert statuses == ["outcome_unknown"]


@pytest.mark.asyncio
@pytest.mark.parametrize("v2", [True, False], ids=["v2", "legacy"])
@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("read timed out"),
        Exception("ReadTimeout: timeout"),
        Exception("connection reset by peer"),
        ConnectionResetError("peer closed"),
        Exception("PSP returned 502 Bad Gateway"),
        Exception("something nobody classified"),
        # Not-sent wording is never proof: a 5xx with resolver text, a later step's refusal,
        # a bare refusal (may be the last of several transport retries), an adapter's own
        # NotImplementedError raised after it did I/O, and a decline wrapped in a timeout.
        Exception("PSP returned 500: internal error (getaddrinfo failed)"),
        Exception("HTTP 503 from psp: upstream DNS failure"),
        Exception("capture failed after create: connection refused"),
        ConnectionRefusedError("refused"),
        NotImplementedError("unsupported next_action"),
        Exception("card_declined? read timed out waiting for the answer"),
    ],
    ids=[
        "timeout_type",
        "timeout_text",
        "reset_text",
        "reset_type",
        "psp_5xx",
        "unclassified",
        "5xx_with_dns_text",
        "5xx_with_dns_text_2",
        "refused_after_create",
        "refused_type",
        "adapter_not_implemented",
        "decline_text_with_timeout",
    ],
)
async def test_a_maybe_write_stops_at_the_first_psp(
    monkeypatch: pytest.MonkeyPatch, v2: bool, error: Exception
) -> None:
    prs, service = _build_service(monkeypatch)
    _set_mode(monkeypatch, prs, v2=v2)
    _route(service, monkeypatch, _two_psp_route())

    calls: list[str] = []

    async def fail(psp_name: str, payment_request: dict):
        calls.append(psp_name)
        if psp_name == "stripe":
            raise error
        return {"transaction_id": "tx_second"}

    monkeypatch.setattr(service, "_execute_with_psp", fail)

    result = await service.execute_with_failover(
        payment_request={"order_id": "ord_m1", "amount": 10.0, "currency": "USD"},
        agent_id="agent_m1",
        merchant_id="merchant_m1",
    )

    assert calls == ["stripe"]
    assert result["success"] is False
    assert result["status"] == "outcome_unknown"
    assert result["psp_used"] == "stripe"
    assert result["reference"] == "ord_m1"


@pytest.mark.asyncio
@pytest.mark.parametrize("v2", [True, False], ids=["v2", "legacy"])
async def test_a_typed_not_sent_failure_still_fails_over(
    monkeypatch: pytest.MonkeyPatch, v2: bool
) -> None:
    prs, service = _build_service(monkeypatch)
    _set_mode(monkeypatch, prs, v2=v2)
    _route(service, monkeypatch, _two_psp_route())

    calls: list[str] = []

    async def refuse_then_ok(psp_name: str, payment_request: dict):
        calls.append(psp_name)
        if psp_name == "stripe":
            raise prs.PaymentNotSentError("connect failed")
        return {"transaction_id": "tx_second"}

    monkeypatch.setattr(service, "_execute_with_psp", refuse_then_ok)

    result = await service.execute_with_failover(
        payment_request={"order_id": "ord_d1", "amount": 10.0, "currency": "USD"},
        agent_id="agent_d1",
        merchant_id="merchant_d1",
    )

    assert calls == ["stripe", "adyen"]
    assert result["success"] is True
    assert result["psp_used"] == "adyen"


@pytest.mark.asyncio
@pytest.mark.parametrize("v2", [True, False], ids=["v2", "legacy"])
async def test_a_decline_ends_the_payment_in_both_modes(
    monkeypatch: pytest.MonkeyPatch, v2: bool
) -> None:
    prs, service = _build_service(monkeypatch)
    _set_mode(monkeypatch, prs, v2=v2)
    _route(service, monkeypatch, _two_psp_route())

    calls: list[str] = []

    async def decline(psp_name: str, payment_request: dict):
        calls.append(psp_name)
        raise Exception("card_declined")

    monkeypatch.setattr(service, "_execute_with_psp", decline)

    result = await service.execute_with_failover(
        payment_request={"order_id": "ord_c1", "amount": 10.0, "currency": "USD"},
        agent_id="agent_c1",
        merchant_id="merchant_c1",
    )

    assert calls == ["stripe"]
    assert result["success"] is False
    assert result.get("outcome_unknown") is not True


@pytest.mark.asyncio
@pytest.mark.parametrize("v2", [True, False], ids=["v2", "legacy"])
@pytest.mark.parametrize(
    "psp_result",
    ["accepted", ["tx_list"], {"success": False, "psp_used": "other", "transaction_id": "tx_odd"}],
    ids=["str", "list", "overriding_keys"],
)
async def test_an_odd_psp_answer_after_a_charge_is_still_one_success(
    monkeypatch: pytest.MonkeyPatch, v2: bool, psp_result
) -> None:
    prs, service = _build_service(monkeypatch)
    _set_mode(monkeypatch, prs, v2=v2)
    _route(service, monkeypatch, _two_psp_route())

    calls: list[str] = []

    async def charge(psp_name: str, payment_request: dict):
        calls.append(psp_name)
        return psp_result

    monkeypatch.setattr(service, "_execute_with_psp", charge)

    result = await service.execute_with_failover(
        payment_request={"order_id": "ord_o1", "amount": 10.0, "currency": "USD"},
        agent_id="agent_o1",
        merchant_id="merchant_o1",
    )

    assert calls == ["stripe"]
    assert result["success"] is True
    assert result["psp_used"] == "stripe"
    if isinstance(psp_result, dict):
        assert result["transaction_id"] == "tx_odd"


@pytest.mark.asyncio
@pytest.mark.parametrize("v2", [True, False], ids=["v2", "legacy"])
@pytest.mark.parametrize("failing_step", ["attempt_update", "route_metrics"])
async def test_bookkeeping_failure_after_a_charge_is_not_a_payment_failure(
    monkeypatch: pytest.MonkeyPatch, v2: bool, failing_step: str
) -> None:
    prs, service = _build_service(monkeypatch)
    _set_mode(monkeypatch, prs, v2=v2)
    _route(service, monkeypatch, _two_psp_route())

    async def db_down(*args, **kwargs):
        raise Exception("connection to the database was lost")

    if failing_step == "attempt_update":
        monkeypatch.setattr(service, "_update_payment_attempt", db_down)
    else:
        monkeypatch.setattr(service, "update_route_metrics", db_down)

    calls: list[str] = []

    async def charge(psp_name: str, payment_request: dict):
        calls.append(psp_name)
        return {"transaction_id": f"tx_{psp_name}"}

    monkeypatch.setattr(service, "_execute_with_psp", charge)

    result = await service.execute_with_failover(
        payment_request={"order_id": "ord_b1", "amount": 10.0, "currency": "USD"},
        agent_id="agent_b1",
        merchant_id="merchant_b1",
    )

    assert calls == ["stripe"], "a DB error after the charge sent the payment to another PSP"
    assert result["success"] is True
    assert result["psp_used"] == "stripe"
    assert result["transaction_id"] == "tx_stripe"


@pytest.mark.asyncio
async def test_dual_routing_runs_the_payment_once_when_it_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prs, service = _build_service(monkeypatch)
    monkeypatch.setattr(service, "get_merchant_routing_policy", AsyncMock(return_value={}))
    monkeypatch.setattr(service, "get_agent_routing_policy", AsyncMock(return_value={}))
    execute = AsyncMock(side_effect=RuntimeError("attempt bookkeeping failed"))
    monkeypatch.setattr(service, "execute_with_failover", execute)

    with pytest.raises(RuntimeError):
        await service.route_transaction_dual(
            payment_request={"order_id": "ord_x1", "amount": 10.0, "currency": "USD"},
            merchant_id="merchant_x1",
            agent_id="agent_x1",
        )

    assert execute.await_count == 1


@pytest.mark.asyncio
async def test_dual_routing_policy_failure_falls_back_to_single_routing_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prs, service = _build_service(monkeypatch)
    monkeypatch.setattr(
        service, "get_merchant_routing_policy", AsyncMock(side_effect=RuntimeError("policy table missing"))
    )
    monkeypatch.setattr(service, "get_agent_routing_policy", AsyncMock(return_value={}))
    execute = AsyncMock(return_value={"success": True, "psp_used": "stripe"})
    monkeypatch.setattr(service, "execute_with_failover", execute)

    result = await service.route_transaction_dual(
        payment_request={"order_id": "ord_x2", "amount": 10.0, "currency": "USD"},
        merchant_id="merchant_x2",
        agent_id="agent_x2",
    )

    assert result["success"] is True
    assert execute.await_count == 1
    assert execute.await_args.kwargs["preferred_psp"] is None


@pytest.mark.asyncio
async def test_route_endpoint_reports_outcome_unknown_not_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.payment_routing_routes as module

    async def unknown(**kwargs):
        return {
            "success": False,
            "status": "outcome_unknown",
            "outcome_unknown": True,
            "psp_used": "stripe",
            "error": "timeout",
            "attempts": 1,
        }

    monkeypatch.setattr(module.routing_service, "execute_with_failover", unknown)
    request = module.RoutePaymentRequest(order_id="ord_r1", amount=10.0, currency="USD", merchant_id="merchant_r1")

    response = await module.execute_payment_with_routing(
        agent_id="agent_r1",
        request=request,
        current_user={"user_id": "agent_r1"},
    )

    assert response.success is False
    assert response.status == "outcome_unknown"
    assert response.psp_used == "stripe"


@pytest.mark.asyncio
@pytest.mark.parametrize("v2", [True, False], ids=["v2", "legacy"])
async def test_the_fail_closed_stub_is_a_definite_failure_not_an_unknown_outcome(
    monkeypatch: pytest.MonkeyPatch, v2: bool
) -> None:
    # _execute_with_psp is not wired to any PSP and raises before sending anything, so the
    # honest answer is "failed", not "outcome_unknown" (nothing to reconcile anywhere).
    prs, service = _build_service(monkeypatch)
    _set_mode(monkeypatch, prs, v2=v2)
    _route(service, monkeypatch, _two_psp_route())

    result = await service.execute_with_failover(
        payment_request={"order_id": "ord_s1", "amount": 10.0, "currency": "USD"},
        agent_id="agent_s1",
        merchant_id="merchant_s1",
    )

    assert result["success"] is False
    assert result.get("outcome_unknown") is not True
    assert "not implemented" in result["error"]
    assert result["attempts"] == 1  # every PSP shares the stub; walking the list is pointless
