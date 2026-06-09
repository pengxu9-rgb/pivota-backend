from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient

import pytest

import routes.billing_routes as module
from utils.auth import create_access_token


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


# ---------------------------------------------------------------------------
# require_approved_merchant — dual auth (API key OR merchant JWT).
# The portal browser only holds a JWT, so the self-serve billing pages depend
# on the JWT path resolving to the same merchant dict shape.
# ---------------------------------------------------------------------------


def _bearer(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _merchant_jwt(merchant_id: str | None = "merch_jwt", role: str = "merchant") -> str:
    payload: dict[str, Any] = {"sub": "user_1", "email": "m@example.com", "role": role}
    if merchant_id is not None:
        payload["merchant_id"] = merchant_id
    return create_access_token(payload)


def _onboarding_loader(*, status: str | None = "approved", contact_email: str = "m@example.com"):
    """Build an async stub for module.get_merchant_onboarding.

    status=None simulates an unknown merchant_id (no onboarding row).
    """

    async def _loader(merchant_id: str):
        if status is None:
            return None
        return {"merchant_id": merchant_id, "status": status, "contact_email": contact_email}

    return _loader


def test_require_approved_merchant_accepts_approved_merchant_jwt(monkeypatch) -> None:
    monkeypatch.setattr(module, "get_merchant_onboarding", _onboarding_loader())
    result = asyncio.run(
        module.require_approved_merchant(
            x_merchant_api_key=None,
            credentials=_bearer(_merchant_jwt("merch_jwt")),
        )
    )
    assert result["merchant_id"] == "merch_jwt"
    assert result["status"] == "approved"


def test_require_approved_merchant_rejects_non_merchant_jwt() -> None:
    # Role check happens before any DB lookup.
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            module.require_approved_merchant(
                x_merchant_api_key=None,
                credentials=_bearer(_merchant_jwt("ag_1", role="agent")),
            )
        )
    assert exc.value.status_code == 403


def test_require_approved_merchant_jwt_missing_merchant_id_returns_401() -> None:
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            module.require_approved_merchant(
                x_merchant_api_key=None,
                credentials=_bearer(_merchant_jwt(merchant_id=None)),
            )
        )
    assert exc.value.status_code == 401


def test_require_approved_merchant_jwt_non_approved_returns_403(monkeypatch) -> None:
    # A logged-in but pending/rejected merchant must NOT reach billing.
    monkeypatch.setattr(module, "get_merchant_onboarding", _onboarding_loader(status="pending"))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            module.require_approved_merchant(
                x_merchant_api_key=None,
                credentials=_bearer(_merchant_jwt("merch_jwt")),
            )
        )
    assert exc.value.status_code == 403


def test_require_approved_merchant_jwt_unknown_merchant_returns_404(monkeypatch) -> None:
    monkeypatch.setattr(module, "get_merchant_onboarding", _onboarding_loader(status=None))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            module.require_approved_merchant(
                x_merchant_api_key=None,
                credentials=_bearer(_merchant_jwt("merch_jwt")),
            )
        )
    assert exc.value.status_code == 404


def test_require_approved_merchant_invalid_jwt_returns_401() -> None:
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            module.require_approved_merchant(
                x_merchant_api_key=None,
                credentials=_bearer("not-a-real-jwt"),
            )
        )
    assert exc.value.status_code == 401


def test_require_approved_merchant_no_credentials_returns_401() -> None:
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            module.require_approved_merchant(x_merchant_api_key=None, credentials=None)
        )
    assert exc.value.status_code == 401


def test_require_approved_merchant_api_key_takes_precedence(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def _fake_verify(api_key: str) -> dict[str, Any]:
        captured["api_key"] = api_key
        return {
            "merchant_id": "merch_api",
            "status": "approved",
            "contact_email": "x@y.com",
        }

    def _no_jwt_path(_merchant_id):
        raise AssertionError("JWT path must not run when an API key is present")

    monkeypatch.setattr(module, "verify_merchant_api_key", _fake_verify)
    monkeypatch.setattr(module, "get_merchant_onboarding", _no_jwt_path)
    result = asyncio.run(
        module.require_approved_merchant(
            x_merchant_api_key="key_123",
            credentials=_bearer(_merchant_jwt("merch_jwt")),
        )
    )
    # API key wins over JWT when both are present.
    assert captured["api_key"] == "key_123"
    assert result["merchant_id"] == "merch_api"


def test_require_approved_merchant_invalid_api_key_does_not_fall_back_to_jwt(monkeypatch) -> None:
    async def _fake_verify(api_key: str) -> dict[str, Any]:
        raise HTTPException(status_code=401, detail="Invalid API key")

    def _no_jwt_path(_merchant_id):
        raise AssertionError("must not fall back to JWT on a bad API key")

    monkeypatch.setattr(module, "verify_merchant_api_key", _fake_verify)
    monkeypatch.setattr(module, "get_merchant_onboarding", _no_jwt_path)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            module.require_approved_merchant(
                x_merchant_api_key="bad-key",
                credentials=_bearer(_merchant_jwt("merch_jwt")),
            )
        )
    assert exc.value.status_code == 401


