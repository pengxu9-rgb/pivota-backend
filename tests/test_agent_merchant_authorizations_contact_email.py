"""GET /agents/{agent_id}/merchants: a merchant's contact email only where the agent ordered.

With the agent's allowed_merchants NULL the route lists every approved merchant (up to 100),
and it returned each one's contact_email unconditionally, so any agent portal account could
read the contact address of merchants it had never traded with. GET /agent/v1/merchants
returns the address only where the calling agent has orders; this route now applies the same
rule. Orders, GMV and every other field are unchanged.

Drives the real route on a minimal app (never `main`, never TestClient) against the test DB,
with real signed JWTs so the route's own ownership check runs.
"""

from __future__ import annotations

import json
import os
import sys
import uuid

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.database import database  # noqa: E402

RUN = uuid.uuid4().hex[:8]
AGENT = f"agent_mae_{RUN}"
OTHER_AGENT = f"agent_mae_other_{RUN}"
NEW_AGENT = f"agent_mae_new_{RUN}"

M_ORDERED = f"merch_mae_ordered_{RUN}"        # AGENT has a live order
M_FRESH = f"merch_mae_fresh_{RUN}"            # nobody has ordered
M_OTHERS = f"merch_mae_others_{RUN}"          # only OTHER_AGENT ordered
M_DELETED_ORDER = f"merch_mae_delord_{RUN}"   # AGENT's only order here is soft-deleted
M_PENDING = f"merch_mae_pending_{RUN}"        # not approved: never listed when unrestricted

MERCHANTS = {
    M_ORDERED: "approved",
    M_FRESH: "approved",
    M_OTHERS: "approved",
    M_DELETED_ORDER: "approved",
    M_PENDING: "pending",
}


def _email(mid: str) -> str:
    return f"owner@{mid}.example"


def _sync_url() -> str:
    return (os.getenv("DATABASE_URL") or "").replace("sqlite+aiosqlite://", "sqlite://").replace(
        "postgresql+asyncpg://", "postgresql://"
    )


async def _table_exists(name: str) -> bool:
    from db.database import IS_SQLITE

    if IS_SQLITE:
        row = await database.fetch_one(
            "SELECT 1 AS x FROM sqlite_master WHERE type = 'table' AND name = :n", {"n": name}
        )
    else:
        row = await database.fetch_one(
            "SELECT 1 AS x FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name = :n",
            {"n": name},
        )
    return row is not None


def _merchant_row(mid: str, status: str):
    from db.merchant_onboarding import merchant_onboarding

    return merchant_onboarding.insert().values(
        merchant_id=mid,
        business_name=f"Biz {mid}",
        store_url=f"https://{mid}.example",
        contact_email=_email(mid),
        region="US",
        status=status,
        psp_connected=True,
        psp_type="stripe",
        operating_mode="storefront",
        apm_enabled=False,
    )


def _order(mid: str, agent: str, total: float, deleted: bool = False):
    from db.orders import orders

    return orders.insert().values(
        order_id=f"ord_mae_{uuid.uuid4().hex[:12]}",
        merchant_id=mid,
        agent_id=agent,
        customer_email="buyer@example.com",
        shipping_address=json.dumps({"country": "US"}),
        items=json.dumps([]),
        subtotal=total,
        total=total,
        currency="USD",
        payment_status="paid",
        is_deleted=deleted,
    )


def _agent_row(agent_id: str, allowed_merchants=None):
    from db.agents import agents, redacted_agent_api_key

    return agents.insert().values(
        agent_id=agent_id,
        agent_name=f"Agent {agent_id}",
        agent_type="custom",
        api_key=redacted_agent_api_key(agent_id),
        api_key_hash=uuid.uuid4().hex,
        is_active=True,
        allowed_merchants=allowed_merchants,
        owner_email=f"{agent_id}@example.com",
    )


