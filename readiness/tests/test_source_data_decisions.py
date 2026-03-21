from __future__ import annotations

import pytest

from db import readiness_source_data_decisions as decisions


@pytest.mark.asyncio
async def test_delete_source_data_decision_checks_remaining_rows_when_delete_returns_none(monkeypatch):
    state = {
        ("merch_123", "out_of_stock", "shopify", "9859804397896"): {
            "merchant_id": "merch_123",
            "reason_code": "out_of_stock",
            "platform": "shopify",
            "platform_product_id": "9859804397896",
            "decision_state": "manual_review",
            "created_at": "2026-03-21T00:00:00Z",
            "updated_at": "2026-03-21T00:00:00Z",
        }
    }

    async def fake_ensure_table() -> None:
        return None

    async def fake_execute(query: str, values=None):
        if "DELETE FROM merchant_readiness_source_data_decisions" in query:
            state.pop(
                (
                    values["merchant_id"],
                    values["reason_code"],
                    values["platform"],
                    values["platform_product_id"],
                ),
                None,
            )
        return None

    async def fake_fetch_all(_query: str, values=None):
        rows = []
        for (
            merchant_id,
            reason_code,
            platform,
            platform_product_id,
        ), row in state.items():
            if merchant_id != values["merchant_id"]:
                continue
            if values.get("reason_code") and reason_code != values["reason_code"]:
                continue
            platform_value = values.get("platform_0")
            product_id_value = values.get("platform_product_id_0")
            if platform_value and platform != platform_value:
                continue
            if product_id_value and platform_product_id != product_id_value:
                continue
            rows.append(row)
        return rows

    monkeypatch.setattr(decisions, "ensure_source_data_decisions_table", fake_ensure_table)
    monkeypatch.setattr(decisions.database, "execute", fake_execute)
    monkeypatch.setattr(decisions.database, "fetch_all", fake_fetch_all)

    deleted = await decisions.delete_source_data_decision(
        "merch_123",
        reason_code="out_of_stock",
        platform="shopify",
        platform_product_id="9859804397896",
    )

    assert deleted is True
    assert state == {}
