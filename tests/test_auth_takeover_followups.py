"""Follow-ups to the tenant-link fix (be#2763 review).

P1-1  /api/auth/register compared the address case-sensitively and then set the
      canonical identity's password: registering victim@x next to a stored
      Victim@x overwrote the victim's identity credential, and login then issued the
      victim's merchant. Register now matches case-insensitively, refuses an
      identity that already has a password or a membership, and never replaces one.
      The reset / change-password / forgot-password lookups are case-insensitive.
P2-1  The agent->merchant conversion wrote an agent membership for every agent at
      the owner_email on the users row's password alone (squat the address before
      staff create the agent, convert, own it). It now keeps only links the account
      already held. The staff fix-up routes link a login by users.id, not by address.
P2-3  agent_management's ownership no longer accepts email == owner_email (or
      email == agent_id, or user_id) for a claim-less agent token, and the legacy
      /auth/signin no longer mints agent tokens without the agent_id claim.
P3    timezone-aware membership timestamps; agent login considers every agent at the
      owner_email; GSC /start has a JSON variant for portals.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from routes import auth
from utils.auth import create_access_token

EMAIL = "victim@example.com"


def _bearer(**claims) -> dict:
    base = {"sub": claims.get("email", "x@example.com"), "email": "x@example.com"}
    base.update(claims)
    return {"Authorization": f"Bearer {create_access_token(base)}"}


# ---------------------------------------------------------------------------
# P1-1: register vs an existing account in another letter case / identity
# ---------------------------------------------------------------------------


class _CaseFoldingUsersDB:
    """A users row stored as Victim@Example.com: only a LOWER() comparison finds it."""

    def __init__(self, *, identity_in_use=False):
        self.identity_in_use = identity_in_use
        self.inserted = []

    async def fetch_one(self, query=None, values=None, **kw):
        q = " ".join(str(query if query is not None else kw.get("query")).split())
        if "FROM users" in q:
            return {"id": 1} if "LOWER(email)" in q else None
        if "FROM auth_identities" in q:
            return {"?column?": 1} if self.identity_in_use else None
        return None

    async def fetch_val(self, query=None, values=None, **kw):
        self.inserted.append(values or kw.get("values"))
        return 2


def _patch_register(monkeypatch, db):
    writes = []

    async def none(**_kwargs):
        return {"identity_id": "identity_v"}

    async def record(**kwargs):
        writes.append(kwargs)

    monkeypatch.setattr(auth.database, "fetch_one", db.fetch_one)
    monkeypatch.setattr(auth.database, "fetch_val", db.fetch_val)
    monkeypatch.setattr(auth, "_safe_ensure_identity", none)
    monkeypatch.setattr(auth, "_safe_upsert_credential_hash", record)
    monkeypatch.setattr(auth, "_safe_insert_credential_hash_if_absent", record)
    return writes


@pytest.mark.asyncio
async def test_register_refuses_an_existing_login_in_another_letter_case(monkeypatch):
    db = _CaseFoldingUsersDB()
    writes = _patch_register(monkeypatch, db)

    with pytest.raises(HTTPException) as exc:
        await auth.register(
            auth.RegisterRequest(email=EMAIL, password="Longenough1", role="merchant"), caller=None
        )

    assert exc.value.status_code == 400
    assert db.inserted == [] and writes == []


@pytest.mark.asyncio
async def test_register_own_existence_check_ignores_case(monkeypatch):
    """Each check stands alone: with the shared tenant check out of the way, the
    route's own users lookup still finds Victim@Example.com."""
    db = _CaseFoldingUsersDB()
    writes = _patch_register(monkeypatch, db)

    async def no_tenant(_email):
        return False

    monkeypatch.setattr(auth, "_email_claims_existing_tenant", no_tenant)

    with pytest.raises(HTTPException) as exc:
        await auth.register(
            auth.RegisterRequest(email=EMAIL, password="Longenough1", role="merchant"), caller=None
        )

    assert exc.value.status_code == 400 and exc.value.detail == "Email already registered"
    assert db.inserted == [] and writes == []


