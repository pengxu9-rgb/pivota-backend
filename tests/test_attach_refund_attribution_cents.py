from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict

import pytest

from services import commerce_attribution_service


def _base_row(**overrides: Any) -> Dict[str, Any]:
    row = {
        "edge_id": "cae_abc",
        "order_id": "ord_1",
        "merchant_id": "merch_1",
        "refund_ids": [],
        "refund_count": 0,
        "refunded_amount": Decimal("0"),
        "refund_amount_cents": 0,
        "refunded_at": None,
        "metadata": {},
        "click_id": "clk_x",
        "interaction_id": "int_x",
        "canonical_product_id": None,
        "canonical_variant_id": None,
        "surface": "agent",
        "prompt_cluster": None,
    }
    row.update(overrides)
    return row


def _install_fakes(monkeypatch: pytest.MonkeyPatch, *, existing_row: Dict[str, Any]) -> None:
    async def fake_fetch_one(*_args: Any, **_kwargs: Any) -> Dict[str, Any]:
        return existing_row

    async def fake_execute(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def fake_record(*_args: Any, **_kwargs: Any) -> Dict[str, Any]:
        return {"interaction_id": "int_x"}

    monkeypatch.setattr(commerce_attribution_service.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(commerce_attribution_service.database, "execute", fake_execute)
    monkeypatch.setattr(
        commerce_attribution_service,
        "record_commerce_event_best_effort",
        fake_record,
    )


@pytest.mark.asyncio
async def test_attach_refund_populates_refund_amount_cents(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fakes(monkeypatch, existing_row=_base_row())

    result = await commerce_attribution_service.attach_refund_to_attribution_edge(
        order_id="ord_1",
        refund_id="ref_first",
        amount=Decimal("25.50"),
    )

    assert result is not None
    assert result["refunded_amount"] == Decimal("25.50")
    assert result["refund_amount_cents"] == 2550
    assert result["refund_count"] == 1
    assert result["refund_ids"] == ["ref_first"]


@pytest.mark.asyncio
async def test_attach_refund_accumulates_two_distinct_refunds(monkeypatch: pytest.MonkeyPatch) -> None:
    existing = _base_row(
        refund_ids=["ref_first"],
        refund_count=1,
        refunded_amount=Decimal("10.00"),
        refund_amount_cents=1000,
    )
    _install_fakes(monkeypatch, existing_row=existing)

    result = await commerce_attribution_service.attach_refund_to_attribution_edge(
        order_id="ord_1",
        refund_id="ref_second",
        amount=Decimal("5.25"),
    )

    assert result is not None
    assert result["refunded_amount"] == Decimal("15.25")
    assert result["refund_amount_cents"] == 1525
    assert result["refund_count"] == 2


@pytest.mark.asyncio
async def test_attach_refund_idempotent_on_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    # Same refund_id replayed (Stripe retry or webhook re-delivery) must not
    # double-count either column.
    existing = _base_row(
        refund_ids=["ref_first"],
        refund_count=1,
        refunded_amount=Decimal("10.00"),
        refund_amount_cents=1000,
    )
    _install_fakes(monkeypatch, existing_row=existing)

    result = await commerce_attribution_service.attach_refund_to_attribution_edge(
        order_id="ord_1",
        refund_id="ref_first",
        amount=Decimal("10.00"),
    )

    assert result is not None
    assert result["refunded_amount"] == Decimal("10.00")
    assert result["refund_amount_cents"] == 1000
    assert result["refund_count"] == 1
    assert result["refund_ids"] == ["ref_first"]


@pytest.mark.asyncio
async def test_attach_refund_handles_fractional_cents(monkeypatch: pytest.MonkeyPatch) -> None:
    # Decimal("0.01") → 1 cent; Decimal("0.99") → 99 cents.
    _install_fakes(monkeypatch, existing_row=_base_row())

    result = await commerce_attribution_service.attach_refund_to_attribution_edge(
        order_id="ord_1",
        refund_id="ref_penny",
        amount=Decimal("0.01"),
    )

    assert result is not None
    assert result["refund_amount_cents"] == 1
