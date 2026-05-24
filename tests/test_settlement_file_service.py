from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from services import settlement_file_service as service


pytestmark = pytest.mark.asyncio


class _FakeSettlementFileDatabase:
    def __init__(self) -> None:
        self.channel_partners: list[dict[str, Any]] = []
        self.billing_runs: list[dict[str, Any]] = []
        self.settlement_snapshots: list[dict[str, Any]] = []
        self.settlement_files: list[dict[str, Any]] = []
        self._next_partner_id = 1
        self._next_billing_run_id = 1
        self._next_snapshot_id = 1
        self._next_file_id = 1
        self._transaction_depth = 0

    @property
    def in_transaction(self) -> bool:
        return self._transaction_depth > 0

    @asynccontextmanager
    async def transaction(self):
        self._transaction_depth += 1
        try:
            yield
        finally:
            self._transaction_depth -= 1

    async def fetch_all(self, query: str, values: dict[str, Any] | None = None):
        sql = _normalize_sql(query)
        params = dict(values or {})

        if sql.startswith("select ss.id, ss.snapshot_payload_jsonb"):
            partner_id = int(params["channel_partner_id"])
            calendar_month = params["calendar_month"]
            period_end = params["period_end"]
            rows = []
            for snapshot in self.settlement_snapshots:
                if int(snapshot["channel_partner_id"]) != partner_id:
                    continue
                if snapshot.get("settled_at") is not None:
                    continue
                billing_run = self._billing_run_by_id(snapshot["billing_run_id"])
                if not (
                    billing_run["period_start"] <= calendar_month
                    and billing_run["period_end"] >= period_end
                ):
                    continue
                rows.append(
                    {
                        "id": snapshot["id"],
                        "snapshot_payload_jsonb": snapshot["snapshot_payload_jsonb"],
                    }
                )
            return sorted(rows, key=lambda row: row["id"])

        if sql.startswith("select distinct cp.id as channel_partner_id"):
            calendar_month = params["calendar_month"]
            period_end = params["period_end"]
            partner_ids = set()
            for partner in self.channel_partners:
                if partner["status"] != "active":
                    continue
                for snapshot in self.settlement_snapshots:
                    if int(snapshot["channel_partner_id"]) != int(partner["id"]):
                        continue
                    if snapshot.get("settled_at") is not None:
                        continue
                    billing_run = self._billing_run_by_id(snapshot["billing_run_id"])
                    if (
                        billing_run["period_start"] <= calendar_month
                        and billing_run["period_end"] >= period_end
                    ):
                        partner_ids.add(int(partner["id"]))
            return [
                {"channel_partner_id": partner_id}
                for partner_id in sorted(partner_ids)
            ]

        if sql.startswith("select id from settlement_files") and "transfer_status = 'pending'" in sql:
            calendar_month = params["calendar_month"]
            return [
                {"id": row["id"]}
                for row in sorted(self.settlement_files, key=lambda item: item["id"])
                if row["calendar_month"] == calendar_month
                and row["transfer_status"] == "pending"
            ]

        raise AssertionError(f"Unhandled fetch_all query: {query}")

    async def fetch_one(self, query: str, values: dict[str, Any] | None = None):
        sql = _normalize_sql(query)
        params = dict(values or {})

        if sql.startswith("insert into settlement_files"):
            existing = self._file_by_partner_month(
                int(params["channel_partner_id"]),
                params["calendar_month"],
            )
            if existing:
                return None
            row = {
                "id": self._next_file_id,
                "channel_partner_id": int(params["channel_partner_id"]),
                "calendar_month": params["calendar_month"],
                "subscription_share_cents": int(params["subscription_share_cents"]),
                "credit_overage_share_cents": int(params["credit_overage_share_cents"]),
                "gmv_share_cents": int(params["gmv_share_cents"]),
                "clawback_cents": int(params["clawback_cents"]),
                "net_before_carryover_cents": int(
                    params["net_before_carryover_cents"]
                ),
                "carryover_applied_cents": int(params["carryover_applied_cents"]),
                "transfer_amount_cents": int(params["transfer_amount_cents"]),
                "carryover_forward_cents": int(params["carryover_forward_cents"]),
                "source_snapshot_ids_jsonb": json.loads(
                    params["source_snapshot_ids_json"]
                ),
                "transfer_status": params["transfer_status"],
                "stripe_transfer_id": None,
                "stripe_transfer_error": None,
                "transferred_at": None,
                "metadata": json.loads(params["metadata_json"]),
            }
            self._next_file_id += 1
            self.settlement_files.append(row)
            return {"id": row["id"]}

        if sql.startswith("select id from settlement_files where channel_partner_id"):
            row = self._file_by_partner_month(
                int(params["channel_partner_id"]),
                params["calendar_month"],
            )
            return {"id": row["id"]} if row else None

        if sql.startswith("select coalesce(carryover_forward_cents"):
            row = self._file_by_partner_month(
                int(params["channel_partner_id"]),
                params["prior_month"],
            )
            return (
                {"carryover_applied_cents": row["carryover_forward_cents"]}
                if row
                else None
            )

        if sql.startswith("select sf.*, cp.stripe_connect_account_id"):
            file_row = self._file_by_id(int(params["settlement_file_id"]))
            if not file_row:
                return None
            partner = self._partner_by_id(file_row["channel_partner_id"])
            return {
                **file_row,
                "stripe_connect_account_id": partner.get(
                    "stripe_connect_account_id"
                ),
            }

        if sql.startswith("select transfer_status from settlement_files"):
            file_row = self._file_by_id(int(params["settlement_file_id"]))
            return (
                {"transfer_status": file_row["transfer_status"]}
                if file_row
                else None
            )

        if sql.startswith("select source_snapshot_ids_jsonb from settlement_files"):
            file_row = self._file_by_id(int(params["settlement_file_id"]))
            return (
                {"source_snapshot_ids_jsonb": file_row["source_snapshot_ids_jsonb"]}
                if file_row
                else None
            )

        raise AssertionError(f"Unhandled fetch_one query: {query}")

    async def execute(self, query: str, values: dict[str, Any] | None = None):
        sql = _normalize_sql(query)
        params = dict(values or {})

        if sql.startswith("update settlement_files set transfer_status = 'transferring'"):
            file_row = self._file_by_id(int(params["settlement_file_id"]))
            if file_row and file_row["transfer_status"] == "pending":
                file_row["transfer_status"] = "transferring"
                file_row["stripe_transfer_error"] = None
            return None

        if sql.startswith("update settlement_files set transfer_status = 'transferred'"):
            file_row = self._file_by_id(int(params["settlement_file_id"]))
            file_row["transfer_status"] = "transferred"
            file_row["stripe_transfer_id"] = params.get("stripe_transfer_id")
            file_row["stripe_transfer_error"] = None
            file_row["transferred_at"] = file_row["transferred_at"] or _now()
            return None

        if sql.startswith("update settlement_files set transfer_status = 'skipped_negative_net'"):
            file_row = self._file_by_id(int(params["settlement_file_id"]))
            file_row["transfer_status"] = "skipped_negative_net"
            file_row["stripe_transfer_error"] = None
            if "metadata_json" in params:
                file_row["metadata"] = json.loads(params["metadata_json"])
            return None

        if sql.startswith("update settlement_files set transfer_status = 'failed'"):
            file_row = self._file_by_id(int(params["settlement_file_id"]))
            file_row["transfer_status"] = "failed"
            file_row["stripe_transfer_error"] = params["stripe_transfer_error"]
            return None

        if sql.startswith("update settlement_snapshots"):
            snapshot = self._snapshot_by_id(int(params["snapshot_id"]))
            snapshot["settled_at"] = snapshot["settled_at"] or _now()
            snapshot["settled_via_file_id"] = int(params["settlement_file_id"])
            return None

        raise AssertionError(f"Unhandled execute query: {query}")

    def add_partner(
        self,
        *,
        status: str = "active",
        stripe_connect_account_id: str | None = "acct_partner",
    ) -> int:
        partner_id = self._next_partner_id
        self._next_partner_id += 1
        self.channel_partners.append(
            {
                "id": partner_id,
                "status": status,
                "stripe_connect_account_id": stripe_connect_account_id,
            }
        )
        return partner_id

    def add_billing_run(self, period_start: date, period_end: date) -> int:
        run_id = self._next_billing_run_id
        self._next_billing_run_id += 1
        self.billing_runs.append(
            {
                "id": run_id,
                "period_start": period_start,
                "period_end": period_end,
            }
        )
        return run_id

    def add_snapshot(
        self,
        *,
        channel_partner_id: int,
        billing_run_id: int,
        payload: dict[str, Any],
        settled: bool = False,
    ) -> int:
        snapshot_id = self._next_snapshot_id
        self._next_snapshot_id += 1
        self.settlement_snapshots.append(
            {
                "id": snapshot_id,
                "billing_run_id": billing_run_id,
                "channel_partner_id": channel_partner_id,
                "snapshot_payload_jsonb": payload,
                "settled_at": _now() if settled else None,
                "settled_via_file_id": 999 if settled else None,
            }
        )
        return snapshot_id

    def add_file(
        self,
        *,
        channel_partner_id: int,
        calendar_month: date,
        transfer_amount_cents: int,
        carryover_forward_cents: int = 0,
        carryover_applied_cents: int = 0,
        net_before_carryover_cents: int | None = None,
        transfer_status: str = "pending",
        source_snapshot_ids: list[int] | None = None,
    ) -> int:
        file_id = self._next_file_id
        self._next_file_id += 1
        self.settlement_files.append(
            {
                "id": file_id,
                "channel_partner_id": channel_partner_id,
                "calendar_month": calendar_month,
                "subscription_share_cents": 0,
                "credit_overage_share_cents": 0,
                "gmv_share_cents": 0,
                "clawback_cents": 0,
                "net_before_carryover_cents": (
                    transfer_amount_cents
                    if net_before_carryover_cents is None
                    else net_before_carryover_cents
                ),
                "carryover_applied_cents": carryover_applied_cents,
                "transfer_amount_cents": transfer_amount_cents,
                "carryover_forward_cents": carryover_forward_cents,
                "source_snapshot_ids_jsonb": list(source_snapshot_ids or []),
                "transfer_status": transfer_status,
                "stripe_transfer_id": None,
                "stripe_transfer_error": None,
                "transferred_at": None,
                "metadata": {},
            }
        )
        return file_id

    def _partner_by_id(self, partner_id: int) -> dict[str, Any]:
        return next(row for row in self.channel_partners if row["id"] == partner_id)

    def _billing_run_by_id(self, billing_run_id: int) -> dict[str, Any]:
        return next(row for row in self.billing_runs if row["id"] == billing_run_id)

    def _snapshot_by_id(self, snapshot_id: int) -> dict[str, Any]:
        return next(row for row in self.settlement_snapshots if row["id"] == snapshot_id)

    def _file_by_id(self, file_id: int) -> dict[str, Any] | None:
        return next(
            (row for row in self.settlement_files if row["id"] == file_id),
            None,
        )

    def _file_by_partner_month(
        self,
        partner_id: int,
        calendar_month: date,
    ) -> dict[str, Any] | None:
        return next(
            (
                row
                for row in self.settlement_files
                if row["channel_partner_id"] == partner_id
                and row["calendar_month"] == calendar_month
            ),
            None,
        )