@pytest.mark.asyncio
async def test_tenant_check_finds_a_login_in_another_letter_case(monkeypatch):
    """/agent/account/register relies on this shared check too."""
    db = _CaseFoldingUsersDB()
    monkeypatch.setattr(auth.database, "fetch_one", db.fetch_one)

    assert await auth._email_claims_existing_tenant(EMAIL) is True


@pytest.mark.asyncio
async def test_register_refuses_an_identity_with_a_password_or_membership(monkeypatch):
    class _DB(_CaseFoldingUsersDB):
        async def fetch_one(self, query=None, values=None, **kw):
            q = " ".join(str(query if query is not None else kw.get("query")).split())
            if "FROM users" in q:
                return None
            return await super().fetch_one(query, values, **kw)

    db = _DB(identity_in_use=True)
    writes = _patch_register(monkeypatch, db)

    with pytest.raises(HTTPException) as exc:
        await auth.register(
            auth.RegisterRequest(email=EMAIL, password="Longenough1", role="agent"), caller=None
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == auth.EMAIL_ALREADY_REGISTERED_DETAIL
    assert db.inserted == [] and writes == []


@pytest.mark.asyncio
async def test_register_only_ever_inserts_a_credential(monkeypatch):
    class _Fresh(_CaseFoldingUsersDB):
        async def fetch_one(self, query=None, values=None, **kw):
            return None

    db = _Fresh()
    upserts, inserts = [], []

    async def identity(**_kwargs):
        return {"identity_id": "identity_new"}

    async def upsert(**kwargs):
        upserts.append(kwargs)

    async def insert(**kwargs):
        inserts.append(kwargs)

    monkeypatch.setattr(auth.database, "fetch_one", db.fetch_one)
    monkeypatch.setattr(auth.database, "fetch_val", db.fetch_val)
    monkeypatch.setattr(auth, "_safe_ensure_identity", identity)
    monkeypatch.setattr(auth, "_safe_upsert_credential_hash", upsert)
    monkeypatch.setattr(auth, "_safe_insert_credential_hash_if_absent", insert)

    await auth.register(auth.RegisterRequest(email=EMAIL, password="Longenough1", role="merchant"), caller=None)

    assert upserts == []
    assert [i["identity_id"] for i in inserts] == ["identity_new"]


class _CredDB:
    def __init__(self, existing):
        self.existing = existing
        self.executed = []

    async def fetch_one(self, query, values=None):
        return {"identity_id": "i", "password_hash": "victims"} if self.existing else None

    async def execute(self, query, values=None):
        self.executed.append(str(query))


@pytest.mark.asyncio
@pytest.mark.parametrize("existing,expected", [(True, False), (False, True)])
async def test_insert_credential_if_absent_never_replaces_one(monkeypatch, existing, expected):
    import db.auth_identity as ident

    db = _CredDB(existing)
    monkeypatch.setattr(ident, "database", db)

    assert await ident.insert_credential_hash_if_absent(
        identity_id="i", password_hash="attackers", source="api_auth_register"
    ) is expected
    assert len(db.executed) == (0 if existing else 1)
    if not existing:
        assert db.executed[0].upper().startswith("INSERT INTO AUTH_CREDENTIALS")


def test_membership_timestamps_are_timezone_aware():
    import db.auth_identity as ident

    now = ident._utcnow()
    assert now.tzinfo is not None and now.utcoffset().total_seconds() == 0


class _ForgotDB:
    def __init__(self):
        self.queries = []
        self.executed = []

    async def fetch_one(self, query, values=None):
        q = " ".join(str(query).split())
        self.queries.append(q)
        if q.startswith("SELECT email, role FROM users"):
            return None
        if "FROM merchant_onboarding" in q:
            ok = "LOWER(contact_email)" in q and "'deleted'" in q
            return {"merchant_id": "merch_v", "business_name": "V"} if ok else None
        return None

    async def execute(self, query, values=None):
        self.executed.append((" ".join(str(query).split()), dict(values or {})))


@pytest.mark.asyncio
async def test_forgot_password_lookups_ignore_case_and_skip_deleted_merchants(monkeypatch):
    db = _ForgotDB()

    async def none(*_a, **_k):
        return None

    async def yes(*_a, **_k):
        return True

    monkeypatch.setattr(auth.database, "fetch_one", db.fetch_one)
    monkeypatch.setattr(auth.database, "execute", db.execute)
    monkeypatch.setattr(auth, "_fetch_active_employee_identity", none)
    monkeypatch.setattr(auth, "_email_has_portal_membership", yes)
    monkeypatch.setattr(auth, "_ensure_password_reset_tokens_table", none)
    monkeypatch.setattr(auth, "_send_reset_password_email", lambda *_a: None)

    await auth.forgot_password(auth.ForgotPasswordRequest(email=EMAIL, portal="merchant"))

    users = [q for q in db.queries if q.startswith("SELECT email, role FROM users")]
    assert users and all("LOWER(email) = LOWER(:email)" in q for q in users)
    assert any(q.startswith("INSERT INTO users") for q, _ in db.executed)


@pytest.mark.asyncio
async def test_portal_membership_check_ignores_case_and_skips_deleted_merchants(monkeypatch):
    db = _ForgotDB()

    async def none(*_a, **_k):
        return None

    monkeypatch.setattr(auth.database, "fetch_one", db.fetch_one)
    monkeypatch.setattr(auth, "_safe_get_active_membership", none)

    assert await auth._email_has_portal_membership("merchant", EMAIL, None, None) is True


class _ChangeDB:
    def __init__(self, rows):
        self.rows = rows
        self.executed = []
        self.queries = []

    async def fetch_all(self, query, values=None):
        self.queries.append(" ".join(str(query).split()))
        return self.rows

    async def execute(self, query, values=None):
        self.executed.append((" ".join(str(query).split()), dict(values or {})))


def _change_request():
    return auth.ChangePasswordRequest(current_password="Oldpassword1", new_password="Newpassword1")


@pytest.mark.asyncio
async def test_change_password_refuses_an_address_with_two_logins(monkeypatch):
    from utils.auth import hash_password

    db = _ChangeDB([
        {"id": 1, "email": "Victim@Example.com", "password_hash": hash_password("Victims1pass")},
        {"id": 2, "email": EMAIL, "password_hash": hash_password("Oldpassword1")},
    ])
    monkeypatch.setattr(auth.database, "fetch_all", db.fetch_all)
    monkeypatch.setattr(auth.database, "execute", db.execute)

    with pytest.raises(HTTPException) as exc:
        await auth.change_password(_change_request(), current_user={"email": EMAIL})

    assert exc.value.status_code == 409
    assert db.executed == []
    assert "LOWER(email) = LOWER(:email)" in db.queries[0]


@pytest.mark.asyncio
async def test_change_password_updates_the_one_row_by_id(monkeypatch):
    from utils.auth import hash_password

    db = _ChangeDB([{"id": 7, "email": EMAIL, "password_hash": hash_password("Oldpassword1")}])

    async def none(*_a, **_k):
        return None

    monkeypatch.setattr(auth.database, "fetch_all", db.fetch_all)
    monkeypatch.setattr(auth.database, "execute", db.execute)
    monkeypatch.setattr(auth, "_safe_ensure_identity", none)
    monkeypatch.setattr(auth, "_fetch_active_employee_identity", none)

    await auth.change_password(_change_request(), current_user={"email": EMAIL})

    updates = [(q, v) for q, v in db.executed if q.startswith("UPDATE users SET password_hash")]
    assert updates and "WHERE id = :id" in updates[0][0] and updates[0][1]["id"] == 7


# ---------------------------------------------------------------------------
# P2-1: staff fix-up routes link a login by id, never by address
# ---------------------------------------------------------------------------


class _AdminDB:
    def __init__(self):
        self.queries = []

    async def fetch_one(self, query, values=None):
        q = " ".join(str(query).split())
        self.queries.append((q, dict(values or {})))
        if q.startswith("UPDATE merchant_onboarding"):
            return {"merchant_id": "merch_x", "business_name": "X", "contact_email": EMAIL,
                    "mcp_shop_domain": None}
        if q.startswith("UPDATE users"):
            return {"id": 5, "email": EMAIL, "merchant_id": "merch_x", "role": "merchant"}
        if q.startswith("SELECT merchant_id, business_name, contact_email"):
            return {"merchant_id": "merch_x", "business_name": "X", "contact_email": EMAIL}
        return None

    async def execute(self, query, values=None):
        self.queries.append((" ".join(str(query).split()), dict(values or {})))

    def transaction(self):
        class _T:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *a):
                return False

        return _T()

    def user_updates(self):
        return [(q, v) for q, v in self.queries if q.startswith("UPDATE users")]