# --- End-to-end through FastAPI's real Authorization: Bearer parsing + routes ---


def test_current_period_with_real_merchant_jwt_header(
    fake_db: _FakeBillingMeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "get_merchant_onboarding", _onboarding_loader())
    fake_db.add_subscription()
    fake_db.add_credit_use(credits=2341)
    client, app = _build_client(authenticated=False)  # real dependency, no override
    try:
        response = client.get(
            "/api/billing/me/current-period",
            headers={"Authorization": f"Bearer {_merchant_jwt('merch_1')}"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["tier"] == "starter"


def test_current_period_with_non_approved_jwt_header_returns_403(
    fake_db: _FakeBillingMeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "get_merchant_onboarding", _onboarding_loader(status="pending"))
    client, app = _build_client(authenticated=False)
    try:
        response = client.get(
            "/api/billing/me/current-period",
            headers={"Authorization": f"Bearer {_merchant_jwt('merch_1')}"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403


def test_statements_with_real_merchant_jwt_header(
    fake_db: _FakeBillingMeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "get_merchant_onboarding", _onboarding_loader())
    fake_db.add_statement(calendar_month=date(2026, 5, 1), status="invoiced")
    client, app = _build_client(authenticated=False)
    try:
        response = client.get(
            "/api/billing/me/statements",
            headers={"Authorization": f"Bearer {_merchant_jwt('merch_1')}"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert len(response.json()["statements"]) == 1


# ---------------------------------------------------------------------------
# GET /api/billing/plans — mode-scoped active plan catalogue
# ---------------------------------------------------------------------------


class _FakePlansDb:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.params: dict[str, Any] | None = None
        self.where_sql: str | None = None

    async def fetch_all(self, query: str, values: dict[str, Any] | None = None):
        self.params = dict(values or {})
        self.where_sql = _normalize_sql(query)
        assert "from subscription_plans" in self.where_sql
        return self._rows


def _patch_plans(monkeypatch: pytest.MonkeyPatch, db: _FakePlansDb, *, mode: str = "live") -> None:
    async def _cols(_db, _table):
        return {
            "name", "stripe_price_id", "price_cents",
            "monthly_credit_allowance", "status", "stripe_mode", "tier_level",
        }

    monkeypatch.setattr(module, "database", db)
    monkeypatch.setattr(module, "_table_columns", _cols)
    monkeypatch.setattr(module, "_platform_stripe_mode", lambda: mode)


def test_list_plans_returns_mode_scoped_merchant_safe_plans(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _FakePlansDb([
        {"name": "starter", "stripe_price_id": "price_live_starter", "price_cents": 4900, "monthly_credit_allowance": 4000},
        {"name": "growth", "stripe_price_id": "price_live_growth", "price_cents": 14900, "monthly_credit_allowance": 18000},
    ])
    _patch_plans(monkeypatch, db, mode="live")
    client, app = _build_client()
    try:
        response = client.get("/api/billing/plans")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    plans = response.json()["plans"]
    assert [p["name"] for p in plans] == ["starter", "growth"]
    assert plans[0]["price_id"] == "price_live_starter"
    assert plans[0]["price_cents"] == 4900
    assert plans[0]["monthly_credit_allowance"] == 4000
    assert plans[0]["currency"] == "usd"
    # mode filter passed through to SQL params
    assert db.params == {"mode": "live"}
    # merchant-safe: no internal columns leak
    assert "tier_level" not in plans[0]
    assert "features_json" not in plans[0]


def test_list_plans_requires_auth() -> None:
    client, app = _build_client(authenticated=False)
    try:
        response = client.get("/api/billing/plans")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 401


def test_platform_stripe_mode_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(module, "settings", SimpleNamespace(stripe_secret_key="sk_live_abc123"))
    assert module._platform_stripe_mode() == "live"
    monkeypatch.setattr(module, "settings", SimpleNamespace(stripe_secret_key="sk_test_abc123"))
    assert module._platform_stripe_mode() == "test"
    monkeypatch.setattr(module, "settings", SimpleNamespace(stripe_secret_key=None))
    assert module._platform_stripe_mode() == "test"


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


# ---------------------------------------------------------------------------
# checkout-session: recreate the Stripe customer when the stored one is invalid
# (e.g. created under a different key before a Stripe key rotation, or deleted).
# ---------------------------------------------------------------------------


class _StripeInvalidRequestError(Exception):
    pass


# The recovery branch matches on the exception class *name*, so expose it as
# Stripe's class name without depending on the installed SDK version.
_StripeInvalidRequestError.__name__ = "InvalidRequestError"
_StripeInvalidRequestError.__qualname__ = "InvalidRequestError"


def _mk_err(message, code=None):
    e = _StripeInvalidRequestError(message)
    e.code = code
    e.user_message = message
    return e


class _SObj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeSessions:
    def __init__(self, fail_for, error):
        self.fail_for = fail_for
        self.error = error
        self.customers_seen: list[str] = []
        self.params_seen: list[dict] = []
        self.keys_seen: list[str] = []

    def create(self, params, opts):
        cust = params["customer"]
        self.customers_seen.append(cust)
        self.params_seen.append(params)
        self.keys_seen.append((opts or {}).get("idempotency_key"))
        if cust == self.fail_for:
            raise self.error
        return _SObj(id="cs_new", url="https://stripe.test/cs_new")


class _FakeCustomers:
    def __init__(self):
        self.created = 0

    def create(self, params, opts):
        self.created += 1
        return _SObj(id="cus_new")


class _FakeStripeClient:
    def __init__(self, fail_for, error):
        self.v1 = _SObj(
            checkout=_SObj(sessions=_FakeSessions(fail_for, error)),
            customers=_FakeCustomers(),
        )


def _patch_checkout_deps(monkeypatch, fake_client):
    monkeypatch.setattr(module, "_require_platform_stripe_key", lambda: None)

    async def _plan(*a, **k):
        return {"id": 1, "name": "starter", "stripe_price_id": "price_x", "monthly_credit_allowance": 4000}

    async def _billing_row(*a, **k):
        return {"stripe_customer_id": "cus_stale", "contact_email": "m@example.com"}

    persisted: dict[str, Any] = {}

    async def _persist(db, *, merchant_id, contact_email, stripe_customer_id):
        persisted["id"] = stripe_customer_id
        return True

    monkeypatch.setattr(module, "_lookup_subscription_plan", _plan)
    monkeypatch.setattr(module, "_fetch_merchant_billing_row", _billing_row)
    monkeypatch.setattr(module, "_update_merchant_stripe_customer_id", _persist)
    monkeypatch.setattr(module, "stripe_client", fake_client)
    return persisted


def _checkout_body():
    return module.CheckoutSessionRequest(
        price_id="price_x", success_url="https://x/s", cancel_url="https://x/c"
    )


def test_checkout_recreates_customer_when_stored_customer_missing(monkeypatch) -> None:
    fake = _FakeStripeClient("cus_stale", _mk_err("No such customer: cus_stale", code="resource_missing"))
    persisted = _patch_checkout_deps(monkeypatch, fake)

    result = asyncio.run(
        module.create_billing_checkout_session(
            body=_checkout_body(),
            merchant={"merchant_id": "merch_1", "contact_email": "m@example.com", "status": "approved"},
        )
    )

    assert result == {"session_url": "https://stripe.test/cs_new", "session_id": "cs_new"}
    assert fake.v1.customers.created == 1                       # recreated exactly once
    assert persisted["id"] == "cus_new"                        # new id persisted → self-heals
    assert fake.v1.checkout.sessions.customers_seen == ["cus_stale", "cus_new"]  # failed, then retried
    # promo-code field enabled on every session payload (free-test support)
    assert all(p.get("allow_promotion_codes") is True for p in fake.v1.checkout.sessions.params_seen)
    # recreate path must use a DIFFERENT idempotency key (payload-derived), so
    # the retry with the new customer never collides with the failed first key.
    keys = fake.v1.checkout.sessions.keys_seen
    assert keys[0] != keys[1] and all(keys)


def test_checkout_idempotency_key_varies_with_urls(monkeypatch) -> None:
    # Same merchant/price but different success/cancel URLs must produce
    # different idempotency keys — otherwise Stripe raises IdempotencyError.
    fake = _FakeStripeClient(fail_for=None, error=_mk_err("never"))  # never fails
    _patch_checkout_deps(monkeypatch, fake)
    merchant = {"merchant_id": "merch_1", "contact_email": "m@example.com", "status": "approved"}

    asyncio.run(module.create_billing_checkout_session(
        body=module.CheckoutSessionRequest(price_id="price_x", success_url="https://a/s", cancel_url="https://a/c"),
        merchant=merchant,
    ))
    asyncio.run(module.create_billing_checkout_session(
        body=module.CheckoutSessionRequest(price_id="price_x", success_url="https://b/s", cancel_url="https://b/c"),
        merchant=merchant,
    ))
    k1, k2 = fake.v1.checkout.sessions.keys_seen
    assert k1 and k2 and k1 != k2


def test_checkout_does_not_swallow_unrelated_stripe_errors(monkeypatch) -> None:
    fake = _FakeStripeClient("cus_stale", _mk_err("No such price: price_x", code="resource_missing_price"))
    _patch_checkout_deps(monkeypatch, fake)

    with pytest.raises(Exception) as exc:
        asyncio.run(
            module.create_billing_checkout_session(
                body=_checkout_body(),
                merchant={"merchant_id": "merch_1", "contact_email": "m@example.com", "status": "approved"},
            )
        )
    assert type(exc.value).__name__ == "InvalidRequestError"
    assert fake.v1.customers.created == 0                       # no recreate for unrelated errors