@pytest.fixture()
def fake_db(monkeypatch: pytest.MonkeyPatch) -> _FakeSettlementFileDatabase:
    db = _FakeSettlementFileDatabase()
    monkeypatch.setattr(service, "database", db)
    monkeypatch.setattr(service, "IS_POSTGRES", False)
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
    monkeypatch.delenv("SETTLEMENT_TRANSFER_ALLOWED_ON_STAGING", raising=False)
    return db


def _normalize_sql(query: str) -> str:
    return " ".join(str(query).split()).lower()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _month_run(db: _FakeSettlementFileDatabase, month: date) -> int:
    next_month = (
        date(month.year + 1, 1, 1)
        if month.month == 12
        else date(month.year, month.month + 1, 1)
    )
    return db.add_billing_run(month, next_month - timedelta(days=1))


def _payload(
    *,
    subscription_share_cents: int = 0,
    credit_overage_share_cents: int = 0,
    gmv_share_cents: int = 0,
    clawback_cents: int = 0,
) -> dict[str, Any]:
    return {
        "subscription_rev_cents": subscription_share_cents,
        "credit_overage_rev_cents": credit_overage_share_cents,
        "gmv_take_rev_cents": gmv_share_cents,
        "clawbacks": (
            [{"amount_cents": clawback_cents, "reason": "90_day_churn"}]
            if clawback_cents
            else []
        ),
    }


