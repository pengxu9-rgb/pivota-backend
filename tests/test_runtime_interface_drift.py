import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
import pytest
from fastapi.testclient import TestClient


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres")
os.chdir(REPO_ROOT)

from main import app  # noqa: E402
from services.agent_governance import agent_governance, governance_runtime_contract  # noqa: E402


def test_backend_governance_runtime_contract_shape() -> None:
    contract = governance_runtime_contract(agent_governance)

    assert contract["compat_mode"] == "keyword_fail_closed"
    assert "fail_closed" in contract["validate_request_params"]
    assert contract["record_response_present"] is True
    assert {"agent_id", "latency_ms", "success"} <= set(contract["record_response_params"])
    assert contract["compat_helper_present"] is True


def test_backend_mutating_routes_source_use_governance_compat_helper() -> None:
    agent_api_source = (REPO_ROOT / "routes" / "agent_api.py").read_text()
    agent_v2_source = (REPO_ROOT / "routes" / "agent_v2.py").read_text()

    assert "validate_request_compat(agent_governance, context.agent_id, fail_closed=True)" in agent_api_source
    assert "validate_request_compat(agent_governance, context.agent_id, fail_closed=True)" in agent_v2_source
    assert "agent_governance.validate_request(" not in agent_api_source
    assert "agent_governance.validate_request(" not in agent_v2_source


def test_backend_health_surfaces_runtime_drift_contract() -> None:
    with TestClient(app) as client:
        resp = client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    runtime_contracts = body["runtime_contracts"]
    build = body["build"]
    settings_contract = body["settings_contract"]

    assert runtime_contracts["agent_governance"]["compat_mode"] == "keyword_fail_closed"
    assert runtime_contracts["agent_governance"]["record_response_present"] is True
    assert all(item["mounted"] for item in runtime_contracts["canonical_mutating_routes"].values())
    assert build["service"] == "pivota-backend"
    assert {"git", "railway"} <= set(build.keys())
    assert settings_contract["rate_limit_rpm_present"] is True
    assert settings_contract["rate_limit_rpm_source"] == "settings"


class _TestAgentContext:
    agent_id = "agent_runtime_drift"
    agent_name = "Agent Runtime Drift"
    allowed_merchants = ["m_runtime"]
    session_id = "session_runtime_drift"

    def can_access_merchant(self, merchant_id: str) -> bool:
        return merchant_id in self.allowed_merchants


async def _override_get_agent_context() -> _TestAgentContext:
    return _TestAgentContext()


@pytest.mark.asyncio
async def test_backend_checkout_session_accepts_legacy_governance_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_v2 as agent_v2
    import services.agent_governance as governance_module
    from routes.agent_auth import get_agent_context

    calls: list[str] = []

    async def legacy_validate_request(agent_id: str) -> None:
        calls.append(agent_id)

    async def fake_get_order(order_id: str) -> Optional[Dict[str, Any]]:
        now = datetime.now(timezone.utc)
        return {
            "order_id": order_id,
            "merchant_id": "m_runtime",
            "agent_id": "agent_runtime_drift",
            "customer_email": "buyer@example.com",
            "items": [],
            "shipping_address": {
                "name": "Buyer Example",
                "address_line1": "123 Market St",
                "city": "San Francisco",
                "state": "CA",
                "postal_code": "94105",
                "country": "US",
            },
            "subtotal": "42.00",
            "shipping_fee": "0.00",
            "tax": "3.20",
            "total": "45.20",
            "currency": "USD",
            "status": "pending",
            "payment_status": "awaiting_payment",
            "created_at": now,
            "updated_at": now,
        }

    async def fake_create_checkout_intent_route(**kwargs: Any) -> Dict[str, Any]:
        req = kwargs["req"]
        return {
            "intent_id": "ci_runtime",
            "checkout_session_id": "ci_runtime",
            "checkout_token": "tok_runtime",
            "checkout_url": "https://checkout.pivota.test/order?checkout_token=tok_runtime",
            "expires_at": 1_900_000_000,
            "order_id": req.order_id,
        }

    app.dependency_overrides[get_agent_context] = _override_get_agent_context
    monkeypatch.setattr(agent_v2, "get_order", fake_get_order)
    monkeypatch.setattr(agent_v2, "create_checkout_intent_route", fake_create_checkout_intent_route)
    monkeypatch.setattr(governance_module.agent_governance, "validate_request", legacy_validate_request)

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/agent/v2/payments/checkout-sessions",
                json={"order_id": "ORD_RUNTIME_DRIFT"},
            )
    finally:
        app.dependency_overrides.pop(get_agent_context, None)

    assert resp.status_code == 200
    assert resp.json()["checkout_session"]["checkout_session_id"] == "ci_runtime"
    assert calls == ["agent_runtime_drift"]
