"""Postgres twin of tests/test_auth_takeover_followups.py: the SQL itself, for real.

The sqlite-lane tests answer queries with fakes, which cannot tell whether the
identity-in-use check or a LOWER() comparison is right. Here they run on Postgres:
- a stored Victim@Example.com plus its identity cannot be re-registered as
  victim@example.com, its identity password is untouched, and login with the
  attacker's password fails;
- an identity that has a password but no users row cannot be claimed;
- a fresh address registers and gets its identity's first password;
- squat an address, staff create an agent at it, convert: no agent membership, and
  agent login refuses;
- a membership written right after its users row counts as that row's (run the
  database with a non-UTC TimeZone locally to see the naive-timestamp bug);
- two logins at one address in different case: change-password refuses.

Throwaway databases only (same markers as the other gate files).
"""
from __future__ import annotations

import os
import secrets
import sys

import pytest
from fastapi import HTTPException

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
    if not was_connected:
        await database.disconnect()


def _addr(tag: str) -> str:
    return f"{tag}-{secrets.token_hex(4)}" + chr(64) + "example.com"


async def _identity_with_password_and_merchant(database, email, merchant_id):
    from db.auth_identity import upsert_membership
    from utils.auth import hash_password

    return await upsert_membership(
        email=email, membership_type="merchant", role="merchant", entity_id=merchant_id,
        password_hash=hash_password("VictimPass1"), credential_source="probe", source="probe",
    )


async def test_p1_mixed_case_victim_cannot_be_reregistered(db):
    from routes import auth
    from utils.auth import hash_password, verify_password

    victim = _addr("victim")
    stored = victim[0].upper() + victim[1:].replace("example", "Example")
    await db.execute(
        "INSERT INTO users (email, password_hash, full_name, role, merchant_id) VALUES (:e, :h, 'V', 'merchant', 'merch_victim')",
        {"e": stored, "h": hash_password("VictimPass1")},
    )
    membership = await _identity_with_password_and_merchant(db, victim, "merch_victim")

    with pytest.raises(HTTPException) as exc:
        await auth.register(auth.RegisterRequest(email=victim, password="Attacker1pass", role="merchant"), caller=None)
    assert exc.value.status_code == 400

    cred = await db.fetch_one("SELECT password_hash FROM auth_credentials WHERE identity_id = :i",
                              {"i": membership["identity"]["identity_id"]})
    assert verify_password("VictimPass1", cred["password_hash"])
    n = await db.fetch_val("SELECT count(*) FROM users WHERE LOWER(email) = LOWER(:e)", {"e": victim})
    assert n == 1

    with pytest.raises(HTTPException):
        await auth.login(auth.LoginRequest(email=victim, password="Attacker1pass", portal="merchant"))


async def test_p1_identity_without_users_row_is_not_claimable(db):
    from routes import auth
    from utils.auth import verify_password

    addr = _addr("orphan")
    membership = await _identity_with_password_and_merchant(db, addr, "merch_orphan")

    with pytest.raises(HTTPException) as exc:
        await auth.register(auth.RegisterRequest(email=addr, password="Attacker1pass", role="agent"), caller=None)
    assert exc.value.status_code == 400
    cred = await db.fetch_one("SELECT password_hash FROM auth_credentials WHERE identity_id = :i",
                              {"i": membership["identity"]["identity_id"]})
    assert verify_password("VictimPass1", cred["password_hash"])


async def test_p1_fresh_address_registers_and_gets_a_first_password(db):
    from routes import auth
    from utils.auth import verify_password

    addr = _addr("fresh")
    assert await auth._email_claims_existing_tenant(addr) is False
    await auth.register(auth.RegisterRequest(email=addr, password="Fresh1password", role="merchant"), caller=None)
    row = await db.fetch_one(
        "SELECT c.password_hash FROM auth_credentials c JOIN auth_identities i USING (identity_id) WHERE i.email_normalized = :e",
        {"e": addr},
    )
    assert row and verify_password("Fresh1password", row["password_hash"])
    assert await auth._email_claims_existing_tenant(addr) is True


