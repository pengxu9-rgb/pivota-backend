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
        overage_pending_credits: int = 0,
        overage_charged_credits: int = 0,
        overage_blocked_until_payment: bool = False,
        overage_last_payment_intent_id: Optional[str] = None,
        overage_last_failed_at: Optional[datetime] = None,
        allowance_period_start: Optional[datetime] = None,
        usd_cogs_internal: Decimal = Decimal("0"),
        plan_tier: str = "free",
    ) -> None:
        self.balances[merchant_id] = {
            "merchant_id": merchant_id,
            "credits": credits,
            "purchased_credits": purchased_credits,
            "allowance_credits": allowance_credits,
            "overage_pending_credits": overage_pending_credits,
            "overage_charged_credits": overage_charged_credits,
            "overage_blocked_until_payment": overage_blocked_until_payment,
            "overage_last_payment_intent_id": overage_last_payment_intent_id,
            "overage_last_failed_at": overage_last_failed_at,
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
        if "merchant_credit_balance/active_subscription_allowance" in sql:
            row = self.subscriptions.get(str(values["merchant_id"]))
            if row and row.get("status") in {"active", "trialing"}:
                return {
                    "monthly_credit_allowance": row["monthly_credit_allowance"],
                    "plan_tier": row["plan_tier"],
                }
            return None
        if "merchant_credit_balance/get_balance" in sql:
            row = self.balances.get(str(values["merchant_id"]))
            return dict(row) if row else None
        if "merchant_credit_balance/apply_subscription_allowance" in sql:
            merchant_id = str(values["merchant_id"])
            row = self.balances[merchant_id]
            period_start = values["allowance_period_start"]
            new_allowance = int(values["allowance_credits"])
            new_tier = str(values["plan_tier"])
            # Mirror the SQL gate: grant on a new calendar month OR on an
            # upgrade (tier changed to a larger allowance). The real SQL is
            # "< period_start OR >= +1 month"; the fake collapses the monthly
            # arm to "different period" which suffices for the monkeypatched
            # single-month tests.
            monthly_due = row.get("allowance_period_start") != period_start
            is_upgrade = (
                str(row.get("plan_tier")) != new_tier
                and new_allowance > int(row.get("allowance_credits") or 0)
            )
            if not (monthly_due or is_upgrade):
                return None
            row["credits"] = int(row.get("purchased_credits") or 0) + new_allowance
            row["allowance_credits"] = new_allowance
            row["allowance_period_start"] = period_start
            row["plan_tier"] = new_tier
            row["updated_at"] = datetime.now(timezone.utc)
            row["version"] = int(row["version"]) + 1
            return dict(row)
        if "merchant_credit_balance/sync_plan_tier" in sql:
            row = self.balances.get(str(values["merchant_id"]))
            if row is None:
                return None
            new_tier = str(values["plan_tier"])
            if str(row.get("plan_tier")) == new_tier:
                return None
            row["plan_tier"] = new_tier
            row["updated_at"] = datetime.now(timezone.utc)
            row["version"] = int(row["version"]) + 1
            return dict(row)
        if "merchant_credit_balance/expire_plan_allowance" in sql:
            row = self.balances.get(str(values["merchant_id"]))
            if row is None:
                return None
            row["credits"] = int(row.get("purchased_credits") or 0)
            row["allowance_credits"] = 0
            row["allowance_period_start"] = None
            row["plan_tier"] = "free"
            row["updated_at"] = datetime.now(timezone.utc)
            row["version"] = int(row["version"]) + 1
            return dict(row)
        if "merchant_credit_balance/fetch_usage_replay" in sql:
            row = self.events.get(str(values["idempotency_key"]))
            return {"payload": dict(row["payload"])} if row else None
        if "merchant_credit_balance/claim_usage_operation" in sql:
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
        if "merchant_credit_balance/debit_update" in sql:
            # debit_update SQL references :balance_debit (not :amount);
            # the credit path uses :amount. The conditional split in
            # _apply_delta keeps each query's params exact to avoid
            # asyncpg prepared-statement ArgumentError on extras.
            return self._update_balance(
                values,
                delta=-int(values["balance_debit"]),
                usd_cogs_delta=Decimal(str(values["usd_cogs"])),
            )
        if "merchant_credit_balance/credit_update" in sql:
            return self._update_balance(
                values,
                delta=int(values["amount"]),
                usd_cogs_delta=-Decimal(str(values["usd_cogs"])),
            )
        if "merchant_credit_balance/topup_credit_update" in sql:
            merchant_id = str(values["merchant_id"])
            row = self.balances[merchant_id]
            row["credits"] = int(row.get("credits") or 0) + int(values["pack_credits"])
            row["purchased_credits"] = (
                int(row.get("purchased_credits") or 0) + int(values["pack_credits"])
            )
            row["overage_blocked_until_payment"] = False
            row["overage_last_payment_intent_id"] = values["payment_intent_id"]
            row["overage_last_failed_at"] = None
            row["version"] = int(row["version"]) + 1
            row["updated_at"] = datetime.now(timezone.utc)
            return dict(row)
        if "merchant_credit_balance/pending_overage_charge_update" in sql:
            merchant_id = str(values["merchant_id"])
            row = self.balances[merchant_id]
            charge = int(values["charge_credits"])
            if int(row.get("overage_pending_credits") or 0) < charge:
                return None
            row["overage_pending_credits"] = int(row["overage_pending_credits"]) - charge
            row["overage_charged_credits"] = int(row["overage_charged_credits"]) + charge
            row["overage_blocked_until_payment"] = False
            row["overage_last_payment_intent_id"] = values["payment_intent_id"]
            row["overage_last_failed_at"] = None
            row["version"] = int(row["version"]) + 1
            row["updated_at"] = datetime.now(timezone.utc)
            return dict(row)
        raise AssertionError(f"unexpected fetch_one SQL: {sql}")

    async def execute(self, query: str, values: Optional[Dict[str, Any]] = None):
        values = values or {}
        sql = str(query)
        if "merchant_credit_balance/ensure_row" in sql:
            merchant_id = str(values["merchant_id"])
            if merchant_id not in self.balances:
                self.seed(merchant_id)
            return None
        if "merchant_credit_balance/store_usage_post_balance" in sql:
            key = str(values["idempotency_key"])
            self.events[key]["payload"].update(json.loads(values["payload"]))
            return None
        if "merchant_credit_balance/set_overage_blocked" in sql:
            merchant_id = str(values["merchant_id"])
            row = self.balances[merchant_id]
            row["overage_blocked_until_payment"] = True
            row["overage_last_payment_intent_id"] = (
                values.get("payment_intent_id") or row.get("overage_last_payment_intent_id")
            )
            row["overage_last_failed_at"] = datetime.now(timezone.utc)
            row["version"] = int(row["version"]) + 1
            row["updated_at"] = datetime.now(timezone.utc)
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
        if delta < 0:
            overage_pending_delta = int(values.get("overage_pending_credits") or 0)
            overage_charge = int(values.get("overage_charge_credits") or 0)
            if int(row.get("overage_pending_credits") or 0) + overage_pending_delta < overage_charge:
                return None
            row["overage_pending_credits"] = (
                int(row.get("overage_pending_credits") or 0)
                + overage_pending_delta
                - overage_charge
            )
            row["overage_charged_credits"] = (
                int(row.get("overage_charged_credits") or 0) + overage_charge
            )
            if overage_charge:
                row["overage_blocked_until_payment"] = False
                row["overage_last_payment_intent_id"] = values.get(
                    "overage_payment_intent_id"
                )
                row["overage_last_failed_at"] = None
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
async def test_apply_subscription_allowance_regrants_on_midcycle_upgrade(monkeypatch):
    # Bug: a merchant who upgrades Starter->Growth mid-cycle kept seeing the
    # stale Starter tier + Starter credits on the AI-readiness / audit preview
    # (which reads plan_tier from this wallet) because the allowance UPDATE is
    # gated to once per calendar month and never re-fired on upgrade. The
    # upgrade must re-grant the larger allowance and resync the tier now.
    from services import merchant_credit_balance_service as svc

    month = datetime(2026, 6, 1, tzinfo=timezone.utc)
    fake = FakeCreditConn()
    fake.seed(
        "merch-A",
        credits=4300,            # 300 purchased + 4000 starter allowance
        purchased_credits=300,
        allowance_credits=4000,
        allowance_period_start=month,   # already granted this month under Starter
        plan_tier="starter",
    )
    # Live subscription is now Growth with a larger allowance.
    fake.seed_subscription("merch-A", allowance=20000, plan_tier="growth")
    monkeypatch.setattr(svc, "_current_month_start", lambda: month)

    balance = await svc.apply_subscription_allowance("merch-A", conn=fake)

    assert balance["plan_tier"] == "growth"
    assert balance["allowance_credits"] == 20000
    assert balance["credits"] == 20300       # 300 purchased + 20000 growth
    assert balance["purchased_credits"] == 300
    assert balance["allowance_period_start"] == month


@pytest.mark.asyncio
async def test_apply_subscription_allowance_syncs_tier_without_refill(monkeypatch):
    # A tier change that is NOT an upgrade (same/smaller allowance — e.g. a plan
    # rename or paid downgrade) must still resync the displayed tier so the
    # snapshot never lags the live subscription, but it must NOT refill credits.
    from services import merchant_credit_balance_service as svc

    month = datetime(2026, 6, 1, tzinfo=timezone.utc)
    fake = FakeCreditConn()
    fake.seed(
        "merch-A",
        credits=1300,            # 300 purchased + 1000 remaining growth allowance
        purchased_credits=300,
        allowance_credits=4000,
        allowance_period_start=month,
        plan_tier="growth",
    )
    # Live subscription downgraded to a same-allowance (or renamed) plan.
    fake.seed_subscription("merch-A", allowance=4000, plan_tier="starter")
    monkeypatch.setattr(svc, "_current_month_start", lambda: month)

    balance = await svc.apply_subscription_allowance("merch-A", conn=fake)

    assert balance["plan_tier"] == "starter"   # label resynced
    assert balance["credits"] == 1300          # NOT refilled
    assert balance["allowance_credits"] == 4000


@pytest.mark.asyncio
async def test_expire_plan_allowance_wipes_allowance_keeps_topups(monkeypatch):
    # ADR-005 §2: downgrade wipes the monthly plan allowance; purchased top-ups
    # survive. Growth merchant with 300 purchased + 1000 remaining allowance.
    from services import merchant_credit_balance_service as svc

    fake = FakeCreditConn()
    fake.seed(
        "merch-A",
        credits=1300,            # 300 purchased + 1000 remaining allowance
        purchased_credits=300,
        allowance_credits=4000,
        allowance_period_start=datetime(2026, 6, 1, tzinfo=timezone.utc),
        plan_tier="growth",
    )

    balance = await svc.expire_plan_allowance("merch-A", conn=fake)

    assert balance["credits"] == 300            # only the purchased top-up survives
    assert balance["purchased_credits"] == 300  # preserved
    assert balance["allowance_credits"] == 0    # allowance wiped
    assert balance["allowance_period_start"] is None
    assert balance["plan_tier"] == "free"


@pytest.mark.asyncio
async def test_expire_plan_allowance_noop_when_no_row(monkeypatch):
    # A merchant with no balance row has nothing to expire — return zero, no crash.
    from services import merchant_credit_balance_service as svc

    fake = FakeCreditConn()
    monkeypatch.setattr(svc, "database", fake)
    balance = await svc.expire_plan_allowance("merch-none", conn=fake)
    assert balance["credits"] == 0
    assert balance["allowance_credits"] == 0


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
async def test_paid_debit_past_zero_charges_overage_increment(monkeypatch):
    from services import merchant_credit_balance_service as svc

    fake = FakeCreditConn()
    fake.seed(
        "merch-A",
        credits=100,
        overage_pending_credits=1900,
        plan_tier="growth",
    )
    payment_intents = []

    async def fake_create_payment_intent(**kwargs):
        payment_intents.append(kwargs)
        return {"id": "pi_overage_1", "status": "succeeded"}

    monkeypatch.setattr(svc, "_create_direct_payment_intent", fake_create_payment_intent)

    balance = await svc.debit(
        "merch-A",
        "audit",
        200,
        "run-overage-1",
        usd_cogs=Decimal("0.5000"),
        conn=fake,
    )

    assert balance["credits"] == 0
    assert balance["overage_pending_credits"] == 0
    assert balance["overage_charged_credits"] == 2000
    assert balance["overage_credits_accrued"] == 100
    # 2000 overage credits charged at the 1.3c/credit overage rate (30% premium
    # over the 1c base credit price): 2000 * 1.3 = 2600 cents = $26.00.
    assert payment_intents[0]["amount_cents"] == 2600
    assert payment_intents[0]["idempotency_key"] == (
        "direct_overage_payment_intent:"
        "direct_overage:merch-A:000000000001-000000002000"
    )

    overage_events = [
        event for event in fake.events.values()
        if event["event_type"] == "credit_overage_charge"
    ]
    assert len(overage_events) == 1
    payload = overage_events[0]["payload"]
    assert payload["payment_intent_id"] == "pi_overage_1"
    assert payload["overage_increment_id"] == (
        "direct_overage:merch-A:000000000001-000000002000"
    )
    assert payload["amount_cents"] == 2600
    assert payload["usd_cogs_internal"] == "13.0000"


@pytest.mark.asyncio
async def test_paid_overage_idempotency_replays_without_double_charge(monkeypatch):
    from services import merchant_credit_balance_service as svc

    fake = FakeCreditConn()
    fake.seed(
        "merch-A",
        credits=100,
        overage_pending_credits=1900,
        plan_tier="growth",
    )
    payment_intents = []

    async def fake_create_payment_intent(**kwargs):
        payment_intents.append(kwargs)
        return {"id": "pi_overage_1", "status": "succeeded"}

    monkeypatch.setattr(svc, "_create_direct_payment_intent", fake_create_payment_intent)

    first = await svc.debit("merch-A", "audit", 200, "run-overage-1", conn=fake)
    second = await svc.debit("merch-A", "audit", 200, "run-overage-1", conn=fake)

    assert first["replay"] is False
    assert second["replay"] is True
    assert len(payment_intents) == 1
    assert fake.balances["merch-A"]["overage_charged_credits"] == 2000
    assert len([
        event for event in fake.events.values()
        if event["event_type"] == "credit_overage_charge"
    ]) == 1


@pytest.mark.asyncio
async def test_overage_failure_sets_hard_stop_and_topup_clears(monkeypatch):
    from services import merchant_credit_balance_service as svc

    fake = FakeCreditConn()
    fake.seed(
        "merch-A",
        credits=0,
        overage_pending_credits=2000,
        plan_tier="growth",
    )

    async def declined_payment_intent(**_kwargs):
        raise RuntimeError("card_declined")

    monkeypatch.setattr(svc, "_create_direct_payment_intent", declined_payment_intent)

    with pytest.raises(svc.OveragePaymentFailedError) as err:
        await svc.debit("merch-A", "audit", 1, "run-fail-1", conn=fake)

    assert err.value.code == "overage_payment_failed"
    assert fake.balances["merch-A"]["overage_blocked_until_payment"] is True

    with pytest.raises(svc.OveragePaymentBlockedError):
        await svc.debit("merch-A", "audit", 1, "run-blocked-1", conn=fake)

    result = await svc.apply_credit_topup_payment_intent_succeeded(
        {
            "id": "pi_topup_clear",
            "amount": 5000,
            "metadata": {
                "pivota_purpose": svc.DIRECT_TOPUP_PURPOSE,
                "merchant_id": "merch-A",
                "pack_credits": "5000",
            },
        },
        conn=fake,
    )

    assert result["credits"] == 5000
    assert result["purchased_credits"] == 5000
    assert result["overage_blocked_until_payment"] is False


@pytest.mark.asyncio
async def test_topup_webhook_replay_does_not_double_credit():
    from services import merchant_credit_balance_service as svc

    fake = FakeCreditConn()
    fake.seed("merch-A", credits=0, plan_tier="growth")
    payment_intent = {
        "id": "pi_topup_1",
        "amount": 2000,
        "metadata": {
            "pivota_purpose": svc.DIRECT_TOPUP_PURPOSE,
            "merchant_id": "merch-A",
            "pack_credits": "2000",
        },
    }

    first = await svc.apply_credit_topup_payment_intent_succeeded(
        payment_intent,
        conn=fake,
    )
    second = await svc.apply_credit_topup_payment_intent_succeeded(
        payment_intent,
        conn=fake,
    )

    assert first["credits"] == 2000
    assert second["credits"] == 2000
    assert second["replay"] is True
    assert fake.balances["merch-A"]["credits"] == 2000
    assert fake.balances["merch-A"]["purchased_credits"] == 2000
    assert len([
        event for event in fake.events.values()
        if event["event_type"] == "credit_topup"
    ]) == 1


@pytest.mark.asyncio
async def test_create_credit_topup_payment_intent_uses_customer_total_not_rate(monkeypatch):
    from services import merchant_credit_balance_service as svc

    payment_intents = []

    async def fake_create_payment_intent(**kwargs):
        payment_intents.append(kwargs)
        return {"id": "pi_topup_create", "status": "succeeded"}

    monkeypatch.setattr(svc, "_create_direct_payment_intent", fake_create_payment_intent)

    result = await svc.create_credit_topup_payment_intent(
        merchant_id="merch-A",
        pack_credits=2000,
        idempotency_key="manual-1",
    )

    assert result == {
        "payment_intent_id": "pi_topup_create",
        "status": "succeeded",
        "pack_credits": 2000,
        "amount": {"currency": "usd", "total": "20.00"},
    }
    assert payment_intents[0]["amount_cents"] == 2000
    assert payment_intents[0]["metadata"]["pack_credits"] == "2000"


@pytest.mark.asyncio
async def test_create_credit_topup_payment_intent_window_idempotency_dedupes_grants(
    monkeypatch,
):
    from services import merchant_credit_balance_service as svc

    fake = FakeCreditConn()
    fake.seed("merch-A", credits=0, plan_tier="growth")
    bucket = {"value": 1000}
    created_payment_intents: Dict[str, Dict[str, Any]] = {}

    async def fake_create_payment_intent(**kwargs):
        key = kwargs["idempotency_key"]
        if key not in created_payment_intents:
            created_payment_intents[key] = {
                "id": f"pi_topup_{len(created_payment_intents) + 1}",
                "status": "succeeded",
                "amount": kwargs["amount_cents"],
                "metadata": dict(kwargs["metadata"]),
            }
        return created_payment_intents[key]

    monkeypatch.setattr(svc, "_topup_idempotency_bucket", lambda: bucket["value"])
    monkeypatch.setattr(svc, "_create_direct_payment_intent", fake_create_payment_intent)

    first = await svc.create_credit_topup_payment_intent(
        merchant_id="merch-A",
        pack_credits=2000,
    )
    second = await svc.create_credit_topup_payment_intent(
        merchant_id="merch-A",
        pack_credits=2000,
    )

    derived_key = "topup:merch-A:2000:1000"
    assert first["payment_intent_id"] == second["payment_intent_id"]
    assert list(created_payment_intents) == [derived_key]
    assert created_payment_intents[derived_key]["metadata"]["idempotency_key"] == derived_key

    first_grant = await svc.apply_credit_topup_payment_intent_succeeded(
        created_payment_intents[derived_key],
        conn=fake,
    )
    replay_grant = await svc.apply_credit_topup_payment_intent_succeeded(
        created_payment_intents[derived_key],
        conn=fake,
    )
    assert first_grant["credits"] == 2000
    assert replay_grant["replay"] is True
    assert fake.balances["merch-A"]["credits"] == 2000

    bucket["value"] = 1001
    later = await svc.create_credit_topup_payment_intent(
        merchant_id="merch-A",
        pack_credits=2000,
    )
    later_key = "topup:merch-A:2000:1001"
    assert later["payment_intent_id"] != first["payment_intent_id"]
    assert later_key in created_payment_intents

    await svc.apply_credit_topup_payment_intent_succeeded(
        created_payment_intents[later_key],
        conn=fake,
    )
    assert fake.balances["merch-A"]["credits"] == 4000

    explicit_first = await svc.create_credit_topup_payment_intent(
        merchant_id="merch-A",
        pack_credits=1000,
        idempotency_key="manual-dedupe",
    )
    bucket["value"] = 1002
    explicit_second = await svc.create_credit_topup_payment_intent(
        merchant_id="merch-A",
        pack_credits=1000,
        idempotency_key="manual-dedupe",
    )

    assert explicit_first["payment_intent_id"] == explicit_second["payment_intent_id"]
    assert "manual-dedupe" in created_payment_intents
    await svc.apply_credit_topup_payment_intent_succeeded(
        created_payment_intents["manual-dedupe"],
        conn=fake,
    )
    await svc.apply_credit_topup_payment_intent_succeeded(
        created_payment_intents["manual-dedupe"],
        conn=fake,
    )

    assert fake.balances["merch-A"]["credits"] == 5000
    assert len([
        event for event in fake.events.values()
        if event["event_type"] == "credit_topup"
    ]) == 3


@pytest.mark.asyncio
async def test_free_tier_insufficient_balance_does_not_attempt_overage(monkeypatch):
    from services import merchant_credit_balance_service as svc

    fake = FakeCreditConn()
    fake.seed("merch-A", credits=1, plan_tier="free")

    async def should_not_charge(**_kwargs):
        raise AssertionError("free tier must not attempt an overage charge")

    monkeypatch.setattr(svc, "_create_direct_payment_intent", should_not_charge)

    with pytest.raises(svc.InsufficientCreditsError):
        await svc.debit("merch-A", "audit", 2, "run-free-1", conn=fake)


@pytest.mark.asyncio
async def test_require_verified_payment_method_raises_typed_error_without_customer(monkeypatch):
    from services import merchant_credit_balance_service as svc

    async def no_customer(*_args, **_kwargs):
        return ""

    monkeypatch.setattr(svc, "resolve_merchant_stripe_customer_id", no_customer)

    with pytest.raises(svc.MissingVerifiedPaymentMethodError) as err:
        await svc.require_verified_payment_method("merch-A")

    assert err.value.code == "missing_verified_payment_method"
    assert err.value.reason == "missing_stripe_customer"


@pytest.mark.asyncio
async def test_require_verified_payment_method_accepts_default_card(monkeypatch):
    from services import merchant_credit_balance_service as svc

    async def billing_row(*_args, **_kwargs):
        return "cus_123"

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
                "card": {
                    "exp_month": 12,
                    "exp_year": datetime.now(timezone.utc).year + 1,
                },
            }

    class _V1:
        customers = _Customers()
        payment_methods = _PaymentMethods()

    class _StripeClient:
        v1 = _V1()

    monkeypatch.setattr(svc, "resolve_merchant_stripe_customer_id", billing_row)
    monkeypatch.setattr(svc, "stripe_client", _StripeClient())

    await svc.require_verified_payment_method("merch-A")


