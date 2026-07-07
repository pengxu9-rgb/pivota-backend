from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

import pytest

import routes.admin_partners as module


_TODAY = date(2026, 7, 7)


class _FakeCreateDatabase:
    def __init__(self) -> None:
        self.channel_partners: list[dict[str, Any]] = []
        self.partner_rate_schedules: list[dict[str, Any]] = []
        self._next_id = 1

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetch_one(self, query: str, values: dict[str, Any] | None = None):
        sql = _normalize_sql(query)
        params = dict(values or {})

        if "lower(legal_name) = lower(" in sql:
            return next(
                (
                    {"id": p["id"]}
                    for p in self.channel_partners
                    if p["legal_name"].lower() == str(params["legal_name"]).lower()
                    and p["status"] != "inactive"
                ),
                None,
            )

        if sql.startswith("insert into channel_partners"):
            partner_id = self._next_id
            self._next_id += 1
            row = {
                "id": partner_id,
                "legal_name": params["legal_name"],
                "contact_email": params["contact_email"],
                "archetype": params["archetype"],
                "status": params["status"],
                "term_start_date": params["term_start_date"] or _TODAY,
                "term_months": params["term_months"],
                "auto_renew": params["term_auto_renew"],
                "per_brand_tail_months": params["per_brand_tail_months"],
                "churn_clawback_days": params["churn_clawback_days"],
                "nonpayment_clawback_days": params["nonpayment_clawback_days"],
                "per_brand_subsidy_cap_cents": params["per_brand_subsidy_cap_cents"],
                "gmv_take_rate_bp": params["gmv_take_rate_bp"],
                "active_rate_scope": params["active_rate_scope"],
                "gmv_take_definition": params["gmv_take_definition"],
                "stripe_connect_account_id": None,
            }
            self.channel_partners.append(row)
            return {"id": partner_id, "term_start_date": row["term_start_date"]}

        if sql.startswith("insert into partner_rate_schedules"):
            self.partner_rate_schedules.append(dict(params))
            return {"id": len(self.partner_rate_schedules)}

        if "from channel_partners cp" in sql:
            row = next(
                (
                    p
                    for p in self.channel_partners
                    if int(p["id"]) == int(params["channel_partner_id"])
                ),
                None,
            )
            if row is None:
                return None
            enriched = dict(row)
            enriched["active_brand_count"] = 0
            enriched["ytd_gmv_cents"] = 0
            return enriched

        raise AssertionError(f"Unhandled fetch_one query: {query}")

    async def execute(self, query: str, values: dict[str, Any] | None = None):
        sql = _normalize_sql(query)
        params = dict(values or {})
        if sql.startswith("insert into partner_rate_schedules"):
            self.partner_rate_schedules.append(dict(params))
            return None
        raise AssertionError(f"Unhandled execute query: {query}")


@pytest.fixture()
def fake_db(monkeypatch: pytest.MonkeyPatch) -> _FakeCreateDatabase:
    db = _FakeCreateDatabase()
    monkeypatch.setattr(module, "database", db)

    async def _no_cohort(channel_partner_id: int) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(
        module.cohort_target_evaluator,
        "get_partner_target_progress",
        _no_cohort,
    )
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


def _post(client: TestClient, **body: Any):
    return client.post("/admin/partners", json=body)


def test_create_minimal_seeds_default_scope_b_rates(
    fake_db: _FakeCreateDatabase,
) -> None:
    client, app = _build_client()
    try:
        response = _post(
            client, legal_name="Acme Channel Co", archetype="agency"
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201
    body = response.json()
    assert body["id"] == 1
    assert body["legal_name"] == "Acme Channel Co"
    assert body["status"] == "pending"
    assert body["gmv_take_definition"] == "net"
    # 3 subscription + 3 credit_overage + 3 gmv_take = 9 seeded rows.
    assert body["seeded_rate_schedule_count"] == 9
    assert len(fake_db.partner_rate_schedules) == 9
    scopes = {r["scope"] for r in fake_db.partner_rate_schedules}
    assert scopes == {"B"}
    streams = {r["stream"] for r in fake_db.partner_rate_schedules}
    assert streams == {"subscription", "credit_overage", "gmv_take"}
    # Every seed row is dated at the partner's term_start_date.
    assert {r["effective_from"] for r in fake_db.partner_rate_schedules} == {_TODAY}


def test_create_channel_tiered_seeds_split_gmv_streams(
    fake_db: _FakeCreateDatabase,
) -> None:
    client, app = _build_client()
    try:
        response = _post(
            client,
            legal_name="Tiered Partner",
            archetype="platform",
            gmv_take_definition="channel_tiered",
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201
    assert response.json()["seeded_rate_schedule_count"] == 12
    streams = {r["stream"] for r in fake_db.partner_rate_schedules}
    assert "gmv_take_personal" in streams
    assert "gmv_take_third_party" in streams
    assert "gmv_take" not in streams


def test_create_honors_active_rate_scope(fake_db: _FakeCreateDatabase) -> None:
    client, app = _build_client()
    try:
        response = _post(
            client,
            legal_name="Scope A Partner",
            archetype="affiliate",
            active_rate_scope="A",
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201
    assert {r["scope"] for r in fake_db.partner_rate_schedules} == {"A"}


def test_create_can_skip_rate_seed(fake_db: _FakeCreateDatabase) -> None:
    client, app = _build_client()
    try:
        response = _post(
            client,
            legal_name="No Seed Partner",
            archetype="other",
            seed_default_rate_schedule=False,
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201
    assert response.json()["seeded_rate_schedule_count"] == 0
    assert fake_db.partner_rate_schedules == []


def test_create_duplicate_legal_name_case_insensitive_returns_409(
    fake_db: _FakeCreateDatabase,
) -> None:
    fake_db.channel_partners.append(
        {"id": 42, "legal_name": "Markato Limited", "status": "active"}
    )
    client, app = _build_client()
    try:
        response = _post(
            client, legal_name="markato limited", archetype="curated_marketplace"
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "partner_already_exists"
    assert body["existing_partner_id"] == 42


def test_create_invalid_archetype_returns_400(
    fake_db: _FakeCreateDatabase,
) -> None:
    client, app = _build_client()
    try:
        response = _post(client, legal_name="Bad Archetype", archetype="reseller")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_archetype"


def test_create_invalid_gmv_take_rate_returns_400(
    fake_db: _FakeCreateDatabase,
) -> None:
    client, app = _build_client()
    try:
        response = _post(
            client,
            legal_name="Bad Rate",
            archetype="agency",
            gmv_take_rate_bp=20000,
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_gmv_take_rate_bp"


def test_create_billing_mode_both_false_returns_400(
    fake_db: _FakeCreateDatabase,
) -> None:
    client, app = _build_client()
    try:
        response = _post(
            client,
            legal_name="No Billing Mode",
            archetype="agency",
            prepaid_credits_supported=False,
            monthly_overage_supported=False,
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_billing_mode"


def test_create_empty_legal_name_returns_400(
    fake_db: _FakeCreateDatabase,
) -> None:
    client, app = _build_client()
    try:
        response = _post(client, legal_name="   ", archetype="agency")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert response.json()["error"] == "legal_name_required"


def test_create_requires_admin(fake_db: _FakeCreateDatabase) -> None:
    client, app = _build_client(authenticated=False)
    try:
        response = _post(client, legal_name="Unauthorized", archetype="agency")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code in {401, 403}


def _normalize_sql(query: str) -> str:
    return " ".join(str(query).split()).lower()
