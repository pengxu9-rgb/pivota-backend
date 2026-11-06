"""GET /agent/v1/merchants: what a NEW agent sees.

Since 2025-10-28 the endpoint listed only merchants the calling agent had already ordered
from, so every new partner's documented first call returned `merchants: []` and gave them no
`merchant_id` to order with. The default scope is now `available`: the merchants
cross-merchant product search treats as transactable (not deleted/rejected, psp_connected),
narrowed to the agent's `allowed_merchants` when that is set. `scope=mine` keeps the
order-history view. A merchant's contact email is returned only where the agent has orders.

Drives the real route on a minimal app (never `main`, never TestClient) against the test DB.
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
AGENT = f"agent_ml_{RUN}"
OTHER_AGENT = f"agent_ml_other_{RUN}"

# merchant_id -> (status, psp_connected)
M_ORDERED = f"merch_ml_ordered_{RUN}"      # approved, PSP on, AGENT has orders
M_FRESH = f"merch_ml_fresh_{RUN}"          # approved, PSP on, no orders from AGENT
M_NO_PSP = f"merch_ml_nopsp_{RUN}"         # approved, PSP off: search skips it
M_PENDING = f"merch_ml_pending_{RUN}"      # pending review
M_DELETED = f"merch_ml_deleted_{RUN}"      # deleted
M_OTHERS = f"merch_ml_others_{RUN}"        # approved, PSP on, only OTHER_AGENT ordered
M_NO_PSP_ORDERED = f"merch_ml_nopsp_ord_{RUN}"  # PSP off, but AGENT ordered (shows in `mine`)

MERCHANTS = {
    M_ORDERED: ("approved", True),
    M_FRESH: ("approved", True),
    M_NO_PSP: ("approved", False),
    M_PENDING: ("pending", True),
    M_DELETED: ("deleted", True),
    M_OTHERS: ("approved", True),
    M_NO_PSP_ORDERED: ("approved", False),
}
ALL_IDS = list(MERCHANTS)


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


@pytest.fixture
async def _rows():
    """Seed rows; any table this fixture had to create is dropped again, because the Postgres
    dialect gate shares one database across files and a later file must not meet our narrow
    `merchant_psps` or a model-built table where it expects a migration-built one."""
    import sqlalchemy

    from db.database import metadata
    from db.merchant_onboarding import merchant_onboarding
    from db.orders import orders

    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    created = [
        t for t in ("merchant_onboarding", "orders", "merchant_psps") if not await _table_exists(t)
    ]
    engine = sqlalchemy.create_engine(_sync_url())
    metadata.create_all(engine, tables=[merchant_onboarding, orders], checkfirst=True)
    engine.dispose()
    await database.execute(
        "CREATE TABLE IF NOT EXISTS merchant_psps ("
        "psp_id VARCHAR(100) PRIMARY KEY, merchant_id VARCHAR(50), provider VARCHAR(50), "
        "status VARCHAR(50), connected_at TIMESTAMP)"
    )

    for mid, (status, psp) in MERCHANTS.items():
        await database.execute(
            merchant_onboarding.insert().values(
                merchant_id=mid,
                business_name=f"Biz {mid}",
                store_url=f"https://{mid}.example",
                contact_email=f"owner@{mid}.example",
                region="US",
                status=status,
                psp_connected=psp,
                psp_type="stripe" if psp else None,
                operating_mode="storefront",
                apm_enabled=False,
            )
        )

    def _order(mid, agent, total, paid=True, deleted=False):
        return orders.insert().values(
            order_id=f"ord_ml_{uuid.uuid4().hex[:12]}",
            merchant_id=mid,
            agent_id=agent,
            customer_email="buyer@example.com",
            shipping_address=json.dumps({"country": "US"}),
            items=json.dumps([]),
            subtotal=total,
            total=total,
            currency="USD",
            payment_status="paid" if paid else "unpaid",
            is_deleted=deleted,
        )

    await database.execute(_order(M_ORDERED, AGENT, 30))
    await database.execute(_order(M_ORDERED, AGENT, 20, paid=False))
    await database.execute(_order(M_ORDERED, AGENT, 999, deleted=True))
    await database.execute(_order(M_NO_PSP_ORDERED, AGENT, 5))
    await database.execute(_order(M_OTHERS, OTHER_AGENT, 50))

    yield

    params = {f"m{i}": mid for i, mid in enumerate(ALL_IDS)}
    in_list = ", ".join(f":m{i}" for i in range(len(ALL_IDS)))
    await database.execute(f"DELETE FROM orders WHERE merchant_id IN ({in_list})", params)
    await database.execute(f"DELETE FROM merchant_onboarding WHERE merchant_id IN ({in_list})", params)
    for table in reversed(created):
        await database.execute(f"DROP TABLE IF EXISTS {table}")
    if not was_connected and database.is_connected:
        await database.disconnect()


def _app(allowed_merchants=None, agent_id=AGENT):
    from fastapi import FastAPI

    from routes.agent_auth import AgentContext, get_agent_context
    from routes.agent_sdk_fixed import router

    app = FastAPI()
    app.include_router(router)
    ctx = AgentContext(
        {"agent_id": agent_id, "agent_name": "t", "allowed_merchants": allowed_merchants},
        request=None,
    )
    app.dependency_overrides[get_agent_context] = lambda: ctx
    return app


async def _get(app, params=None):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.get("/agent/v1/merchants", params={"limit": 100, **(params or {})})
    return resp


def _ours(body):
    return {m["merchant_id"]: m for m in body["merchants"] if m["merchant_id"] in MERCHANTS}


async def test_a_new_agent_sees_the_transactable_merchants_by_default(_rows):
    resp = await _get(_app(agent_id=f"agent_ml_brand_new_{RUN}"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scope"] == "available"
    ours = _ours(body)
    # Approved + PSP connected, whether or not anyone ordered there.
    assert set(ours) == {M_ORDERED, M_FRESH, M_OTHERS}
    assert body["pagination"]["total"] >= 3
    for m in ours.values():
        # A new agent has no orders anywhere: no contact details, no stats.
        assert m["contact_email"] is None
        assert m["total_orders"] == 0
        assert m["total_gmv"] == 0
        assert m["psp_connected"] is True
        assert m["status"] == "active"


async def test_available_shows_the_agents_own_stats_and_email_only_where_it_ordered(_rows):
    ours = _ours((await _get(_app())).json())
    assert set(ours) == {M_ORDERED, M_FRESH, M_OTHERS}
    ordered = ours[M_ORDERED]
    # Deleted order excluded; GMV counts paid only.
    assert ordered["total_orders"] == 2
    assert ordered["total_gmv"] == 30.0
    assert ordered["contact_email"] == f"owner@{M_ORDERED}.example"
    # Another agent's orders never leak into this agent's view.
    assert ours[M_OTHERS]["total_orders"] == 0
    assert ours[M_OTHERS]["contact_email"] is None
    assert ours[M_FRESH]["contact_email"] is None


async def test_mine_keeps_the_order_history_view(_rows):
    body = (await _get(_app(), {"scope": "mine"})).json()
    assert body["scope"] == "mine"
    ours = _ours(body)
    # Order history is not gated on PSP state; other agents' merchants never appear.
    assert set(ours) == {M_ORDERED, M_NO_PSP_ORDERED}
    assert body["pagination"]["total"] == 2
    assert ours[M_ORDERED]["contact_email"] == f"owner@{M_ORDERED}.example"


async def test_mine_is_empty_for_a_new_agent(_rows):
    body = (await _get(_app(agent_id=f"agent_ml_brand_new_{RUN}"), {"scope": "mine"})).json()
    assert body["merchants"] == []
    assert body["pagination"]["total"] == 0


async def test_allowed_merchants_narrows_available(_rows):
    body = (await _get(_app(allowed_merchants=[M_FRESH, M_NO_PSP, M_DELETED]))).json()
    # Only allowlisted AND transactable.
    assert [m["merchant_id"] for m in body["merchants"]] == [M_FRESH]
    assert body["pagination"]["total"] == 1


async def test_an_empty_allowlist_fails_closed(_rows):
    body = (await _get(_app(allowed_merchants=[]))).json()
    assert body["merchants"] == []
    assert body["pagination"]["total"] == 0


async def test_status_filter_still_applies(_rows):
    ours = _ours((await _get(_app(), {"status": "pending"})).json())
    assert set(ours) == {M_PENDING}
    ours = _ours((await _get(_app(), {"status": "deleted"})).json())
    assert ours == {}


async def test_pagination_is_stable_and_counts_match(_rows):
    app = _app(allowed_merchants=[M_ORDERED, M_FRESH, M_OTHERS])
    first = (await _get(app, {"limit": 2, "offset": 0})).json()
    second = (await _get(app, {"limit": 2, "offset": 2})).json()
    assert first["pagination"] == {"total": 3, "limit": 2, "offset": 0, "has_more": True}
    assert second["pagination"]["has_more"] is False
    ids = [m["merchant_id"] for m in first["merchants"] + second["merchants"]]
    # Ordered-from merchant first, then by name; no duplicates across pages.
    assert ids[0] == M_ORDERED
    assert sorted(ids) == sorted([M_ORDERED, M_FRESH, M_OTHERS])


async def test_an_unknown_scope_is_rejected(_rows):
    resp = await _get(_app(), {"scope": "everything"})
    assert resp.status_code == 422
