from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.chdir(REPO_ROOT)

from main import app
from utils.order_track_token import mint_order_track_token


def _track_order() -> dict:
    now = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)
    return {
        "order_id": "ORD_TRACK_TOKEN_1",
        "merchant_id": "merch_track",
        "customer_email": "Buyer@Example.com",
        "currency": "USD",
        "status": "confirmed",
        "payment_status": "paid",
        "fulfillment_status": None,
        "tracking_number": None,
        "total": "45.20",
        "created_at": now,
        "updated_at": now,
        "paid_at": now,
        "shipping_address": {},
        "items": [],
        "metadata": {},
    }


@pytest.mark.asyncio
async def test_public_track_by_token_returns_same_track_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.accounts_orders_api as route_module

    recorded: list[tuple[str, str, str]] = []

    async def fake_count_recent_public_lookup_by_ip(_ip: str) -> int:
        return 0

    async def fake_count_recent_public_lookup_by_key(_email: str, _order_id: str) -> int:
        return 0

    async def fake_record_public_lookup(ip: str, email: str, order_id: str) -> None:
        recorded.append((ip, email, order_id))

    async def fake_fetch_one(_query, _values=None):
        return _track_order()

    monkeypatch.setenv("ORDER_TRACK_TOKEN_SECRET", "test-order-track-secret")
    monkeypatch.setattr(
        route_module,
        "count_recent_public_lookup_by_ip",
        fake_count_recent_public_lookup_by_ip,
    )
    monkeypatch.setattr(
        route_module,
        "count_recent_public_lookup_by_key",
        fake_count_recent_public_lookup_by_key,
    )
    monkeypatch.setattr(route_module, "record_public_lookup", fake_record_public_lookup)
    monkeypatch.setattr(route_module.database, "fetch_one", fake_fetch_one)

    token = mint_order_track_token("ORD_TRACK_TOKEN_1")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        token_resp = await client.get("/accounts/public/track-by-token", params={"token": token})
        email_resp = await client.get(
            "/accounts/public/track",
            params={"order_id": "ORD_TRACK_TOKEN_1", "email": "buyer@example.com"},
        )

    assert token_resp.status_code == 200
    assert email_resp.status_code == 200
    assert token_resp.json() == email_resp.json()
    assert token_resp.json()["timeline"][1]["status"] == "paid"
    assert recorded == [
        ("127.0.0.1", "buyer@example.com", "ORD_TRACK_TOKEN_1"),
        ("127.0.0.1", "buyer@example.com", "ORD_TRACK_TOKEN_1"),
    ]


@pytest.mark.asyncio
async def test_public_track_by_token_returns_404_for_invalid_or_expired_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.accounts_orders_api as route_module

    async def fake_count_recent_public_lookup_by_ip(_ip: str) -> int:
        return 0

    async def fake_fetch_one(_query, _values=None):
        raise AssertionError("invalid tokens should not load orders")

    monkeypatch.setenv("ORDER_TRACK_TOKEN_SECRET", "test-order-track-secret")
    monkeypatch.setattr(
        route_module,
        "count_recent_public_lookup_by_ip",
        fake_count_recent_public_lookup_by_ip,
    )
    monkeypatch.setattr(route_module.database, "fetch_one", fake_fetch_one)

    expired = mint_order_track_token("ORD_TRACK_TOKEN_2", expires_in_seconds=-60)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        invalid_resp = await client.get("/accounts/public/track-by-token", params={"token": "invalid"})
        expired_resp = await client.get("/accounts/public/track-by-token", params={"token": expired})

    assert invalid_resp.status_code == 404
    assert expired_resp.status_code == 404
    assert invalid_resp.json()["detail"]["error"]["code"] == "NOT_FOUND"
    assert expired_resp.json()["detail"]["error"]["code"] == "NOT_FOUND"