async def test_generate_positive_net_creates_file_with_transfer_amount(
    fake_db: _FakeSettlementFileDatabase,
) -> None:
    partner_id = fake_db.add_partner()
    run_id = _month_run(fake_db, date(2025, 6, 1))
    fake_db.add_snapshot(
        channel_partner_id=partner_id,
        billing_run_id=run_id,
        payload=_payload(subscription_share_cents=1000, gmv_share_cents=300),
    )

    file_id = await service.generate(
        channel_partner_id=partner_id,
        calendar_month=date(2025, 6, 1),
    )

    file_row = fake_db._file_by_id(file_id)
    assert file_row["subscription_share_cents"] == 1000
    assert file_row["gmv_share_cents"] == 300
    assert file_row["clawback_cents"] == 0
    assert file_row["net_before_carryover_cents"] == 1300
    assert file_row["transfer_amount_cents"] == 1300
    assert file_row["carryover_forward_cents"] == 0
    assert file_row["transfer_status"] == "pending"


async def test_generate_negative_net_skips_transfer_and_carries_forward(
    fake_db: _FakeSettlementFileDatabase,
) -> None:
    partner_id = fake_db.add_partner()
    run_id = _month_run(fake_db, date(2025, 7, 1))
    fake_db.add_snapshot(
        channel_partner_id=partner_id,
        billing_run_id=run_id,
        payload=_payload(subscription_share_cents=500, clawback_cents=2000),
    )

    file_id = await service.generate(
        channel_partner_id=partner_id,
        calendar_month=date(2025, 7, 1),
    )

    file_row = fake_db._file_by_id(file_id)
    assert file_row["net_before_carryover_cents"] == -1500
    assert file_row["transfer_amount_cents"] == 0
    assert file_row["carryover_forward_cents"] == -1500
    assert file_row["transfer_status"] == "skipped_negative_net"


