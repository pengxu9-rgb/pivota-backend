"""Fail-closed contract for two protocol stubs (mock/stub production audit).

Locks in that neither endpoint can silently return fabricated data again:
- /ap2/x402/quote must never invent a 1:1 FX rate for a real cross-currency pair
  when the rate snapshot is missing/stale — it must raise.
- /mcp/orders writes (in-memory simulation store) must refuse, never fake an order.
"""
import pytest
from fastapi import HTTPException

from routes import ap2_routes
from routes.ap2_routes import AP2ExchangeRateRequest, get_exchange_quote
from routes.mcp_routes import (
    CreateOrderRequest,
    OrderItemRequest,
    OrderStatusUpdate,
    cancel_order_endpoint,
    create_order_endpoint,
    update_order_status_endpoint,
)


@pytest.mark.asyncio
async def test_x402_quote_same_currency_is_legitimate_1to1():
    resp = await get_exchange_quote(
        AP2ExchangeRateRequest(from_currency="usd", to_currency="USD", amount=10.0)
    )
    assert resp.rate == 1.0
    assert resp.converted_amount == 10.0
    assert resp.from_currency == "USD" and resp.to_currency == "USD"


@pytest.mark.asyncio
async def test_x402_quote_no_snapshot_fails_closed_not_fabricated(monkeypatch):
    async def _no_row(*_a, **_k):
        return None

    monkeypatch.setattr(ap2_routes.database, "fetch_one", _no_row)
    with pytest.raises(HTTPException) as ei:
        await get_exchange_quote(
            AP2ExchangeRateRequest(from_currency="USD", to_currency="EUR", amount=10.0)
        )
    assert ei.value.status_code == 503  # never a made-up 1:1


@pytest.mark.asyncio
async def test_x402_quote_pair_missing_from_snapshot_fails_closed(monkeypatch):
    async def _row_without_pair(*_a, **_k):
        return {"rates": {"GBP": 0.8}}  # EUR absent

    monkeypatch.setattr(ap2_routes.database, "fetch_one", _row_without_pair)
    with pytest.raises(HTTPException) as ei:
        await get_exchange_quote(
            AP2ExchangeRateRequest(from_currency="USD", to_currency="EUR", amount=10.0)
        )
    assert ei.value.status_code == 503


@pytest.mark.asyncio
async def test_mcp_order_writes_are_disabled_501():
    req = CreateOrderRequest(
        agent_id="a", merchant_id="m",
        items=[OrderItemRequest(sku="s", quantity=1, unit_price=1.0)],
    )
    with pytest.raises(HTTPException) as ei:
        await create_order_endpoint(req)
    assert ei.value.status_code == 501

    with pytest.raises(HTTPException) as ei:
        await update_order_status_endpoint(
            "ord_x", OrderStatusUpdate(order_id="ord_x", status="paid")
        )
    assert ei.value.status_code == 501

    with pytest.raises(HTTPException) as ei:
        await cancel_order_endpoint("ord_x")
    assert ei.value.status_code == 501