async def test_p2_squat_then_staff_agent_then_conversion_links_nothing(db):
    import db.agents as agents_db
    import routes.agent_account as agent_account
    import routes.merchant_onboarding_routes as onb
    from db.auth_identity import has_active_membership_for_entity
    from routes import auth

    v = _addr("partner")
    await auth.register(auth.RegisterRequest(email=v, password="Squatter1pass", role="agent"), caller=None)
    created = await agents_db.create_agent(agent_name="Partner", agent_type="custom", owner_email=v)
    agent_id = created["agent_id"]

    assert await onb.keep_converted_agent_memberships(v) == []
    assert not await has_active_membership_for_entity(email=v, membership_type="agent", entity_id=agent_id)

    with pytest.raises(HTTPException) as exc:
        await agent_account.login_agent(agent_account.AgentLoginRequest(email=v, password="Squatter1pass"))
    assert exc.value.status_code == 403, exc.value.detail


async def test_p3_membership_written_after_the_users_row_reads_as_newer_off_utc(db):
    import db.agents as agents_db
    import routes.agent_account as agent_account
    from db.auth_identity import upsert_membership
    from routes import auth

    w = _addr("legit")
    await auth.register(auth.RegisterRequest(email=w, password="Legit1password", role="agent"), caller=None)
    created = await agents_db.create_agent(agent_name="Legit", agent_type="custom", owner_email=w)
    await upsert_membership(email=w, membership_type="agent", role="agent",
                            entity_id=created["agent_id"], source="agent_account_sync")
    user = await db.fetch_one("SELECT id, email, role, created_at FROM users WHERE email = :e", {"e": w})

    assert await agent_account._holds_agent_membership(dict(user), {"agent_id": created["agent_id"]})


async def test_p1_change_password_refuses_case_variant_logins(db):
    from routes import auth
    from utils.auth import hash_password

    a = _addr("dup")
    await db.execute("INSERT INTO users (email, password_hash, role) VALUES (:e, :h, 'merchant')",
                     {"e": a, "h": hash_password("Oldpassword1")})
    await db.execute("INSERT INTO users (email, password_hash, role) VALUES (:e, :h, 'merchant')",
                     {"e": a.upper(), "h": hash_password("Other1password")})
    with pytest.raises(HTTPException) as exc:
        await auth.change_password(
            auth.ChangePasswordRequest(current_password="Oldpassword1", new_password="Newpassword1"),
            current_user={"email": a},
        )
    assert exc.value.status_code == 409


# ---------------------------------------------------------------------------
# Review follow-ups: N2 (identity in use = live tenant membership), N4 (login
# picks the oldest of a case-split pair), M8 (reset rewrites a mixed-case row)
# ---------------------------------------------------------------------------


async def test_n2_a_buyer_only_identity_may_register_and_keeps_its_buyer_password(db):
    from db.auth_identity import upsert_membership
    from routes import auth
    from utils.auth import hash_password, verify_password

    buyer = _addr("buyer")
    seeded = await upsert_membership(
        email=buyer, membership_type="customer", role="customer", entity_id="buyer_" + secrets.token_hex(4),
        password_hash=hash_password("BuyerPass1x"), credential_source="shop_user_passwords_backfill",
        source="probe",
    )
    assert await auth._email_claims_existing_tenant(buyer) is False

    await auth.register(auth.RegisterRequest(email=buyer, password="Merchant1pass", role="merchant"), caller=None)

    cred = await db.fetch_one("SELECT password_hash FROM auth_credentials WHERE identity_id = :i",
                              {"i": seeded["identity"]["identity_id"]})
    assert verify_password("BuyerPass1x", cred["password_hash"])  # insert-if-absent kept it
    assert await db.fetch_val("SELECT count(*) FROM users WHERE LOWER(email) = LOWER(:e)", {"e": buyer}) == 1


async def test_n2_dead_memberships_do_not_hold_the_address_or_its_tenant(db):
    from db.auth_identity import upsert_membership
    from routes import auth
    from utils.auth import decode_token, hash_password

    gone = _addr("gone")
    old_merchant = "merch_gone_" + secrets.token_hex(4)
    await upsert_membership(
        email=gone, membership_type="merchant", role="merchant", entity_id=old_merchant, status="inactive",
        password_hash=hash_password("OldOwner1pw"), credential_source="probe", source="probe",
    )
    assert await auth._email_claims_existing_tenant(gone) is False

    await auth.register(auth.RegisterRequest(email=gone, password="NewOwner1pw", role="merchant"), caller=None)

    try:
        out = await auth.login(auth.LoginRequest(email=gone, password="NewOwner1pw", portal="merchant"))
        claims = decode_token(out.token)
    except HTTPException as exc:
        claims = {"refused": exc.status_code}
    assert claims.get("merchant_id") != old_merchant, claims


