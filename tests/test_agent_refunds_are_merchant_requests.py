"""D-03 (2026-11-08): an agent-initiated refund is a REQUEST to the merchant; Pivota never executes it.

Every agent route that used to run `process_refund` (an immediate PSP refund of the whole remainder on the
merchant's key, plus restock) is pinned here through the real app. The executor is replaced by a recorder,
so "never executed" is asserted, not assumed. Ownership checks still come first: a foreign agent gets its
403 / 404, not the merchant-refund answer.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)

from main import app  # noqa: E402

AGENT = "agent_refund_policy"
OTHER = "agent_someone_else"
MERCHANT = "merchant_refund_policy"


class _Ctx:
    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        self.agent_name = agent_id
        self.allowed_merchants = [MERCHANT]
        self.session_id = "s"

    def can_access_merchant(self, merchant_id: str) -> bool:
        return merchant_id in self.allowed_merchants


def _order(order_id: str, agent_id: str = AGENT) -> Dict[str, Any]:
    return {"order_id": order_id, "merchant_id": MERCHANT, "agent_id": agent_id, "total": "10.00",
            "currency": "USD", "payment_status": "paid", "status": "paid", "metadata": {}}


@pytest.fixture()
def harness(monkeypatch):
    import routes.after_sales_cases as asc
    import routes.agent_api as agent_api
    import routes.agent_commerce as agent_commerce
    import routes.agent_v2 as agent_v2
    import routes.refund_api as refund_api

    executed: List[Any] = []

    async def recorder(*args, **kwargs):
        executed.append((args, kwargs))
        return {"status": "success"}

    for mod, name in ((agent_api, "process_refund"), (agent_v2, "process_refund_route"),
                      (agent_commerce, "process_refund_route"), (asc, "process_refund"),
                      (refund_api, "process_refund")):
        if hasattr(mod, name):
            monkeypatch.setattr(mod, name, recorder)

    orders = {"ORD_MINE": _order("ORD_MINE"), "ORD_THEIRS": _order("ORD_THEIRS", OTHER)}

    async def get_order(order_id: str) -> Optional[Dict[str, Any]]:
        return orders.get(order_id)

    for mod in (agent_api, agent_v2, agent_commerce, asc):
        monkeypatch.setattr(mod, "get_order", get_order)

    async def compat(*_a, **_k):
        return None

    monkeypatch.setattr(agent_v2, "validate_request_compat", compat)

    cases = {"CASE_MINE": {"case_id": "CASE_MINE", "agent_id": AGENT, "order_id": "ORD_MINE",
                           "resolution": "refund_only", "status": "requested", "requested_refund_amount": 4.0,
                           "audit_log": []}}
    writes: List[Dict[str, Any]] = []

    async def get_case(case_id: str):
        return dict(cases[case_id]) if case_id in cases else None

    async def enforce(**_kw):
        return None

    async def execute(query, values=None):
        writes.append(dict(values or {}))
        if values and values.get("case_id") in cases and "status" in values:
            cases[values["case_id"]]["status"] = values["status"]

    monkeypatch.setattr(asc, "_get_case_by_id", get_case)
    monkeypatch.setattr(asc, "_enforce_order_access", enforce)
    monkeypatch.setattr(asc, "_serialize_case", lambda c: dict(c or {}))
    monkeypatch.setattr(asc.database, "execute", execute)

    state = {"agent": AGENT}

    async def ctx():
        return _Ctx(state["agent"])

    async def no_user():
        return None

    for mod in (agent_api, agent_v2, agent_commerce, asc):
        if hasattr(mod, "get_agent_context"):
            app.dependency_overrides[mod.get_agent_context] = ctx
        if hasattr(mod, "get_agent_user_context"):
            app.dependency_overrides[mod.get_agent_user_context] = no_user
    try:
        yield {"executed": executed, "writes": writes, "state": state, "cases": cases}
    finally:
        app.dependency_overrides.clear()


async def _post(path: str, json: Optional[Dict[str, Any]] = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, json=json or {})


DIRECT = [
    ("/agent/v1/orders/{oid}/refund", None),
    ("/agent/v2/orders/{oid}/refunds", {"amount": 4.0, "reason": "r"}),
    ("/agent/v2/commerce/checkouts/{oid}/refunds", {"amount": 4.0, "reason": "r"}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("path, body", DIRECT)
async def test_an_agent_refund_route_refuses_and_executes_nothing(harness, path, body) -> None:
    response = await _post(path.format(oid="ORD_MINE"), body)
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "REFUND_HANDLED_BY_MERCHANT"
    assert detail["retryable"] is False
    assert harness["executed"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path, body", DIRECT)
async def test_ownership_is_still_checked_first(harness, path, body) -> None:
    response = await _post(path.format(oid="ORD_THEIRS"), body)
    assert response.status_code in (403, 404), response.text
    assert harness["executed"] == []


@pytest.mark.asyncio
async def test_an_after_sales_case_refund_is_recorded_as_a_request_and_not_executed(harness) -> None:
    response = await _post("/agent/v1/after-sales/cases/CASE_MINE/refund")
    assert response.status_code == 200, response.text
    out = response.json()
    assert out["refund"]["executed"] is False
    assert out["refund"]["state"] == "requested_from_merchant"
    assert out["refund"]["code"] == "REFUND_HANDLED_BY_MERCHANT"
    assert harness["cases"]["CASE_MINE"]["status"] == "refund_requested_from_merchant"
    assert harness["executed"] == []


@pytest.mark.asyncio
async def test_a_foreign_agents_case_is_refused(harness) -> None:
    harness["state"]["agent"] = OTHER
    response = await _post("/agent/v1/after-sales/cases/CASE_MINE/refund")
    assert response.status_code == 403
    assert harness["executed"] == []
    assert harness["cases"]["CASE_MINE"]["status"] == "requested"


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix, method", [("status", "get"), ("payment-intent", "post"), ("returns", "post")])
async def test_another_agents_checkout_is_not_found_on_every_checkout_route(harness, suffix, method) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await getattr(client, method)(f"/agent/v2/commerce/checkouts/ORD_THEIRS/{suffix}",
                                                 **({} if method == "get" else {"json": {}}))
    assert response.status_code == 404, response.text
    assert harness["executed"] == []


def test_ensure_order_access_requires_the_owner() -> None:
    from fastapi import HTTPException

    from routes.agent_commerce import _ensure_order_access

    ctx = _Ctx(AGENT)
    assert _ensure_order_access(_order("X"), ctx)["order_id"] == "X"
    for bad in (_order("Y", OTHER), {**_order("Z"), "agent_id": None}, {**_order("W"), "agent_id": ""}):
        with pytest.raises(HTTPException) as err:
            _ensure_order_access(bad, ctx)
        assert err.value.status_code == 404