async def test_generate_applies_prior_carryover(
    fake_db: _FakeSettlementFileDatabase,
) -> None:
    partner_id = fake_db.add_partner()
    fake_db.add_file(
        channel_partner_id=partner_id,
        calendar_month=date(2025, 7, 1),
        transfer_amount_cents=0,
        carryover_forward_cents=-1500,
        transfer_status="skipped_negative_net",
    )
    run_id = _month_run(fake_db, date(2025, 8, 1))
    fake_db.add_snapshot(
        channel_partner_id=partner_id,
        billing_run_id=run_id,
        payload=_payload(subscription_share_cents=1000),
    )

    file_id = await service.generate(
        channel_partner_id=partner_id,
        calendar_month=date(2025, 8, 1),
    )

    file_row = fake_db._file_by_id(file_id)
    assert file_row["carryover_applied_cents"] == -1500
    assert file_row["net_before_carryover_cents"] == 1000
    assert file_row["transfer_amount_cents"] == 0
    assert file_row["carryover_forward_cents"] == -500


async def test_generate_carryover_resolves_when_positive_exceeds_prior(
    fake_db: _FakeSettlementFileDatabase,
) -> None:
    partner_id = fake_db.add_partner()
    fake_db.add_file(
        channel_partner_id=partner_id,
        calendar_month=date(2025, 8, 1),
        transfer_amount_cents=0,
        carryover_forward_cents=-1500,
        transfer_status="skipped_negative_net",
    )
    run_id = _month_run(fake_db, date(2025, 9, 1))
    fake_db.add_snapshot(
        channel_partner_id=partner_id,
        billing_run_id=run_id,
        payload=_payload(subscription_share_cents=3000),
    )

    file_id = await service.generate(
        channel_partner_id=partner_id,
        calendar_month=date(2025, 9, 1),
    )

    file_row = fake_db._file_by_id(file_id)
    assert file_row["carryover_applied_cents"] == -1500
    assert file_row["transfer_amount_cents"] == 1500
    assert file_row["carryover_forward_cents"] == 0
    assert file_row["transfer_status"] == "pending"


async def test_generate_idempotent_returns_existing_file_id(
    fake_db: _FakeSettlementFileDatabase,
) -> None:
    partner_id = fake_db.add_partner()
    run_id = _month_run(fake_db, date(2025, 6, 1))
    fake_db.add_snapshot(
        channel_partner_id=partner_id,
        billing_run_id=run_id,
        payload=_payload(subscription_share_cents=1000),
    )

    first_file_id = await service.generate(
        channel_partner_id=partner_id,
        calendar_month=date(2025, 6, 1),
    )
    second_file_id = await service.generate(
        channel_partner_id=partner_id,
        calendar_month=date(2025, 6, 1),
    )

    assert second_file_id == first_file_id
    assert len(fake_db.settlement_files) == 1