@pytest.mark.asyncio
async def test_require_verified_payment_method_rejects_expired_card(monkeypatch):
    from services import merchant_credit_balance_service as svc

    async def billing_row(*_args, **_kwargs):
        return "cus_123"

    class _Customers:
        def retrieve(self, customer_id):
            return {
                "id": customer_id,
                "invoice_settings": {
                    "default_payment_method": "pm_expired",
                },
            }

    class _PaymentMethods:
        def retrieve(self, payment_method_id):
            return {
                "id": payment_method_id,
                "type": "card",
                "customer": "cus_123",
                "card": {
                    "exp_month": 1,
                    "exp_year": datetime.now(timezone.utc).year - 1,
                },
            }

    class _V1:
        customers = _Customers()
        payment_methods = _PaymentMethods()

    class _StripeClient:
        v1 = _V1()

    monkeypatch.setattr(svc, "resolve_merchant_stripe_customer_id", billing_row)
    monkeypatch.setattr(svc, "stripe_client", _StripeClient())

    with pytest.raises(svc.MissingVerifiedPaymentMethodError) as err:
        await svc.require_verified_payment_method("merch-A")

    assert err.value.reason == "card_expired"


@pytest.mark.asyncio
async def test_require_verified_payment_method_maps_stripe_error(monkeypatch):
    from services import merchant_credit_balance_service as svc

    async def billing_row(*_args, **_kwargs):
        return "cus_123"

    StripeError = type("StripeError", (Exception,), {})

    class _Customers:
        def retrieve(self, _customer_id):
            raise StripeError("stripe unavailable")

    class _PaymentMethods:
        def retrieve(self, _payment_method_id):
            raise AssertionError("payment method lookup should not run")

    class _V1:
        customers = _Customers()
        payment_methods = _PaymentMethods()

    class _StripeClient:
        v1 = _V1()

    monkeypatch.setattr(svc, "resolve_merchant_stripe_customer_id", billing_row)
    monkeypatch.setattr(svc, "stripe_client", _StripeClient())

    with pytest.raises(svc.MissingVerifiedPaymentMethodError) as err:
        await svc.require_verified_payment_method("merch-A")

    assert err.value.reason == "stripe_unavailable"


