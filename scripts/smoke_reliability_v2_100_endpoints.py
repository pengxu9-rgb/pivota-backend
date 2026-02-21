#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional

import httpx


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./pivota.db")


def _fail(message: str) -> None:
    raise RuntimeError(message)


class _SmokeAgentContext:
    agent_id = "agent_smoke"
    agent_name = "Smoke Agent"
    allowed_merchants = ["m_smoke"]
    session_id = "session_smoke"

    def can_access_merchant(self, merchant_id: str) -> bool:
        return True


async def _override_get_agent_context() -> _SmokeAgentContext:
    return _SmokeAgentContext()


async def _run() -> None:
    from main import app
    from routes.agent_auth import get_agent_context

    app.dependency_overrides[get_agent_context] = _override_get_agent_context
    try:
        import mvp.events as mvp_events
        import mvp.governance as mvp_governance
        import routes.agent_api as agent_api
        import routes.agent_payment_sdk as payment_module
        import routes.agent_shop_gateway as gateway
        from db.database import database as database_obj
        from models.standard_product import ProductStatus, StandardProduct

        # ------------------------------------------------------------------
        # search smoke patches
        # ------------------------------------------------------------------
        async def noop_log_agent_request(*args: Any, **kwargs: Any) -> None:
            return None

        async def fake_verify_merchant_active(_merchant_id: str) -> Dict[str, Any]:
            return {"merchant_id": "m_smoke", "business_name": "Smoke Merchant"}

        async def fake_get_products_hybrid(**kwargs: Any):
            product = StandardProduct(
                id="prod_smoke_1",
                product_id="prod_smoke_1",
                platform="shopify",
                merchant_id="m_smoke",
                title="Smoke Product",
                description="smoke-search",
                price=9.9,
                currency="USD",
                inventory_quantity=10,
                orderable=True,
                status=ProductStatus.ACTIVE,
            )
            return [product], "cache", None

        async def fake_load_external_seed(*args: Any, **kwargs: Any):
            return []

        agent_api.log_agent_request = noop_log_agent_request
        agent_api.verify_merchant_active = fake_verify_merchant_active
        agent_api.get_products_hybrid = fake_get_products_hybrid
        agent_api._load_external_seed_products_for_search = fake_load_external_seed  # type: ignore[assignment]

        # ------------------------------------------------------------------
        # shop invoke smoke patches
        # ------------------------------------------------------------------
        async def fake_multi_handler(payload: Any, metadata: Dict[str, Any], background_tasks: Any) -> Dict[str, Any]:
            return {
                "products": [{"id": "p_smoke_1", "title": "Smoke Multi Product"}],
                "total": 1,
                "page": 1,
                "page_size": 1,
                "reply": "ok",
                "metadata": {"query_source": "smoke"},
            }

        gateway._handle_find_products_multi = fake_multi_handler  # type: ignore[assignment]
        gateway.INVOKE_MULTI_BYPASS_QUEUE_SHOPPING = True

        # ------------------------------------------------------------------
        # payment smoke patches
        # ------------------------------------------------------------------
        async def fake_get_order(order_id: str) -> Dict[str, Any]:
            return {
                "order_id": order_id,
                "merchant_id": "m_smoke",
                "payment_status": "unpaid",
                "total": 10.0,
                "currency": "USD",
                "shipping_address": {
                    "country": "US",
                    "postal_code": "94107",
                    "city": "SF",
                    "state": "CA",
                },
                "metadata": {},
            }

        async def fake_update_payment_info(**kwargs: Any) -> None:
            return None

        async def fake_get_merchant_onboarding(merchant_id: str) -> Dict[str, Any]:
            return {"merchant_id": merchant_id, "psp_connected": True}

        async def fake_select_psp(self, *, agent_id: str, merchant_id: str, amount: float, currency: str):
            return "stripe", {"route_id": "route_smoke", "psp_priority": [{"psp": "stripe", "priority": 1}]}

        async def fake_fetch_one(query: Any, values: Optional[Dict[str, Any]] = None):
            return None

        async def fake_execute(query: Any, values: Optional[Dict[str, Any]] = None):
            return None

        async def fake_create_payment_with_failover(*args: Any, **kwargs: Any):
            payment_intent = SimpleNamespace(
                id="pi_smoke_1",
                status="succeeded",
                client_secret="cs_smoke_1",
            )
            return True, payment_intent, None, "stripe"

        class _Decision:
            decision = "allow"
            reason_codes = []
            required_scopes = []
            risk_tier = "low"

        payment_module.log_agent_request = noop_log_agent_request
        payment_module.get_order = fake_get_order
        payment_module.update_payment_info = fake_update_payment_info
        payment_module.get_merchant_onboarding = fake_get_merchant_onboarding
        payment_module.PaymentRoutingService.select_psp = fake_select_psp  # type: ignore[assignment]
        payment_module.create_payment_with_failover = fake_create_payment_with_failover
        database_obj.fetch_one = fake_fetch_one  # type: ignore[assignment]
        database_obj.execute = fake_execute  # type: ignore[assignment]
        mvp_events.emit_best_effort = lambda **_: None  # type: ignore[assignment]
        mvp_governance.governance.evaluate = lambda *_a, **_k: _Decision()  # type: ignore[assignment]
        mvp_governance.governance.record_audit_event = lambda **_: None  # type: ignore[assignment]

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # 1) /agent/v1/products/search
            r1 = await client.get(
                "/agent/v1/products/search",
                params={
                    "merchant_id": "m_smoke",
                    "query": "smoke",
                    "limit": 10,
                    "offset": 0,
                    "in_stock_only": "false",
                },
            )
            if r1.status_code != 200:
                _fail(f"/agent/v1/products/search unexpected status: {r1.status_code}")
            b1 = r1.json()
            if b1.get("status") != "success":
                _fail("/agent/v1/products/search status field is not success")
            if not isinstance(b1.get("products"), list):
                _fail("/agent/v1/products/search products is not list")

            # 2) /agent/shop/v1/invoke
            r2 = await client.post(
                "/agent/shop/v1/invoke",
                json={
                    "operation": "find_products_multi",
                    "payload": {
                        "search": {
                            "query": "smoke",
                            "page": 1,
                            "limit": 10,
                            "in_stock_only": False,
                        }
                    },
                    "metadata": {"source": "shopping_agent"},
                },
            )
            if r2.status_code != 200:
                _fail(f"/agent/shop/v1/invoke unexpected status: {r2.status_code}")
            b2 = r2.json()
            if not isinstance(b2.get("products"), list):
                _fail("/agent/shop/v1/invoke products is not list")
            if not isinstance(b2.get("total"), int):
                _fail("/agent/shop/v1/invoke total is not int")

            # 3) /agent/v1/payments
            r3 = await client.post(
                "/agent/v1/payments",
                json={
                    "order_id": "ord_smoke_1",
                    "payment_method": {"type": "card", "token": "tok_smoke"},
                    "idempotency_key": "idem_smoke_1",
                },
            )
            if r3.status_code != 200:
                _fail(f"/agent/v1/payments unexpected status: {r3.status_code}")
            b3 = r3.json()
            for key in ("status", "payment_id", "payment_intent_id", "amount", "currency", "psp_used", "created_at"):
                if key not in b3:
                    _fail(f"/agent/v1/payments missing field: {key}")
            datetime.fromisoformat(str(b3["created_at"]))

        print("SMOKE_OK search=200 invoke=200 payments=200")
    finally:
        app.dependency_overrides.pop(get_agent_context, None)


if __name__ == "__main__":
    asyncio.run(_run())
