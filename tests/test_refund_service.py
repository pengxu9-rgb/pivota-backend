from __future__ import annotations

import pytest

from services.refund_service import RefundService


class FakeDB:
    def __init__(self) -> None:
        self.executed: list[tuple[str, dict]] = []
        self.rows: list[dict] = []
        self.fetch_one_rows: list[dict | None] = []

    async def execute(self, query: str, values=None):
        self.executed.append((str(query), dict(values or {})))
        return None

    async def fetch_one(self, query: str, values=None):
        self.executed.append((str(query), dict(values or {})))
        if self.fetch_one_rows:
            return self.fetch_one_rows.pop(0)
        return {"refund_id": dict(values or {}).get("refund_id")}

    async def fetch_all(self, query: str, values=None):
        return self.rows


@pytest.mark.asyncio
async def test_update_refund_success_backfills_platform_refund_id_from_psp_refund_id():
    fake_db = FakeDB()
    fake_db.fetch_one_rows = [{"refund_id": "REF_ALPHA_1"}]
    service = RefundService(database=fake_db)

    applied = await service._update_refund_success(
        refund_id="REF_ALPHA_1",
        psp_refund_id="re_alpha_1",
        order_id="ORD_ALPHA_1",
        amount=29.0,
    )

    assert applied is True
    assert len(fake_db.executed) == 2

    update_refund_sql, update_refund_values = fake_db.executed[0]
    assert "UPDATE refund_records" in update_refund_sql
    assert "platform_refund_id = COALESCE(platform_refund_id, :psp_refund_id)" in update_refund_sql
    assert "status != 'completed'" in update_refund_sql
    assert "RETURNING refund_id" in update_refund_sql
    assert update_refund_values["refund_id"] == "REF_ALPHA_1"
    assert update_refund_values["psp_refund_id"] == "re_alpha_1"

    update_order_sql, update_order_values = fake_db.executed[1]
    assert "UPDATE orders" in update_order_sql
    assert update_order_values["order_id"] == "ORD_ALPHA_1"
    assert update_order_values["amount"] == 29.0


@pytest.mark.asyncio
async def test_update_refund_success_replay_does_not_increment_order_total_again():
    fake_db = FakeDB()
    fake_db.fetch_one_rows = [None]
    service = RefundService(database=fake_db)

    applied = await service._update_refund_success(
        refund_id="REF_ALPHA_REPLAY",
        psp_refund_id="re_alpha_replay",
        order_id="ORD_ALPHA_REPLAY",
        amount=29.0,
    )

    assert applied is False
    assert len(fake_db.executed) == 1
    update_refund_sql, update_refund_values = fake_db.executed[0]
    assert "UPDATE refund_records" in update_refund_sql
    assert update_refund_values["refund_id"] == "REF_ALPHA_REPLAY"
    assert not any("UPDATE orders" in sql for sql, _ in fake_db.executed)


@pytest.mark.asyncio
async def test_validate_refund_rejects_negative_amount():
    fake_db = FakeDB()
    service = RefundService(database=fake_db)

    result = await service._validate_refund(
        {
            "order_id": "ORD_NEGATIVE",
            "payment_status": "paid",
            "total": "100.00",
            "created_at": None,
        },
        -1,
    )

    assert result == {"valid": False, "error": "Refund amount must be greater than zero"}


@pytest.mark.asyncio
async def test_queue_for_retry_uses_db_computed_timestamp():
    fake_db = FakeDB()
    service = RefundService(database=fake_db)

    await service._queue_for_retry("REF_ALPHA_RETRY_1")

    assert len(fake_db.executed) == 1
    sql, values = fake_db.executed[0]
    assert "INSERT INTO refund_retry_queue" in sql
    assert "NOW() + INTERVAL '5 minutes'" in sql
    assert values == {"refund_id": "REF_ALPHA_RETRY_1"}


@pytest.mark.asyncio
async def test_retry_failed_refunds_uses_interval_sql_for_backoff(monkeypatch):
    fake_db = FakeDB()
    fake_db.rows = [
        {
            "queue_id": 7,
            "refund_id": "REF_ALPHA_RETRY_2",
            "retry_count": 2,
            "order_id": "ORD_ALPHA_2",
            "amount": 29.0,
            "reason": "temporary_psp_error",
        }
    ]
    service = RefundService(database=fake_db)

    async def fake_get_order(_order_id: str):
        return {
            "order_id": "ORD_ALPHA_2",
            "merchant_id": "merch_1",
            "payment_intent_id": "pi_alpha_retry_2",
            "psp_used": "stripe",
        }

    async def fake_process_psp_refund(*_args, **_kwargs):
        return {"success": False, "error": "psp_temporarily_unavailable"}

    monkeypatch.setattr("services.refund_service.get_order", fake_get_order)
    monkeypatch.setattr(service, "_process_psp_refund", fake_process_psp_refund)

    processed = await service.retry_failed_refunds()

    assert processed == 0
    assert len(fake_db.executed) == 1
    sql, values = fake_db.executed[0]
    assert "UPDATE refund_retry_queue" in sql
    assert "NOW() + (:retry_delay_minutes * INTERVAL '1 minute')" in sql
    assert values["id"] == 7
    assert values["retry_delay_minutes"] == 20
    assert values["error"] == "psp_temporarily_unavailable"
