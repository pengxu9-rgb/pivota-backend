"""Browser collector token lifecycle (PR-0.5, migration 215).

Before this a collector token was a signed JWT and nothing else: no record it
existed, no revocation short of disconnecting the store or rotating the
signing secret for every merchant, no persisted expiry to alert on, and a
400-day cap. These tests pin the registry, the store generation, the 90-day
cap, and the management routes.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import databases
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from db.database import metadata  # noqa: E402
from db.merchant_collector_tokens import (  # noqa: E402
    merchant_collector_token_policy,
    merchant_collector_tokens,
)
from services import merchant_collector_token_registry as registry  # noqa: E402
from services import merchant_web_collector_service as collector  # noqa: E402

MERCHANT_ID = "merch_life"
OTHER_MERCHANT = "merch_other"
STORE_ID = "store_life_1"
PLATFORM = "woocommerce"
ORIGIN = "https://shop.example.com"
SECRET = "collector-lifecycle-secret-that-is-long-enough-12345"
_MIGRATION = BACKEND_ROOT / "db" / "migrations" / "215_merchant_collector_token_registry.sql"


@pytest.fixture(autouse=True)
def _signing_secret(monkeypatch):
    monkeypatch.setenv("MERCHANT_WEB_COLLECTOR_SIGNING_SECRET", SECRET)


@pytest.fixture
async def sqlite_registry(tmp_path, monkeypatch):
    db_path = tmp_path / "collector-tokens.sqlite3"
    engine = create_engine(f"sqlite:///{db_path}")
    metadata.create_all(
        engine, tables=[merchant_collector_tokens, merchant_collector_token_policy], checkfirst=True
    )
    engine.dispose()
    db = databases.Database(f"sqlite+aiosqlite:///{db_path}")
    await db.connect()
    monkeypatch.setattr(registry, "database", db)
    try:
        yield db
    finally:
        await db.disconnect()


def _issue(**overrides):
    kwargs = dict(
        merchant_id=MERCHANT_ID,
        store_id=STORE_ID,
        platform=PLATFORM,
        allowed_origins=[ORIGIN],
    )
    kwargs.update(overrides)
    return collector.issue_web_collector_token(**kwargs)


def _legacy_v1_token(now=None):
    """What every token looked like before this change: v1, no jti, no sv."""
    import jwt

    issued_at = now or datetime.now(timezone.utc)
    claims = {
        "iss": collector.WEB_COLLECTOR_ISSUER,
        "aud": collector.WEB_COLLECTOR_AUDIENCE,
        "typ": collector.WEB_COLLECTOR_TOKEN_TYPE,
        "v": 1,
        "merchant_id": MERCHANT_ID,
        "store_id": STORE_ID,
        "platform": PLATFORM,
        "allowed_origins": [ORIGIN],
        "iat": int(issued_at.timestamp()),
        "exp": int((issued_at + timedelta(days=300)).timestamp()),
    }
    return jwt.encode(claims, collector._collector_signing_key(), algorithm="HS256")


# ---- 1. schema homes agree ------------------------------------------------------


def test_the_model_and_migration_declare_the_same_tables():
    sql = _MIGRATION.read_text()
    for table in (merchant_collector_tokens, merchant_collector_token_policy):
        assert f"CREATE TABLE IF NOT EXISTS {table.name}" in sql
        for column in table.columns:
            assert column.name in sql, f"{table.name}.{column.name} missing from migration"
    from db.sql_migrations import needs_autocommit

    assert needs_autocommit(sql) is False
    guard = (BACKEND_ROOT / "db" / "schema_guard.py").read_text()
    assert "CREATE TABLE IF NOT EXISTS merchant_collector_tokens" in guard
    assert "CREATE TABLE IF NOT EXISTS merchant_collector_token_policy" in guard
    assert "import db.merchant_collector_tokens" in (BACKEND_ROOT / "main.py").read_text()


# ---- 2. the token itself --------------------------------------------------------------


def test_ttl_is_capped_at_ninety_days_and_the_token_carries_jti_and_generation():
    assert collector.MAX_TOKEN_TTL_DAYS == 90
    now = datetime(2026, 9, 4, tzinfo=timezone.utc)
    issued = _issue(ttl_days=400, now=now, store_token_version=3)
    claims = collector.verify_web_collector_token(issued["token"], request_origin=ORIGIN)
    assert claims["exp"] == int((now + timedelta(days=90)).timestamp())
    assert claims["v"] == 2
    assert claims["jti"] == issued["jti"] and issued["jti"].startswith("ct_")
    assert claims["sv"] == 3
    assert issued["renewal_due_at"] == (now + timedelta(days=60)).isoformat()
    # Two issuances never share a jti.
    assert _issue(now=now)["jti"] != issued["jti"]


def test_a_v2_token_without_a_jti_is_refused_but_legacy_v1_still_verifies():
    import jwt

    now = datetime.now(timezone.utc)
    claims = {
        "iss": collector.WEB_COLLECTOR_ISSUER,
        "aud": collector.WEB_COLLECTOR_AUDIENCE,
        "typ": collector.WEB_COLLECTOR_TOKEN_TYPE,
        "v": 2,
        "merchant_id": MERCHANT_ID,
        "store_id": STORE_ID,
        "platform": PLATFORM,
        "allowed_origins": [ORIGIN],
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=1)).timestamp()),
    }
    forged = jwt.encode(claims, collector._collector_signing_key(), algorithm="HS256")
    with pytest.raises(collector.WebCollectorError):
        collector.verify_web_collector_token(forged, request_origin=ORIGIN)
    legacy = collector.verify_web_collector_token(_legacy_v1_token(), request_origin=ORIGIN)
    assert legacy["v"] == 1 and legacy["jti"] is None and legacy["sv"] == 1


def test_pixel_token_carries_the_same_lifecycle_claims():
    issued = collector.issue_shopify_pixel_token(
        merchant_id=MERCHANT_ID, store_id="store_shopify", ttl_days=1000, store_token_version=2
    )
    claims = collector.verify_shopify_pixel_token(issued["token"])
    assert claims["jti"] == issued["jti"] and claims["sv"] == 2
    assert issued["token_type"] == collector.SHOPIFY_PIXEL_TOKEN_TYPE
    assert datetime.fromisoformat(issued["expires_at"]) - datetime.fromisoformat(
        issued["issued_at"]
    ) == timedelta(days=90)


# ---- 3. the registry -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_registered_token_is_honoured_and_revocation_refuses_it(sqlite_registry):
    issued = _issue()
    await registry.register_issued_token(
        issued=issued, merchant_id=MERCHANT_ID, store_id=STORE_ID, issued_by="user_1"
    )
    claims = collector.verify_web_collector_token(issued["token"], request_origin=ORIGIN)
    await registry.enforce_token_registry(claims)  # does not raise

    assert await registry.revoke_token(jti=issued["jti"], merchant_id=MERCHANT_ID, reason="leaked")
    with pytest.raises(collector.WebCollectorError) as error:
        await registry.enforce_token_registry(claims)
    assert error.value.status_code == 401
    # A second revoke is a no-op, and a foreign merchant cannot revoke it at all.
    assert not await registry.revoke_token(jti=issued["jti"], merchant_id=MERCHANT_ID, reason="x")
    fresh = _issue()
    await registry.register_issued_token(issued=fresh, merchant_id=MERCHANT_ID, store_id=STORE_ID)
    assert not await registry.revoke_token(jti=fresh["jti"], merchant_id=OTHER_MERCHANT, reason="x")


@pytest.mark.asyncio
async def test_an_unregistered_v2_token_is_refused(sqlite_registry):
    # Signed with our key but never recorded: e.g. minted from a leaked signing
    # secret. The registry is the second factor.
    claims = collector.verify_web_collector_token(_issue()["token"], request_origin=ORIGIN)
    with pytest.raises(collector.WebCollectorError) as error:
        await registry.enforce_token_registry(claims)
    assert error.value.status_code == 401


@pytest.mark.asyncio
async def test_a_registered_token_presented_for_another_store_is_refused(sqlite_registry):
    issued = _issue()
    await registry.register_issued_token(issued=issued, merchant_id=MERCHANT_ID, store_id=STORE_ID)
    claims = collector.verify_web_collector_token(issued["token"], request_origin=ORIGIN)
    with pytest.raises(collector.WebCollectorError):
        await registry.enforce_token_registry({**claims, "store_id": "store_other"})


@pytest.mark.asyncio
async def test_store_revocation_refuses_legacy_and_registered_tokens_but_not_successors(
    sqlite_registry,
):
    legacy = collector.verify_web_collector_token(_legacy_v1_token(), request_origin=ORIGIN)
    await registry.enforce_token_registry(legacy)  # honoured while generation is 1

    old = _issue(store_token_version=await registry.current_store_token_version(STORE_ID))
    await registry.register_issued_token(issued=old, merchant_id=MERCHANT_ID, store_id=STORE_ID)
    old_claims = collector.verify_web_collector_token(old["token"], request_origin=ORIGIN)
    await registry.enforce_token_registry(old_claims)

    result = await registry.revoke_store_tokens(
        store_id=STORE_ID, merchant_id=MERCHANT_ID, reason="rotate"
    )
    assert result == {"store_id": STORE_ID, "min_token_version": 2, "revoked_count": 1}

    for claims in (legacy, old_claims):
        with pytest.raises(collector.WebCollectorError) as error:
            await registry.enforce_token_registry(claims)
        assert error.value.status_code == 401

    # A token issued AFTER the bump carries the new generation and works.
    new = _issue(store_token_version=await registry.current_store_token_version(STORE_ID))
    await registry.register_issued_token(issued=new, merchant_id=MERCHANT_ID, store_id=STORE_ID)
    new_claims = collector.verify_web_collector_token(new["token"], request_origin=ORIGIN)
    assert new_claims["sv"] == 2
    await registry.enforce_token_registry(new_claims)

    # Bumping again invalidates that one too; the generation only moves forward.
    await registry.revoke_store_tokens(store_id=STORE_ID, merchant_id=MERCHANT_ID, reason="again")
    with pytest.raises(collector.WebCollectorError):
        await registry.enforce_token_registry(new_claims)
    assert await registry.current_store_token_version(STORE_ID) == 3


@pytest.mark.asyncio
async def test_registry_outage_fails_closed(sqlite_registry, monkeypatch):
    class Broken:
        async def fetch_one(self, *_a, **_k):
            raise RuntimeError("db down")

    monkeypatch.setattr(registry, "database", Broken())
    claims = collector.verify_web_collector_token(_issue()["token"], request_origin=ORIGIN)
    with pytest.raises(collector.WebCollectorError) as error:
        await registry.enforce_token_registry(claims)
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_listing_reports_state_and_renewal_and_expiring_excludes_renewed(sqlite_registry):
    now = datetime.now(timezone.utc)
    soon = _issue(ttl_days=10, now=now)  # inside the 30-day renewal window
    later = _issue(ttl_days=90, now=now)
    dead = _issue(ttl_days=1, now=now - timedelta(days=5))  # already expired
    for issued in (soon, later, dead):
        await registry.register_issued_token(issued=issued, merchant_id=MERCHANT_ID, store_id=STORE_ID)
    # A token for another merchant must never appear in this merchant's views.
    foreign = collector.issue_web_collector_token(
        merchant_id=OTHER_MERCHANT, store_id="store_foreign", platform=PLATFORM,
        allowed_origins=[ORIGIN], ttl_days=5, now=now,
    )
    await registry.register_issued_token(issued=foreign, merchant_id=OTHER_MERCHANT, store_id="store_foreign")

    listing = {row["jti"]: row for row in await registry.list_store_tokens(store_id=STORE_ID, merchant_id=MERCHANT_ID)}
    assert set(listing) == {soon["jti"], later["jti"], dead["jti"]}
    assert listing[soon["jti"]]["state"] == "active" and listing[soon["jti"]]["renewal_due"] is True
    assert listing[later["jti"]]["state"] == "active" and listing[later["jti"]]["renewal_due"] is False
    assert listing[dead["jti"]]["state"] == "expired" and listing[dead["jti"]]["renewal_due"] is False
    assert "token" not in listing[soon["jti"]], "the credential itself is never listed"

    expiring = await registry.expiring_tokens(within_days=30, merchant_id=MERCHANT_ID)
    assert [row["jti"] for row in expiring] == [soon["jti"]]
    everyone = await registry.expiring_tokens(within_days=30)
    assert {row["jti"] for row in everyone} == {soon["jti"], foreign["jti"]}

    # Renewal: the successor supersedes the old one, which leaves the alert list.
    successor = _issue(ttl_days=90, now=now)
    await registry.register_issued_token(
        issued=successor, merchant_id=MERCHANT_ID, store_id=STORE_ID, supersedes=soon["jti"]
    )
    assert await registry.expiring_tokens(within_days=30, merchant_id=MERCHANT_ID) == []
    listing = {row["jti"]: row for row in await registry.list_store_tokens(store_id=STORE_ID, merchant_id=MERCHANT_ID)}
    assert listing[soon["jti"]]["state"] == "superseded"
    assert listing[soon["jti"]]["superseded_by"] == successor["jti"]
    # Superseded is not revoked: the old snippet keeps working until it expires.
    await registry.enforce_token_registry(
        collector.verify_web_collector_token(soon["token"], request_origin=ORIGIN)
    )


# ---- 4. the routes ---------------------------------------------------------------------


class _StoreDatabase:
    """`_connected_store` lookups only; the registry has its own database."""

    def __init__(self, stores):
        self.stores = stores

    async def fetch_one(self, _query, values):
        return self.stores.get(values["store_id"])


def _app(current_user):
    from routes.merchant_events import router
    from utils.auth import get_current_user

    app = FastAPI()
    app.include_router(router)

    async def fake_user():
        return current_user

    app.dependency_overrides[get_current_user] = fake_user
    return TestClient(app)


@pytest.fixture
def stores(monkeypatch):
    from routes import merchant_events as route

    db = _StoreDatabase(
        {
            STORE_ID: {"store_id": STORE_ID, "merchant_id": MERCHANT_ID, "platform": PLATFORM,
                       "domain": "shop.example.com", "status": "active"},
            "store_foreign": {"store_id": "store_foreign", "merchant_id": OTHER_MERCHANT,
                              "platform": PLATFORM, "domain": "other.example.com", "status": "active"},
        }
    )
    monkeypatch.setattr(route, "database", db)
    return db


MERCHANT_USER = {"role": "merchant", "merchant_id": MERCHANT_ID}
OTHER_USER = {"role": "merchant", "merchant_id": OTHER_MERCHANT}
EMPLOYEE = {"role": "employee", "id": "emp_1"}


@pytest.mark.asyncio
async def test_install_token_route_registers_and_refuses_ttl_above_the_cap(sqlite_registry, stores):
    client = _app(MERCHANT_USER)
    assert client.post("/merchant-events/v1/web/install-token", json={"store_id": STORE_ID, "ttl_days": 400}).status_code == 422
    response = client.post("/merchant-events/v1/web/install-token", json={"store_id": STORE_ID, "ttl_days": 90})
    assert response.status_code == 200, response.text
    payload = response.json()
    row = await registry.fetch_token(payload["jti"])
    assert row is not None and row["merchant_id"] == MERCHANT_ID and row["store_id"] == STORE_ID
    assert row["issued_by"] == "merchant"  # no id on this principal, role recorded
    assert row["token_type"] == collector.WEB_COLLECTOR_TOKEN_TYPE


@pytest.mark.asyncio
async def test_web_batch_refuses_a_revoked_token(sqlite_registry, stores, monkeypatch):
    from routes import merchant_events as route

    calls = []

    async def fake_ingest(**kwargs):
        calls.append(kwargs)
        return {"accepted": 1, "duplicates": 0, "events": []}

    monkeypatch.setattr(route, "ingest_merchant_event_batch", fake_ingest)
    issued = _issue()
    await registry.register_issued_token(issued=issued, merchant_id=MERCHANT_ID, store_id=STORE_ID)
    body = json.dumps(
        {
            "collector_token": issued["token"],
            "events": [
                {"event_id": "e1", "event_type": "product.viewed",
                 "occurred_at": datetime.now(timezone.utc).isoformat(), "session_id": "s1"}
            ],
        }
    )
    client = _app(MERCHANT_USER)
    headers = {"Content-Type": "text/plain", "Origin": ORIGIN}
    assert client.post("/merchant-events/v1/web/batch", content=body, headers=headers).status_code == 200
    await registry.revoke_token(jti=issued["jti"], merchant_id=MERCHANT_ID, reason="leaked")
    refused = client.post("/merchant-events/v1/web/batch", content=body, headers=headers)
    assert refused.status_code == 401
    assert "revoked" in refused.text
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_list_revoke_and_renew_routes_are_tenant_scoped(sqlite_registry, stores):
    mine = _issue()
    await registry.register_issued_token(issued=mine, merchant_id=MERCHANT_ID, store_id=STORE_ID)
    theirs = collector.issue_web_collector_token(
        merchant_id=OTHER_MERCHANT, store_id="store_foreign", platform=PLATFORM, allowed_origins=[ORIGIN]
    )
    await registry.register_issued_token(issued=theirs, merchant_id=OTHER_MERCHANT, store_id="store_foreign")

    me = _app(MERCHANT_USER)
    listing = me.get("/merchant-events/v1/tokens", params={"store_id": STORE_ID})
    assert listing.status_code == 200
    assert [row["jti"] for row in listing.json()["tokens"]] == [mine["jti"]]
    assert listing.json()["store_token_version"] == 1
    assert me.get("/merchant-events/v1/tokens", params={"store_id": "store_foreign"}).status_code == 403

    # Foreign and unknown jtis are indistinguishable to a merchant.
    assert me.post(f"/merchant-events/v1/tokens/{theirs['jti']}/revoke", json={}).status_code == 404
    assert me.post("/merchant-events/v1/tokens/ct_nope/revoke", json={}).status_code == 404
    assert (await registry.fetch_token(theirs["jti"]))["revoked_at"] is None

    renewed = me.post(f"/merchant-events/v1/tokens/{mine['jti']}/renew", json={"ttl_days": 30})
    assert renewed.status_code == 200, renewed.text
    body = renewed.json()
    assert body["previous_jti"] == mine["jti"] and body["previous_revoked"] is False
    new_row = await registry.fetch_token(body["jti"])
    assert new_row["store_id"] == STORE_ID and new_row["allowed_origins"] == [ORIGIN]
    assert (await registry.fetch_token(mine["jti"]))["superseded_by"] == body["jti"]
    # The successor verifies against the same origin and is honoured.
    claims = collector.verify_web_collector_token(body["collector_token"], request_origin=ORIGIN)
    await registry.enforce_token_registry(claims)

    revoked = me.post(f"/merchant-events/v1/tokens/{mine['jti']}/revoke", json={"reason": "swapped"})
    assert revoked.json()["status"] == "revoked"
    assert me.post(f"/merchant-events/v1/tokens/{mine['jti']}/revoke", json={}).json()["status"] == "already_revoked"

    # An employee may act on any merchant's token, and sees the real 403 shape on stores.
    staff = _app(EMPLOYEE)
    assert staff.post(f"/merchant-events/v1/tokens/{theirs['jti']}/revoke", json={}).status_code == 200


@pytest.mark.asyncio
async def test_renew_with_revoke_previous_closes_the_old_token(sqlite_registry, stores):
    mine = _issue()
    await registry.register_issued_token(issued=mine, merchant_id=MERCHANT_ID, store_id=STORE_ID)
    me = _app(MERCHANT_USER)
    renewed = me.post(f"/merchant-events/v1/tokens/{mine['jti']}/renew", json={"revoke_previous": True})
    assert renewed.status_code == 200
    assert renewed.json()["previous_revoked"] is True
    old = await registry.fetch_token(mine["jti"])
    assert old["revoked_at"] is not None and old["revoked_reason"] == "renewed"


@pytest.mark.asyncio
async def test_revoke_all_route_bumps_the_generation_and_expiring_route_is_scoped(sqlite_registry, stores):
    now = datetime.now(timezone.utc)
    mine = _issue(ttl_days=7, now=now)
    await registry.register_issued_token(issued=mine, merchant_id=MERCHANT_ID, store_id=STORE_ID)
    theirs = collector.issue_web_collector_token(
        merchant_id=OTHER_MERCHANT, store_id="store_foreign", platform=PLATFORM,
        allowed_origins=[ORIGIN], ttl_days=7, now=now,
    )
    await registry.register_issued_token(issued=theirs, merchant_id=OTHER_MERCHANT, store_id="store_foreign")

    me = _app(MERCHANT_USER)
    assert [r["jti"] for r in me.get("/merchant-events/v1/tokens/expiring").json()["tokens"]] == [mine["jti"]]
    staff = _app(EMPLOYEE)
    assert {r["jti"] for r in staff.get("/merchant-events/v1/tokens/expiring", params={"within_days": 30}).json()["tokens"]} == {mine["jti"], theirs["jti"]}
    assert _app({"role": "agent"}).get("/merchant-events/v1/tokens/expiring").status_code == 403

    assert me.post("/merchant-events/v1/stores/store_foreign/tokens/revoke-all", json={}).status_code == 403
    response = me.post(f"/merchant-events/v1/stores/{STORE_ID}/tokens/revoke-all", json={"reason": "rotate"})
    assert response.status_code == 200
    assert response.json()["min_token_version"] == 2 and response.json()["revoked_count"] == 1
    with pytest.raises(collector.WebCollectorError):
        await registry.enforce_token_registry(
            collector.verify_web_collector_token(mine["token"], request_origin=ORIGIN)
        )
    # The foreign store is untouched.
    await registry.enforce_token_registry(
        collector.verify_web_collector_token(theirs["token"], request_origin=ORIGIN)
    )
    # And the next issuance for this store carries the new generation.
    issued = me.post("/merchant-events/v1/web/install-token", json={"store_id": STORE_ID}).json()
    assert collector.verify_web_collector_token(issued["collector_token"], request_origin=ORIGIN)["sv"] == 2
