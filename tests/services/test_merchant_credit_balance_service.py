from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pytest


class FakeCreditConn:
    def __init__(self) -> None:
        self.balances: Dict[str, Dict[str, Any]] = {}
        self.events: Dict[str, Dict[str, Any]] = {}
        self.version_conflict_once = False
        self.conflict_kind = "audit"
        self.conflict_amount = 0

    def seed(
        self,
        merchant_id: str,
        *,
        audit: int = 0,
        prompt: int = 0,
        execution: int = 0,
        plan_tier: str = "free",
    ) -> None:
        self.balances[merchant_id] = {
            "merchant_id": merchant_id,
            "audit_credits": audit,
            "prompt_credits": prompt,
            "execution_credits": execution,
            "plan_tier": plan_tier,
            "updated_at": datetime.now(timezone.utc),
            "version": 0,
        }

    async def fetch_one(self, query: str, values: Optional[Dict[str, Any]] = None):
        values = values or {}
        sql = str(query)
        if "merchant_credit_balance:get_balance" in sql:
            row = self.balances.get(str(values["merchant_id"]))
            return dict(row) if row else None
        if "merchant_credit_balance:fetch_usage_replay" in sql:
            row = self.events.get(str(values["idempotency_key"]))
            return {"payload": dict(row["payload"])} if row else None
        if "merchant_credit_balance:claim_usage_operation" in sql:
            key = str(values["idempotency_key"])
            if key in self.events:
                return None
            payload = json.loads(values["payload"])
            self.events[key] = {
                "idempotency_key": key,
                "payload": payload,
                "event_type": values["event_type"],
                "billing_mode": values["billing_mode"],
                "billing_status": values["billing_status"],
                "quantity": values["quantity"],
            }
            return {"idempotency_key": key, "payload": payload}
        if "merchant_credit_balance:debit_update" in sql:
            return self._update_balance(values, delta=-int(values["amount"]))
        if "merchant_credit_balance:credit_update" in sql:
            return self._update_balance(values, delta=int(values["amount"]))
        raise AssertionError(f"unexpected fetch_one SQL: {sql}")

    async def execute(self, query: str, values: Optional[Dict[str, Any]] = None):
        values = values or {}
        sql = str(query)
        if "merchant_credit_balance:ensure_row" in sql:
            merchant_id = str(values["merchant_id"])
            if merchant_id not in self.balances:
                self.seed(merchant_id)
            return None
        if "merchant_credit_balance:store_usage_post_balance" in sql:
            key = str(values["idempotency_key"])
            self.events[key]["payload"] = json.loads(values["payload"])
            return None
        raise AssertionError(f"unexpected execute SQL: {sql}")

    def _update_balance(self, values: Dict[str, Any], *, delta: int):
        merchant_id = str(values["merchant_id"])
        row = self.balances[merchant_id]
        column = self._column_from_kind(self._kind_from_update(delta))
        if self.version_conflict_once:
            self.version_conflict_once = False
            if self.conflict_amount:
                conflict_col = self._column_from_kind(self.conflict_kind)
                row[conflict_col] += int(self.conflict_amount)
                row["version"] += 1
            return None
        if int(row["version"]) != int(values["version"]):
            return None
        if delta < 0 and int(row[column]) < abs(delta):
            return None
        row[column] += delta
        row["version"] += 1
        row["updated_at"] = datetime.now(timezone.utc)
        return dict(row)

    def _kind_from_update(self, delta: int) -> str:
        # The service sends one update at a time. Tests set this before
        # calling when the target is prompt/execution.
        return getattr(self, "active_kind", "audit")

    @staticmethod
    def _column_from_kind(kind: str) -> str:
        return {
            "audit": "audit_credits",
            "prompt": "prompt_credits",
            "execution": "execution_credits",
        }[kind]


@pytest.mark.asyncio
async def test_get_balance_missing_row_returns_zero(monkeypatch):
    from services import merchant_credit_balance_service as svc

    fake = FakeCreditConn()
    monkeypatch.setattr(svc, "database", fake)

    balance = await svc.get_balance("merch-missing")

    assert balance["audit_credits"] == 0
    assert balance["prompt_credits"] == 0
    assert balance["execution_credits"] == 0
    assert balance["plan_tier"] == "free"
    assert balance["version"] == 0


@pytest.mark.asyncio
async def test_ensure_row_is_idempotent():
    from services import merchant_credit_balance_service as svc

    fake = FakeCreditConn()
    await svc.ensure_row("merch-A", conn=fake)
    await svc.ensure_row("merch-A", conn=fake)

    assert list(fake.balances) == ["merch-A"]
    assert fake.balances["merch-A"]["audit_credits"] == 0


@pytest.mark.asyncio
async def test_debit_deducts_amount_and_bumps_version():
    from services import merchant_credit_balance_service as svc

    fake = FakeCreditConn()
    fake.seed("merch-A", audit=5)

    balance = await svc.debit("merch-A", "audit", 2, "run-1", conn=fake)

    assert balance["audit_credits"] == 3
    assert balance["version"] == 1
    assert balance["replay"] is False


@pytest.mark.asyncio
async def test_debit_insufficient_balance_raises():
    from services import merchant_credit_balance_service as svc

    fake = FakeCreditConn()
    fake.seed("merch-A", audit=1)

    with pytest.raises(svc.InsufficientCreditsError) as err:
        await svc.debit("merch-A", "audit", 2, "run-1", conn=fake)

    assert err.value.kind == "audit"
    assert err.value.required == 2
    assert err.value.available == 1


@pytest.mark.asyncio
async def test_debit_idempotency_replays_same_post_balance():
    from services import merchant_credit_balance_service as svc

    fake = FakeCreditConn()
    fake.seed("merch-A", audit=5)

    first = await svc.debit("merch-A", "audit", 2, "run-1", conn=fake)
    second = await svc.debit("merch-A", "audit", 2, "run-1", conn=fake)

    assert first["audit_credits"] == 3
    assert second["audit_credits"] == 3
    assert second["replay"] is True
    assert len(fake.events) == 1


@pytest.mark.asyncio
async def test_credit_adds_amount_and_is_idempotent():
    from services import merchant_credit_balance_service as svc

    fake = FakeCreditConn()
    fake.seed("merch-A", prompt=1)
    fake.active_kind = "prompt"

    first = await svc.credit("merch-A", "prompt", 4, "stripe_evt_1", conn=fake)
    second = await svc.credit("merch-A", "prompt", 4, "stripe_evt_1", conn=fake)

    assert first["prompt_credits"] == 5
    assert second["prompt_credits"] == 5
    assert second["replay"] is True
    assert len(fake.events) == 1


@pytest.mark.asyncio
async def test_execution_debit_path_is_defined():
    from services import merchant_credit_balance_service as svc

    fake = FakeCreditConn()
    fake.seed("merch-A", execution=2)
    fake.active_kind = "execution"

    balance = await svc.debit("merch-A", "execution", 1, "exec-1", conn=fake)

    assert balance["execution_credits"] == 1
    assert balance["version"] == 1


@pytest.mark.asyncio
async def test_debit_retries_version_mismatch_then_raises_if_spent():
    from services import merchant_credit_balance_service as svc

    fake = FakeCreditConn()
    fake.seed("merch-A", audit=5)
    fake.version_conflict_once = True
    fake.conflict_kind = "audit"
    fake.conflict_amount = -3

    with pytest.raises(svc.InsufficientCreditsError) as err:
        await svc.debit("merch-A", "audit", 4, "run-1", conn=fake)

    assert err.value.available == 2
    assert fake.balances["merch-A"]["audit_credits"] == 2
