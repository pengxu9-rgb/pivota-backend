from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional

import pytest


class FakeCreditConn:
    def __init__(self) -> None:
        self.balances: Dict[str, Dict[str, Any]] = {}
        self.events: Dict[str, Dict[str, Any]] = {}
        self.version_conflict_once = False
        self.conflict_amount = 0

    def seed(
        self,
        merchant_id: str,
        *,
        credits: int = 0,
        allowance_credits: int = 0,
        usd_cogs_internal: Decimal = Decimal("0"),
        plan_tier: str = "free",
    ) -> None:
        self.balances[merchant_id] = {
            "merchant_id": merchant_id,
            "credits": credits,
            "allowance_credits": allowance_credits,
            "usd_cogs_internal": usd_cogs_internal,
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
            return self._update_balance(
                values,
                delta=-int(values["amount"]),
                usd_cogs_delta=Decimal(str(values["usd_cogs"])),
            )
        if "merchant_credit_balance:credit_update" in sql:
            return self._update_balance(
                values,
                delta=int(values["amount"]),
                usd_cogs_delta=-Decimal(str(values["usd_cogs"])),
            )
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

    def _update_balance(
        self,
        values: Dict[str, Any],
        *,
        delta: int,
        usd_cogs_delta: Decimal,
    ):
        merchant_id = str(values["merchant_id"])
        row = self.balances[merchant_id]
        if self.version_conflict_once:
            self.version_conflict_once = False
            row["credits"] += int(self.conflict_amount)
            row["version"] += 1
            return None
        if int(row["version"]) != int(values["version"]):
            return None
        if delta < 0 and int(row["credits"]) < abs(delta):
            return None
        row["credits"] += delta
        row["usd_cogs_internal"] = max(
            Decimal("0"),
            Decimal(str(row["usd_cogs_internal"])) + usd_cogs_delta,
        )
        row["version"] += 1
        row["updated_at"] = datetime.now(timezone.utc)
        return dict(row)


@pytest.mark.asyncio
async def test_get_balance_missing_row_returns_zero(monkeypatch):
    from services import merchant_credit_balance_service as svc

    fake = FakeCreditConn()
    monkeypatch.setattr(svc, "database", fake)

    balance = await svc.get_balance("merch-missing")

    assert balance["credits"] == 0
    assert balance["allowance_credits"] == 0
    assert balance["plan_tier"] == "free"
    assert balance["version"] == 0
    assert balance["usd_cogs_internal"] == Decimal("0")


@pytest.mark.asyncio
async def test_ensure_row_is_idempotent():
    from services import merchant_credit_balance_service as svc

    fake = FakeCreditConn()
    await svc.ensure_row("merch-A", conn=fake)
    await svc.ensure_row("merch-A", conn=fake)

    assert list(fake.balances) == ["merch-A"]
    assert fake.balances["merch-A"]["credits"] == 0


@pytest.mark.asyncio
async def test_debit_deducts_single_balance_tags_category_and_accrues_cogs():
    from services import merchant_credit_balance_service as svc

    fake = FakeCreditConn()
    fake.seed("merch-A", credits=5)

    balance = await svc.debit(
        "merch-A",
        "audit",
        2,
        "run-1",
        usd_cogs=Decimal("0.0737"),
        conn=fake,
    )

    assert balance["credits"] == 3
    assert balance["usd_cogs_internal"] == Decimal("0.0737")
    assert balance["version"] == 1
    assert balance["replay"] is False
    event = next(iter(fake.events.values()))
    assert event["event_type"] == "credit_debit_audit"
    assert event["billing_mode"] == "debit"
    assert event["quantity"] == 2


@pytest.mark.asyncio
async def test_debit_insufficient_balance_raises():
    from services import merchant_credit_balance_service as svc

    fake = FakeCreditConn()
    fake.seed("merch-A", credits=1)

    with pytest.raises(svc.InsufficientCreditsError) as err:
        await svc.debit("merch-A", "audit", 2, "run-1", conn=fake)

    assert err.value.kind == "audit"
    assert err.value.required == 2
    assert err.value.available == 1


@pytest.mark.asyncio
async def test_debit_idempotency_replays_same_post_balance_without_cogs_dup():
    from services import merchant_credit_balance_service as svc

    fake = FakeCreditConn()
    fake.seed("merch-A", credits=5)

    first = await svc.debit(
        "merch-A",
        "audit",
        2,
        "run-1",
        usd_cogs=Decimal("0.0737"),
        conn=fake,
    )
    second = await svc.debit(
        "merch-A",
        "audit",
        2,
        "run-1",
        usd_cogs=Decimal("0.0737"),
        conn=fake,
    )

    assert first["credits"] == 3
    assert second["credits"] == 3
    assert second["usd_cogs_internal"] == Decimal("0.0737")
    assert second["replay"] is True
    assert fake.balances["merch-A"]["usd_cogs_internal"] == Decimal("0.0737")
    assert len(fake.events) == 1


@pytest.mark.asyncio
async def test_credit_adds_amount_reverses_optional_cogs_and_is_idempotent():
    from services import merchant_credit_balance_service as svc

    fake = FakeCreditConn()
    fake.seed("merch-A", credits=1, usd_cogs_internal=Decimal("0.0500"))

    first = await svc.credit(
        "merch-A",
        "prompt",
        4,
        "refund-1",
        usd_cogs=Decimal("0.0200"),
        conn=fake,
    )
    second = await svc.credit(
        "merch-A",
        "prompt",
        4,
        "refund-1",
        usd_cogs=Decimal("0.0200"),
        conn=fake,
    )

    assert first["credits"] == 5
    assert first["usd_cogs_internal"] == Decimal("0.0300")
    assert second["credits"] == 5
    assert second["replay"] is True
    assert len(fake.events) == 1


@pytest.mark.asyncio
async def test_execution_debit_path_is_category_tag_only():
    from services import merchant_credit_balance_service as svc

    fake = FakeCreditConn()
    fake.seed("merch-A", credits=2)

    balance = await svc.debit("merch-A", "execution", 1, "exec-1", conn=fake)

    assert balance["credits"] == 1
    assert balance["version"] == 1
    event = next(iter(fake.events.values()))
    assert event["event_type"] == "credit_debit_execution"


@pytest.mark.asyncio
async def test_debit_retries_version_mismatch_then_raises_if_spent():
    from services import merchant_credit_balance_service as svc

    fake = FakeCreditConn()
    fake.seed("merch-A", credits=5)
    fake.version_conflict_once = True
    fake.conflict_amount = -3

    with pytest.raises(svc.InsufficientCreditsError) as err:
        await svc.debit("merch-A", "audit", 4, "run-1", conn=fake)

    assert err.value.available == 2
    assert fake.balances["merch-A"]["credits"] == 2


def test_credits_for_probe_uses_seeded_provider_config():
    from services.provider_credit_rates import credits_for_probe

    assert credits_for_probe("gemini", grounded=True) == pytest.approx(5.7)
    assert credits_for_probe("deepseek", grounded=False) == pytest.approx(0.1)
