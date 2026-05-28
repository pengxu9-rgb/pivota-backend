from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from routes import billing_routes


class FakeDB:
    """In-memory stripe_events fixture that supports the helper's SQL shape.

    Only the queries _claim_retryable_event issues are interpreted: a
    SELECT ... FOR UPDATE on event_id and an UPDATE to the same row. Other
    queries either no-op or assert.
    """

    def __init__(self) -> None:
        # event_id -> dict of column values
        self.rows: Dict[str, Dict[str, Any]] = {}
        self.executes: List[tuple[str, Dict[str, Any]]] = []
        self.locked_event_ids: set[str] = set()

    async def fetch_one(self, query: str, values: Optional[Dict[str, Any]] = None):
        params = dict(values or {})
        sql = str(query)

        if "FROM stripe_events" in sql and "FOR UPDATE" in sql:
            event_id = params["event_id"]
            if event_id in self.locked_event_ids:
                # Simulate SKIP LOCKED: row currently held by another worker.
                return None
            row = self.rows.get(event_id)
            if row is None:
                return None
            return {
                "id": row.get("id", 1),
                "status": row["status"],
                "age_seconds": row.get("age_seconds", 0),
            }

        if "INSERT INTO stripe_events" in sql:
            event_id = params["event_id"]
            if event_id in self.rows:
                return None  # ON CONFLICT DO NOTHING
            self.rows[event_id] = {
                "id": len(self.rows) + 1,
                "event_id": event_id,
                "event_type": params["event_type"],
                "payload": params["payload_json"],
                "status": "pending",
                "age_seconds": 0,
            }
            return {"id": self.rows[event_id]["id"]}

        raise AssertionError(f"Unexpected fetch_one query: {sql}")

    async def execute(self, query: str, values: Optional[Dict[str, Any]] = None):
        params = dict(values or {})
        sql = str(query)
        self.executes.append((sql, params))

        if "UPDATE stripe_events" in sql and "status = 'pending'" in sql:
            event_id = params["event_id"]
            row = self.rows.get(event_id)
            assert row is not None, f"reclaim UPDATE missed event_id={event_id}"
            row["status"] = "pending"
            row["age_seconds"] = 0
            row["event_type"] = params["event_type"]
            row["payload"] = params["payload_json"]
            row["error"] = None
            return None

        # Other status transitions (processed/ignored/failed) — accept silently.
        if "UPDATE stripe_events" in sql:
            return None

        raise AssertionError(f"Unexpected execute query: {sql}")


@pytest.fixture
def patch_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(billing_routes, "IS_POSTGRES", True)


@pytest.mark.asyncio
async def test_claim_returns_ack_duplicate_for_processed_row(
    monkeypatch: pytest.MonkeyPatch, patch_postgres: None
) -> None:
    db = FakeDB()
    db.rows["evt_1"] = {"id": 1, "status": "processed", "age_seconds": 600}

    result = await billing_routes._claim_retryable_event(
        "evt_1", "invoice.paid", {"id": "evt_1"}, db
    )

    assert result == "ack_duplicate"
    # No UPDATE issued for an already-processed event.
    assert not any("status = 'pending'" in sql for sql, _ in db.executes)


@pytest.mark.asyncio
async def test_claim_returns_ack_duplicate_for_ignored_row(
    monkeypatch: pytest.MonkeyPatch, patch_postgres: None
) -> None:
    db = FakeDB()
    db.rows["evt_2"] = {"id": 1, "status": "ignored", "age_seconds": 60}

    result = await billing_routes._claim_retryable_event(
        "evt_2", "customer.subscription.created", {"id": "evt_2"}, db
    )

    assert result == "ack_duplicate"


@pytest.mark.asyncio
async def test_claim_reclaims_failed_row(
    monkeypatch: pytest.MonkeyPatch, patch_postgres: None
) -> None:
    db = FakeDB()
    db.rows["evt_3"] = {"id": 1, "status": "failed", "age_seconds": 30}

    result = await billing_routes._claim_retryable_event(
        "evt_3", "invoice.paid", {"id": "evt_3", "type": "invoice.paid"}, db
    )

    assert result == "reclaimed_failed"
    assert db.rows["evt_3"]["status"] == "pending"
    # Payload refreshed.
    payload = json.loads(db.rows["evt_3"]["payload"])
    assert payload["id"] == "evt_3"


@pytest.mark.asyncio
async def test_claim_reclaims_stale_pending_row(
    monkeypatch: pytest.MonkeyPatch, patch_postgres: None
) -> None:
    db = FakeDB()
    # Older than STALE_PENDING_SECONDS — handler crashed mid-flight previously.
    db.rows["evt_4"] = {
        "id": 1,
        "status": "pending",
        "age_seconds": billing_routes.STALE_PENDING_SECONDS + 60,
    }

    result = await billing_routes._claim_retryable_event(
        "evt_4", "invoice.paid", {"id": "evt_4"}, db
    )

    assert result == "reclaimed_stale_pending"
    assert db.rows["evt_4"]["status"] == "pending"