def _admin_client(monkeypatch):
    import routes.admin_fix_merchant as module
    from routes.auth_routes import require_admin
    from utils.auth import require_admin_or_key

    db = _AdminDB()
    monkeypatch.setattr(module, "database", db)
    app = FastAPI()
    app.include_router(module.router)
    app.dependency_overrides[require_admin] = lambda: {"role": "admin", "user_id": "a"}
    app.dependency_overrides[require_admin_or_key] = lambda: {"role": "admin"}
    return TestClient(app), db, module


def test_fix_merchant_id_links_no_login_without_a_user_id(monkeypatch):
    client, db, _ = _admin_client(monkeypatch)

    resp = client.post("/admin/fix/merchant-id", json={"email": EMAIL, "correct_merchant_id": "merch_x"})

    assert resp.status_code == 200, resp.text
    assert db.user_updates() == []
    assert resp.json()["user_updated"] is False


def test_fix_merchant_id_links_the_named_login_by_id(monkeypatch):
    client, db, _ = _admin_client(monkeypatch)

    resp = client.post(
        "/admin/fix/merchant-id", json={"email": EMAIL, "correct_merchant_id": "merch_x", "user_id": 5}
    )

    assert resp.status_code == 200, resp.text
    (q, v), = db.user_updates()
    assert "WHERE id = :user_id" in q and v["user_id"] == 5


