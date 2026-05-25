from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

import pytest

import routes.billing_routes as module


class _FakeBillingMeDatabase:
    def __init__(self) -> None:
        self.subscriptions: list[dict[str, Any]] = []
        self.credit_ledger: list[dict[str, Any]] = []
        self.monthly_brand_statements: list[dict[str, Any]] = []
        self.last_statement_limit: int | None = None

    async def fetch_one(
        self,
        query: str,
        values: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        sql = _normalize_sql(query)
        params = dict(values or {})

        if "from user_subscriptions us" in sql:
            merchant_id = params["merchant_id"]
            period_start = params["period_start"]
            period_end = params["period_end"]
            rows = [
                row
                for row in self.subscriptions
                if row["merchant_id"] == merchant_id
                and row["status"] in {"active", "trialing", "past_due"}
                and (
                    row.get("current_period_start") is None
                    or _as_date(row["current_period_start"]) < period_end
                )
                and (
                    row.get("current_period_end") is None
                    or _as_date(row["current_period_end"]) > period_start
                )
            ]
            rows.sort(
                key=lambda row: (
                    _as_date(row.get("current_period_start") or date.min),
                    int(row["id"]),
                ),
                reverse=True,
            )
            if not rows:
                return None
            row = rows[0]
            return {
                "subscription_plan_id": row["plan_id"],
                "current_period_end": row.get("current_period_end"),
                "tier_name": row["tier_name"],
                "monthly_credit_allowance": row["monthly_credit_allowance"],
                "subscription_revenue_usd_cents": row.get(
                    "subscription_revenue_usd_cents", 0
                ),
            }

        raise AssertionError(f"Unhandled fetch_one query: {query}")

    async def fetch_all(
        self,
        query: str,
        values: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        sql = _normalize_sql(query)
        params = dict(values or {})

        if "from credit_ledger" in sql:
            merchant_id = params["merchant_id"]
            period_start = params["period_start"]
            period_end = params["period_end"]
            return [
                {"id": row["id"], "credits_delta": row["credits_delta"]}
                for row in sorted(self.credit_ledger, key=lambda item: item["id"])
                if row["merchant_id"] == merchant_id
                and row["credits_delta"] < 0
                and period_start <= _as_date(row["occurred_at"]) < period_end
            ]

        if "from monthly_brand_statements" in sql:
            merchant_id = params["merchant_id"]
            self.last_statement_limit = int(params["limit"])
            rows = [
                row
                for row in self.monthly_brand_statements
                if row["merchant_id"] == merchant_id
                and row["status"] in {"frozen", "invoiced"}
            ]
            rows.sort(key=lambda row: row["calendar_month"], reverse=True)
            return rows[: self.last_statement_limit]

        raise AssertionError(f"Unhandled fetch_all query: {query}")

    def add_subscription(
        self,
        *,
        merchant_id: str = "merch_1",
        tier_name: str = "starter",
        allowance_credits: int = 4000,
        current_period_start: date = date(2026, 5, 1),
        current_period_end: date = date(2026, 6, 1),
        status: str = "active",
    ) -> None:
        self.subscriptions.append(
            {
                "id": len(self.subscriptions) + 1,
                "merchant_id": merchant_id,
                "plan_id": len(self.subscriptions) + 100,
                "tier_name": tier_name,
                "monthly_credit_allowance": allowance_credits,
                "current_period_start": current_period_start,
                "current_period_end": current_period_end,
                "status": status,
            }
        )

    def add_credit_use(
        self,
        *,
        merchant_id: str = "merch_1",
        credits: int,
        occurred_at: date = date(2026, 5, 15),
    ) -> None:
        self.credit_ledger.append(
            {
                "id": len(self.credit_ledger) + 1,
                "merchant_id": merchant_id,
                "credits_delta": -int(credits),
                "occurred_at": occurred_at,
            }
        )

    def add_statement(
        self,
        *,
        merchant_id: str = "merch_1",
        calendar_month: date,
        status: str,
        tier_name: str = "starter",
        subscription_revenue_usd_cents: int = 9900,
        overage_credits: int = 0,
        overage_revenue_usd_cents: int = 0,
    ) -> None:
        self.monthly_brand_statements.append(
            {
                "merchant_id": merchant_id,
                "calendar_month": calendar_month,
                "tier_name": tier_name,
                "subscription_revenue_usd_cents": subscription_revenue_usd_cents,
                "overage_credits": overage_credits,
                "overage_revenue_usd_cents": overage_revenue_usd_cents,
                "status": status,
                "frozen_at": datetime(2026, 6, 5, 2, tzinfo=timezone.utc),
                "invoiced_at": (
                    datetime(2026, 6, 5, 2, 1, tzinfo=timezone.utc)
                    if status == "invoiced"
                    else None
                ),
                "total_cogs_usd_cents": 111,
                "pivota_gross_margin_usd_cents": 222,
                "metadata": {"assembly_hash": "internal"},
                "gmv_usd_cents": 333,
                "gmv_personal_usd_cents": 123,
                "gmv_third_party_usd_cents": 210,
                "pivota_gmv_take_usd_cents": 33,
            }
        )


@pytest.fixture()
def fake_db(monkeypatch: pytest.MonkeyPatch) -> _FakeBillingMeDatabase:
    db = _FakeBillingMeDatabase()
    monkeypatch.setattr(module.monthly_brand_statements_service, "database", db)
    monkeypatch.setattr(
        module.monthly_brand_statements_service,
        "_utc_today",
        lambda: date(2026, 5, 25),
    )
    return db


def _build_client(
    *,
    merchant_id: str = "merch_1",
    authenticated: bool = True,
) -> tuple[TestClient, FastAPI]:
    app = FastAPI()
    app.include_router(module.router)
    if authenticated:
        app.dependency_overrides[module.require_approved_merchant] = lambda: {
            "merchant_id": merchant_id,
            "status": "approved",
        }
    return TestClient(app), app


def test_current_period_unauthenticated_returns_401() -> None:
    client, app = _build_client(authenticated=False)
    try:
        response = client.get("/api/billing/me/current-period")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401


def test_current_period_for_merchant_without_subscription_returns_zero_state(
    fake_db: _FakeBillingMeDatabase,
) -> None:
    fake_db.add_credit_use(credits=2500)
    client, app = _build_client()
    try:
        response = client.get("/api/billing/me/current-period")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["tier"] is None
    assert body["allowance_credits"] == 0
    assert body["consumed_credits"] == 0
    assert body["overage_count"] == 0
    assert body["overage_total_usd_cents"] == 0
    assert body["in_overage"] is False


def test_current_period_under_allowance_returns_no_overage(
    fake_db: _FakeBillingMeDatabase,
) -> None:
    fake_db.add_subscription()
    fake_db.add_credit_use(credits=2341)
    client, app = _build_client()
    try:
        response = client.get("/api/billing/me/current-period")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["tier"] == "starter"
    assert body["allowance_credits"] == 4000
    assert body["consumed_credits"] == 2341
    assert body["overage_count"] == 0
    assert body["overage_total_usd_cents"] == 0
    assert body["days_remaining"] == 7
    assert body["period_start"] == "2026-05-01"
    assert body["period_end"] == "2026-06-01"
    assert body["in_overage"] is False


def test_current_period_in_overage_returns_overage_count_and_total(
    fake_db: _FakeBillingMeDatabase,
) -> None:
    fake_db.add_subscription()
    fake_db.add_credit_use(credits=5500)
    client, app = _build_client()
    try:
        response = client.get("/api/billing/me/current-period")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["consumed_credits"] == 5500
    assert body["overage_count"] == 1500
    assert body["overage_total_usd_cents"] == 1950
    assert body["in_overage"] is True


def test_current_period_response_has_no_credit_dollar_value_field(
    fake_db: _FakeBillingMeDatabase,
) -> None:
    fake_db.add_subscription()
    fake_db.add_credit_use(credits=5500)
    client, app = _build_client()
    try:
        response = client.get("/api/billing/me/current-period")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    keys = set(_flatten_keys(response.json()))
    forbidden_exact = {
        "allowance_usd_cents",
        "consumed_usd_cents",
        "credit_value_usd",
        "bundle_value",
    }
    assert not keys.intersection(forbidden_exact)
    for key in keys:
        if key == "overage_total_usd_cents":
            continue
        assert not re.search(r"(allowance|consumed|credit).*usd", key)


def test_statements_returns_recent_frozen_invoiced_rows_only(
    fake_db: _FakeBillingMeDatabase,
) -> None:
    fake_db.add_statement(calendar_month=date(2026, 3, 1), status="frozen")
    fake_db.add_statement(calendar_month=date(2026, 4, 1), status="open")
    fake_db.add_statement(
        calendar_month=date(2026, 5, 1),
        status="invoiced",
        overage_credits=1500,
        overage_revenue_usd_cents=1950,
    )
    client, app = _build_client()
    try:
        response = client.get("/api/billing/me/statements?limit=12")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    statements = response.json()["statements"]
    assert [row["calendar_month"] for row in statements] == [
        "2026-05-01",
        "2026-03-01",
    ]
    assert {row["status"] for row in statements} == {"frozen", "invoiced"}


def test_statements_response_excludes_internal_fields(
    fake_db: _FakeBillingMeDatabase,
) -> None:
    fake_db.add_statement(calendar_month=date(2026, 5, 1), status="invoiced")
    client, app = _build_client()
    try:
        response = client.get("/api/billing/me/statements")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    statement = response.json()["statements"][0]
    excluded = {
        "total_cogs_usd_cents",
        "pivota_gross_margin_usd_cents",
        "metadata",
        "gmv_usd_cents",
        "gmv_personal_usd_cents",
        "gmv_third_party_usd_cents",
        "pivota_gmv_take_usd_cents",
        "bundled_credits_consumed",
    }
    assert not excluded.intersection(statement)


def test_statements_limit_capped_at_36(fake_db: _FakeBillingMeDatabase) -> None:
    for index in range(40):
        fake_db.add_statement(
            calendar_month=date(2026, 1, 1).replace(month=(index % 12) + 1),
            status="frozen",
        )
    client, app = _build_client()
    try:
        response = client.get("/api/billing/me/statements?limit=100")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert fake_db.last_statement_limit == 36
    assert len(response.json()["statements"]) == 36


def _normalize_sql(query: str) -> str:
    return " ".join(str(query).split()).lower()


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _flatten_keys(value: Any) -> list[str]:
    if isinstance(value, dict):
        keys: list[str] = []
        for key, child in value.items():
            keys.append(str(key))
            keys.extend(_flatten_keys(child))
        return keys
    if isinstance(value, list):
        keys = []
        for item in value:
            keys.extend(_flatten_keys(item))
        return keys
    return []
