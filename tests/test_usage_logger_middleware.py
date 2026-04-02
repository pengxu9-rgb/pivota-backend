from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI, Request

from middleware.usage_logger import UsageLoggerMiddleware


@pytest.mark.asyncio
async def test_usage_logger_preserves_json_body_for_downstream_route() -> None:
    app = FastAPI()
    app.add_middleware(UsageLoggerMiddleware)

    @app.post("/agent/v1/payments")
    async def read_payment_request(request: Request) -> dict:
        payload = await request.json()
        return {
            "order_id": payload.get("order_id"),
            "payment_method": payload.get("payment_method"),
        }

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/agent/v1/payments",
            json={
                "order_id": "ord_usage_logger",
                "payment_method": {"type": "card"},
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "order_id": "ord_usage_logger",
        "payment_method": {"type": "card"},
    }