@pytest.mark.asyncio
async def test_claim_backs_off_for_fresh_pending_row(
    monkeypatch: pytest.MonkeyPatch, patch_postgres: None
) -> None:
    # Another worker holds the row; we must not steal it from them.
    db = FakeDB()
    db.rows["evt_5"] = {
        "id": 1,
        "status": "pending",
        "age_seconds": 10,  # well under STALE_PENDING_SECONDS
    }

    result = await billing_routes._claim_retryable_event(
        "evt_5", "invoice.paid", {"id": "evt_5"}, db
    )

    assert result == "concurrent_in_flight"
    # No status change.
    assert db.rows["evt_5"]["status"] == "pending"


@pytest.mark.asyncio
async def test_claim_returns_concurrent_when_skip_locked_blocks(
    monkeypatch: pytest.MonkeyPatch, patch_postgres: None
) -> None:
    # Simulate another worker holding the row lock — SKIP LOCKED returns None.
    db = FakeDB()
    db.rows["evt_6"] = {"id": 1, "status": "failed", "age_seconds": 60}
    db.locked_event_ids.add("evt_6")

    result = await billing_routes._claim_retryable_event(
        "evt_6", "invoice.paid", {"id": "evt_6"}, db
    )

    assert result == "concurrent_in_flight"
    # Row untouched because we never acquired the lock.
    assert db.rows["evt_6"]["status"] == "failed"


def test_stale_pending_threshold_is_reasonable() -> None:
    # Guard the constant: too short → false reclaims of in-flight handlers;
    # too long → failed events sit unrecovered for hours.
    assert 60 <= billing_routes.STALE_PENDING_SECONDS <= 1800


@pytest.mark.asyncio
async def test_payment_intent_succeeded_hooks_direct_topup_webhook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services import merchant_credit_balance_service as credit_service

    db = FakeDB()
    calls: List[Dict[str, Any]] = []

    async def fake_apply_topup(payment_intent: Dict[str, Any], *, conn: Any):
        calls.append({"payment_intent": payment_intent, "conn": conn})
        return {"credits": 2000, "replay": False}

    monkeypatch.setattr(
        credit_service,
        "apply_credit_topup_payment_intent_succeeded",
        fake_apply_topup,
    )
    event = {
        "id": "evt_topup_pi",
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "id": "pi_topup_1",
                "metadata": {
                    "pivota_purpose": "direct_credit_topup",
                    "merchant_id": "merch-A",
                    "pack_credits": "2000",
                },
            }
        },
    }

    await billing_routes._handle_payment_intent_succeeded(event, db)

    assert calls == [{
        "payment_intent": event["data"]["object"],
        "conn": db,
    }]
    assert any("status = 'processed'" in sql for sql, _ in db.executes)


@pytest.mark.asyncio
async def test_payment_intent_succeeded_ignores_non_topup_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services import merchant_credit_balance_service as credit_service

    db = FakeDB()

    async def should_not_apply(*_args: Any, **_kwargs: Any):
        raise AssertionError("non-topup payment_intent.succeeded must be ignored")

    monkeypatch.setattr(
        credit_service,
        "apply_credit_topup_payment_intent_succeeded",
        should_not_apply,
    )
    event = {
        "id": "evt_other_pi",
        "type": "payment_intent.succeeded",
        "data": {"object": {"id": "pi_other", "metadata": {}}},
    }

    await billing_routes._handle_payment_intent_succeeded(event, db)

    assert any("status = 'ignored'" in sql for sql, _ in db.executes)


@pytest.mark.asyncio
async def test_create_credit_topup_route_delegates_to_direct_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services import merchant_credit_balance_service as credit_service

    calls: List[Dict[str, Any]] = []

    def noop_require_key() -> None:
        return None

    async def fake_create_topup(**kwargs: Any) -> Dict[str, Any]:
        calls.append(kwargs)
        return {
            "payment_intent_id": "pi_topup_route",
            "status": "succeeded",
            "pack_credits": 2000,
            "amount": {"currency": "usd", "total": "20.00"},
        }

    monkeypatch.setattr(billing_routes, "_require_platform_stripe_key", noop_require_key)
    monkeypatch.setattr(
        credit_service,
        "create_credit_topup_payment_intent",
        fake_create_topup,
    )

    result = await billing_routes.create_credit_topup(
        billing_routes.CreditTopUpRequest(
            merchant_id="merch-A",
            pack_credits=2000,
            idempotency_key="manual-1",
        ),
        auth_merchant_id="merch-A",
    )

    assert result["payment_intent_id"] == "pi_topup_route"
    assert result["amount"] == {"currency": "usd", "total": "20.00"}
    assert calls == [{
        "merchant_id": "merch-A",
        "pack_credits": 2000,
        "idempotency_key": "manual-1",
    }]