def test_merge_merchants_moves_only_the_login_already_on_the_removed_merchant(monkeypatch):
    client, db, _ = _admin_client(monkeypatch)

    resp = client.post(
        "/admin/fix/merge-merchants",
        json={"keep_merchant_id": "merch_keep", "remove_merchant_id": "merch_rm", "user_email": EMAIL},
    )

    assert resp.status_code == 200, resp.text
    (q, v), = db.user_updates()
    assert "merchant_id = :remove" in q and v["remove"] == "merch_rm"


def test_fix_user_role_needs_a_user_id_to_link_a_merchant(monkeypatch):
    client, db, _ = _admin_client(monkeypatch)

    resp = client.post(
        "/admin/fix/user-role", json={"email": EMAIL, "role": "merchant", "merchant_id": "merch_x"}
    )
    assert resp.status_code == 400, resp.text
    assert db.user_updates() == []

    resp = client.post(
        "/admin/fix/user-role", json={"email": EMAIL, "role": "merchant", "merchant_id": "merch_x", "user_id": 5}
    )
    assert resp.status_code == 200, resp.text
    (q, v), = db.user_updates()
    assert "WHERE id = :user_id" in q


def test_fix_user_role_never_infers_the_merchant_from_contact_email(monkeypatch):
    client, db, _ = _admin_client(monkeypatch)

    resp = client.post("/admin/fix/user-role", json={"email": EMAIL, "role": "merchant"})

    assert resp.status_code == 200, resp.text
    assert not any("FROM merchant_onboarding" in q for q, _ in db.queries)
    (q, v), = db.user_updates()
    assert "SET role = :role WHERE" in q and "merchant_id" not in v


def test_merge_to_merchant_needs_the_existing_logins_id(monkeypatch):
    client, db, module = _admin_client(monkeypatch)
    import routes.merchant_onboarding_routes as onb

    synced = []

    async def binding(_email):
        return {"id": 5, "email": EMAIL, "role": "agent", "merchant_id": None}

    async def onboarding(_email):
        return {"merchant_id": "merch_x"}

    async def sync(**kwargs):
        synced.append(kwargs)

    monkeypatch.setattr(onb, "get_user_auth_binding", binding)
    monkeypatch.setattr(onb, "get_active_onboarding_by_email", onboarding)
    monkeypatch.setattr(onb, "sync_merchant_auth_user", sync)

    resp = client.post("/admin/fix/merge-to-merchant", json={"email": EMAIL})
    assert resp.status_code == 409, resp.text
    assert synced == []

    resp = client.post("/admin/fix/merge-to-merchant", json={"email": EMAIL, "user_id": 5})
    assert resp.status_code == 200, resp.text
    assert len(synced) == 1


