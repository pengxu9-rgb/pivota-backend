from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import pytest

from services import subsidy_service as service


pytestmark = pytest.mark.asyncio


class _FakeSubsidyDatabase:
    def __init__(self) -> None:
        self.channel_partners: dict[int, dict[str, Any]] = {}
        self.partner_subsidy_ledger: list[dict[str, Any]] = []
        self._next_partner_id = 1
        self._next_ledger_id = 1
        self._locks: dict[int, asyncio.Lock] = {}
        self._held_locks: dict[asyncio.Task[Any], list[int]] = {}
        self.advisory_lock_calls: list[int] = []

    @asynccontextmanager
    async def transaction(self):
        try:
            yield
        finally:
            task = asyncio.current_task()
            if task is not None:
                held = self._held_locks.pop(task, [])
                for key in reversed(held):
                    self._locks[key].release()

    async def execute(
        self,
        query: str,
        values: dict[str, Any] | None = None,
    ) -> None:
        sql = _normalize_sql(query)
        params = dict(values or {})

        if sql.startswith("select pg_advisory_xact_lock"):
            key = int(params["k"])
            lock = self._locks.setdefault(key, asyncio.Lock())
            await lock.acquire()
            task = asyncio.current_task()
            if task is not None:
                self._held_locks.setdefault(task, []).append(key)
            self.advisory_lock_calls.append(key)
            return None

        raise AssertionError(f"Unhandled execute query: {query}")

    async def fetch_one(
        self,
        query: str,
        values: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        sql = _normalize_sql(query)
        params = dict(values or {})

        if "from channel_partners" in sql and "for update" in sql:
            partner = self.channel_partners.get(int(params["id"]))
            if partner is None:
                return None
            return {
                "per_brand_subsidy_cap_cents": partner.get(
                    "per_brand_subsidy_cap_cents"
                )
            }

        if "from partner_subsidy_ledger" in sql and "sum(amount_cents)" in sql:
            total = sum(
                int(row["amount_cents"])
                for row in self.partner_subsidy_ledger
                if int(row["channel_partner_id"])
                == int(params["channel_partner_id"])
                and row["merchant_id"] == params["merchant_id"]
            )
            return {"total": total}

        if sql.startswith("insert into partner_subsidy_ledger"):
            row = {
                "id": self._next_ledger_id,
                "channel_partner_id": int(params["channel_partner_id"]),
                "merchant_id": params["merchant_id"],
                "kind": params["kind"],
                "amount_cents": int(params["amount_cents"]),
                "reference_id": params.get("reference_id"),
                "notes": params.get("notes"),
                "issued_by": params.get("issued_by"),
                "metadata": json.loads(params["metadata_json"]),
                "issued_at": datetime.now(timezone.utc),
            }
            self._next_ledger_id += 1
            self.partner_subsidy_ledger.append(row)
            return {"id": row["id"]}

        raise AssertionError(f"Unhandled fetch_one query: {query}")

    def add_partner(self, *, cap_cents: int | None = 500000) -> int:
        partner_id = self._next_partner_id
        self._next_partner_id += 1
        self.channel_partners[partner_id] = {
            "id": partner_id,
            "per_brand_subsidy_cap_cents": cap_cents,
        }
        return partner_id


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch) -> _FakeSubsidyDatabase:
    db = _FakeSubsidyDatabase()
    monkeypatch.setattr(service, "database", db)
    return db


async def test_issue_under_cap_succeeds(fake_db: _FakeSubsidyDatabase) -> None:
    partner_id = fake_db.add_partner()

    row_id = await service.issue(
        channel_partner_id=partner_id,
        merchant_id="brand_A",
        kind="waived_setup_fee",
        amount_cents=100000,
    )

    assert row_id == 1
    assert await service.per_brand_subsidy_total_cents(
        channel_partner_id=partner_id,
        merchant_id="brand_A",
    ) == 100000


async def test_issue_at_exactly_cap_succeeds(
    fake_db: _FakeSubsidyDatabase,
) -> None:
    partner_id = fake_db.add_partner()

    row_id = await service.issue(
        channel_partner_id=partner_id,
        merchant_id="brand_A",
        kind="complimentary_credits",
        amount_cents=500000,
    )

    assert row_id == 1


