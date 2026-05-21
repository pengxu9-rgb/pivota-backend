from __future__ import annotations

import asyncio
from typing import Any

import pytest


class _FakeTransaction:
    def __init__(self, db: "_FakeMeteringDatabase") -> None:
        self.db = db

    async def __aenter__(self) -> "_FakeTransaction":
        await self.db.transaction_lock.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self.db.transaction_lock.release()
        return False


class _FakeMeteringDatabase:
    def __init__(self) -> None:
        self.transaction_lock = asyncio.Lock()
        self.operation_costs: dict[str, int] = {}
        self.merchant_credits: dict[str, dict[str, Any]] = {}
        self.reservations: dict[str, dict[str, Any]] = {}
        self.ledger: list[dict[str, Any]] = []
        self.queries: list[str] = []
        self.next_reservation_id = 1

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction(self)

    def add_merchant(
        self,
        merchant_id: str,
        *,
        balance: int,
        auto_topup_enabled: bool = False,
        auto_topup_amount_credits: int = 0,
    ) -> None:
        self.merchant_credits[merchant_id] = {
            "merchant_id": merchant_id,
            "balance": balance,
            "auto_topup_enabled": auto_topup_enabled,
            "auto_topup_threshold": 0,
            "auto_topup_amount_credits": auto_topup_amount_credits,
        }

    async def fetch_one(self, query: str, values: dict[str, Any] | None = None):
        values = dict(values or {})
        q = self._record(query)

        if "from operation_cost_config" in q:
            operation_type = values["operation_type"]
            if operation_type not in self.operation_costs:
                return None
            return {"credits": self.operation_costs[operation_type]}

        if "from credit_ledger" in q and "operation_type = 'auto_topup'" in q:
            auto_topups = [row for row in self.ledger if row.get("operation_type") == "auto_topup"]
            return {
                "daily_topup_count": len(auto_topups),
                "monthly_spend_cents": sum(int(row["credits_delta"]) for row in auto_topups),
            }

        if q.startswith("insert into credit_reservations"):
            reservation_id = str(self.next_reservation_id)
            self.next_reservation_id += 1
            self.reservations[reservation_id] = {
                "id": reservation_id,
                "merchant_id": values["merchant_id"],
                "operation_type": values["operation_type"],
                "operation_id": values["operation_id"],
                "credits_held": values["credits_held"],
                "status": "reserved",
                "metadata": values.get("metadata"),
                "is_stale": False,
            }
            return {"id": reservation_id}

        if "from credit_reservations" in q and "for update" in q:
            reservation = self.reservations.get(str(values["reservation_id"]))
            if not reservation:
                return None
            if "and status = 'reserved'" in q and reservation["status"] != "reserved":
                return None
            return dict(reservation)

        if q.startswith("update merchant_credits") and "balance = balance +" in q:
            merchant = self.merchant_credits.get(values["merchant_id"])
            if not merchant:
                return None
            merchant["balance"] += int(values["credits_delta"])
            return {"balance": merchant["balance"]}

        if (
            "from merchant_credits" in q
            and "auto_topup_amount_credits" in q
            and "for update" not in q
        ):
            merchant = self.merchant_credits.get(values["merchant_id"])
            if not merchant:
                return None
            return {"auto_topup_amount_credits": merchant["auto_topup_amount_credits"]}

        if "from merchant_credits" in q and "for update" in q:
            merchant = self.merchant_credits.get(values["merchant_id"])
            return dict(merchant) if merchant else None

        raise AssertionError(f"Unhandled fetch_one query: {query}")

    async def fetch_all(self, query: str, values: dict[str, Any] | None = None):
        q = self._record(query)
        if "from credit_reservations" in q and "expires_at < now()" in q:
            return [
                dict(row)
                for row in self.reservations.values()
                if row["status"] == "reserved" and row.get("is_stale")
            ]
        raise AssertionError(f"Unhandled fetch_all query: {query}")

    async def execute(self, query: str, values: dict[str, Any] | None = None):
        values = dict(values or {})
        q = self._record(query)

        if q.startswith("update merchant_credits") and "balance = balance -" in q:
            self.merchant_credits[values["merchant_id"]]["balance"] -= int(values["credits_held"])
            return None

        if q.startswith("update credit_reservations"):
            reservation = self.reservations[str(values["reservation_id"])]
            if "status = 'committed'" in q:
                reservation["status"] = "committed"
            elif "status = 'released'" in q:
                reservation["status"] = "released"
            elif "status = 'expired'" in q:
                reservation["status"] = "expired"
            return None

        if q.startswith("insert into credit_ledger"):
            self.ledger.append(dict(values))
            return None

        raise AssertionError(f"Unhandled execute query: {query}")

    def _record(self, query: str) -> str:
        normalized = " ".join(str(query).split()).lower()
        self.queries.append(normalized)
        return normalized


@pytest.fixture
def metering(monkeypatch: pytest.MonkeyPatch):
    from services import metering_service as module

    fake_db = _FakeMeteringDatabase()
    module._cost_cache.clear()
    monkeypatch.setattr(module, "database", fake_db)
    monkeypatch.delenv("METERING_AUTO_TOPUP_DAILY_CAP", raising=False)
    monkeypatch.delenv("METERING_AUTO_TOPUP_MONTHLY_SPEND_CEILING_CENTS", raising=False)
    return module, fake_db