@pytest.mark.asyncio
async def test_simple_fix_links_a_login_only_by_configured_id(monkeypatch):
    import routes.admin_simple_fix as module

    db = _AdminDB()
    monkeypatch.setattr(module, "database", db)
    monkeypatch.setattr(module, "require_runtime_gate", lambda _name: None)
    env = {"ADMIN_SIMPLE_FIX_MERCHANT_ID": "merch_x", "ADMIN_SIMPLE_FIX_CONTACT_EMAIL": EMAIL}
    monkeypatch.setattr(module, "configured_env_value", lambda name: env.get(name))

    await module.update_contact_email(current_user={"role": "admin"})
    assert db.user_updates() == []

    env["ADMIN_SIMPLE_FIX_USER_ID"] = "5"
    await module.update_contact_email(current_user={"role": "admin"})
    (q, v), = db.user_updates()
    assert "WHERE id = :user_id" in q and v["user_id"] == 5


# ---------------------------------------------------------------------------
# P2-3: agent_management ownership is the agent_id claim only
# ---------------------------------------------------------------------------

AGENT = "agent_partner01"
OWNER = "partner@example.com"


def _agent_mgmt_client(monkeypatch):
    import routes.agent_management as module

    async def get_agent(agent_id):
        return {"agent_id": agent_id, "agent_name": "P", "agent_type": "basic", "owner_email": OWNER}

    monkeypatch.setattr(module, "get_agent", get_agent)
    app = FastAPI()
    app.include_router(module.router)
    return TestClient(app)


CLAIMLESS = [
    {"role": "agent", "email": OWNER, "sub": OWNER},                     # email == owner_email
    {"role": "agent", "email": AGENT, "sub": AGENT},                     # email == agent_id
    {"role": "agent", "email": OWNER, "sub": "u1", "user_id": AGENT},    # user_id == agent_id
]

AGENT_READS = ["", "/analytics", "/usage", "/funnel", "/query-analytics", "/merchants"]


@pytest.mark.parametrize("suffix", AGENT_READS)
@pytest.mark.parametrize("claims", CLAIMLESS, ids=["owner_email", "email_is_id", "user_id"])
def test_agent_reads_refuse_a_token_without_the_agent_id_claim(monkeypatch, suffix, claims):
    client = _agent_mgmt_client(monkeypatch)

    resp = client.get(f"/agents/{AGENT}{suffix}", headers=_bearer(**claims))

    assert resp.status_code == 403, f"{suffix}: {resp.status_code} {resp.text[:200]}"


def test_agent_detail_serves_the_agent_its_own_claim_names(monkeypatch):
    client = _agent_mgmt_client(monkeypatch)

    resp = client.get(f"/agents/{AGENT}", headers=_bearer(role="agent", agent_id=AGENT, email="op@example.com"))

    assert resp.status_code == 200, resp.text
    assert resp.json()["agent"]["agent_id"] == AGENT


def test_update_agent_refuses_a_user_id_claim(monkeypatch):
    client = _agent_mgmt_client(monkeypatch)

    resp = client.put(
        f"/agents/{AGENT}",
        json={"description": "x"},
        headers=_bearer(role="agent", email=OWNER, sub="u1", user_id=AGENT),
    )

    assert resp.status_code == 403, resp.text


def test_own_agent_identity_is_the_agent_id_claim_of_an_agent_token():
    from routes.agent_management import _own_agent_identity

    assert _own_agent_identity({"role": "agent", "agent_id": AGENT, "email": OWNER}) == ({AGENT}, "")
    assert _own_agent_identity({"role": "agent", "email": OWNER, "user_id": AGENT}) == (set(), "")
    assert _own_agent_identity({"role": "merchant", "agent_id": AGENT}) == (set(), "")
    assert _own_agent_identity({"role": "agent", "agent_id": [AGENT]}) == (set(), "")


