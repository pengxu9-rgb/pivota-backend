from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

import pytest

import routes.admin_partners as module


class _FakeAdminPartnersDatabase:
    def __init__(self) -> None:
        self.channel_partners: list[dict[str, Any]] = []
        self.partner_attribution: list[dict[str, Any]] = []
        self.monthly_brand_statements: list[dict[str, Any]] = []
        self.partner_subsidy_ledger: dict[int, dict[str, Any]] = {}
        self.settlement_files: list[dict[str, Any]] = []
        self._transaction_depth = 0

    @asynccontextmanager
    async def transaction(self):
        self._transaction_depth += 1
        try:
            yield
        finally:
            self._transaction_depth -= 1

    async def fetch_all(
        self,
        query: str,
        values: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        sql = _normalize_sql(query)
        if "from channel_partners cp" in sql:
            rows = []
            for partner in sorted(
                self.channel_partners,
                key=lambda row: int(row["id"]),
            ):
                partner_id = int(partner["id"])
                active_brand_count = len(
                    {
                        row["merchant_id"]
                        for row in self.partner_attribution
                        if int(row["channel_partner_id"]) == partner_id
                        and row["status"] in {"signed", "active"}
                        and row.get("activated_at") is not None
                    }
                )
                ytd_gmv_raw_cents = 0
                merchant_ids = {
                    row["merchant_id"]
                    for row in self.partner_attribution
                    if int(row["channel_partner_id"]) == partner_id
                }
                for statement in self.monthly_brand_statements:
                    if statement["merchant_id"] not in merchant_ids:
                        continue
                    if statement["status"] not in {"frozen", "invoiced"}:
                        continue
                    if statement["calendar_month"] < date(2026, 1, 1):
                        continue
                    ytd_gmv_raw_cents += int(statement["gmv_usd_cents"])
                rows.append(
                    {
                        **partner,
                        "active_brand_count": active_brand_count,
                        "ytd_gmv_cents": ytd_gmv_raw_cents,
                    }
                )
            return rows

        raise AssertionError(f"Unhandled fetch_all query: {query}")

    async def fetch_one(
        self,
        query: str,
        values: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        sql = _normalize_sql(query)
        params = dict(values or {})

        if "from partner_subsidy_ledger" in sql:
            return self.partner_subsidy_ledger.get(int(params["ledger_id"]))

        if (
            "from settlement_files" in sql
            and "for update" in sql
            and "channel_partner_id = :channel_partner_id" in sql
        ):
            return self._file_by_id_and_partner(
                int(params["file_id"]),
                int(params["channel_partner_id"]),
            )

        if sql.startswith("select sf.*, cp.stripe_connect_account_id"):
            file_row = self._file_by_id(int(params["settlement_file_id"]))
            if not file_row:
                return None
            partner = self._partner_by_id(int(file_row["channel_partner_id"]))
            return {
                **file_row,
                "stripe_connect_account_id": partner.get(
                    "stripe_connect_account_id"
                ),
            }

        if sql.startswith("select source_snapshot_ids_jsonb from settlement_files"):
            file_row = self._file_by_id(int(params["settlement_file_id"]))
            return (
                {"source_snapshot_ids_jsonb": file_row["source_snapshot_ids_jsonb"]}
                if file_row
                else None
            )

        if (
            "from settlement_files" in sql
            and "stripe_transfer_id" in sql
            and "limit 1" in sql
        ):
            return self._file_by_id_and_partner(
                int(params["file_id"]),
                int(params["channel_partner_id"]),
            )

        raise AssertionError(f"Unhandled fetch_one query: {query}")

    async def execute(
        self,
        query: str,
        values: dict[str, Any] | None = None,
    ) -> None:
        sql = _normalize_sql(query)
        params = dict(values or {})

        if sql.startswith("update settlement_files set transfer_status = 'pending'"):
            file_row = self._file_by_id(int(params["file_id"]))
            if file_row and file_row["transfer_status"] == "failed":
                file_row["transfer_status"] = "pending"
                file_row["stripe_transfer_error"] = None
            return None

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

        if sql.startswith("update settlement_files set transfer_status = 'failed'"):
            file_row = self._file_by_id(int(params["settlement_file_id"]))
            file_row["transfer_status"] = "failed"
            file_row["stripe_transfer_error"] = params["stripe_transfer_error"]
            return None

        raise AssertionError(f"Unhandled execute query: {query}")

    def add_partner(
        self,
        *,
        partner_id: int = 19,
        legal_name: str = "Markato Limited",
        archetype: str = "curated_marketplace",
        status: str = "active",
        term_start_date: date = date(2026, 5, 24),
        stripe_connect_account_id: str | None = "acct_1TaZ8",
    ) -> None:
        self.channel_partners.append(
            {
                "id": partner_id,
                "legal_name": legal_name,
                "archetype": archetype,
                "status": status,
                "term_start_date": term_start_date,
                "stripe_connect_account_id": stripe_connect_account_id,
            }
        )

    def add_file(
        self,
        *,
        file_id: int = 7,
        channel_partner_id: int = 19,
        transfer_status: str = "failed",
        transfer_amount_cents: int = 1104500,
    ) -> None:
        self.settlement_files.append(
            {
                "id": file_id,
                "channel_partner_id": channel_partner_id,
                "calendar_month": date(2026, 5, 1),
                "transfer_amount_cents": transfer_amount_cents,
                "carryover_forward_cents": 0,
                "source_snapshot_ids_jsonb": [],
                "transfer_status": transfer_status,
                "stripe_transfer_id": None,
                "stripe_transfer_error": (
                    "stripe outage" if transfer_status == "failed" else None
                ),
                "transferred_at": None,
            }
        )

    def _partner_by_id(self, partner_id: int) -> dict[str, Any]:
        return next(row for row in self.channel_partners if int(row["id"]) == partner_id)

    def _file_by_id(self, file_id: int) -> dict[str, Any] | None:
        return next(
            (row for row in self.settlement_files if int(row["id"]) == file_id),
            None,
        )

    def _file_by_id_and_partner(
        self,
        file_id: int,
        channel_partner_id: int,
    ) -> dict[str, Any] | None:
        row = self._file_by_id(file_id)
        if not row or int(row["channel_partner_id"]) != channel_partner_id:
            return None
        return row


@pytest.fixture()
def fake_db(monkeypatch: pytest.MonkeyPatch) -> _FakeAdminPartnersDatabase:
    db = _FakeAdminPartnersDatabase()
    monkeypatch.setattr(module, "database", db)
    monkeypatch.setattr(module.settlement_file_service, "database", db)
    monkeypatch.setattr(module.settlement_file_service, "IS_POSTGRES", False)
    return db


def _build_client(*, authenticated: bool = True) -> tuple[TestClient, FastAPI]:
    app = FastAPI()
    app.include_router(module.router)
    if authenticated:
        app.dependency_overrides[module.require_admin] = lambda: {
            "email": "admin@example.com",
            "role": "admin",
        }
    return TestClient(app), app


def test_partners_list_requires_admin() -> None:
    client, app = _build_client(authenticated=False)
    try:
        response = client.get("/admin/partners")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code in {401, 403}


def test_partners_list_returns_partner_with_cohort_progress(
    fake_db: _FakeAdminPartnersDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_db.add_partner()
    for index in range(12):
        fake_db.partner_attribution.append(
            {
                "merchant_id": f"merch_{index}",
                "channel_partner_id": 19,
                "status": "active",
                "activated_at": datetime(2026, 5, 1, tzinfo=timezone.utc),
            }
        )
        fake_db.monthly_brand_statements.append(
            {
                "merchant_id": f"merch_{index}",
                "calendar_month": date(2026, 5, 1),
                "gmv_usd_cents": 2_850_000,
                "status": "frozen",
            }
        )
    fake_db.monthly_brand_statements.append(
        {
            "merchant_id": "merch_0",
            "calendar_month": date(2026, 4, 1),
            "gmv_usd_cents": 1_000_000,
            "status": "open",
        }
    )

    async def fake_progress(channel_partner_id: int) -> list[dict[str, Any]]:
        assert channel_partner_id == 19
        return [
            {
                "id": 1,
                "target_brand_count": 20,
                "current_count": 12,
                "status": "open",
            }
        ]

    monkeypatch.setattr(
        module.cohort_target_evaluator,
        "get_partner_target_progress",
        fake_progress,
    )
    client, app = _build_client()
    try:
        response = client.get("/admin/partners")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    partner = response.json()["partners"][0]
    assert partner["id"] == 19
    assert partner["legal_name"] == "Markato Limited"
    assert partner["active_brand_count"] == 12
    assert partner["ytd_gmv_cents"] == 34_200_000
    assert partner["stripe_connect_account_id"] == "acct_1TaZ8"
    assert partner["cohort_progress"] == {
        "target_id": 1,
        "target_brand_count": 20,
        "current_count": 12,
    }


def test_subsidy_issue_under_cap_returns_201_with_ledger_id(
    fake_db: _FakeAdminPartnersDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issued_at = datetime(2026, 5, 25, 12, tzinfo=timezone.utc)
    fake_db.partner_subsidy_ledger[123] = {"issued_at": issued_at}
    captured: dict[str, Any] = {}

    async def fake_issue(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 123

    monkeypatch.setattr(module.subsidy_service, "issue", fake_issue)
    client, app = _build_client()
    try:
        response = client.post(
            "/admin/partners/19/subsidies",
            json={
                "merchant_id": "merch_xyz",
                "kind": "waived_setup_fee",
                "amount_cents": 200000,
                "reference_id": "INV-001",
                "notes": "launch support",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201
    assert response.json()["ledger_id"] == 123
    assert response.json()["issued_at"].startswith("2026-05-25T12:00:00")
    assert captured["channel_partner_id"] == 19
    assert captured["merchant_id"] == "merch_xyz"
    assert captured["amount_cents"] == 200000
    assert captured["issued_by"] == "admin@example.com"


def test_subsidy_issue_invalid_kind_returns_400_with_allowed_values(
    fake_db: _FakeAdminPartnersDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_issue(**kwargs: Any) -> int:
        raise module.subsidy_service.SubsidyKindInvalid("invalid kind")

    monkeypatch.setattr(module.subsidy_service, "issue", fake_issue)
    client, app = _build_client()
    try:
        response = client.post(
            "/admin/partners/19/subsidies",
            json={
                "merchant_id": "merch_xyz",
                "kind": "gift_card",
                "amount_cents": 1000,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_subsidy_kind"
    assert "waived_setup_fee" in body["allowed_values"]


def test_subsidy_issue_over_cap_returns_409_with_cap_details(
    fake_db: _FakeAdminPartnersDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_issue(**kwargs: Any) -> int:
        raise module.subsidy_service.SubsidyCapExceeded(
            cap_cents=500000,
            already_issued_cents=450000,
            requested_cents=100000,
        )

    monkeypatch.setattr(module.subsidy_service, "issue", fake_issue)
    client, app = _build_client()
    try:
        response = client.post(
            "/admin/partners/19/subsidies",
            json={
                "merchant_id": "merch_xyz",
                "kind": "discounted_subscription",
                "amount_cents": 100000,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "subsidy_cap_exceeded"
    assert body["cap_cents"] == 500000
    assert body["already_issued_cents"] == 450000
    assert body["requested_cents"] == 100000
    assert body["available_cents"] == 50000


def test_settlement_retry_requires_admin() -> None:
    client, app = _build_client(authenticated=False)
    try:
        response = client.post("/admin/partners/19/settlements/7/retry")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code in {401, 403}


def test_settlement_retry_404_when_file_not_belonging_to_partner(
    fake_db: _FakeAdminPartnersDatabase,
) -> None:
    fake_db.add_partner(partner_id=19)
    fake_db.add_partner(partner_id=20)
    fake_db.add_file(file_id=7, channel_partner_id=20, transfer_status="failed")
    client, app = _build_client()
    try:
        response = client.post("/admin/partners/19/settlements/7/retry")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404


def test_settlement_retry_409_when_file_not_failed(
    fake_db: _FakeAdminPartnersDatabase,
) -> None:
    fake_db.add_partner()
    fake_db.add_file(file_id=7, channel_partner_id=19, transfer_status="pending")
    client, app = _build_client()
    try:
        response = client.post("/admin/partners/19/settlements/7/retry")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "settlement_transfer_not_retryable"
    assert body["transfer_status"] == "pending"


def test_settlement_retry_resets_status_and_calls_transfer(
    fake_db: _FakeAdminPartnersDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_db.add_partner()
    fake_db.add_file(file_id=7, channel_partner_id=19, transfer_status="failed")
    transfer_calls: list[dict[str, Any]] = []
    monkeypatch.setenv("SETTLEMENT_TRANSFER_ALLOWED_ON_STAGING", "true")
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)

    def fake_stripe_transfer_create(**kwargs: Any) -> SimpleNamespace:
        transfer_calls.append(kwargs)
        return SimpleNamespace(id="tr_retry_123")

    monkeypatch.setattr(
        module.settlement_file_service.stripe.Transfer,
        "create",
        fake_stripe_transfer_create,
        raising=False,
    )
    client, app = _build_client()
    try:
        response = client.post("/admin/partners/19/settlements/7/retry")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["file_id"] == 7
    assert body["transfer_status"] == "transferred"
    assert body["stripe_transfer_id"] == "tr_retry_123"
    assert body["stripe_transfer_error"] is None
    assert transfer_calls[0]["amount"] == 1104500
    assert fake_db._file_by_id(7)["transfer_status"] == "transferred"


def _normalize_sql(query: str) -> str:
    return " ".join(str(query).split()).lower()


def _now() -> datetime:
    return datetime.now(timezone.utc)
