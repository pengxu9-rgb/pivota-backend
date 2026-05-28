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
        self.subscriptions: Dict[str, Dict[str, Any]] = {}
        self.version_conflict_once = False
        self.conflict_amount = 0

    def seed(
        self,
        merchant_id: str,
        *,
        credits: int = 0,
        purchased_credits: int = 0,
        allowance_credits: int = 0,
        allowance_period_start: Optional[datetime] = None,
        usd_cogs_internal: Decimal = Decimal("0"),
        plan_tier: str = "free",
    ) -> None:
        self.balances[merchant_id] = {
            "merchant_id": merchant_id,
            "credits": credits,
            "purchased_credits": purchased_credits,
            "allowance_credits": allowance_credits,
            "allowance_period_start": allowance_period_start,
            "usd_cogs_internal": usd_cogs_internal,
            "plan_tier": plan_tier,
            "updated_at": datetime.now(timezone.utc),
            "version": 0,
        }

    def seed_subscription(
        self,
        merchant_id: str,
        *,
        allowance: int,
        plan_tier: str = "starter",
        status: str = "active",
    ) -> None:
        self.subscriptions[merchant_id] = {
            "merchant_id": merchant_id,
            "monthly_credit_allowance": allowance,
            "plan_tier": plan_tier,
            "status": status,
        }

    async def fetch_one(self, query: str, values: Optional[Dict[str, Any]] = None):
        values = values or {}
        sql = str(query)
        if "merchant_credit_balance:active_subscription_allowance" in sql:
            row = self.subscriptions.get(str(values["merchant_id"]))
            if row and row.get("status") in {"active", "trialing"}:
                return {
                    "monthly_credit_allowance": row["monthly_credit_allowance"],
                    "plan_tier": row["plan_tier"],
                }
            return None
        if "merchant_credit_balance:get_balance" in sql:
            row = self.balances.get(str(values["merchant_id"]))
            return dict(row) if row else None
        if "merchant_credit_balance:apply_subscription_allowance" in sql:
            merchant_id = str(values["merchant_id"])
            row = self.balances[merchant_id]
            period_start = values["allowance_period_start"]
            if row.get("allowance_period_start") == period_start:
                return None
            row["credits"] = (
                int(row.get("purchased_credits") or 0)
                + int(values["allowance_credits"])
            )
            row["allowance_credits"] = int(values["allowance_credits"])
            row["allowance_period_start"] = period_start
            row["plan_tier"] = str(values["plan_tier"])
            row["updated_at"] = datetime.now(timezone.utc)
            row["version"] = int(row["version"]) + 1
            return dict(row)
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
        purchased_delta = int(values.get("purchased_credits") or 0)
        if delta < 0 and int(row.get("purchased_credits") or 0) < purchased_delta:
            return None
        row["credits"] += delta
        row["purchased_credits"] = (
            int(row.get("purchased_credits") or 0)
            + (purchased_delta if delta >= 0 else -purchased_delta)
        )
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
    assert balance["purchased_credits"] == 0
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
    assert first["purchased_credits"] == 4
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


@pytest.mark.asyncio
async def test_apply_subscription_allowance_grants_once_per_calendar_month(monkeypatch):
    from services import merchant_credit_balance_service as svc

    month = datetime(2026, 5, 1, tzinfo=timezone.utc)
    fake = FakeCreditConn()
    fake.seed_subscription("merch-A", allowance=4000, plan_tier="starter")
    monkeypatch.setattr(svc, "_current_month_start", lambda: month)

    first = await svc.apply_subscription_allowance("merch-A", conn=fake)
    second = await svc.apply_subscription_allowance("merch-A", conn=fake)

    assert first["credits"] == 4000
    assert first["allowance_credits"] == 4000
    assert first["allowance_period_start"] == month
    assert first["plan_tier"] == "starter"
    assert second["credits"] == 4000
    assert fake.balances["merch-A"]["version"] == 1


@pytest.mark.asyncio
async def test_apply_subscription_allowance_rolls_month_and_keeps_topups(monkeypatch):
    from services import merchant_credit_balance_service as svc

    april = datetime(2026, 4, 1, tzinfo=timezone.utc)
    may = datetime(2026, 5, 1, tzinfo=timezone.utc)
    fake = FakeCreditConn()
    fake.seed(
        "merch-A",
        credits=1300,
        purchased_credits=300,
        allowance_credits=4000,
        allowance_period_start=april,
        plan_tier="starter",
    )
    fake.seed_subscription("merch-A", allowance=4000, plan_tier="starter")
    monkeypatch.setattr(svc, "_current_month_start", lambda: may)

    balance = await svc.apply_subscription_allowance("merch-A", conn=fake)

    assert balance["credits"] == 4300
    assert balance["purchased_credits"] == 300
    assert balance["allowance_credits"] == 4000
    assert balance["allowance_period_start"] == may