@pytest.mark.asyncio
async def test_require_verified_payment_method_falls_back_to_subscription_pm(monkeypatch):
    """No customer-level default PM -> use the active subscription's default PM."""
    from services import merchant_credit_balance_service as svc

    async def resolver(*_args, **_kwargs):
        return "cus_123"

    class _Customers:
        def retrieve(self, customer_id):
            return {"id": customer_id, "invoice_settings": {"default_payment_method": None}}

    class _Subscriptions:
        def list(self, params=None, options=None):
            assert params["customer"] == "cus_123"
            return {"data": [{"default_payment_method": "pm_sub"}]}

    class _PaymentMethods:
        def retrieve(self, payment_method_id):
            assert payment_method_id == "pm_sub"
            return {
                "id": payment_method_id,
                "type": "card",
                "customer": "cus_123",
                "card": {"exp_month": 12, "exp_year": datetime.now(timezone.utc).year + 1},
            }

        def list(self, params=None, options=None):  # pragma: no cover - sub PM wins first
            raise AssertionError("attached-card fallback should not run when a sub PM exists")

    class _V1:
        customers = _Customers()
        subscriptions = _Subscriptions()
        payment_methods = _PaymentMethods()

    class _StripeClient:
        v1 = _V1()

    monkeypatch.setattr(svc, "resolve_merchant_stripe_customer_id", resolver)
    monkeypatch.setattr(svc, "stripe_client", _StripeClient())

    customer_id, pm_id = await svc._verified_default_payment_method_for_direct_merchant("merch-A")
    assert (customer_id, pm_id) == ("cus_123", "pm_sub")


