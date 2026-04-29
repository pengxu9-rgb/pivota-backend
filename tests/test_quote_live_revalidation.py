from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from services.quote_service import QuoteError, QuoteService


def _quote_snapshot() -> SimpleNamespace:
    return SimpleNamespace(
        quote_id="q_live_revalidate",
        merchant_id="merch_1",
        agent_id="agent_1",
        expires_at=datetime.now(timezone.utc),
        status="active",
        engine="shopify_storefront_cart",
        engine_ref="cart_old",
        request_fingerprint="fp_1",
        quote_hash_sha256="h" * 64,
        debug_id="dbg_old",
        request_json={
            "merchant_id": "merch_1",
            "items": [{"product_id": "p_1", "variant_id": "v_1", "quantity": 1}],
            "discount_codes": [],
            "shipping_address": {"country": "US", "postal_code": "94105", "city": "San Francisco", "state": "CA"},
            "selected_delivery_option": None,
            "payment_context": None,
        },
        snapshot_json={
            "currency": "USD",
            "presentment_currency": "USD",
            "charge_currency": "USD",
            "pricing": {
                "subtotal": "10.00",
                "discount_total": "0.00",
                "shipping_fee": "2.00",
                "tax": "1.00",
                "total": "13.00",
            },
            "line_items": [
                {
                    "product_id": "p_1",
                    "variant_id": "v_1",
                    "quantity": 1,
                    "unit_price_original": "10.00",
                    "unit_price_effective": "10.00",
                    "line_discount_total": "0.00",
                }
            ],
        },
    )


def _live_quote(*, total: str = "13.00") -> dict:
    return {
        "quote_id": "q_transient",
        "engine": "shopify_storefront_cart",
        "engine_ref": "cart_live",
        "currency": "USD",
        "presentment_currency": "USD",
        "charge_currency": "USD",
        "pricing": {
            "subtotal": "10.00",
            "discount_total": "0.00",
            "shipping_fee": "2.00",
            "tax": "1.00",
            "total": total,
        },
        "line_items": [
            {
                "product_id": "p_1",
                "variant_id": "v_1",
                "quantity": 1,
                "unit_price_original": "10.00",
                "unit_price_effective": "10.00",
                "line_discount_total": "0.00",
            }
        ],
        "debug_id": "dbg_live",
    }


@pytest.mark.asyncio
async def test_validate_quote_snapshot_live_allows_matching_quote(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = QuoteService()
    captured: dict = {}

    async def fake_preview_quote(**kwargs):
        captured.update(kwargs)
        return _live_quote()

    monkeypatch.setattr(svc, "preview_quote", fake_preview_quote)

    result = await svc.validate_quote_snapshot_live(_quote_snapshot(), customer_email="buyer@example.com")

    assert result["status"] == "validated"
    assert result["engine_ref"] == "cart_live"
    assert captured["persist"] is False
    assert captured["emit_analytics"] is False
    assert captured["customer_email"] == "buyer@example.com"


@pytest.mark.asyncio
async def test_validate_quote_snapshot_live_rejects_changed_price(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = QuoteService()

    async def fake_preview_quote(**kwargs):
        return _live_quote(total="14.00")

    monkeypatch.setattr(svc, "preview_quote", fake_preview_quote)

    with pytest.raises(QuoteError) as exc:
        await svc.validate_quote_snapshot_live(_quote_snapshot())

    assert exc.value.code == "QUOTE_STALE_REPRICE_REQUIRED"
    assert any(row["field"] == "pricing.total" for row in exc.value.details["mismatches"])


@pytest.mark.asyncio
async def test_validate_quote_snapshot_live_propagates_inventory_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = QuoteService()

    async def fake_preview_quote(**kwargs):
        raise QuoteError("INSUFFICIENT_INVENTORY", "Not enough inventory", details={"available": 0})

    monkeypatch.setattr(svc, "preview_quote", fake_preview_quote)

    with pytest.raises(QuoteError) as exc:
        await svc.validate_quote_snapshot_live(_quote_snapshot())

    assert exc.value.code == "INSUFFICIENT_INVENTORY"