@pytest.fixture
async def _rows():
    """Seed rows; any table this fixture had to create is dropped again, because the Postgres
    dialect gate shares one database across files and a later file must not meet a model-built
    table where it expects a migration-built one."""
    import sqlalchemy

    from db.agents import agents
    from db.database import metadata
    from db.merchant_onboarding import merchant_onboarding
    from db.orders import orders

    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    created = [t for t in ("merchant_onboarding", "orders", "agents") if not await _table_exists(t)]
    engine = sqlalchemy.create_engine(_sync_url())
    metadata.create_all(engine, tables=[merchant_onboarding, orders, agents], checkfirst=True)
    engine.dispose()

    for mid, status in MERCHANTS.items():
        await database.execute(_merchant_row(mid, status))
    for agent_id in (AGENT, OTHER_AGENT, NEW_AGENT):
        await database.execute(_agent_row(agent_id))

    await database.execute(_order(M_ORDERED, AGENT, 30))
    await database.execute(_order(M_ORDERED, AGENT, 20))
    await database.execute(_order(M_OTHERS, OTHER_AGENT, 50))
    await database.execute(_order(M_DELETED_ORDER, AGENT, 999, deleted=True))
    await database.execute(_order(M_PENDING, AGENT, 5))

    yield

    mparams = {f"m{i}": mid for i, mid in enumerate(MERCHANTS)}
    in_m = ", ".join(f":{k}" for k in mparams)
    await database.execute(f"DELETE FROM orders WHERE merchant_id IN ({in_m})", mparams)
    await database.execute(f"DELETE FROM merchant_onboarding WHERE merchant_id IN ({in_m})", mparams)
    aparams = {f"a{i}": aid for i, aid in enumerate((AGENT, OTHER_AGENT, NEW_AGENT))}
    in_a = ", ".join(f":{k}" for k in aparams)
    await database.execute(f"DELETE FROM agents WHERE agent_id IN ({in_a})", aparams)
    for t in created:
        await database.execute(f"DROP TABLE IF EXISTS {t}")
    if not was_connected:
        await database.disconnect()


def _agent_token(agent_id: str) -> str:
    from utils.auth import create_access_token

    return create_access_token(
        {
            "sub": f"u-{agent_id}",
            "email": f"{agent_id}@example.com",
            "role": "agent",
            "agent_id": agent_id,
        }
    )


def _staff_token(role: str = "super_admin") -> str:
    from utils.auth import create_access_token

    return create_access_token({"sub": f"u-{role}", "email": f"{role}@example.com", "role": role})


def _app():
    from fastapi import FastAPI

    from routes.agent_management import router

    app = FastAPI()
    app.include_router(router)
    return app


async def _get(agent_id: str, token: str):
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.get(
            f"/agents/{agent_id}/merchants", headers={"Authorization": f"Bearer {token}"}
        )


def _ours(body):
    return {m["merchant_id"]: m for m in body["merchants"] if m["merchant_id"] in MERCHANTS}


async def test_an_agent_sees_a_contact_email_only_where_it_has_ordered(_rows):
    resp = await _get(AGENT, _agent_token(AGENT))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["access_type"] == "all"
    ours = _ours(body)
    # Unrestricted lists approved merchants only.
    assert set(ours) == {M_ORDERED, M_FRESH, M_OTHERS, M_DELETED_ORDER}

    assert ours[M_ORDERED]["contact_email"] == _email(M_ORDERED)
    assert ours[M_ORDERED]["total_orders"] == 2
    assert ours[M_ORDERED]["total_gmv"] == 50.0
    # Never ordered, another agent's customer, or only a deleted order: no address.
    for mid in (M_FRESH, M_OTHERS, M_DELETED_ORDER):
        assert ours[mid]["contact_email"] is None, mid
        assert ours[mid]["total_orders"] == 0, mid
    # Everything else is still served for every listed merchant.
    for mid, m in ours.items():
        assert m["business_name"] == f"Biz {mid}"
        assert m["store_url"] == f"https://{mid}.example"
        assert m["region"] == "US"


async def test_a_new_agent_sees_no_contact_email_at_all(_rows):
    resp = await _get(NEW_AGENT, _agent_token(NEW_AGENT))
    assert resp.status_code == 200, resp.text
    ours = _ours(resp.json())
    assert set(ours) == {M_ORDERED, M_FRESH, M_OTHERS, M_DELETED_ORDER}
    assert all(m["contact_email"] is None for m in ours.values())


async def test_the_rule_is_per_agent_not_per_merchant(_rows):
    # M_OTHERS's address goes to the agent that ordered there, and only to it.
    ours = _ours((await _get(OTHER_AGENT, _agent_token(OTHER_AGENT))).json())
    assert ours[M_OTHERS]["contact_email"] == _email(M_OTHERS)
    assert ours[M_ORDERED]["contact_email"] is None


async def test_staff_reading_an_agent_get_the_same_view_the_agent_gets(_rows):
    resp = await _get(AGENT, _staff_token())
    assert resp.status_code == 200, resp.text
    ours = _ours(resp.json())
    assert ours[M_ORDERED]["contact_email"] == _email(M_ORDERED)
    assert ours[M_FRESH]["contact_email"] is None


async def test_another_agent_is_still_refused(_rows):
    resp = await _get(AGENT, _agent_token(OTHER_AGENT))
    assert resp.status_code == 403, resp.text
