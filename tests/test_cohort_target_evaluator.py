from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import pytest

from services import cohort_target_evaluator as service


pytestmark = pytest.mark.asyncio


class _FakeCohortDatabase:
    def __init__(self) -> None:
        self.partner_cohort_targets: list[dict[str, Any]] = []
        self.partner_attribution: list[dict[str, Any]] = []
        self.update_count = 0
        self._next_target_id = 1

    async def fetch_all(
        self,
        query: str,
        values: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        sql = _normalize_sql(query)
        params = dict(values or {})

        if "select distinct channel_partner_id" in sql:
            partner_ids = sorted(
                {
                    int(row["channel_partner_id"])
                    for row in self.partner_cohort_targets
                    if row["status"] == "open"
                }
            )
            return [{"channel_partner_id": partner_id} for partner_id in partner_ids]

        if "from partner_cohort_targets" in sql:
            partner_id = int(params["channel_partner_id"])
            rows = [
                row
                for row in self.partner_cohort_targets
                if int(row["channel_partner_id"]) == partner_id
            ]
            return sorted(rows, key=lambda row: (row["window_start_date"], row["id"]))

        raise AssertionError(f"Unhandled fetch_all query: {query}")

    async def fetch_one(
        self,
        query: str,
        values: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        sql = _normalize_sql(query)
        params = dict(values or {})

        if "count(distinct pa.merchant_id)" in sql:
            partner_id = int(params["partner_id"])
            window_start = params["window_start"]
            window_end = params["window_end"]
            merchant_ids = {
                row["merchant_id"]
                for row in self.partner_attribution
                if int(row["channel_partner_id"]) == partner_id
                and row.get("activated_at") is not None
                and window_start <= _as_date(row["activated_at"]) <= window_end
            }
            return {"current_count": len(merchant_ids)}

        raise AssertionError(f"Unhandled fetch_one query: {query}")

    async def execute(
        self,
        query: str,
        values: dict[str, Any] | None = None,
    ) -> None:
        sql = _normalize_sql(query)
        params = dict(values or {})

        if sql.startswith("update partner_cohort_targets"):
            target = next(
                row
                for row in self.partner_cohort_targets
                if int(row["id"]) == int(params["target_id"])
            )
            if target["status"] != "open":
                return None
            if "status = 'achieved'" in sql:
                target["status"] = "achieved"
                target["achieved_at"] = target.get("achieved_at") or datetime.now(
                    timezone.utc
                )
            elif "status = 'expired'" in sql:
                target["status"] = "expired"
            self.update_count += 1
            return None

        raise AssertionError(f"Unhandled execute query: {query}")

    def add_target(
        self,
        *,
        channel_partner_id: int = 1,
        label: str = "20 brands in 12 months",
        target_brand_count: int = 2,
        window_months: int = 12,
        window_start_date: date = date(2025, 1, 1),
        bonus_cents: int = 0,
        status: str = "open",
        achieved_at: datetime | None = None,
        paid_at: datetime | None = None,
    ) -> int:
        target_id = self._next_target_id
        self._next_target_id += 1
        self.partner_cohort_targets.append(
            {
                "id": target_id,
                "channel_partner_id": channel_partner_id,
                "label": label,
                "target_brand_count": target_brand_count,
                "window_months": window_months,
                "window_start_date": window_start_date,
                "bonus_cents": bonus_cents,
                "status": status,
                "achieved_at": achieved_at,
                "paid_at": paid_at,
            }
        )
        return target_id

    def add_attribution(
        self,
        *,
        merchant_id: str,
        channel_partner_id: int = 1,
        activated_at: date | datetime | None,
    ) -> None:
        self.partner_attribution.append(
            {
                "merchant_id": merchant_id,
                "channel_partner_id": channel_partner_id,
                "activated_at": activated_at,
            }
        )


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch) -> _FakeCohortDatabase:
    db = _FakeCohortDatabase()
    monkeypatch.setattr(service, "database", db)
    monkeypatch.setattr(service, "_today", lambda: date(2025, 6, 1))
    return db


async def test_open_target_with_zero_activations_stays_open(
    fake_db: _FakeCohortDatabase,
) -> None:
    fake_db.add_target()

    results = await service.evaluate_partner_targets(1)

    assert results[0]["current_count"] == 0
    assert results[0]["status_after"] == "open"
    assert fake_db.partner_cohort_targets[0]["status"] == "open"
    assert fake_db.update_count == 0


async def test_open_target_reaches_count_transitions_to_achieved(
    fake_db: _FakeCohortDatabase,
) -> None:
    fake_db.add_target(target_brand_count=2)
    fake_db.add_attribution(merchant_id="brand_A", activated_at=date(2025, 3, 1))
    fake_db.add_attribution(merchant_id="brand_B", activated_at=date(2025, 5, 1))

    results = await service.evaluate_partner_targets(1)

    assert results[0]["current_count"] == 2
    assert results[0]["status_before"] == "open"
    assert results[0]["status_after"] == "achieved"
    assert results[0]["achieved_at_set"] is True
    assert fake_db.partner_cohort_targets[0]["status"] == "achieved"
    assert fake_db.partner_cohort_targets[0]["achieved_at"] is not None


async def test_brand_activated_outside_window_doesnt_count(
    fake_db: _FakeCohortDatabase,
) -> None:
    fake_db.add_target(target_brand_count=1)
    fake_db.add_attribution(merchant_id="brand_before", activated_at=date(2024, 12, 31))
    fake_db.add_attribution(merchant_id="brand_after", activated_at=date(2026, 1, 2))

    results = await service.evaluate_partner_targets(1)

    assert results[0]["current_count"] == 0
    assert results[0]["status_after"] == "open"


async def test_brand_with_null_activated_at_doesnt_count(
    fake_db: _FakeCohortDatabase,
) -> None:
    fake_db.add_target(target_brand_count=1)
    fake_db.add_attribution(merchant_id="brand_A", activated_at=None)

    results = await service.evaluate_partner_targets(1)

    assert results[0]["current_count"] == 0
    assert results[0]["status_after"] == "open"


async def test_window_expired_transitions_to_expired(
    fake_db: _FakeCohortDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service, "_today", lambda: date(2026, 2, 2))
    fake_db.add_target(target_brand_count=1)

    results = await service.evaluate_partner_targets(1)

    assert results[0]["window_end"] == date(2026, 1, 1)
    assert results[0]["window_open"] is False
    assert results[0]["status_after"] == "expired"
    assert fake_db.partner_cohort_targets[0]["status"] == "expired"


async def test_already_achieved_target_is_not_re_evaluated(
    fake_db: _FakeCohortDatabase,
) -> None:
    achieved_at = datetime(2025, 4, 1, tzinfo=timezone.utc)
    fake_db.add_target(status="achieved", achieved_at=achieved_at)
    fake_db.add_attribution(merchant_id="brand_A", activated_at=date(2025, 3, 1))
    fake_db.add_attribution(merchant_id="brand_B", activated_at=date(2025, 5, 1))

    results = await service.evaluate_partner_targets(1)

    assert results[0]["status_before"] == "achieved"
    assert results[0]["status_after"] == "achieved"
    assert results[0]["achieved_at_set"] is False
    assert fake_db.partner_cohort_targets[0]["achieved_at"] == achieved_at
    assert fake_db.update_count == 0


async def test_already_paid_target_is_not_re_evaluated(
    fake_db: _FakeCohortDatabase,
) -> None:
    paid_at = datetime(2025, 5, 1, tzinfo=timezone.utc)
    fake_db.add_target(status="paid", paid_at=paid_at)
    fake_db.add_attribution(merchant_id="brand_A", activated_at=date(2025, 3, 1))
    fake_db.add_attribution(merchant_id="brand_B", activated_at=date(2025, 5, 1))

    results = await service.evaluate_partner_targets(1)

    assert results[0]["status_before"] == "paid"
    assert results[0]["status_after"] == "paid"
    assert fake_db.partner_cohort_targets[0]["paid_at"] == paid_at
    assert fake_db.update_count == 0


async def test_paid_at_remains_null_after_achievement(
    fake_db: _FakeCohortDatabase,
) -> None:
    fake_db.add_target(target_brand_count=1, paid_at=None)
    fake_db.add_attribution(merchant_id="brand_A", activated_at=date(2025, 3, 1))

    await service.evaluate_partner_targets(1)

    assert fake_db.partner_cohort_targets[0]["status"] == "achieved"
    assert fake_db.partner_cohort_targets[0]["paid_at"] is None


def _normalize_sql(query: str) -> str:
    return " ".join(str(query).lower().split())


def _as_date(value: date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    return value