@pytest.mark.asyncio
async def test_require_verified_payment_method_no_pm_anywhere_raises(monkeypatch):
    """No default PM, no subscription PM, no attached card -> no_default_pm."""
    from services import merchant_credit_balance_service as svc

    async def resolver(*_args, **_kwargs):
        return "cus_123"

    class _Customers:
        def retrieve(self, customer_id):
            return {"id": customer_id, "invoice_settings": {"default_payment_method": None}}

    class _Subscriptions:
        def list(self, params=None, options=None):
            return {"data": []}

    class _PaymentMethods:
        def list(self, params=None, options=None):
            return {"data": []}

    class _V1:
        customers = _Customers()
        subscriptions = _Subscriptions()
        payment_methods = _PaymentMethods()

    class _StripeClient:
        v1 = _V1()

    monkeypatch.setattr(svc, "resolve_merchant_stripe_customer_id", resolver)
    monkeypatch.setattr(svc, "stripe_client", _StripeClient())

    with pytest.raises(svc.MissingVerifiedPaymentMethodError) as err:
        await svc.require_verified_payment_method("merch-A")
    assert err.value.reason == "no_default_pm"


def test_credits_for_probe_uses_seeded_provider_config():
    from services.provider_credit_rates import credits_for_probe

    # Pricing: COGS x flat_multiple(1.2) / credit_to_usd(0.01); gpt-5.5 output $30.
    # ChatGPT is priced on its measured grounded input (~15k tok/probe via
    # web_search_preview), not the flat 2000 that under-recovered COGS — see the
    # audit-billing-cogs fix (#1506). Gemini/DeepSeek keep the shared 2000/500.
    assert credits_for_probe("gemini", grounded=True) == pytest.approx(4.4)
    assert credits_for_probe("chatgpt", grounded=True) == pytest.approx(11.7)
    assert credits_for_probe("deepseek", grounded=False) == pytest.approx(0.1)