async def test_generate_only_pulls_unsettled_snapshots(
    fake_db: _FakeSettlementFileDatabase,
) -> None:
    partner_id = fake_db.add_partner()
    run_id = _month_run(fake_db, date(2025, 6, 1))
    fake_db.add_snapshot(
        channel_partner_id=partner_id,
        billing_run_id=run_id,
        payload=_payload(subscription_share_cents=5000),
        settled=True,
    )
    included_snapshot_id = fake_db.add_snapshot(
        channel_partner_id=partner_id,
        billing_run_id=run_id,
        payload=_payload(subscription_share_cents=700),
    )

    file_id = await service.generate(
        channel_partner_id=partner_id,
        calendar_month=date(2025, 6, 1),
    )

    file_row = fake_db._file_by_id(file_id)
    assert file_row["transfer_amount_cents"] == 700
    assert file_row["source_snapshot_ids_jsonb"] == [included_snapshot_id]


async def test_transfer_calls_stripe_with_correct_idempotency_key(
    fake_db: _FakeSettlementFileDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    partner_id = fake_db.add_partner(stripe_connect_account_id="acct_123")
    snapshot_id = fake_db.add_snapshot(
        channel_partner_id=partner_id,
        billing_run_id=_month_run(fake_db, date(2025, 6, 1)),
        payload=_payload(subscription_share_cents=1500),
    )
    file_id = fake_db.add_file(
        channel_partner_id=partner_id,
        calendar_month=date(2025, 6, 1),
        transfer_amount_cents=1500,
        source_snapshot_ids=[snapshot_id],
    )
    calls: list[dict[str, Any]] = []

    def fake_create(**kwargs: Any) -> dict[str, str]:
        assert not fake_db.in_transaction
        calls.append(kwargs)
        return {"id": "tr_123"}

    monkeypatch.setattr(service.stripe.Transfer, "create", fake_create)

    await service.transfer(settlement_file_id=file_id)

    assert calls[0]["amount"] == 1500
    assert calls[0]["currency"] == "usd"
    assert calls[0]["destination"] == "acct_123"
    assert calls[0]["idempotency_key"] == "settlement:partner_1:month_2025-06"
    assert fake_db._file_by_id(file_id)["stripe_transfer_id"] == "tr_123"


async def test_transfer_skips_when_amount_zero_and_carryover_zero(
    fake_db: _FakeSettlementFileDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    partner_id = fake_db.add_partner()
    file_id = fake_db.add_file(
        channel_partner_id=partner_id,
        calendar_month=date(2025, 6, 1),
        transfer_amount_cents=0,
        carryover_forward_cents=0,
    )

    monkeypatch.setattr(
        service.stripe.Transfer,
        "create",
        lambda **_kwargs: pytest.fail("Stripe should not be called"),
    )

    await service.transfer(settlement_file_id=file_id)

    assert fake_db._file_by_id(file_id)["transfer_status"] == "transferred"


async def test_transfer_skips_when_skipped_negative_net(
    fake_db: _FakeSettlementFileDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    partner_id = fake_db.add_partner()
    file_id = fake_db.add_file(
        channel_partner_id=partner_id,
        calendar_month=date(2025, 7, 1),
        transfer_amount_cents=0,
        carryover_forward_cents=-1500,
        transfer_status="skipped_negative_net",
    )
    monkeypatch.setattr(
        service.stripe.Transfer,
        "create",
        lambda **_kwargs: pytest.fail("Stripe should not be called"),
    )

    await service.transfer(settlement_file_id=file_id)

    assert fake_db._file_by_id(file_id)["transfer_status"] == "skipped_negative_net"


async def test_transfer_skipped_on_staging_env(
    fake_db: _FakeSettlementFileDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "staging")
    partner_id = fake_db.add_partner()
    file_id = fake_db.add_file(
        channel_partner_id=partner_id,
        calendar_month=date(2025, 6, 1),
        transfer_amount_cents=1500,
    )
    monkeypatch.setattr(
        service.stripe.Transfer,
        "create",
        lambda **_kwargs: pytest.fail("Stripe should not be called"),
    )

    await service.transfer(settlement_file_id=file_id)

    file_row = fake_db._file_by_id(file_id)
    assert file_row["transfer_status"] == "skipped_negative_net"
    assert file_row["metadata"]["env_gate"] is True
    assert file_row["metadata"]["railway_environment"] == "staging"


async def test_transfer_allowed_on_staging_with_override(
    fake_db: _FakeSettlementFileDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "staging")
    monkeypatch.setenv("SETTLEMENT_TRANSFER_ALLOWED_ON_STAGING", "true")
    partner_id = fake_db.add_partner(stripe_connect_account_id="acct_123")
    file_id = fake_db.add_file(
        channel_partner_id=partner_id,
        calendar_month=date(2025, 6, 1),
        transfer_amount_cents=1500,
    )
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        service.stripe.Transfer,
        "create",
        lambda **kwargs: calls.append(kwargs) or {"id": "tr_staging"},
    )

    await service.transfer(settlement_file_id=file_id)

    assert calls
    assert fake_db._file_by_id(file_id)["transfer_status"] == "transferred"


async def test_transfer_failed_when_no_stripe_connect_account(
    fake_db: _FakeSettlementFileDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    partner_id = fake_db.add_partner(stripe_connect_account_id=None)
    file_id = fake_db.add_file(
        channel_partner_id=partner_id,
        calendar_month=date(2025, 6, 1),
        transfer_amount_cents=1500,
    )
    monkeypatch.setattr(
        service.stripe.Transfer,
        "create",
        lambda **_kwargs: pytest.fail("Stripe should not be called"),
    )

    await service.transfer(settlement_file_id=file_id)

    file_row = fake_db._file_by_id(file_id)
    assert file_row["transfer_status"] == "failed"
    assert file_row["stripe_transfer_error"] == "no_stripe_connect_account"


async def test_transfer_marks_snapshots_settled_on_success(
    fake_db: _FakeSettlementFileDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    partner_id = fake_db.add_partner()
    run_id = _month_run(fake_db, date(2025, 6, 1))
    snapshot_id = fake_db.add_snapshot(
        channel_partner_id=partner_id,
        billing_run_id=run_id,
        payload=_payload(subscription_share_cents=1500),
    )
    file_id = fake_db.add_file(
        channel_partner_id=partner_id,
        calendar_month=date(2025, 6, 1),
        transfer_amount_cents=1500,
        source_snapshot_ids=[snapshot_id],
    )
    monkeypatch.setattr(
        service.stripe.Transfer,
        "create",
        lambda **_kwargs: {"id": "tr_success"},
    )

    await service.transfer(settlement_file_id=file_id)

    snapshot = fake_db._snapshot_by_id(snapshot_id)
    assert snapshot["settled_at"] is not None
    assert snapshot["settled_via_file_id"] == file_id


async def test_transfer_stripe_error_marks_failed_and_no_snapshot_update(
    fake_db: _FakeSettlementFileDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    partner_id = fake_db.add_partner()
    run_id = _month_run(fake_db, date(2025, 6, 1))
    snapshot_id = fake_db.add_snapshot(
        channel_partner_id=partner_id,
        billing_run_id=run_id,
        payload=_payload(subscription_share_cents=1500),
    )
    file_id = fake_db.add_file(
        channel_partner_id=partner_id,
        calendar_month=date(2025, 6, 1),
        transfer_amount_cents=1500,
        source_snapshot_ids=[snapshot_id],
    )

    def raise_stripe_error(**_kwargs: Any) -> None:
        raise RuntimeError("balance insufficient")

    monkeypatch.setattr(service.stripe.Transfer, "create", raise_stripe_error)

    await service.transfer(settlement_file_id=file_id)

    file_row = fake_db._file_by_id(file_id)
    snapshot = fake_db._snapshot_by_id(snapshot_id)
    assert file_row["transfer_status"] == "failed"
    assert "balance insufficient" in file_row["stripe_transfer_error"]
    assert snapshot["settled_at"] is None
    assert snapshot["settled_via_file_id"] is None


async def test_settle_only_trigger_blocks_payload_mutation() -> None:
    migration = (
        Path(__file__).resolve().parents[1]
        / "db"
        / "migrations"
        / "131_settlement_files.sql"
    ).read_text(encoding="utf-8")
    sql = _normalize_sql(migration)
    mutation_block = sql.split("if (", 1)[1].split(") then", 1)[0]

    assert "create or replace function prevent_settlement_snapshot_payload_mutation()" in sql
    assert "new.snapshot_payload_jsonb is distinct from old.snapshot_payload_jsonb" in mutation_block
    assert "new.computed_comp_cents is distinct from old.computed_comp_cents" in mutation_block
    assert "new.settled_at" not in mutation_block
    assert "new.settled_via_file_id" not in mutation_block
    assert "create trigger trg_settlement_snapshots_settle_only before update on settlement_snapshots" in sql
    assert "create trigger trg_settlement_snapshots_no_delete before delete on settlement_snapshots" in sql
    assert "for each row execute procedure prevent_monetization_append_only_mutation()" in sql
