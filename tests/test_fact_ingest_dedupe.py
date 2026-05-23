from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict

import pytest

import services.pcs_fact_ingest as fact_ingest
from services.pcs_fact_ingest import build_internal_fact_dedupe_key, build_shopify_fact_dedupe_key


class _FailingDb:
    def __init__(self, error: sqlite3.DatabaseError) -> None:
        self.error = error

    async def execute(self, query: str, values: Dict[str, Any]) -> None:
        raise self.error


class _RecordingDb:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Dict[str, Any]]] = []

    async def execute(self, query: str, values: Dict[str, Any]) -> None:
        self.calls.append((query, values))


def test_build_shopify_fact_dedupe_key_is_stable():
    assert build_shopify_fact_dedupe_key(idempotency_key="shop:topic:wid") == "shopify:shop:topic:wid"


def test_build_internal_fact_dedupe_key_prefers_idempotency_key():
    k1 = build_internal_fact_dedupe_key(fact_type="internal.order_created", order_id="ord_1", idempotency_key="idem_1")
    k2 = build_internal_fact_dedupe_key(fact_type="internal.order_created", order_id="ord_1", idempotency_key="idem_1")
    assert k1 == k2
    assert k1.startswith("internal:internal.order_created:idem_1")


def test_build_internal_fact_dedupe_key_falls_back_to_order_id():
    k = build_internal_fact_dedupe_key(fact_type="internal.payment_updated", order_id="ord_2", idempotency_key=None)
    assert k == "internal:internal.payment_updated:ord_2"


@pytest.mark.asyncio
async def test_append_internal_fact_raises_for_monetization_db_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PCS_FACT_NEVER_RAISES", raising=False)
    error = sqlite3.DatabaseError("pcs insert failed")

    with pytest.raises(sqlite3.DatabaseError) as exc_info:
        await fact_ingest.append_internal_fact_best_effort(
            merchant_id="m_1",
            order_id="ord_1",
            fact_type="order_completed",
            payload={"order_id": "ord_1"},
            idempotency_key="idem_1",
            db=_FailingDb(error),
        )

    assert exc_info.value is error


@pytest.mark.asyncio
async def test_append_internal_fact_logs_non_monetization_db_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PCS_FACT_NEVER_RAISES", raising=False)
    warnings: list[Dict[str, Any]] = []
    error = sqlite3.DatabaseError("pcs insert failed")

    def fake_warning(payload: Dict[str, Any]) -> None:
        warnings.append(payload)

    monkeypatch.setattr(fact_ingest.logger, "warning", fake_warning)

    result = await fact_ingest.append_internal_fact_best_effort(
        merchant_id="m_1",
        order_id="ord_1",
        fact_type="browse_event",
        payload={"path": "/products/sku_1"},
        idempotency_key="idem_1",
        db=_FailingDb(error),
    )

    assert result is None
    assert warnings == [
        {
            "event": "pcs_internal_fact_emit_failed",
            "fact_type": "browse_event",
            "order_id": "ord_1",
            "error": "pcs insert failed",
        }
    ]


@pytest.mark.asyncio
async def test_append_internal_fact_env_rollback_swallows_monetization_db_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PCS_FACT_NEVER_RAISES", "true")
    error = sqlite3.DatabaseError("pcs insert failed")

    def fail_warning(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("rollback path should preserve silent best-effort behavior")

    monkeypatch.setattr(fact_ingest.logger, "warning", fail_warning)

    result = await fact_ingest.append_internal_fact_best_effort(
        merchant_id="m_1",
        order_id="ord_1",
        fact_type="order_completed",
        payload={"order_id": "ord_1"},
        idempotency_key="idem_1",
        db=_FailingDb(error),
    )

    assert result is None


@pytest.mark.asyncio
async def test_append_internal_fact_happy_path_inserts_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PCS_FACT_NEVER_RAISES", raising=False)
    db = _RecordingDb()
    occurred_at = datetime(2026, 5, 23, tzinfo=timezone.utc)

    await fact_ingest.append_internal_fact_best_effort(
        merchant_id="m_1",
        order_id="ord_1",
        fact_type="order_completed",
        payload={"order_id": "ord_1", "total": 1299},
        occurred_at=occurred_at,
        idempotency_key="idem_1",
        db=db,
    )

    assert len(db.calls) == 1
    query, values = db.calls[0]
    assert "ON CONFLICT (merchant_id, dedupe_key) DO NOTHING" in query
    assert values["merchant_id"] == "m_1"
    assert values["order_id"] == "ord_1"
    assert values["fact_type"] == "order_completed"
    assert values["occurred_at"] == occurred_at
    assert values["dedupe_key"] == "internal:order_completed:idem_1"
    assert json.loads(values["payload_json"]) == {"order_id": "ord_1", "total": 1299}
