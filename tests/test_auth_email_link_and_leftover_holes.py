"""Tenant links are never inferred from an unverified email, plus the leftover
tenant-scope holes from the B2 review (F1, F2 and the P2 routes).

F3. /api/auth/register proved no mailbox, and login then resolved merchant_id from
merchant_onboarding.contact_email and agent_id from agents.owner_email. Registering a
staff-created agent's or merchant's address therefore yielded a verified token for
that tenant. Pinned here:
- register (/api/auth/register and /agent/account/register) refuses an address that
  already names an agent owner, a merchant contact or an active employee, and fails
  closed when it cannot tell;
- /api/auth/login links a merchant only through users.merchant_id and an agent only
  through an agent membership written after the users row existed; a membership left
  by an earlier account at the same email is ignored;
- /agent/account/login refuses role='agent' whose agent is matched by owner_email only;
- merchant signup that reuses an existing login must prove its password, and does not
  mint a login for an onboarding row that has none;
- the password reset (mailbox proof) records the links: users.merchant_id, agent
  memberships, and an agent-portal reset bootstraps the users row for an agent owner.
F1. Merchant trends ignore ?merchant_id= for a merchant token.
F2. GET /merchants/ is admin-only.
P2. sync-all-platforms, quality eval, enrichment run, GSC OAuth start/status need the
    merchant itself or staff.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from routes import auth
from utils.auth import create_access_token

EMAIL = "owner@partner.example"
CREATED = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)


def _token(**claims) -> str:
    base = {"sub": claims.get("email", "x@example.com"), "email": "x@example.com"}
    base.update(claims)
    return create_access_token(base)


def _bearer(**claims) -> dict:
    return {"Authorization": f"Bearer {_token(**claims)}"}


MERCHANT_A = {"role": "merchant", "merchant_id": "merch_a", "email": "a@example.com"}
MERCHANT_NO_CLAIM = {"role": "merchant", "email": "noclaim@example.com"}
AGENT = {"role": "agent", "agent_id": "agent_x", "email": "agent@example.com"}
ADMIN = {"role": "admin", "email": "admin@pivota.example"}
EMPLOYEE = {"role": "employee", "email": "emp@pivota.example", "membership_type": "employee"}


# ---------------------------------------------------------------------------
# F3: registration refuses addresses that already stand for a tenant
# ---------------------------------------------------------------------------


class _RegisterDB:
    def __init__(self, *, owns: str | None = None, broken: bool = False):
        self.owns = owns
        self.broken = broken
        self.inserted = []

    async def fetch_one(self, query, values=None):
        q = " ".join(str(query).split())
        if q.startswith("SELECT id FROM users"):
            return None
        if self.broken and q.startswith("SELECT 1 FROM"):
            raise RuntimeError("connection reset")
        if q.startswith("SELECT 1 FROM agents"):
            return {"?column?": 1} if self.owns == "agent" else None
        if q.startswith("SELECT 1 FROM merchant_onboarding"):
            return {"?column?": 1} if self.owns == "merchant" else None
        if q.startswith("SELECT 1 FROM employees"):
            return {"?column?": 1} if self.owns == "employee" else None
        return None

    async def fetch_val(self, query, values=None):
        self.inserted.append(values)
        return 1


def _patch_register(monkeypatch, db):
    monkeypatch.setattr(auth.database, "fetch_one", db.fetch_one)
    monkeypatch.setattr(auth.database, "fetch_val", db.fetch_val)

    async def _none(**_kwargs):
        return None

    monkeypatch.setattr(auth, "_safe_ensure_identity", _none)


def _register_request(role="agent"):
    return auth.RegisterRequest(email=EMAIL, password="Longenough1", role=role)


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["agent", "merchant", "employee"])
@pytest.mark.parametrize("role", ["agent", "merchant"])
async def test_register_refuses_an_email_that_already_names_a_tenant(monkeypatch, owner, role):
    db = _RegisterDB(owns=owner)
    _patch_register(monkeypatch, db)

    with pytest.raises(HTTPException) as exc:
        await auth.register(_register_request(role), caller=None)

    assert exc.value.status_code == 400
    assert exc.value.detail == auth.EMAIL_ALREADY_REGISTERED_DETAIL
    assert db.inserted == []


@pytest.mark.asyncio
async def test_register_still_accepts_a_fresh_email(monkeypatch):
    db = _RegisterDB(owns=None)
    _patch_register(monkeypatch, db)

    resp = await auth.register(_register_request("merchant"), caller=None)

    assert resp.success is True
    assert db.inserted and db.inserted[0]["email"] == EMAIL


@pytest.mark.asyncio
async def test_register_fails_closed_when_the_tenant_lookup_breaks(monkeypatch):
    db = _RegisterDB(broken=True)
    _patch_register(monkeypatch, db)

    with pytest.raises(HTTPException) as exc:
        await auth.register(_register_request("agent"), caller=None)

    assert exc.value.status_code == 503
    assert db.inserted == []


def test_agent_self_registration_refuses_a_staff_created_agents_owner_email(monkeypatch):
    import routes.agent_account as module

    db = _RegisterDB(owns="agent")
    executed = []

    async def fetch_one(query, values=None):
        q = " ".join(str(query).split())
        if q.startswith("SELECT id FROM users"):
            return None
        return await db.fetch_one(query, values)

    async def execute(query, values=None):
        executed.append(query)

    monkeypatch.setattr(module, "_self_serve_registration_enabled", lambda: True)
    monkeypatch.setattr(module, "_check_registration_rate_limit", lambda _ip: True)
    monkeypatch.setattr(module.database, "fetch_one", fetch_one)
    monkeypatch.setattr(module.database, "execute", execute)
    monkeypatch.setattr(auth.database, "fetch_one", fetch_one)

    app = FastAPI()
    app.include_router(module.router)
    resp = TestClient(app).post(
        "/agent/account/register",
        json={"email": EMAIL, "password": "Longenough1", "agent_name": "Claimer"},
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == auth.EMAIL_ALREADY_REGISTERED_DETAIL
    assert not any("INSERT INTO users" in q for q in executed)


# ---------------------------------------------------------------------------
# F3: /api/auth/login links tenants only through stored, proven links
# ---------------------------------------------------------------------------


def _users_row(role, merchant_id=None, created_at=CREATED):
    return {
        "id": 42,
        "email": EMAIL,
        "password_hash": "hash",
        "full_name": "Owner",
        "role": role,
        "active": True,
        "merchant_id": merchant_id,
        "created_at": created_at,
    }


def _patch_login(monkeypatch, *, user, membership=None, agent_rows=(), linked_agents=()):
    seen = {"fetch_one": [], "upserts": [], "membership_checks": []}

    async def fetch_one(query, values=None):
        q = " ".join(str(query).split())
        seen["fetch_one"].append(q)
        if "FROM users" in q:
            return user
        if "FROM merchant_onboarding" in q:
            # The email match login used to trust: a staff-onboarded merchant.
            return {"merchant_id": "merch_victim"}
        if "FROM agents" in q:
            return {"agent_id": "agent_victim"}
        return None

    async def ready():
        return None

    async def execute(_q, _v):
        return None

    async def fetch_all(query, values=None):
        return [{"agent_id": a} for a in agent_rows]

    async def get_membership(_email, _type):
        return membership

    async def upsert(**kwargs):
        seen["upserts"].append(kwargs)
        return None

    async def none(*_a, **_k):
        return None

    async def has_membership(*, email, membership_type, entity_id, not_before=None):
        seen["membership_checks"].append((entity_id, not_before))
        return entity_id in linked_agents

    import db.auth_identity as auth_identity

    monkeypatch.setattr(auth, "_ensure_auth_database_ready", ready)
    monkeypatch.setattr(auth, "_auth_fetch_one", fetch_one)
    monkeypatch.setattr(auth, "_auth_execute", execute)
    monkeypatch.setattr(auth.database, "fetch_all", fetch_all)
    monkeypatch.setattr(auth, "verify_password", lambda _p, _h: True)
    monkeypatch.setattr(auth, "_safe_get_active_membership", get_membership)
    monkeypatch.setattr(auth, "_safe_upsert_membership", upsert)
    monkeypatch.setattr(auth, "_safe_verify_identity_password", none)
    monkeypatch.setattr(auth, "_safe_upsert_credential_hash", none)
    monkeypatch.setattr(auth, "_safe_record_identity_event", none)
    monkeypatch.setattr(auth_identity, "has_active_membership_for_entity", has_membership)
    return seen


def _login(portal=None):
    return auth.login(auth.LoginRequest(email=EMAIL, password="Longenough1", portal=portal))


@pytest.mark.asyncio
async def test_merchant_login_does_not_link_a_merchant_by_contact_email(monkeypatch):
    seen = _patch_login(monkeypatch, user=_users_row("merchant", merchant_id=None))

    with pytest.raises(HTTPException) as exc:
        await _login()

    assert exc.value.status_code == 403
    assert not any("merch_victim" == u.get("entity_id") for u in seen["upserts"])
    assert not any("FROM merchant_onboarding" in q for q in seen["fetch_one"])


@pytest.mark.asyncio
async def test_merchant_login_uses_the_users_rows_own_merchant_id(monkeypatch):
    _patch_login(monkeypatch, user=_users_row("merchant", merchant_id="merch_own"))

    resp = await _login()

    assert resp.user["merchant_id"] == "merch_own"
    assert resp.user["membership_type"] == "merchant"


@pytest.mark.asyncio
async def test_agent_login_does_not_link_an_agent_by_owner_email(monkeypatch):
    seen = _patch_login(
        monkeypatch, user=_users_row("agent"), agent_rows=["agent_victim"], linked_agents=()
    )

    with pytest.raises(HTTPException) as exc:
        await _login()

    assert exc.value.status_code == 403
    assert not any(u.get("entity_id") == "agent_victim" for u in seen["upserts"])
    # The check is bound to this users row's age (minus the clock allowance).
    assert seen["membership_checks"] == [("agent_victim", CREATED - auth._MEMBERSHIP_CLOCK_SKEW)]


@pytest.mark.asyncio
async def test_agent_login_links_an_agent_with_a_proven_membership(monkeypatch):
    seen = _patch_login(
        monkeypatch,
        user=_users_row("agent"),
        agent_rows=["agent_other", "agent_mine"],
        linked_agents=("agent_mine",),
    )

    resp = await _login()

    assert resp.user["agent_id"] == "agent_mine"
    assert any(u.get("entity_id") == "agent_mine" for u in seen["upserts"])


@pytest.mark.asyncio
async def test_login_ignores_a_membership_older_than_the_users_row(monkeypatch):
    stale = {
        "membership_id": "m_old",
        "membership_type": "merchant",
        "role": "merchant",
        "entity_id": "merch_previous_owner",
        "created_at": datetime(2026, 6, 1),
        "updated_at": datetime(2026, 6, 2),
        "identity": {"identity_id": "id_1", "email": EMAIL},
    }
    _patch_login(monkeypatch, user=_users_row("merchant", merchant_id=None), membership=stale)

    with pytest.raises(HTTPException) as exc:
        await _login()

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_login_keeps_a_membership_written_after_the_users_row(monkeypatch):
    fresh = {
        "membership_id": "m_new",
        "membership_type": "merchant",
        "role": "merchant",
        "entity_id": "merch_own",
        # naive = UTC, as upsert_membership writes utcnow(); 30s after the row
        "created_at": datetime(2026, 11, 1, 12, 0, 30),
        "updated_at": datetime(2026, 11, 1, 12, 0, 30),
        "identity": {"identity_id": "id_1", "email": EMAIL},
    }
    _patch_login(monkeypatch, user=_users_row("merchant", merchant_id=None), membership=fresh)

    resp = await _login()

    assert resp.user["merchant_id"] == "merch_own"


def test_membership_predates_user_allows_clock_skew():
    user = _users_row("agent")
    just_before = {"created_at": CREATED - timedelta(seconds=10), "updated_at": None}
    long_before = {"created_at": CREATED - timedelta(minutes=10), "updated_at": None}
    assert auth._membership_predates_user(just_before, user) is False
    assert auth._membership_predates_user(long_before, user) is True
    # unknown row age: cannot tell, keep the old behaviour
    assert auth._membership_predates_user(long_before, {"created_at": None}) is False


# ---------------------------------------------------------------------------
# F3: /agent/account/login needs the proven link for role='agent'
# ---------------------------------------------------------------------------


def _agent_login_client(monkeypatch, *, linked, agents=("agent_staff_made",)):
    """linked: True/False for agent_staff_made, or the set of agent ids the login holds."""
    linked_ids = set(linked) if isinstance(linked, (set, frozenset, list, tuple)) else (
        {"agent_staff_made"} if linked else set()
    )
    import db.auth_identity as auth_identity
    import db.agents as agents_db
    import routes.agent_account as module
    from utils.auth import hash_password

    checks = []

    async def fetch_one(query, values=None):
        q = " ".join(str(query).split())
        if q.startswith("SELECT id, email, password_hash"):
            return {
                "id": 9,
                "email": EMAIL,
                "password_hash": hash_password("Longenough1"),
                "full_name": "Claimer",
                "role": "agent",
                "active": True,
                "created_at": CREATED,
            }
        return None

    async def fetch_all(query, values=None):
        q = " ".join(str(query).split())
        if "FROM agents" in q and "owner_email" in q:
            return [
                {
                    "agent_id": a,
                    "agent_name": "Partner",
                    "owner_email": EMAIL,
                    "api_key": f"redacted:{a}",
                    "agent_type": "basic",
                }
                for a in agents
            ]
        return []

    async def execute(query, values=None):
        return None

    async def has_membership(*, email, membership_type, entity_id, not_before=None):
        checks.append((entity_id, not_before))
        return entity_id in linked_ids

    async def no_sync(**_kwargs):
        return None

    monkeypatch.setattr(module.database, "fetch_one", fetch_one)
    monkeypatch.setattr(module.database, "fetch_all", fetch_all)
    monkeypatch.setattr(module.database, "execute", execute)
    monkeypatch.setattr(module, "_sync_agent_auth_membership", no_sync)
    monkeypatch.setattr(auth_identity, "has_active_membership_for_entity", has_membership)
    monkeypatch.setattr(agents_db, "IS_POSTGRES", True)
    app = FastAPI()
    app.include_router(module.router)
    return TestClient(app), checks


def test_agent_portal_login_refuses_an_owner_email_match_without_a_membership(monkeypatch):
    client, checks = _agent_login_client(monkeypatch, linked=False)

    resp = client.post("/agent/account/login", json={"email": EMAIL, "password": "Longenough1"})

    assert resp.status_code == 403, resp.text
    assert "not linked" in resp.json()["detail"]
    assert "token" not in resp.json()
    assert checks == [("agent_staff_made", CREATED - timedelta(seconds=60))]


def test_agent_portal_login_admits_the_linked_owner(monkeypatch):
    client, _ = _agent_login_client(monkeypatch, linked=True)

    resp = client.post("/agent/account/login", json={"email": EMAIL, "password": "Longenough1"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["agent"]["agent_id"] == "agent_staff_made"


# ---------------------------------------------------------------------------
# F3: merchant signup must not reset someone else's login
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merchant_signup_reusing_a_login_needs_its_current_password():
    import routes.merchant_onboarding_routes as module
    from utils.auth import hash_password

    existing = {"id": 3, "email": EMAIL, "role": "merchant", "merchant_id": "merch_v",
                "password_hash": hash_password("TheOwnersPassword1")}

    with pytest.raises(HTTPException) as exc:
        await module.resolve_public_merchant_identity_merge(
            existing_user=existing, existing_onboarding=None, entered_password="AttackerChosen1",
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "MERCHANT_SIGNIN_REQUIRED"
    assert "merchant_id" not in exc.value.detail

    user, merge = await module.resolve_public_merchant_identity_merge(
        existing_user=existing, existing_onboarding=None, entered_password="TheOwnersPassword1",
    )
    assert user is existing and merge is None


@pytest.mark.asyncio
async def test_merchant_signup_does_not_mint_a_login_for_an_onboarding_without_one(monkeypatch):
    import routes.merchant_onboarding_routes as module
    from routes.merchant_onboarding_routes import MerchantRegisterRequest

    synced = []

    async def execute(*_a, **_k):
        return None

    async def onboarding_by_email(_email):
        return {"merchant_id": "merch_staff_made", "store_url": "victim.example", "status": "approved"}

    async def no_user(_email):
        return None

    async def sync_user(**kwargs):
        synced.append(kwargs)

    monkeypatch.setattr(module.database, "execute", execute)
    monkeypatch.setattr(module, "get_active_onboarding_by_email", onboarding_by_email)
    monkeypatch.setattr(module, "get_user_auth_binding", no_user)
    monkeypatch.setattr(module, "sync_merchant_auth_user", sync_user)

    class _BG:
        def add_task(self, *a, **k):
            pass

    req = MerchantRegisterRequest(
        business_name="Victim Co",
        store_url="https://victim.example",
        region="US",
        contact_email=EMAIL,
        password="AttackerChosen1",
    )
    with pytest.raises(HTTPException) as exc:
        await module.register_merchant(req, _BG())

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "MERCHANT_SIGNIN_REQUIRED"
    assert synced == []


# ---------------------------------------------------------------------------
# F3: the password reset proves the mailbox and records the links
# ---------------------------------------------------------------------------


class _ResetDB:
    def __init__(self, *, user, onboarding=None, agents=()):
        self.user = user
        self.onboarding = onboarding
        self.agents = list(agents)
        self.executed = []

    async def fetch_one(self, query, values=None):
        q = " ".join(str(query).split())
        if q.startswith("SELECT email, role, merchant_id FROM users"):
            return self.user
        if "FROM merchant_onboarding" in q:
            return self.onboarding
        return None

    async def fetch_all(self, query, values=None):
        return [{"agent_id": a, "agent_name": a} for a in self.agents]

    async def execute(self, query, values=None):
        self.executed.append((" ".join(str(query).split()), dict(values or {})))


@pytest.mark.asyncio
async def test_merchant_reset_links_the_onboarding_by_contact_email(monkeypatch):
    db = _ResetDB(user={"email": EMAIL, "role": "merchant", "merchant_id": None},
                  onboarding={"merchant_id": "merch_own"})
    monkeypatch.setattr(auth.database, "fetch_one", db.fetch_one)
    monkeypatch.setattr(auth.database, "execute", db.execute)

    await auth._link_tenants_after_mailbox_proof(EMAIL, "merchant")

    updates = [v for q, v in db.executed if q.startswith("UPDATE users SET merchant_id")]
    assert updates == [{"merchant_id": "merch_own", "email": EMAIL}]


@pytest.mark.asyncio
async def test_agent_reset_records_an_agent_membership(monkeypatch):
    db = _ResetDB(user={"email": EMAIL, "role": "agent", "merchant_id": None},
                  agents=["agent_staff_made"])
    upserts = []

    async def upsert(**kwargs):
        upserts.append(kwargs)

    monkeypatch.setattr(auth.database, "fetch_one", db.fetch_one)
    monkeypatch.setattr(auth.database, "fetch_all", db.fetch_all)
    monkeypatch.setattr(auth, "_safe_upsert_membership", upsert)

    await auth._link_tenants_after_mailbox_proof(EMAIL, "agent")

    assert [(u["membership_type"], u["entity_id"], u["source"]) for u in upserts] == [
        ("agent", "agent_staff_made", "password_reset_mailbox_proof")
    ]


@pytest.mark.asyncio
async def test_reset_for_another_portal_records_no_link(monkeypatch):
    db = _ResetDB(user={"email": EMAIL, "role": "agent", "merchant_id": None},
                  agents=["agent_staff_made"])
    upserts = []

    async def upsert(**kwargs):
        upserts.append(kwargs)

    monkeypatch.setattr(auth.database, "fetch_one", db.fetch_one)
    monkeypatch.setattr(auth.database, "fetch_all", db.fetch_all)
    monkeypatch.setattr(auth, "_safe_upsert_membership", upsert)

    await auth._link_tenants_after_mailbox_proof(EMAIL, "merchant")

    assert upserts == []


@pytest.mark.asyncio
async def test_agent_portal_forgot_password_bootstraps_the_owner_login(monkeypatch):
    executed = []
    sent = []

    async def fetch_one(query, values=None):
        return None  # no users row

    async def execute(query, values=None):
        executed.append((" ".join(str(query).split()), dict(values or {})))

    async def no_employee(_email):
        return None

    async def owns_agent(_email):
        return "agent_staff_made"

    async def has_membership(*_a, **_k):
        return True

    async def table_ready():
        return None

    monkeypatch.setattr(auth.database, "fetch_one", fetch_one)
    monkeypatch.setattr(auth.database, "execute", execute)
    monkeypatch.setattr(auth, "_fetch_active_employee_identity", no_employee)
    monkeypatch.setattr(auth, "_resolve_agent_id_for_email", owns_agent)
    monkeypatch.setattr(auth, "_email_has_portal_membership", has_membership)
    monkeypatch.setattr(auth, "_ensure_password_reset_tokens_table", table_ready)
    monkeypatch.setattr(auth, "_send_reset_password_email", lambda email, link: sent.append(email))

    await auth.forgot_password(auth.ForgotPasswordRequest(email=EMAIL, portal="agent"))

    user_inserts = [v for q, v in executed if q.startswith("INSERT INTO users")]
    assert len(user_inserts) == 1
    assert user_inserts[0]["role"] == "agent" and user_inserts[0]["email"] == EMAIL
    assert "merchant_id" not in user_inserts[0]
    assert sent == [EMAIL]


# ---------------------------------------------------------------------------
# F1: merchant trends ignore ?merchant_id= for a merchant token
# ---------------------------------------------------------------------------


class _RecordingCache(dict):
    def __init__(self):
        super().__init__()
        self.keys_seen = []

    def __contains__(self, key):
        self.keys_seen.append(key)
        return True

    def __getitem__(self, key):
        return ({"merchant_id_served": key.split("|")[0]}, time.time())


def _trends_client(monkeypatch, principal):
    import routes.merchant_analytics_routes as module

    cache = _RecordingCache()
    monkeypatch.setattr(module, "TRENDS_CACHE", cache)
    app = FastAPI()
    app.include_router(module.router)
    app.dependency_overrides[module._get_principal] = lambda: principal
    return TestClient(app), cache


@pytest.mark.parametrize("path", ["/merchant/analytics/trends", "/merchant/dashboard/trends",
                                  "/merchant/analytics/trends.csv"])
def test_trends_refuse_a_merchant_token_without_a_claim(monkeypatch, path):
    client, cache = _trends_client(monkeypatch, {"role": "merchant", "email": "x@example.com"})

    resp = client.get(path, params={"merchant_id": "merch_victim"})

    assert resp.status_code == 403, resp.text
    assert cache.keys_seen == []


@pytest.mark.parametrize("path", ["/merchant/analytics/trends", "/merchant/dashboard/trends"])
def test_trends_ignore_the_query_for_a_merchant_token(monkeypatch, path):
    client, cache = _trends_client(monkeypatch, {"role": "merchant", "merchant_id": "merch_a"})

    resp = client.get(path, params={"merchant_id": "merch_victim"})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"merchant_id_served": "merch_a"}


def test_trends_staff_still_choose_the_merchant(monkeypatch):
    client, _ = _trends_client(monkeypatch, {"role": "admin"})

    resp = client.get("/merchant/analytics/trends", params={"merchant_id": "merch_victim"})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"merchant_id_served": "merch_victim"}


# ---------------------------------------------------------------------------
# F2: GET /merchants/ is admin-only
# ---------------------------------------------------------------------------


def _merchants_client(monkeypatch):
    import routes.merchant_routes as module

    async def all_merchants(_status=None):
        return [{"id": 1, "contact_email": "pii@example.com", "contact_phone": "+1555"}]

    monkeypatch.setattr(module, "get_all_merchants", all_merchants)
    app = FastAPI()
    app.include_router(module.router)
    return TestClient(app)


@pytest.mark.parametrize("claims", [MERCHANT_A, AGENT, EMPLOYEE])
def test_merchants_list_refuses_non_admins(monkeypatch, claims):
    resp = _merchants_client(monkeypatch).get("/merchants/", headers=_bearer(**claims))

    assert resp.status_code == 403, resp.text
    assert "pii@example.com" not in resp.text


def test_merchants_list_serves_admins(monkeypatch):
    resp = _merchants_client(monkeypatch).get("/merchants/", headers=_bearer(**ADMIN))

    assert resp.status_code == 200, resp.text
    assert resp.json()["count"] == 1


# ---------------------------------------------------------------------------
# P2: body/query merchant routes need that merchant or staff
# ---------------------------------------------------------------------------


def _app(*routers):
    app = FastAPI()
    for r in routers:
        app.include_router(r)
    return TestClient(app)


def _sync_client(monkeypatch):
    import routes.sync_all_platforms as module

    async def no_merchant(_mid):
        return None

    monkeypatch.setattr(module, "get_merchant_onboarding", no_merchant)
    return _app(module.router)


@pytest.mark.parametrize("claims", [MERCHANT_A, MERCHANT_NO_CLAIM, AGENT])
def test_sync_all_platforms_refuses_another_merchants_sync(monkeypatch, claims):
    resp = _sync_client(monkeypatch).post(
        "/products/sync-all-platforms/", json={"merchant_id": "merch_victim"}, headers=_bearer(**claims)
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.parametrize("claims", [MERCHANT_A, ADMIN, EMPLOYEE])
def test_sync_all_platforms_admits_the_merchant_and_staff(monkeypatch, claims):
    resp = _sync_client(monkeypatch).post(
        "/products/sync-all-platforms/", json={"merchant_id": "merch_a"}, headers=_bearer(**claims)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["message"] == "Merchant account not found"


def _quality_client(monkeypatch, calls):
    import routes.product_enrichment_routes as enrich
    import routes.product_quality_routes as quality

    async def eval_(**kwargs):
        calls.append(("eval", kwargs["merchant_id"]))
        return {"ok": True}

    async def enrich_(**kwargs):
        calls.append(("enrich", kwargs["merchant_id"]))
        return {"ok": True}

    monkeypatch.setattr(quality, "full_quality_eval", eval_)
    monkeypatch.setattr(enrich, "run_enrichment_for_product", enrich_)
    return _app(quality.router, enrich.router)


_EVAL_BODY = {"merchant_id": "merch_a", "platform": "shopify", "platform_product_id": "1", "payload": {}}
_ENRICH_BODY = {"merchant_id": "merch_a", "platform": "shopify", "platform_product_id": "1"}


@pytest.mark.parametrize("path,body", [
    ("/internal/product/quality/eval", _EVAL_BODY),
    ("/internal/product/enrichment/run", _ENRICH_BODY),
])
@pytest.mark.parametrize("headers", [None, "other_merchant", "agent"])
def test_quality_writes_refuse_anonymous_and_other_tenants(monkeypatch, path, body, headers):
    calls = []
    client = _quality_client(monkeypatch, calls)
    hdrs = {
        None: {},
        "other_merchant": _bearer(role="merchant", merchant_id="merch_b", email="b@example.com"),
        "agent": _bearer(**AGENT),
    }[headers]

    resp = client.post(path, json=body, headers=hdrs)

    assert resp.status_code in (401, 403), resp.text
    assert calls == []


@pytest.mark.parametrize("path,body", [
    ("/internal/product/quality/eval", _EVAL_BODY),
    ("/internal/product/enrichment/run", _ENRICH_BODY),
])
@pytest.mark.parametrize("claims", [MERCHANT_A, ADMIN])
def test_quality_writes_admit_the_merchant_and_staff(monkeypatch, path, body, claims):
    calls = []
    resp = _quality_client(monkeypatch, calls).post(path, json=body, headers=_bearer(**claims))

    assert resp.status_code == 200, resp.text
    assert [c[1] for c in calls] == ["merch_a"]


def _gsc_client(monkeypatch):
    import db.database as dbmod
    import routes.gsc_oauth_routes as module
    from config import settings as settings_module

    monkeypatch.setattr(settings_module.settings, "gsc_integration_enabled", True)
    monkeypatch.setattr(settings_module.settings, "google_oauth_client_id", "cid")
    monkeypatch.setattr(settings_module.settings, "google_oauth_client_secret", "secret")
    monkeypatch.setattr(settings_module.settings, "google_oauth_redirect_uri", "https://t.example/cb")
    monkeypatch.setattr(module, "_state_secret", lambda: b"x" * 32)

    async def no_row(*_a, **_k):
        return None

    monkeypatch.setattr(dbmod.database, "fetch_one", no_row)
    return _app(module.router)


@pytest.mark.parametrize("path", ["/api/gsc/oauth/start", "/api/gsc/oauth/status"])
@pytest.mark.parametrize("headers", [None, "other_merchant", "agent"])
def test_gsc_oauth_refuses_anonymous_and_other_tenants(monkeypatch, path, headers):
    client = _gsc_client(monkeypatch)
    hdrs = {
        None: {},
        "other_merchant": _bearer(role="merchant", merchant_id="merch_b", email="b@example.com"),
        "agent": _bearer(**AGENT),
    }[headers]

    resp = client.get(path, params={"merchant_id": "merch_a"}, headers=hdrs, follow_redirects=False)

    assert resp.status_code in (401, 403), resp.text


@pytest.mark.parametrize("claims", [MERCHANT_A, ADMIN])
def test_gsc_oauth_admits_the_merchant_and_staff(monkeypatch, claims):
    client = _gsc_client(monkeypatch)

    start = client.get("/api/gsc/oauth/start", params={"merchant_id": "merch_a"},
                       headers=_bearer(**claims), follow_redirects=False)
    status_resp = client.get("/api/gsc/oauth/status", params={"merchant_id": "merch_a"},
                             headers=_bearer(**claims))

    assert start.status_code == 302, start.text
    assert start.headers["location"].startswith("https://accounts.google.com/")
    assert status_resp.status_code == 200, status_resp.text
    assert status_resp.json()["reason"] == "no_oauth_row"


@pytest.mark.asyncio
async def test_redeeming_an_agent_reset_link_records_the_agent_membership(monkeypatch):
    """reset_password itself calls the link step, after the token is spent."""
    executed = []
    upserts = []

    async def fetch_one(query, values=None):
        q = " ".join(str(query).split())
        if q.startswith("SELECT token, user_email, portal"):
            return {"token": "t", "user_email": EMAIL, "portal": "agent",
                    "expires_at": datetime.utcnow() + timedelta(minutes=30), "used": False}
        if q.startswith("SELECT email, role, merchant_id FROM users"):
            return {"email": EMAIL, "role": "agent", "merchant_id": None}
        if q.startswith("SELECT email, role FROM users"):
            return {"email": EMAIL, "role": "agent"}
        return None

    async def fetch_all(query, values=None):
        return [{"agent_id": "agent_staff_made", "agent_name": "Partner"}]

    async def execute(query, values=None):
        executed.append(" ".join(str(query).split()))

    async def none(*_a, **_k):
        return None

    async def yes(*_a, **_k):
        return True

    async def upsert(**kwargs):
        upserts.append(kwargs)

    monkeypatch.setattr(auth.database, "fetch_one", fetch_one)
    monkeypatch.setattr(auth.database, "fetch_all", fetch_all)
    monkeypatch.setattr(auth.database, "execute", execute)
    monkeypatch.setattr(auth, "_ensure_password_reset_tokens_table", none)
    monkeypatch.setattr(auth, "_fetch_active_employee_identity", none)
    monkeypatch.setattr(auth, "_email_has_portal_membership", yes)
    monkeypatch.setattr(auth, "_safe_ensure_identity", none)
    monkeypatch.setattr(auth, "_safe_upsert_membership", upsert)

    resp = await auth.reset_password(auth.ResetPasswordRequest(token="t", new_password="Longenough1"))

    assert resp.success is True
    assert any(q.startswith("UPDATE password_reset_tokens SET used = TRUE") for q in executed)
    assert [(u["entity_id"], u["source"]) for u in upserts] == [
        ("agent_staff_made", "password_reset_mailbox_proof")
    ]
