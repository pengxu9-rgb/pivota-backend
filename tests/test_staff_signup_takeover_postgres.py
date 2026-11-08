"""A public sign-up at an employee's address must not yield a staff login (real routes, Postgres).

The exposed state: an active employee with an active employee membership (written by
the identity backfill, or by employees_create when its users sync failed) and NO users
row at that address. Before the fix:
- POST /merchant/onboarding/register (store_less) created the users row with the
  caller's password, and so did the Shopify claim's new-account branch (with a claim
  token from the caller's own dev store);
- POST /api/auth/login then promoted that merchant row to staff because "this
  identity already holds an active employee membership", and issued role=admin with
  the employee_id.

Now both sign-ups refuse the address with 409 MERCHANT_SIGNIN_REQUIRED and create no
users row, and login promotes a non-staff users row only on the employee's own
legacy password.

Throwaway databases only (same markers as the other gate files). Every row here is
keyed by a fresh random address, so the shared dialect-gate database is safe.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
pytestmark = pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL")
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")

USERS_DDL = """
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    full_name VARCHAR(255),
    role VARCHAR(50) NOT NULL,
    merchant_id VARCHAR(50),
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    last_login TIMESTAMP WITH TIME ZONE
)
"""
EMPLOYEES_DDL = """
CREATE TABLE IF NOT EXISTS employees (
    employee_id VARCHAR(64) PRIMARY KEY, name VARCHAR(255), email VARCHAR(255),
    password VARCHAR(255), role VARCHAR(50), status VARCHAR(32), permissions JSONB,
    created_at TIMESTAMP DEFAULT NOW()
)
"""

ATTACKER_PW = "Attacker123pw"
EMPLOYEE_PW = "EmployeeOwn1pw"


def _legacy_employee_hash(plain: str) -> str:
    # The employees.password scheme routes.auth._verify_legacy_employee_password checks.
    return hashlib.sha256(f"{plain}pivota_employee_salt_v1".encode()).hexdigest()


def _addr(tag: str) -> str:
    return f"{tag}-{secrets.token_hex(4)}" + chr(64) + "example.com"


@pytest.fixture
async def db():
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    import db.agents as agents_db
    import db.auth_identity as ident
    import db.merchant_onboarding as mo
    from db.database import database

    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to write test rows to {dbname!r}; throwaway only")
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    for table in (agents_db.agents, mo.merchant_onboarding, ident.auth_identities,
                  ident.auth_credentials, ident.auth_memberships, ident.auth_identity_events):
        await database.execute(str(CreateTable(table, if_not_exists=True).compile(dialect=postgresql.dialect())))
    await database.execute(USERS_DDL)
    await database.execute(EMPLOYEES_DDL)
    yield database
    if not was_connected and database.is_connected:
        await database.disconnect()


@pytest.fixture
async def client(monkeypatch):
    import httpx

    import main
    import routes.merchant_onboarding_routes as mo_routes

    # Signup's welcome / verification side effects are not under test.
    monkeypatch.setattr(
        mo_routes.BackgroundTasks, "add_task", lambda self, *a, **k: None, raising=False
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as c:
        yield c


async def _seed_exposed_employee(db, email, *, employee_id, role="admin", status="active", membership=True):
    """An employees row plus (by default) an active employee membership, and no users row."""
    from db.auth_identity import upsert_membership

    await db.execute(
        "INSERT INTO employees (employee_id, name, email, password, role, status) "
        "VALUES (:i, 'Staff', :e, :p, :r, :s)",
        {"i": employee_id, "e": email, "p": _legacy_employee_hash(EMPLOYEE_PW), "r": role, "s": status},
    )
    if not membership:
        return
    await upsert_membership(
        email=email, membership_type="employee", role=role, entity_id=employee_id,
        status="active", source="employees_backfill",
    )


async def _users_rows(db, email):
    return await db.fetch_val("SELECT count(*) FROM users WHERE LOWER(email) = LOWER(:e)", {"e": email.strip()})


async def _login(client, email, password, portal):
    from utils.auth import decode_token

    body = {"email": email, "password": password}
    if portal:
        body["portal"] = portal
    resp = await client.post("/api/auth/login", json=body)
    if resp.status_code != 200:
        return {"refused": resp.status_code}
    return decode_token(resp.json()["token"])


def _signin_required(resp):
    assert resp.status_code == 409, (resp.status_code, resp.text)
    detail = resp.json()["detail"]
    assert detail["code"] == "MERCHANT_SIGNIN_REQUIRED", detail
    assert detail["resolution"] == "sign_in_or_reset_password", detail


def _store_less(email, password=ATTACKER_PW):
    return {
        "business_name": "Shop A",
        "region": "US",
        "contact_email": email,
        "password": password,
        "operating_mode": "store_less",
    }


# ---------------------------------------------------------------------------
# Lane 1: POST /merchant/onboarding/register (store_less)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status, membership",
    [("active", True), ("inactive", True), ("inactive", False)],
    ids=["active", "inactive", "inactive-no-membership"],
)
async def test_store_less_register_refuses_an_employee_address(db, client, status, membership):
    """Any status: an inactive employee can be reactivated after the address was taken."""
    staff = _addr("staff")
    emp_id = "emp_" + secrets.token_hex(4)
    await _seed_exposed_employee(db, staff, employee_id=emp_id, status=status, membership=membership)

    resp = await client.post("/merchant/onboarding/register", json=_store_less(staff.upper()))

    _signin_required(resp)
    assert await _users_rows(db, staff) == 0
    for portal in ("employee", None):
        claims = await _login(client, staff, ATTACKER_PW, portal)
        assert claims.get("role") not in ("admin", "employee", "super_admin"), (portal, claims)
        assert not claims.get("employee_id"), (portal, claims)


async def test_store_less_register_refuses_an_agent_owner_address(db, client):
    import db.agents as agents_db

    owner = _addr("agent-owner")
    await agents_db.create_agent(agent_name="Agent A", agent_type="custom", owner_email=owner)

    resp = await client.post("/merchant/onboarding/register", json=_store_less(owner))

    _signin_required(resp)
    assert await _users_rows(db, owner) == 0


async def test_store_less_register_refuses_an_identity_in_use(db, client):
    from db.auth_identity import upsert_membership
    from utils.auth import hash_password, verify_password

    addr = _addr("identity")
    seeded = await upsert_membership(
        email=addr, membership_type="agent", role="agent", entity_id="agent_" + secrets.token_hex(4),
        status="active", password_hash=hash_password("OwnerPass1x"), credential_source="probe", source="probe",
    )

    resp = await client.post("/merchant/onboarding/register", json=_store_less(addr))

    _signin_required(resp)
    assert await _users_rows(db, addr) == 0
    cred = await db.fetch_one(
        "SELECT password_hash FROM auth_credentials WHERE identity_id = :i",
        {"i": seeded["identity"]["identity_id"]},
    )
    assert verify_password("OwnerPass1x", cred["password_hash"])


async def test_store_less_register_of_a_fresh_address_still_works(db, client):
    addr = _addr("founder")
    resp = await client.post("/merchant/onboarding/register", json=_store_less(addr.title(), "Founder123x"))

    assert resp.status_code == 200, resp.text
    merchant_id = resp.json()["merchant_id"]
    assert await _users_rows(db, addr) == 1
    claims = await _login(client, addr, "Founder123x", "merchant")
    assert claims.get("merchant_id") == merchant_id and claims.get("role") == "merchant", claims


# ---------------------------------------------------------------------------
# Lane 2: POST /integrations/shopify/claim, new-account branch
# ---------------------------------------------------------------------------


async def _claim_token(shop):
    import routes.merchant_store_connections as msc

    await msc._ensure_shopify_oauth_tables()
    token = await msc._mint_store_claim_token(merchant_id="merch_shell_" + secrets.token_hex(4), shop_domain=shop)
    assert token
    return token


async def test_shopify_claim_refuses_an_employee_address(db, client):
    staff = _addr("staff")
    emp_id = "emp_" + secrets.token_hex(4)
    await _seed_exposed_employee(db, staff, employee_id=emp_id)
    token = await _claim_token(f"shop-a-{secrets.token_hex(3)}.myshopify.com")

    resp = await client.post(
        "/integrations/shopify/claim",
        json={"claim_token": token, "email": " " + staff.upper() + " ", "password": ATTACKER_PW},
    )

    _signin_required(resp)
    assert await _users_rows(db, staff) == 0
    for portal in ("employee", None):
        claims = await _login(client, staff, ATTACKER_PW, portal)
        assert claims.get("role") not in ("admin", "employee", "super_admin"), (portal, claims)
        assert not claims.get("employee_id"), (portal, claims)


async def test_shopify_claim_refuses_a_merchant_contact_with_no_login(db, client):
    """A staff-onboarded merchant has a contact address and no users row yet; the claim
    must not mint that login (the merchant-portal reset does, by emailing the address)."""
    contact = _addr("contact")
    await db.execute(
        "INSERT INTO merchant_onboarding (merchant_id, business_name, contact_email, status, created_at) "
        "VALUES (:m, 'Shop C', :e, 'approved', NOW())",
        {"m": "merch_c_" + secrets.token_hex(4), "e": contact},
    )
    token = await _claim_token(f"shop-c-{secrets.token_hex(3)}.myshopify.com")

    resp = await client.post(
        "/integrations/shopify/claim", json={"claim_token": token, "email": contact, "password": ATTACKER_PW}
    )

    _signin_required(resp)
    assert await _users_rows(db, contact) == 0


async def test_shopify_claim_of_a_fresh_address_still_creates_the_account(db, client):
    addr = _addr("installer")
    token = await _claim_token(f"shop-b-{secrets.token_hex(3)}.myshopify.com")

    resp = await client.post(
        "/integrations/shopify/claim", json={"claim_token": token, "email": addr, "password": "Installer1x"}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["role"] == "merchant"
    assert await _users_rows(db, addr) == 1


# ---------------------------------------------------------------------------
# The promotion: a non-staff users row at an employee's address
# ---------------------------------------------------------------------------


async def _seed_merchant_row_at(db, email, password):
    """The users row a pre-fix sign-up (or a merchant email move) left at the address."""
    from utils.auth import hash_password

    await db.execute(
        "INSERT INTO users (email, password_hash, full_name, role, merchant_id) "
        "VALUES (:e, :h, 'M', 'merchant', NULL)",
        {"e": email, "h": hash_password(password)},
    )


async def test_login_does_not_promote_a_merchant_row_on_an_employee_membership_alone(db, client):
    staff = _addr("staff")
    emp_id = "emp_" + secrets.token_hex(4)
    await _seed_exposed_employee(db, staff, employee_id=emp_id)
    await _seed_merchant_row_at(db, staff, ATTACKER_PW)

    employee_portal = await _login(client, staff, ATTACKER_PW, "employee")
    assert employee_portal == {"refused": 403}, employee_portal
    any_portal = await _login(client, staff, ATTACKER_PW, None)
    assert any_portal.get("role") not in ("admin", "employee", "super_admin"), any_portal
    assert not any_portal.get("employee_id"), any_portal
    row = await db.fetch_one("SELECT role FROM users WHERE LOWER(email) = LOWER(:e)", {"e": staff})
    assert row["role"] == "merchant"


async def test_login_promotes_on_the_employees_own_legacy_password(db, client):
    staff = _addr("staff")
    emp_id = "emp_" + secrets.token_hex(4)
    await _seed_exposed_employee(db, staff, employee_id=emp_id)
    await _seed_merchant_row_at(db, staff, ATTACKER_PW)

    claims = await _login(client, staff, EMPLOYEE_PW, "employee")

    assert claims.get("employee_id") == emp_id and claims.get("role") == "admin", claims
    # Proven for the employee, so the stored membership is used, not refused and rebuilt.
    refused = await db.fetch_val(
        "SELECT count(*) FROM auth_identity_events "
        "WHERE email_normalized = :e AND event_type = 'employee_promotion_refused'",
        {"e": staff},
    )
    assert refused == 0
    assert claims.get("membership_id") and not str(claims["membership_id"]).startswith("legacy:"), claims


# ---------------------------------------------------------------------------
# The identity-in-use check counts only live tenant memberships; that must not
# reopen the hole on any register lane.
# ---------------------------------------------------------------------------


async def _register(client, lane, email, monkeypatch):
    if lane == "api_register":
        return await client.post("/api/auth/register", json={"email": email, "password": ATTACKER_PW, "role": "merchant"})
    if lane == "agent_register":
        from routes import agent_account

        monkeypatch.setattr(agent_account, "_check_registration_rate_limit", lambda ip: True)
        monkeypatch.setattr(agent_account, "_self_serve_registration_enabled", lambda: True)
        return await client.post(
            "/agent/account/register", json={"email": email, "password": ATTACKER_PW, "agent_name": "A"}
        )
    return await client.post("/merchant/onboarding/register", json=_store_less(email))


LANES = ["api_register", "agent_register", "merchant_register"]


@pytest.mark.parametrize("lane", LANES)
async def test_an_inactive_employee_with_a_dead_membership_still_holds_the_address(db, client, monkeypatch, lane):
    """No live membership, so only the employees arm (any status) refuses: the employee
    can be reactivated, which re-activates the membership."""
    from db.auth_identity import upsert_membership

    staff = _addr("former")
    emp_id = "emp_" + secrets.token_hex(4)
    await _seed_exposed_employee(db, staff, employee_id=emp_id, status="inactive", membership=False)
    await upsert_membership(email=staff, membership_type="employee", role="admin", entity_id=emp_id,
                            status="inactive", source="employees_backfill")

    resp = await _register(client, lane, staff, monkeypatch)

    assert resp.status_code in (400, 409), (resp.status_code, resp.text)
    assert await _users_rows(db, staff) == 0


@pytest.mark.parametrize("lane", LANES)
async def test_a_live_employee_membership_without_an_employees_row_holds_the_address(db, client, monkeypatch, lane):
    from db.auth_identity import upsert_membership

    staff = _addr("orphan-staff")
    await upsert_membership(email=staff, membership_type="employee", role="admin",
                            entity_id="emp_" + secrets.token_hex(4), status="active", source="employees_backfill")

    resp = await _register(client, lane, staff, monkeypatch)

    assert resp.status_code in (400, 409), (resp.status_code, resp.text)
    assert await _users_rows(db, staff) == 0
    for portal in ("employee", None):
        claims = await _login(client, staff, ATTACKER_PW, portal)
        assert not claims.get("employee_id"), (portal, claims)


# ---------------------------------------------------------------------------
# Review R1: a buyer-only identity is not a tenant, so it may still sign up
# ---------------------------------------------------------------------------


async def test_d_buyer_identity_can_still_sign_up_as_merchant(db, client):
    """An address that only has a buyer (customer) identity, as every accounts OTP or
    password login writes, signs up as a merchant through the public onboarding route.
    Refusing it was a dead end: no users row to sign in with, and a merchant-portal
    reset sends nothing for an address with no merchant contact or membership."""
    from db.accounts import sync_customer_auth_membership

    buyer = _addr("buyer")
    await sync_customer_auth_membership({"id": "su_" + secrets.token_hex(4), "email": buyer, "primary_role": "customer"})

    resp = await client.post("/merchant/onboarding/register", json=_store_less(buyer, "Founder123x"))

    assert resp.status_code == 200, resp.text
    merchant_id = resp.json()["merchant_id"]
    claims = await _login(client, buyer, "Founder123x", "merchant")
    assert claims.get("merchant_id") == merchant_id and claims.get("role") == "merchant", claims


@pytest.mark.parametrize("lane", ["api_register", "merchant_register", "shopify_claim"])
async def test_a_buyer_with_a_buyer_password_can_sign_up_on_every_lane(db, client, monkeypatch, lane):
    """/agent/account/register gates on the same _email_claims_existing_tenant call as
    /api/auth/register (asserted below); its own agents INSERT needs columns the
    model-built agents table here does not default, so it is not driven end to end."""
    from db.accounts import sync_customer_auth_membership
    from routes import auth
    from utils.auth import hash_password, verify_password

    buyer = _addr("buyer-pw")
    seeded = await sync_customer_auth_membership(
        {"id": "su_" + secrets.token_hex(4), "email": buyer, "primary_role": "customer"},
        password_hash=hash_password("BuyerShop1pw"),
    )
    assert await auth._email_claims_existing_tenant(buyer) is False
    if lane == "shopify_claim":
        token = await _claim_token(f"shop-d-{secrets.token_hex(3)}.myshopify.com")
        resp = await client.post(
            "/integrations/shopify/claim", json={"claim_token": token, "email": buyer, "password": ATTACKER_PW}
        )
    else:
        resp = await _register(client, lane, buyer, monkeypatch)

    assert resp.status_code == 200, (lane, resp.status_code, resp.text)
    assert await _users_rows(db, buyer) == 1
    if lane == "api_register":
        # Insert-if-absent: the buyer's own password is kept on this lane.
        cred = await db.fetch_one("SELECT password_hash FROM auth_credentials WHERE identity_id = :i",
                                  {"i": seeded["identity"]["identity_id"]})
        assert verify_password("BuyerShop1pw", cred["password_hash"])


# ---------------------------------------------------------------------------
# Review R3: the claimed-address check runs before the claim token is spent
# ---------------------------------------------------------------------------


async def _token_used_at(token):
    import hashlib as _hashlib

    import routes.merchant_store_connections as msc
    from db.database import database

    jti = msc._verify_claim_token(token)["jti"]
    return await database.fetch_val(
        "SELECT used_at FROM shopify_store_claim_tokens WHERE jti_sha256 = :h",
        {"h": _hashlib.sha256(jti.encode("utf-8")).hexdigest()},
    )


async def test_a_refused_claim_leaves_the_token_usable_once_the_address_is_free(db, client):
    import db.agents as agents_db

    addr = _addr("installer-agent")
    created = await agents_db.create_agent(agent_name="Agent B", agent_type="custom", owner_email=addr)
    token = await _claim_token(f"shop-e-{secrets.token_hex(3)}.myshopify.com")
    body = {"claim_token": token, "email": addr, "password": "Installer1x"}

    refused = await client.post("/integrations/shopify/claim", json=body)
    _signin_required(refused)
    assert await _token_used_at(token) is None
    assert await _users_rows(db, addr) == 0

    # The address issue is fixed (the agent no longer names it): the same link works.
    await db.execute("DELETE FROM agents WHERE agent_id = :a", {"a": created["agent_id"]})
    ok = await client.post("/integrations/shopify/claim", json=body)
    assert ok.status_code == 200, ok.text
    assert await _token_used_at(token) is not None
    assert await _users_rows(db, addr) == 1

    replay = await client.post("/integrations/shopify/claim", json={**body, "email": _addr("other")})
    assert replay.status_code == 400 and "already been used" in replay.text


async def test_a_claim_token_is_consumed_exactly_once_under_concurrent_claims(db, client):
    import asyncio

    token = await _claim_token(f"shop-f-{secrets.token_hex(3)}.myshopify.com")
    first, second = _addr("racer-1"), _addr("racer-2")

    results = await asyncio.gather(
        client.post("/integrations/shopify/claim", json={"claim_token": token, "email": first, "password": "Racer1pass"}),
        client.post("/integrations/shopify/claim", json={"claim_token": token, "email": second, "password": "Racer2pass"}),
    )

    codes = sorted(r.status_code for r in results)
    assert codes == [200, 400], [(r.status_code, r.text[:200]) for r in results]
    assert await _users_rows(db, first) + await _users_rows(db, second) == 1
    assert await _token_used_at(token) is not None