async def test_issue_over_cap_raises(fake_db: _FakeSubsidyDatabase) -> None:
    partner_id = fake_db.add_partner()

    with pytest.raises(service.SubsidyCapExceeded) as exc_info:
        await service.issue(
            channel_partner_id=partner_id,
            merchant_id="brand_A",
            kind="discounted_subscription",
            amount_cents=550000,
        )

    assert exc_info.value.cap_cents == 500000
    assert exc_info.value.already_issued_cents == 0
    assert exc_info.value.requested_cents == 550000


async def test_cumulative_issues_respect_cap(
    fake_db: _FakeSubsidyDatabase,
) -> None:
    partner_id = fake_db.add_partner()

    await service.issue(
        channel_partner_id=partner_id,
        merchant_id="brand_A",
        kind="waived_setup_fee",
        amount_cents=450000,
    )
    await service.issue(
        channel_partner_id=partner_id,
        merchant_id="brand_A",
        kind="complimentary_credits",
        amount_cents=50000,
    )

    with pytest.raises(service.SubsidyCapExceeded):
        await service.issue(
            channel_partner_id=partner_id,
            merchant_id="brand_A",
            kind="discounted_subscription",
            amount_cents=1,
        )


async def test_per_brand_cap_isolated(fake_db: _FakeSubsidyDatabase) -> None:
    partner_id = fake_db.add_partner()

    await service.issue(
        channel_partner_id=partner_id,
        merchant_id="brand_A",
        kind="co_funded_marketing",
        amount_cents=450000,
    )
    await service.issue(
        channel_partner_id=partner_id,
        merchant_id="brand_B",
        kind="co_funded_marketing",
        amount_cents=450000,
    )

    assert len(fake_db.partner_subsidy_ledger) == 2


async def test_null_cap_allows_any_positive_amount(
    fake_db: _FakeSubsidyDatabase,
) -> None:
    partner_id = fake_db.add_partner(cap_cents=None)

    row_id = await service.issue(
        channel_partner_id=partner_id,
        merchant_id="brand_A",
        kind="waived_setup_fee",
        amount_cents=50_000_000,
    )

    assert row_id == 1


async def test_invalid_kind_raises(fake_db: _FakeSubsidyDatabase) -> None:
    partner_id = fake_db.add_partner()

    with pytest.raises(service.SubsidyKindInvalid):
        await service.issue(
            channel_partner_id=partner_id,
            merchant_id="brand_A",
            kind="gift_card",
            amount_cents=1000,
        )


async def test_negative_amount_raises(fake_db: _FakeSubsidyDatabase) -> None:
    partner_id = fake_db.add_partner()

    with pytest.raises(ValueError):
        await service.issue(
            channel_partner_id=partner_id,
            merchant_id="brand_A",
            kind="waived_setup_fee",
            amount_cents=-100,
        )


async def test_zero_amount_raises(fake_db: _FakeSubsidyDatabase) -> None:
    partner_id = fake_db.add_partner()

    with pytest.raises(ValueError):
        await service.issue(
            channel_partner_id=partner_id,
            merchant_id="brand_A",
            kind="waived_setup_fee",
            amount_cents=0,
        )


async def test_unknown_partner_raises(fake_db: _FakeSubsidyDatabase) -> None:
    with pytest.raises(ValueError):
        await service.issue(
            channel_partner_id=999999,
            merchant_id="brand_A",
            kind="waived_setup_fee",
            amount_cents=1000,
        )


async def test_concurrent_issues_under_cap_serialize(
    fake_db: _FakeSubsidyDatabase,
) -> None:
    partner_id = fake_db.add_partner()

    results = await asyncio.gather(
        service.issue(
            channel_partner_id=partner_id,
            merchant_id="brand_A",
            kind="waived_setup_fee",
            amount_cents=300000,
        ),
        service.issue(
            channel_partner_id=partner_id,
            merchant_id="brand_A",
            kind="complimentary_credits",
            amount_cents=300000,
        ),
        return_exceptions=True,
    )

    successes = [result for result in results if isinstance(result, int)]
    failures = [
        result
        for result in results
        if isinstance(result, service.SubsidyCapExceeded)
    ]
    assert len(successes) == 1
    assert len(failures) == 1
    assert len(fake_db.partner_subsidy_ledger) == 1
    assert len(fake_db.advisory_lock_calls) == 2


def _normalize_sql(query: str) -> str:
    return " ".join(str(query).lower().split())
