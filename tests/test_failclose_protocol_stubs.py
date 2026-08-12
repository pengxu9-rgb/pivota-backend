"""Fail-closed contract for the x402 quote stub (mock/stub production audit).

/ap2/x402/quote must never invent a 1:1 FX rate for a real cross-currency pair
when the rate snapshot is missing/stale — it must raise.

The companion half of this file covered /mcp/orders, whose in-memory simulation
store was fail-closed to 501. That whole surface (routes/mcp_routes.py + the
ai_router fixture package) was DELETED on 2026-08-12; the paths are now 404 and
tests/test_platform_connectors_prefix.py asserts they stay gone.
"""
import pytest
from fastapi import HTTPException

from routes import ap2_routes
from routes.ap2_routes import AP2ExchangeRateRequest, get_exchange_quote

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
