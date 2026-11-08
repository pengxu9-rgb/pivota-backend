"""An agent account converted into a merchant keeps its agent-portal access.

Public merchant signup with an email that already belongs to an agent verifies that
agent's password and then converts the account (resolve_public_merchant_identity_merge
-> sync_merchant_auth_user rewrites users.role to 'merchant'). users holds one role per
email, so the owner was then refused by the developer portal ("This login is for agents
only"). The conversion now keeps (re-affirms) the agent memberships the account already
held, which is what agent login admits a non-agent role through
(routes/agent_account._holds_agent_membership). It never creates one from an owner_email
match: that password proves only the users row, which self-service registration
creates without mailbox proof.

Pinned here:
1. keep_converted_agent_memberships re-affirms only an agent membership this account
   already held (written after its users row) per agents row at the email, and keeps
   nothing without the users row's age;
2. register_merchant calls it only for a conversion from 'agent', and BEFORE the role
   rewrite, so a failed membership write leaves an intact agent account;
3. has_active_membership_for_entity filters on the exact entity, type and active status,
   never queries for an inactive identity, and refuses a membership last written before
   the users row existed (a deleted account's identity inherited by a re-registration).
"""

import pytest
from sqlalchemy.dialects import postgresql

import routes.merchant_onboarding_routes as module
from routes.merchant_onboarding_routes import MerchantRegisterRequest

EMAIL = "converted@example.com"


class _DummyBackgroundTasks:
    def add_task(self, *args, **kwargs):
        pass


# ── 1. the membership writer ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_keeps_only_the_agent_memberships_the_account_already_held(monkeypatch):
    """An owner_email match is not a link: of the two agents at this address, only the
    one this account already holds a membership for (written after its users row)
    is kept. The other -- e.g. staff created it at an address the converting account
    had squatted -- gets nothing."""
    import db.auth_identity as auth_identity
    from datetime import datetime, timedelta, timezone

    created = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    queries = []
    upserts = []
    checks = []

    async def fetch_one(query, values=None):
        queries.append((" ".join(str(query).split()), dict(values or {})))
        return {"created_at": created}

    async def fetch_all(query, values=None):
        queries.append((" ".join(str(query).split()), dict(values or {})))
        return [
            {"agent_id": "agent_one", "agent_name": "One"},
            {"agent_id": "agent_two", "agent_name": "Two"},
        ]

    async def has_membership(*, email, membership_type, entity_id, not_before=None):
        checks.append((email, membership_type, entity_id, not_before))
        return entity_id == "agent_one"

    async def upsert_membership(**kwargs):
        upserts.append(kwargs)
        return {}

    monkeypatch.setattr(module.database, "fetch_one", fetch_one)
    monkeypatch.setattr(module.database, "fetch_all", fetch_all)
    monkeypatch.setattr(auth_identity, "has_active_membership_for_entity", has_membership)
    monkeypatch.setattr(module, "upsert_membership", upsert_membership)

    kept = await module.keep_converted_agent_memberships(EMAIL)

    assert kept == ["agent_one"]
    assert any("LOWER(owner_email) = LOWER(:email)" in q for q, _ in queries)
    assert [(c[2], c[3]) for c in checks] == [
        ("agent_one", created - timedelta(seconds=60)),
        ("agent_two", created - timedelta(seconds=60)),
    ]
    assert [(u["email"], u["membership_type"], u["entity_id"], u["status"]) for u in upserts] == [
        (EMAIL, "agent", "agent_one", "active"),
    ]


@pytest.mark.asyncio
async def test_without_the_users_rows_age_nothing_is_kept(monkeypatch):
    upserts = []

    async def fetch_one(query, values=None):
        return {"created_at": None}

    async def fetch_all(query, values=None):
        raise AssertionError("no agent lookup without the row's age")

    async def upsert_membership(**kwargs):
        upserts.append(kwargs)

    monkeypatch.setattr(module.database, "fetch_one", fetch_one)
    monkeypatch.setattr(module.database, "fetch_all", fetch_all)
    monkeypatch.setattr(module, "upsert_membership", upsert_membership)

    assert await module.keep_converted_agent_memberships(EMAIL) == []
    assert upserts == []


# ── 2. register_merchant wiring ──────────────────────────────────────────────


def _patch_register(monkeypatch, *, identity_merge, order, fail_membership=False):
    async def noop(*args, **kwargs):
        return None

    async def fake_resolve_identity_merge(**kwargs):
        existing = {"id": 5, "email": EMAIL, "role": "agent"} if identity_merge else None
        return existing, identity_merge

    async def fake_create(merchant_dict):
        return "merch_conv"

    async def fake_update_kyc_status(*a, **k):
        return True

    async def fake_keep(email):
        order.append(("keep", email))
        if fail_membership:
            raise RuntimeError("membership write failed")
        return ["agent_one"]

    async def fake_sync_user(**kwargs):
        order.append(("sync_user", kwargs["contact_email"]))

    monkeypatch.setattr(module.database, "execute", noop)
    monkeypatch.setattr(module.database, "fetch_one", noop)
    monkeypatch.setattr(module, "get_active_onboarding_by_email", noop)
    monkeypatch.setattr(module, "get_user_auth_binding", noop)
    monkeypatch.setattr(module, "resolve_public_merchant_identity_merge", fake_resolve_identity_merge)
    monkeypatch.setattr(module, "get_merchant_onboarding", noop)
    monkeypatch.setattr(module, "create_merchant_onboarding", fake_create)
    monkeypatch.setattr(module, "update_kyc_status", fake_update_kyc_status)
    monkeypatch.setattr(module, "keep_converted_agent_memberships", fake_keep)
    monkeypatch.setattr(module, "sync_merchant_auth_user", fake_sync_user)


