from __future__ import annotations

import pytest

from services.refund_service import RefundService


class FakeDB:
    def __init__(self) -> None:
        self.executed: list[tuple[str, dict]] = []

    async def execute(self, query: str, values=None):
        self.executed.append((str(query), dict(values or {})))
        return None


@pytest.mark.asyncio
async def test_update_refund_success_backfills_platform_refund_id_from_psp_refund_id():
    fake_db = FakeDB()
    service = RefundService(database=fake_db)

    await service._update_refund_success(
        refund_id="REF_ALPHA_1",
        psp_refund_id="re_alpha_1",
        order_id="ORD_ALPHA_1",
        amount=29.0,
    )

    assert len(fake_db.executed) == 2

    update_refund_sql, update_refund_values = fake_db.executed[0]
    assert "UPDATE refund_records" in update_refund_sql
    assert "platform_refund_id = COALESCE(platform_refund_id, :psp_refund_id)" in update_refund_sql
    assert update_refund_values["refund_id"] == "REF_ALPHA_1"
    assert update_refund_values["psp_refund_id"] == "re_alpha_1"

    update_order_sql, update_order_values = fake_db.executed[1]
    assert "UPDATE orders" in update_order_sql
    assert update_order_values["order_id"] == "ORD_ALPHA_1"
    assert update_order_values["amount"] == 29.0