@pytest.mark.asyncio
async def test_get_balance_lazily_resets_stale_allowance(monkeypatch):
    from services import merchant_credit_balance_service as svc

    april = datetime(2026, 4, 1, tzinfo=timezone.utc)
    may = datetime(2026, 5, 1, tzinfo=timezone.utc)
    fake = FakeCreditConn()
    fake.seed(
        "merch-A",
        credits=250,
        purchased_credits=100,
        allowance_credits=4000,
        allowance_period_start=april,
        plan_tier="starter",
    )
    fake.seed_subscription("merch-A", allowance=4000, plan_tier="starter")
    monkeypatch.setattr(svc, "database", fake)
    monkeypatch.setattr(svc, "_current_month_start", lambda: may)

    balance = await svc.get_balance("merch-A")

    assert balance["credits"] == 4100
    assert balance["purchased_credits"] == 100
    assert balance["allowance_period_start"] == may


@pytest.mark.asyncio
async def test_debit_lazily_resets_stale_allowance_and_consumes_allowance_first(monkeypatch):
    from services import merchant_credit_balance_service as svc

    april = datetime(2026, 4, 1, tzinfo=timezone.utc)
    may = datetime(2026, 5, 1, tzinfo=timezone.utc)
    fake = FakeCreditConn()
    fake.seed(
        "merch-A",
        credits=50,
        purchased_credits=25,
        allowance_credits=4000,
        allowance_period_start=april,
        plan_tier="starter",
    )
    fake.seed_subscription("merch-A", allowance=4000, plan_tier="starter")
    monkeypatch.setattr(svc, "_current_month_start", lambda: may)

    balance = await svc.debit("merch-A", "audit", 100, "run-1", conn=fake)

    assert balance["credits"] == 3925
    assert balance["purchased_credits"] == 25
    assert balance["purchased_credits_debited"] == 0
    assert balance["allowance_period_start"] == may


@pytest.mark.asyncio
async def test_purchased_topup_credits_survive_reset_after_allowance_spend(monkeypatch):
    from services import merchant_credit_balance_service as svc

    may = datetime(2026, 5, 1, tzinfo=timezone.utc)
    june = datetime(2026, 6, 1, tzinfo=timezone.utc)
    fake = FakeCreditConn()
    fake.seed(
        "merch-A",
        credits=5000,
        purchased_credits=1000,
        allowance_credits=4000,
        allowance_period_start=may,
        plan_tier="starter",
    )
    fake.seed_subscription("merch-A", allowance=4000, plan_tier="starter")
    monkeypatch.setattr(svc, "_current_month_start", lambda: may)

    spent = await svc.debit("merch-A", "audit", 4500, "run-1", conn=fake)
    assert spent["credits"] == 500
    assert spent["purchased_credits"] == 500
    assert spent["purchased_credits_debited"] == 500

    monkeypatch.setattr(svc, "_current_month_start", lambda: june)
    balance = await svc.apply_subscription_allowance("merch-A", conn=fake)

    assert balance["credits"] == 4500
    assert balance["purchased_credits"] == 500
    assert balance["allowance_credits"] == 4000


@pytest.mark.asyncio
async def test_require_verified_payment_method_raises_typed_error_without_customer(monkeypatch):
    from services import merchant_credit_balance_service as svc

    async def no_billing_row(*_args, **_kwargs):
        return None

    monkeypatch.setattr(svc, "_fetch_merchant_billing_row", no_billing_row)

    with pytest.raises(svc.MissingVerifiedPaymentMethodError) as err:
        await svc.require_verified_payment_method("merch-A")

    assert err.value.code == "missing_verified_payment_method"
    assert err.value.reason == "missing_stripe_customer"


@pytest.mark.asyncio
async def test_require_verified_payment_method_accepts_default_card(monkeypatch):
    from services import merchant_credit_balance_service as svc

    async def billing_row(*_args, **_kwargs):
        return {"stripe_customer_id": "cus_123"}

    class _Customers:
        def retrieve(self, customer_id):
            assert customer_id == "cus_123"
            return {
                "id": customer_id,
                "invoice_settings": {
                    "default_payment_method": "pm_123",
                },
            }

    class _PaymentMethods:
        def retrieve(self, payment_method_id):
            assert payment_method_id == "pm_123"
            return {
                "id": payment_method_id,
                "type": "card",
                "customer": "cus_123",
            }

    class _V1:
        customers = _Customers()
        payment_methods = _PaymentMethods()

    class _StripeClient:
        v1 = _V1()

    monkeypatch.setattr(svc, "_fetch_merchant_billing_row", billing_row)
    monkeypatch.setattr(svc, "stripe_client", _StripeClient())

    await svc.require_verified_payment_method("merch-A")


def test_credits_for_probe_uses_seeded_provider_config():
    from services.provider_credit_rates import credits_for_probe

    assert credits_for_probe("gemini", grounded=True) == pytest.approx(5.7)
    assert credits_for_probe("deepseek", grounded=False) == pytest.approx(0.1)