def _request():
    return MerchantRegisterRequest(
        business_name="Converted Co",
        region="US",
        contact_email=EMAIL,
        password="hunter2hunter2",
        operating_mode="store_less",
    )


@pytest.mark.asyncio
async def test_conversion_from_agent_keeps_membership_before_the_role_rewrite(monkeypatch):
    order = []
    _patch_register(monkeypatch, identity_merge={"converted_from_role": "agent"}, order=order)

    resp = await module.register_merchant(_request(), _DummyBackgroundTasks())

    assert resp["status"] == "success"
    assert order == [("keep", EMAIL), ("sync_user", EMAIL)]


@pytest.mark.asyncio
async def test_plain_merchant_signup_writes_no_agent_membership(monkeypatch):
    order = []
    _patch_register(monkeypatch, identity_merge=None, order=order)

    await module.register_merchant(_request(), _DummyBackgroundTasks())

    assert order == [("sync_user", EMAIL)]


@pytest.mark.asyncio
async def test_failed_membership_write_stops_before_the_role_rewrite(monkeypatch):
    order = []
    _patch_register(monkeypatch, identity_merge={"converted_from_role": "agent"}, order=order, fail_membership=True)

    with pytest.raises(Exception):
        await module.register_merchant(_request(), _DummyBackgroundTasks())

    assert ("sync_user", EMAIL) not in order


# ── 3. the entity-bound membership reader ────────────────────────────────────


@pytest.mark.asyncio
async def test_membership_lookup_is_bound_to_type_entity_and_active_status(monkeypatch):
    import db.auth_identity as auth_identity

    captured = []

    async def get_identity(email):
        return {"identity_id": "ident_1", "status": "active"}

    async def fetch_one(query, values=None):
        captured.append(str(query.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})))
        return {"membership_id": "m1"}

    monkeypatch.setattr(auth_identity, "get_identity_by_email", get_identity)
    monkeypatch.setattr(auth_identity.database, "fetch_one", fetch_one)

    assert await auth_identity.has_active_membership_for_entity(
        email=EMAIL, membership_type="agent", entity_id="agent_one"
    )
    sql = captured[0]
    assert "auth_memberships.identity_id = 'ident_1'" in sql
    assert "auth_memberships.membership_type = 'agent'" in sql
    assert "auth_memberships.entity_id = 'agent_one'" in sql
    assert "auth_memberships.status = 'active'" in sql


@pytest.mark.asyncio
async def test_membership_lookup_answers_false_without_a_row_or_an_active_identity(monkeypatch):
    import db.auth_identity as auth_identity

    queried = []

    async def fetch_none(query, values=None):
        queried.append(query)
        return None

    monkeypatch.setattr(auth_identity.database, "fetch_one", fetch_none)

    async def active_identity(email):
        return {"identity_id": "ident_1", "status": "active"}

    monkeypatch.setattr(auth_identity, "get_identity_by_email", active_identity)
    assert not await auth_identity.has_active_membership_for_entity(
        email=EMAIL, membership_type="agent", entity_id="agent_one"
    )

    async def inactive_identity(email):
        return {"identity_id": "ident_1", "status": "suspended"}

    monkeypatch.setattr(auth_identity, "get_identity_by_email", inactive_identity)
    queried.clear()
    assert not await auth_identity.has_active_membership_for_entity(
        email=EMAIL, membership_type="agent", entity_id="agent_one"
    )
    assert queried == []


@pytest.mark.asyncio
async def test_a_membership_older_than_the_users_row_does_not_count(monkeypatch):
    """Admin merchant deletion drops the users row but keeps identity + memberships; a
    stranger re-registering that email reuses the identity. Their users row is newer
    than every membership write, so the stale agent membership must not vouch for it."""
    from datetime import datetime, timezone

    import db.auth_identity as auth_identity

    async def get_identity(email):
        return {"identity_id": "ident_1", "status": "active"}

    written = datetime(2026, 3, 1, 12, 0, 0)  # naive, as upsert_membership writes utcnow()

    async def fetch_one(query, values=None):
        return {"membership_id": "m1", "created_at": written, "updated_at": written}

    monkeypatch.setattr(auth_identity, "get_identity_by_email", get_identity)
    monkeypatch.setattr(auth_identity.database, "fetch_one", fetch_one)

    async def check(users_created_at):
        return await auth_identity.has_active_membership_for_entity(
            email=EMAIL, membership_type="agent", entity_id="agent_one", not_before=users_created_at
        )

    # hr@chydan.com shape: users row long before the membership write -> admitted
    assert await check(datetime(2026, 2, 27, 7, 26, 34, tzinfo=timezone.utc))
    # re-registered after deletion: users row newer than the membership -> refused
    assert not await check(datetime(2026, 6, 1, tzinfo=timezone.utc))
    # boundary: written in the same instant the row was created still counts
    assert await check(datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc))
