"""GET /agent/v1/merchants: what a NEW agent sees.

Since 2025-10-28 the endpoint listed only merchants the calling agent had already ordered
from, so every new partner's documented first call returned `merchants: []` and gave them no
`merchant_id` to order with. The default scope is now `available`: the merchants
cross-merchant product search treats as transactable (not deleted/rejected, psp_connected),
narrowed to the agent's `allowed_merchants` when that is set. `scope=mine` keeps the
order-history view. A merchant's contact email is returned only where the agent has orders.

Managed onboarding (D-09, 2026-11-08): `available` also needs a STAFF approval (not the
signup auto-approval, which wrote `auto_approved=True`; a staff approval stamps
`verified_at` and clears it) and a live connected store. Every row expected in `available`
below therefore carries both, and the M_FLOOR / M_UNVERIFIED / M_STORELESS / M_STORE_GONE /
M_PENDING_PSP rows each miss exactly one rule.

Drives the real route on a minimal app (never `main`, never TestClient) against the test DB.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import uuid
from datetime import datetime
from typing import Optional

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
M_NULL_PSP = f"merch_ml_nullpsp_{RUN}"     # approved, psp_connected NULL: not transactable
# Managed onboarding: each of these is approved + PSP-connected and misses ONE listing rule.
M_FLOOR = f"merch_ml_floor_{RUN}"          # approved by the signup floor (auto_approved=True)
M_UNVERIFIED = f"merch_ml_unverif_{RUN}"   # status written directly: no staff verified_at
M_STORELESS = f"merch_ml_storeless_{RUN}"  # staff-approved, no store at all
M_STORE_GONE = f"merch_ml_storegone_{RUN}"  # staff-approved, its only store disconnected
M_PENDING_PSP = f"merch_ml_pendpsp_{RUN}"  # pending review, PSP + store connected

MERCHANTS = {
    M_ORDERED: ("approved", True),
    M_FRESH: ("approved", True),
    M_NO_PSP: ("approved", False),
    M_PENDING: ("pending", True),
    M_DELETED: ("deleted", True),
    M_OTHERS: ("approved", True),
    M_NO_PSP_ORDERED: ("approved", False),
    M_NULL_PSP: ("approved", None),
    M_FLOOR: ("approved", True),
    M_UNVERIFIED: ("approved", True),
    M_STORELESS: ("approved", True),
    M_STORE_GONE: ("approved", True),
    M_PENDING_PSP: ("pending_verification", True),
}
ALL_IDS = list(MERCHANTS)

# merchant_id -> (auto_approved, staff verified_at set). Default: (False, True), a staff
# approval. M_OTHERS carries auto_approved NULL, as rows inserted through the `databases`
# path do (it never applied the column default): NULL is not an automatic approval.
APPROVAL = {
    M_FLOOR: (True, True),
    M_UNVERIFIED: (False, False),
    M_OTHERS: (None, True),
    M_PENDING: (False, False),
    M_PENDING_PSP: (False, False),
}

# merchant_id -> its one store's status (no entry: no store). Mixed case/padding on purpose:
# the rule normalises like services/connection_layer does.
STORES = {mid: "connected" for mid in ALL_IDS if mid not in (M_STORELESS, M_STORE_GONE)}
STORES[M_FRESH] = "active"
STORES[M_OTHERS] = " Active "
STORES[M_STORE_GONE] = "disconnected"
STORE_IDS = {mid: f"st_ml_{i}_{RUN}" for i, mid in enumerate(STORES)}


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


def _merchant_row(mid, status="approved", psp=True, auto_approved=False, verified=True):
    """`auto_approved=False, verified=True` is what a staff approval leaves behind."""
    from db.merchant_onboarding import merchant_onboarding

    return merchant_onboarding.insert().values(
        merchant_id=mid,
        business_name=f"Biz {mid}",
        store_url=f"https://{mid}.example",
        contact_email=f"owner@{mid}.example",
        region="US",
        status=status,
        psp_connected=psp,
        psp_type="stripe" if psp else None,
        auto_approved=auto_approved,
        verified_at=datetime(2026, 11, 1) if verified else None,
        operating_mode="storefront",
        apm_enabled=False,
    )


MERCHANT_STORES_DDL = (
    "CREATE TABLE merchant_stores ("
    "store_id VARCHAR(50) PRIMARY KEY, merchant_id VARCHAR(50) NOT NULL, "
    "platform VARCHAR(50) NOT NULL, name VARCHAR(255) NOT NULL, domain VARCHAR(255), "
    "api_key TEXT, status VARCHAR(50) DEFAULT 'connected', "
    "connected_at TIMESTAMP WITH TIME ZONE, created_at TIMESTAMP WITH TIME ZONE "
    "DEFAULT CURRENT_TIMESTAMP)"
)
STORE_COLUMNS = ("merchant_id", "platform", "name", "domain", "status")


async def _insert_store(store_id, mid, status, domain=None):
    await database.execute(
        "INSERT INTO merchant_stores (store_id, merchant_id, platform, name, domain, status) "
        "VALUES (:s, :m, 'shopify', 'store', :d, :st)",
        {"s": store_id, "m": mid, "d": domain or f"{store_id}.example", "st": status},
    )


def _order(mid, agent, total, paid=True, deleted=False):
    from db.orders import orders

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
    store_undo: list = []
    if not await _table_exists("merchant_stores"):
        await database.execute(MERCHANT_STORES_DDL)
        store_undo.append(("table", "merchant_stores"))
    await _ensure_columns("merchant_stores", STORE_COLUMNS, store_undo)
    engine = sqlalchemy.create_engine(_sync_url())
    metadata.create_all(engine, tables=[merchant_onboarding, orders], checkfirst=True)
    engine.dispose()
    await database.execute(
        "CREATE TABLE IF NOT EXISTS merchant_psps ("
        "psp_id VARCHAR(100) PRIMARY KEY, merchant_id VARCHAR(50), provider VARCHAR(50), "
        "status VARCHAR(50), connected_at TIMESTAMP)"
    )

    for mid, (status, psp) in MERCHANTS.items():
        auto, verified = APPROVAL.get(mid, (False, True))
        await database.execute(_merchant_row(mid, status, psp, auto_approved=auto, verified=verified))
    for mid, store_status in STORES.items():
        await _insert_store(STORE_IDS[mid], mid, store_status)

    await database.execute(_order(M_ORDERED, AGENT, 30))
    await database.execute(_order(M_ORDERED, AGENT, 20, paid=False))
    await database.execute(_order(M_ORDERED, AGENT, 999, deleted=True))
    await database.execute(_order(M_NO_PSP_ORDERED, AGENT, 5))
    await database.execute(_order(M_OTHERS, OTHER_AGENT, 50))
    # Orders at non-approved merchants: only `scope=mine` with that status may surface them,
    # and a deleted merchant never.
    await database.execute(_order(M_PENDING, AGENT, 7))
    await database.execute(_order(M_DELETED, AGENT, 8))

    yield

    params = {f"m{i}": mid for i, mid in enumerate(ALL_IDS)}
    in_list = ", ".join(f":m{i}" for i in range(len(ALL_IDS)))
    await database.execute(f"DELETE FROM orders WHERE merchant_id IN ({in_list})", params)
    await database.execute(f"DELETE FROM merchant_onboarding WHERE merchant_id IN ({in_list})", params)
    await database.execute(f"DELETE FROM merchant_stores WHERE merchant_id IN ({in_list})", params)
    await _restore(store_undo)
    for table in reversed(created):
        await database.execute(f"DROP TABLE IF EXISTS {table}")
    if not was_connected and database.is_connected:
        await database.disconnect()


# Test/demo rigs (services/test_merchant_policy): one baked-in id, one env-listed id, one
# caught only by its demo storefront domain. All approved, PSP-connected, and AGENT has
# ordered from each, so nothing but the rig exclusion can keep them out of either scope.
R_ENV = f"merch_ml_envrig_{RUN}"
R_DEMO = f"merch_ml_demorig_{RUN}"
DEMO_DOMAIN = f"pivota-review-demo-ml-{RUN}.myshopify.com"


async def _ensure_columns(table: str, columns, undo: list) -> None:
    """Add each missing column as TEXT and record it in `undo`. The dialect gate shares one
    database, and tests/test_connection_layer_postgres.py leaves a `merchant_stores` with only
    `store_id` plus a few columns (no `name`), which CREATE TABLE IF NOT EXISTS then keeps.
    A probe per column so the same code works on both dialects."""
    for column in columns:
        try:
            await database.fetch_all(f"SELECT {column} FROM {table} WHERE 1 = 0")
        except Exception:  # noqa: BLE001 - the probe only says "missing"
            await database.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
            undo.append(("column", table, column))


async def _restore(undo: list) -> None:
    """Put the shared database's schema back as found: newest first; a column added to a
    table this fixture created goes with the table."""
    dropped = set()
    for item in reversed(undo):
        if item[0] == "table":
            await database.execute(f"DROP TABLE IF EXISTS {item[1]}")
            dropped.add(item[1])
        elif item[1] not in dropped:
            await database.execute(f"ALTER TABLE {item[1]} DROP COLUMN {item[2]}")


async def _pick_known_rig() -> tuple:
    """A baked-in rig id this test can make approved + PSP-connected, so only the rig
    exclusion can keep it out. Prefer an id the shared DB does not hold; otherwise borrow
    an existing row and return its original state for restoring."""
    from services.test_merchant_policy import KNOWN_TEST_MERCHANT_IDS

    ids = sorted(KNOWN_TEST_MERCHANT_IDS)
    for mid in ids:
        row = await database.fetch_one(
            "SELECT 1 AS x FROM merchant_onboarding WHERE merchant_id = :m", {"m": mid}
        )
        if row is None:
            return mid, None
    mid = ids[0]
    row = await database.fetch_one(
        "SELECT status, psp_connected, auto_approved, verified_at FROM merchant_onboarding "
        "WHERE merchant_id = :m",
        {"m": mid},
    )
    return mid, {
        "status": row["status"],
        "psp_connected": row["psp_connected"],
        "auto_approved": row["auto_approved"],
        "verified_at": row["verified_at"],
    }


RIG_STORE_ID = f"st_ml_{RUN}"
# The other two rigs get a connected store too, so every rig meets the listing rules and
# only the rig exclusion can keep it out.
RIG_EXTRA_STORE_IDS = {"known": f"st_ml_rk_{RUN}", "env": f"st_ml_re_{RUN}"}
RIG_STEPS = ("known", "stores_table", "stores_columns", "store_row", "rig_rows", "orders")


@contextlib.asynccontextmanager
async def _rig_rows(fail_after: Optional[str] = None):
    """Seed the three rigs. Every mutation is recorded the moment it succeeds and the
    `try` opens before the first one, so a setup error part-way still undoes exactly what
    was done: the shared dialect-gate DB must never keep a baked-in id approved +
    PSP-connected, a stray store row, or an added column. `fail_after` injects that
    setup error for the teardown tests."""
    from services import test_merchant_policy

    test_merchant_policy.reset_cache()
    undo: list = []   # schema: tables / columns this created
    done: list = []   # rows: what to delete or put back, in order

    def _step(name: str) -> None:
        if fail_after == name:
            raise RuntimeError(f"injected setup failure after {name}")

    try:
        r_known, known_original = await _pick_known_rig()
        if known_original is None:
            await database.execute(_merchant_row(r_known))
            done.append(("delete_merchant", r_known))
        else:
            await database.execute(
                "UPDATE merchant_onboarding SET status = 'approved', psp_connected = TRUE, "
                "auto_approved = FALSE, verified_at = :v WHERE merchant_id = :m",
                {"m": r_known, "v": datetime(2026, 11, 1)},
            )
            done.append(("restore_merchant", r_known, known_original))
        _step("known")
        if not await _table_exists("merchant_stores"):
            await database.execute(MERCHANT_STORES_DDL)
            undo.append(("table", "merchant_stores"))
        _step("stores_table")
        await _ensure_columns("merchant_stores", STORE_COLUMNS, undo)
        _step("stores_columns")
        await _insert_store(RIG_STORE_ID, R_DEMO, "connected", domain=DEMO_DOMAIN)
        done.append(("delete_store", RIG_STORE_ID))
        for key, mid in (("known", r_known), ("env", R_ENV)):
            await _insert_store(RIG_EXTRA_STORE_IDS[key], mid, "connected")
            done.append(("delete_store", RIG_EXTRA_STORE_IDS[key]))
        _step("store_row")
        for mid in (R_ENV, R_DEMO):
            await database.execute(_merchant_row(mid))
            done.append(("delete_merchant", mid))
        _step("rig_rows")
        for mid in (R_ENV, R_DEMO, r_known):
            await database.execute(_order(mid, AGENT, 11))
            done.append(("delete_orders", mid))
        _step("orders")
        yield {"known": r_known, "env": R_ENV, "demo": R_DEMO}
    finally:
        for item in reversed(done):
            if item[0] == "delete_orders":
                await database.execute(
                    "DELETE FROM orders WHERE agent_id = :a AND merchant_id = :m",
                    {"a": AGENT, "m": item[1]},
                )
            elif item[0] == "delete_merchant":
                await database.execute(
                    "DELETE FROM merchant_onboarding WHERE merchant_id = :m", {"m": item[1]}
                )
            elif item[0] == "restore_merchant":
                await database.execute(
                    "UPDATE merchant_onboarding SET status = :s, psp_connected = :p, "
                    "auto_approved = :a, verified_at = :v WHERE merchant_id = :m",
                    {
                        "s": item[2]["status"],
                        "p": item[2]["psp_connected"],
                        "a": item[2]["auto_approved"],
                        "v": item[2]["verified_at"],
                        "m": item[1],
                    },
                )
            elif item[0] == "delete_store":
                await database.execute(
                    "DELETE FROM merchant_stores WHERE store_id = :s", {"s": item[1]}
                )
        await _restore(undo)
        test_merchant_policy.reset_cache()


@pytest.fixture
async def _rigs(_rows, monkeypatch):
    monkeypatch.setenv("PIVOTA_TEST_MERCHANT_IDS", R_ENV)
    # Checked around EVERY rig test, not only in the dedicated teardown tests: those run
    # after earlier rig tests, so a schema change one of them left behind would already
    # be part of their "before".
    before = await _db_state()
    async with _rig_rows() as rigs:
        yield rigs
    assert (await _db_state()) == before, "rig teardown left the shared DB changed"


async def _columns(table: str) -> list:
    """Column names over the async connection (a second, sync engine would wait on the
    SQLite file lock)."""
    from db.database import IS_SQLITE

    if IS_SQLITE:
        rows = await database.fetch_all(f"PRAGMA table_info({table})")
        return sorted(r["name"] for r in rows)
    rows = await database.fetch_all(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = :t",
        {"t": table},
    )
    return sorted(r["column_name"] for r in rows)


async def _db_state() -> dict:
    """What the rig setup may touch, for before/after comparison."""
    from services.test_merchant_policy import KNOWN_TEST_MERCHANT_IDS

    watched = sorted(KNOWN_TEST_MERCHANT_IDS) + [R_ENV, R_DEMO]
    merchants = {}
    for mid in watched:
        row = await database.fetch_one(
            "SELECT status, psp_connected, auto_approved, verified_at FROM merchant_onboarding "
            "WHERE merchant_id = :m",
            {"m": mid},
        )
        if row is not None:
            merchants[mid] = (
                row["status"],
                None if row["psp_connected"] is None else bool(row["psp_connected"]),
                None if row["auto_approved"] is None else bool(row["auto_approved"]),
                str(row["verified_at"]) if row["verified_at"] is not None else None,
            )
    store_cols = await _columns("merchant_stores") if await _table_exists("merchant_stores") else None
    store_rows = None
    if store_cols is not None:
        rig_store_ids = [RIG_STORE_ID, *RIG_EXTRA_STORE_IDS.values()]
        row = await database.fetch_one(
            "SELECT COUNT(*) AS n FROM merchant_stores WHERE store_id IN (:s0, :s1, :s2)",
            {f"s{i}": sid for i, sid in enumerate(rig_store_ids)},
        )
        store_rows = int(row["n"])
    params = {f"m{i}": mid for i, mid in enumerate(watched)}
    in_list = ", ".join(f":m{i}" for i in range(len(watched)))
    row = await database.fetch_one(
        f"SELECT COUNT(*) AS n FROM orders WHERE agent_id = :a AND merchant_id IN ({in_list})",
        {**params, "a": AGENT},
    )
    return {
        "merchants": merchants,
        "store_cols": store_cols,
        "store_rows": store_rows,
        "orders": int(row["n"]),
    }


async def _assert_rigs_are_eligible_but_for_the_exclusion(rigs) -> None:
    """Guard against a vacuous pass: every rig row must otherwise qualify for `available`."""
    for mid in rigs.values():
        row = await database.fetch_one(
            "SELECT status, psp_connected, auto_approved, verified_at FROM merchant_onboarding "
            "WHERE merchant_id = :m",
            {"m": mid},
        )
        assert row is not None, mid
        assert row["status"] == "approved" and bool(row["psp_connected"]) is True, mid
        # Staff-approved with a connected store: the managed-onboarding rules pass too.
        assert not row["auto_approved"] and row["verified_at"] is not None, mid
        store = await database.fetch_one(
            "SELECT COUNT(*) AS n FROM merchant_stores WHERE merchant_id = :m AND status = 'connected'",
            {"m": mid},
        )
        assert int(store["n"]) >= 1, mid


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
        # A new agent has no orders anywhere: no contact or processor details, no stats.
        assert m["contact_email"] is None
        assert m["psp_type"] is None
        assert m["created_at"] is None
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
    assert ordered["psp_type"] == "stripe"
    assert ordered["created_at"] is not None
    # Another agent's orders never leak into this agent's view.
    assert ours[M_OTHERS]["total_orders"] == 0
    assert ours[M_OTHERS]["contact_email"] is None
    assert ours[M_OTHERS]["psp_type"] is None
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


@pytest.mark.parametrize("status", ["pending", "rejected", "deleted", "suspended"])
async def test_available_accepts_only_the_active_status(_rows, status):
    # Any key could otherwise enumerate unapproved merchants by passing another status.
    resp = await _get(_app(), {"status": status})
    assert resp.status_code == 422


async def test_mine_still_filters_by_status(_rows):
    ours = _ours((await _get(_app(), {"scope": "mine", "status": "pending"})).json())
    assert set(ours) == {M_PENDING}
    # A deleted merchant never lists, even where the agent has orders.
    ours = _ours((await _get(_app(), {"scope": "mine", "status": "deleted"})).json())
    assert ours == {}


async def test_mine_honours_allowed_merchants(_rows):
    body = (await _get(_app(allowed_merchants=[M_NO_PSP_ORDERED]), {"scope": "mine"})).json()
    assert [m["merchant_id"] for m in body["merchants"]] == [M_NO_PSP_ORDERED]
    body = (await _get(_app(allowed_merchants=[]), {"scope": "mine"})).json()
    assert body["merchants"] == []


@pytest.mark.parametrize("limit", [0, -1, 101])
async def test_limit_is_bounded(_rows, limit):
    resp = await _get(_app(), {"limit": limit})
    assert resp.status_code == 422


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


async def test_approved_is_an_alias_for_active_in_available(_rows):
    active = (await _get(_app())).json()
    approved = await _get(_app(), {"status": "approved"})
    assert approved.status_code == 200
    assert _ours(approved.json()) == _ours(active)


async def test_null_psp_connected_is_not_transactable(_rows):
    ours = _ours((await _get(_app())).json())
    assert M_NULL_PSP not in ours


async def test_has_more_is_false_on_the_last_full_page(_rows):
    app = _app(allowed_merchants=[M_ORDERED, M_FRESH, M_OTHERS])
    exact = (await _get(app, {"limit": 3})).json()["pagination"]
    assert exact == {"total": 3, "limit": 3, "offset": 0, "has_more": False}
    tail = (await _get(app, {"limit": 2, "offset": 1})).json()["pagination"]
    assert tail["has_more"] is False


@pytest.mark.parametrize("scope", ["available", "mine"])
async def test_an_empty_allowlist_returns_before_touching_the_db(_rows, monkeypatch, scope):
    # SQLite accepts `IN ()`, so without this only the Postgres twin would catch a lost
    # early return.
    async def _boom(*a, **k):
        raise AssertionError("an empty allowlist must not query")

    monkeypatch.setattr(database, "fetch_all", _boom)
    monkeypatch.setattr(database, "fetch_one", _boom)
    resp = await _get(_app(allowed_merchants=[]), {"scope": scope})
    assert resp.status_code == 200
    assert resp.json()["merchants"] == []


async def test_a_db_error_is_not_echoed_to_the_caller(_rows, monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError('relation "merchant_onboarding_secret_detail" does not exist')

    monkeypatch.setattr(database, "fetch_all", _boom)
    resp = await _get(_app())
    assert resp.status_code == 500
    assert "secret_detail" not in resp.text
    assert resp.json()["detail"] == "Failed to list merchants"


async def test_test_and_demo_rigs_never_list_in_available(_rigs):
    await _assert_rigs_are_eligible_but_for_the_exclusion(_rigs)
    for agent in (AGENT, f"agent_ml_brand_new_{RUN}"):
        ids = {m["merchant_id"] for m in (await _get(_app(agent_id=agent))).json()["merchants"]}
        assert not ids & set(_rigs.values()), ids & set(_rigs.values())
        if agent == AGENT:
            assert M_ORDERED in ids


async def test_test_and_demo_rigs_never_list_in_mine(_rigs):
    await _assert_rigs_are_eligible_but_for_the_exclusion(_rigs)
    ids = {m["merchant_id"] for m in (await _get(_app(), {"scope": "mine"})).json()["merchants"]}
    assert not ids & set(_rigs.values())
    assert M_ORDERED in ids


async def test_an_allowlist_cannot_reach_a_rig(_rigs):
    body = (await _get(_app(allowed_merchants=list(_rigs.values()) + [M_FRESH]))).json()
    assert [m["merchant_id"] for m in body["merchants"]] == [M_FRESH]
    assert body["pagination"]["total"] == 1


async def test_a_failed_demo_domain_lookup_still_applies_the_static_ids(_rigs, monkeypatch):
    # The path production takes: the policy's merchant_stores query fails inside the resolver.
    real_fetch_all = database.fetch_all

    async def _stores_down(query, *a, **k):
        # Only the policy's demo-domain lookup: the listing query itself also reads
        # merchant_stores (the connected-store rule) and must keep working.
        if "merchant_stores" in str(query) and "LIKE :prefix" in str(query):
            raise RuntimeError("merchant_stores unavailable")
        return await real_fetch_all(query, *a, **k)

    from services import test_merchant_policy

    # No backoff here, so the recovery check below can retry at once; the window itself
    # is pinned in tests/services/test_test_merchant_policy.py.
    monkeypatch.setattr(test_merchant_policy, "_FAILURE_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(database, "fetch_all", _stores_down)
    ids = {m["merchant_id"] for m in (await _get(_app())).json()["merchants"]}
    assert _rigs["known"] not in ids
    assert _rigs["env"] not in ids
    # No last-good demo set yet (cache reset by the fixture), so only the domain leg is lost.
    assert _rigs["demo"] in ids
    # And the failure was not cached: once the store lookup recovers, the demo rig is gone.
    monkeypatch.setattr(database, "fetch_all", real_fetch_all)
    ids = {m["merchant_id"] for m in (await _get(_app())).json()["merchants"]}
    assert _rigs["demo"] not in ids


async def test_the_route_falls_back_to_static_ids_if_the_policy_raises(_rigs, monkeypatch):
    import routes.agent_sdk_fixed as mod

    async def _boom(*a, **k):
        raise RuntimeError("resolver down")

    monkeypatch.setattr(mod, "get_excluded_merchant_ids", _boom)
    ids = {m["merchant_id"] for m in (await _get(_app())).json()["merchants"]}
    # Baked-in and env ids still excluded; only the domain leg is lost.
    assert _rigs["known"] not in ids
    assert _rigs["env"] not in ids
    assert _rigs["demo"] in ids


async def test_rig_teardown_leaves_the_db_as_found(_rows, monkeypatch):
    monkeypatch.setenv("PIVOTA_TEST_MERCHANT_IDS", R_ENV)
    before = await _db_state()
    async with _rig_rows() as rigs:
        assert (await _db_state()) != before
        await _assert_rigs_are_eligible_but_for_the_exclusion(rigs)
    assert (await _db_state()) == before


async def test_rig_teardown_restores_a_borrowed_baked_in_row(_rows, monkeypatch):
    # Every baked-in id already present (non-approved, PSP off): the fixture must borrow
    # one and put it back exactly.
    from services.test_merchant_policy import KNOWN_TEST_MERCHANT_IDS

    monkeypatch.setenv("PIVOTA_TEST_MERCHANT_IDS", R_ENV)
    seeded = []
    for mid in sorted(KNOWN_TEST_MERCHANT_IDS):
        if await database.fetch_one(
            "SELECT 1 AS x FROM merchant_onboarding WHERE merchant_id = :m", {"m": mid}
        ) is None:
            await database.execute(_merchant_row(mid, status="pending", psp=False))
            seeded.append(mid)
    try:
        before = await _db_state()
        async with _rig_rows() as rigs:
            await _assert_rigs_are_eligible_but_for_the_exclusion(rigs)
        assert (await _db_state()) == before
    finally:
        for mid in seeded:
            await database.execute("DELETE FROM merchant_onboarding WHERE merchant_id = :m", {"m": mid})


@pytest.mark.parametrize("fail_after", RIG_STEPS)
async def test_a_rig_setup_error_leaves_the_db_as_found(_rows, monkeypatch, fail_after):
    monkeypatch.setenv("PIVOTA_TEST_MERCHANT_IDS", R_ENV)
    before = await _db_state()
    with pytest.raises(RuntimeError, match="injected setup failure"):
        async with _rig_rows(fail_after=fail_after):
            pass
    assert (await _db_state()) == before


# ---- managed onboarding (D-09): staff approval + a connected store ---------------------

# Each row misses exactly one listing rule; the SQL repairs only that one. A row that still
# did not list after its repair would mean some OTHER rule kept it out, and the test would
# pass for the wrong reason.
ONE_RULE_MISSING = {
    "signup_auto_approval": (M_FLOOR, "UPDATE merchant_onboarding SET auto_approved = FALSE WHERE merchant_id = :m"),
    "no_staff_verified_at": (M_UNVERIFIED, "UPDATE merchant_onboarding SET verified_at = :v WHERE merchant_id = :m"),
    "store_disconnected": (M_STORE_GONE, "UPDATE merchant_stores SET status = 'connected' WHERE merchant_id = :m"),
    "pending_review_with_psp": (
        M_PENDING_PSP,
        "UPDATE merchant_onboarding SET status = 'approved', verified_at = :v WHERE merchant_id = :m",
    ),
}


async def _available_ids(agent_id=AGENT):
    resp = await _get(_app(agent_id=agent_id))
    assert resp.status_code == 200, resp.text
    return {m["merchant_id"] for m in resp.json()["merchants"]}


async def test_only_staff_approved_merchants_with_a_connected_store_list(_rows):
    ids = await _available_ids()
    assert not ids & {M_FLOOR, M_UNVERIFIED, M_STORELESS, M_STORE_GONE, M_PENDING_PSP}
    # NULL auto_approved (M_OTHERS) and a padded ' Active ' store still list.
    assert {M_ORDERED, M_FRESH, M_OTHERS} <= ids


@pytest.mark.parametrize("rule", sorted(ONE_RULE_MISSING))
async def test_each_listing_rule_alone_keeps_a_merchant_out(_rows, rule):
    mid, repair = ONE_RULE_MISSING[rule]
    assert mid not in await _available_ids()
    params = {"m": mid}
    if ":v" in repair:
        params["v"] = datetime(2026, 11, 2)
    await database.execute(repair, params)
    assert mid in await _available_ids(), f"{rule}: repairing that one rule did not list it"


async def test_a_store_less_merchant_lists_once_a_store_connects(_rows):
    assert M_STORELESS not in await _available_ids()
    await _insert_store(f"st_ml_late_{RUN}", M_STORELESS, "connected")
    assert M_STORELESS in await _available_ids()


async def test_a_staff_approval_lists_a_floor_approved_merchant(_rows):
    """The marker end to end: staff re-approving a merchant the signup floor approved goes
    through update_kyc_status, which clears auto_approved, and only then does it list."""
    from db.merchant_onboarding import update_kyc_status

    assert M_FLOOR not in await _available_ids()
    await update_kyc_status(M_FLOOR, "approved", approved_by_staff=True)
    assert M_FLOOR in await _available_ids()


async def test_a_psp_connect_alone_never_lists_a_merchant(_rows):
    """What the PSP connect routes write (psp_connected, psp_type) on a pending signup with a
    store: not listed. The status a PSP route never writes is what keeps it out."""
    await database.execute(
        "UPDATE merchant_onboarding SET psp_connected = TRUE, psp_type = 'stripe' WHERE merchant_id = :m",
        {"m": M_PENDING_PSP},
    )
    assert M_PENDING_PSP not in await _available_ids()


async def test_mine_is_not_narrowed_by_the_listing_rules(_rows):
    """Order history is a different question: an agent that ordered from a merchant still
    sees it in `mine` whatever its onboarding state."""
    await database.execute(_order(M_FLOOR, AGENT, 9))
    await database.execute(_order(M_STORELESS, AGENT, 9))
    ids = {m["merchant_id"] for m in (await _get(_app(), {"scope": "mine"})).json()["merchants"]}
    assert {M_FLOOR, M_STORELESS} <= ids