@pytest.mark.parametrize("membership_type", ["merchant", "agent", "employee"])
async def test_n2_a_live_tenant_membership_still_holds_the_address(db, membership_type):
    from db.auth_identity import upsert_membership
    from routes import auth

    held = _addr("held")
    await upsert_membership(
        email=held, membership_type=membership_type, role=membership_type if membership_type != "employee" else "admin",
        entity_id=f"{membership_type}_" + secrets.token_hex(4), status="active", source="probe",
    )
    assert await auth._email_claims_existing_tenant(held) is True
    with pytest.raises(HTTPException) as exc:
        await auth.register(auth.RegisterRequest(email=held, password="Attacker1pass", role="merchant"), caller=None)
    assert exc.value.status_code == 400


async def test_n4_login_reads_the_oldest_row_of_a_case_split_pair(db):
    """Two legacy rows at one address in different case. A LOWER() match can't use the
    unique email index, so without ORDER BY the row read follows heap order; the newer
    id is inserted first here so heap order and id order disagree."""
    from routes import auth
    from utils.auth import hash_password

    a = _addr("split")
    base = 700_000_000 + secrets.randbelow(100_000_000)
    await db.execute(
        "INSERT INTO users (id, email, password_hash, full_name, role) VALUES (:i, :e, :h, 'N', 'merchant')",
        {"i": base + 1, "e": a.upper(), "h": hash_password("NewerRow1pw")},
    )
    await db.execute(
        "INSERT INTO users (id, email, password_hash, full_name, role) VALUES (:i, :e, :h, 'O', 'merchant')",
        {"i": base, "e": a, "h": hash_password("OlderRow1pw")},
    )
    outcomes = {}
    for pw in ("OlderRow1pw", "NewerRow1pw"):
        try:
            await auth.login(auth.LoginRequest(email=a, password=pw))
            outcomes[pw] = 200
        except HTTPException as exc:
            outcomes[pw] = exc.status_code
    assert outcomes["NewerRow1pw"] == 401, outcomes
    assert outcomes["OlderRow1pw"] != 401, outcomes


async def test_m8_reset_rewrites_a_mixed_case_stored_login(db, monkeypatch):
    from db.auth_identity import upsert_membership
    from routes import auth
    from utils.auth import hash_password

    sent = {}
    monkeypatch.setattr(auth, "_send_reset_password_email", lambda e, link: sent.__setitem__(e, link.split("token=")[1]))
    lower = _addr("legit")
    stored = lower[0].upper() + lower[1:].replace("example", "Example")
    merchant_id = "merch_legit_" + secrets.token_hex(4)
    await db.execute(
        "INSERT INTO users (email, password_hash, full_name, role, merchant_id) VALUES (:e, :h, 'L', 'merchant', :m)",
        {"e": stored, "h": hash_password("Before1reset"), "m": merchant_id},
    )
    await upsert_membership(email=stored, membership_type="merchant", role="merchant", entity_id=merchant_id,
                            password_hash=hash_password("Before1reset"), credential_source="probe", source="probe")
    await db.execute(
        "INSERT INTO merchant_onboarding (merchant_id, business_name, contact_email, status, created_at) "
        "VALUES (:m, 'L', :e, 'approved', NOW())",
        {"m": merchant_id, "e": stored},
    )

    await auth.forgot_password(auth.ForgotPasswordRequest(email=stored, portal="merchant"))
    token = sent.get(lower)
    assert token, sent
    await auth.reset_password(auth.ResetPasswordRequest(token=token, new_password="After1resetx"))

    row = await db.fetch_one("SELECT password_hash FROM users WHERE email = :e", {"e": stored})
    from utils.auth import verify_password

    assert verify_password("After1resetx", row["password_hash"])
    with pytest.raises(HTTPException):
        await auth.login(auth.LoginRequest(email=lower, password="Before1reset", portal="merchant"))