def test_legacy_signin_mints_no_agent_token(monkeypatch):
    from routes import auth_routes
    from utils.auth import hash_password

    async def fetch_one(query, values=None):
        q = str(query)
        if "FROM employees" in q:
            return None
        if "FROM users" in q:
            return {"id": 3, "email": OWNER, "password_hash": hash_password("Longenough1"),
                    "full_name": "P", "role": "agent", "active": True, "merchant_id": None}
        return None

    monkeypatch.setattr(auth_routes.database, "fetch_one", fetch_one)
    app = FastAPI()
    app.include_router(auth_routes.router)

    resp = TestClient(app).post("/auth/signin", json={"email": OWNER, "password": "Longenough1"})

    assert resp.status_code == 403, resp.text
    assert "token" not in resp.json()


# ---------------------------------------------------------------------------
# P3: several agents at one owner_email; GSC JSON start
# ---------------------------------------------------------------------------


def test_agent_login_uses_the_linked_agent_not_merely_the_oldest(monkeypatch):
    from tests.test_auth_email_link_and_leftover_holes import _agent_login_client

    client, checks = _agent_login_client(
        monkeypatch, linked={"agent_mine"}, agents=("agent_staff_made", "agent_mine")
    )

    resp = client.post(
        "/agent/account/login", json={"email": "owner@partner.example", "password": "Longenough1"}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["agent"]["agent_id"] == "agent_mine"
    assert [c[0] for c in checks] == ["agent_staff_made", "agent_mine"]


def test_gsc_start_has_a_json_variant_for_portals(monkeypatch):
    import routes.gsc_oauth_routes as module
    from config import settings as settings_module

    monkeypatch.setattr(settings_module.settings, "gsc_integration_enabled", True)
    monkeypatch.setattr(settings_module.settings, "google_oauth_client_id", "cid")
    monkeypatch.setattr(settings_module.settings, "google_oauth_client_secret", "secret")
    monkeypatch.setattr(settings_module.settings, "google_oauth_redirect_uri", "https://t.example/cb")
    monkeypatch.setattr(module, "_state_secret", lambda: b"x" * 32)
    app = FastAPI()
    app.include_router(module.router)
    client = TestClient(app)
    hdrs = _bearer(role="merchant", merchant_id="merch_a", email="a@example.com")

    resp = client.get("/api/gsc/oauth/start", params={"merchant_id": "merch_a", "format": "json"}, headers=hdrs)
    assert resp.status_code == 200, resp.text
    assert resp.json()["authorization_url"].startswith("https://accounts.google.com/")

    other = client.get("/api/gsc/oauth/start", params={"merchant_id": "merch_b", "format": "json"}, headers=hdrs)
    assert other.status_code == 403


def test_merchant_guard_on_body_routes_needs_an_employee_membership_for_staff():
    """The body/query routes moved onto assert_merchant_scope: a token whose role
    string is staff but whose membership is a merchant's is not staff."""
    from utils.auth import EMPLOYEE_ROLES
    from utils.tenant_scope import assert_merchant_scope

    assert_merchant_scope({"role": "employee", "membership_type": "employee"}, "m", staff_roles=EMPLOYEE_ROLES)
    with pytest.raises(HTTPException):
        assert_merchant_scope(
            {"role": "admin", "membership_type": "merchant", "merchant_id": "other"}, "m",
            staff_roles=EMPLOYEE_ROLES,
        )


def test_conversion_after_squatting_writes_no_agent_membership(monkeypatch):
    """P2-1 end to end at the merchant-onboarding layer: an agent-role users row
    (registered before staff created the agent at that address) converts; no
    membership is created, so the agent login that follows has nothing to admit."""
    import asyncio

    import db.auth_identity as auth_identity
    import routes.merchant_onboarding_routes as module

    upserts = []

    async def fetch_one(query, values=None):
        return {"created_at": datetime(2026, 11, 1, tzinfo=timezone.utc)}

    async def fetch_all(query, values=None):
        return [{"agent_id": "agent_staff_made", "agent_name": "Partner"}]

    async def never_linked(**_kwargs):
        return False

    async def upsert(**kwargs):
        upserts.append(kwargs)

    monkeypatch.setattr(module.database, "fetch_one", fetch_one)
    monkeypatch.setattr(module.database, "fetch_all", fetch_all)
    monkeypatch.setattr(auth_identity, "has_active_membership_for_entity", never_linked)
    monkeypatch.setattr(module, "upsert_membership", upsert)

    assert asyncio.run(module.keep_converted_agent_memberships(OWNER)) == []
    assert upserts == []