@pytest.mark.asyncio
async def test_reserve_commit_happy_path(metering):
    module, db = metering
    db.operation_costs["agent.search"] = 5
    db.add_merchant("merch_1", balance=10)

    reservation_id = await module.reserve(
        "merch_1",
        "agent.search",
        "op_1",
        metadata={"request_id": "req_1"},
    )
    await module.commit(reservation_id)

    assert db.merchant_credits["merch_1"]["balance"] == 5
    assert db.reservations[reservation_id]["status"] == "committed"
    assert db.ledger == [
        {
            "merchant_id": "merch_1",
            "operation_type": "agent.search",
            "operation_id": "op_1",
            "credits_delta": -5,
            "balance_after": 5,
            "reservation_id": reservation_id,
            "source_payment_intent_id": None,
            "source_type": "operation_commit",
            "metadata": "{}",
        }
    ]
    assert any("from merchant_credits" in q and "for update" in q for q in db.queries)
    assert any("from credit_reservations" in q and "for update" in q for q in db.queries)


@pytest.mark.asyncio
async def test_reserve_release_happy_path(metering):
    module, db = metering
    db.operation_costs["agent.search"] = 5
    db.add_merchant("merch_1", balance=10)

    reservation_id = await module.reserve("merch_1", "agent.search", "op_2")
    await module.release(reservation_id)

    assert db.merchant_credits["merch_1"]["balance"] == 10
    assert db.reservations[reservation_id]["status"] == "released"
    assert db.ledger[-1]["operation_type"] == "release_refund"
    assert db.ledger[-1]["credits_delta"] == 5
    assert db.ledger[-1]["balance_after"] == 10
    assert db.ledger[-1]["source_type"] == "operation_release"


@pytest.mark.asyncio
async def test_reserve_expire_stale_reservation(metering):
    module, db = metering
    db.operation_costs["agent.search"] = 5
    db.add_merchant("merch_1", balance=10)

    reservation_id = await module.reserve("merch_1", "agent.search", "op_3")
    db.reservations[reservation_id]["is_stale"] = True

    expired = await module.expire_stale_reservations()

    assert expired == 1
    assert db.merchant_credits["merch_1"]["balance"] == 10
    assert db.reservations[reservation_id]["status"] == "expired"
    assert db.ledger[-1]["operation_type"] == "reservation_expired"
    assert db.ledger[-1]["credits_delta"] == 5
    assert db.ledger[-1]["source_type"] == "expiry"


@pytest.mark.asyncio
async def test_reserve_insufficient_credits_without_auto_topup(metering):
    module, db = metering
    db.operation_costs["agent.search"] = 5
    db.add_merchant("merch_1", balance=3, auto_topup_enabled=False)

    with pytest.raises(module.InsufficientCreditsError, match="balance=3, cost=5"):
        await module.reserve("merch_1", "agent.search", "op_4")

    assert db.merchant_credits["merch_1"]["balance"] == 3
    assert db.reservations == {}
    assert db.ledger == []


@pytest.mark.asyncio
async def test_reserve_triggers_auto_topup_and_does_not_reserve(
    metering,
    monkeypatch: pytest.MonkeyPatch,
):
    module, db = metering
    db.operation_costs["agent.search"] = 5
    db.add_merchant(
        "merch_1",
        balance=0,
        auto_topup_enabled=True,
        auto_topup_amount_credits=100,
    )
    triggered: list[tuple[str, int]] = []

    async def fake_trigger_auto_topup(merchant_id: str, needed_credits: int) -> None:
        triggered.append((merchant_id, needed_credits))

    monkeypatch.setattr(module, "_trigger_auto_topup", fake_trigger_auto_topup)

    with pytest.raises(module.AutoTopupCapExceeded, match="auto_topup_triggered"):
        await module.reserve("merch_1", "agent.search", "op_5")

    assert triggered == [("merch_1", 5)]
    assert db.merchant_credits["merch_1"]["balance"] == 0
    assert db.reservations == {}


@pytest.mark.asyncio
async def test_two_concurrent_reserves_only_one_succeeds(metering):
    module, db = metering
    db.operation_costs["agent.search"] = 5
    db.add_merchant("merch_1", balance=5)

    results = await asyncio.gather(
        module.reserve("merch_1", "agent.search", "op_race_1"),
        module.reserve("merch_1", "agent.search", "op_race_2"),
        return_exceptions=True,
    )

    successes = [result for result in results if isinstance(result, str)]
    failures = [result for result in results if isinstance(result, module.InsufficientCreditsError)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert db.merchant_credits["merch_1"]["balance"] == 0
    assert len(db.reservations) == 1


@pytest.mark.asyncio
async def test_commit_already_committed_reservation_raises(metering):
    module, db = metering
    db.operation_costs["agent.search"] = 5
    db.add_merchant("merch_1", balance=10)

    reservation_id = await module.reserve("merch_1", "agent.search", "op_6")
    await module.commit(reservation_id)

    with pytest.raises(module.ReservationAlreadyFinalized, match="status=committed"):
        await module.commit(reservation_id)


@pytest.mark.asyncio
async def test_topup_adds_balance_and_inserts_ledger(metering):
    module, db = metering
    db.add_merchant("merch_1", balance=5)

    await module.topup(
        merchant_id="merch_1",
        payment_intent_id="pi_topup_1",
        credits_purchased=20,
        topup_type="auto_topup",
    )

    assert db.merchant_credits["merch_1"]["balance"] == 25
    assert db.ledger == [
        {
            "merchant_id": "merch_1",
            "operation_type": "auto_topup",
            "operation_id": None,
            "credits_delta": 20,
            "balance_after": 25,
            "reservation_id": None,
            "source_payment_intent_id": "pi_topup_1",
            "source_type": "topup",
            "metadata": "{}",
        }
    ]
