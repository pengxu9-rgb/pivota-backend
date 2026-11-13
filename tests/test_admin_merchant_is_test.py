"""POST/GET /admin/merchants/{merchant_id}/is-test, and what the fact does downstream.

The route (routes/admin_merchant_is_test.py) is admin-only, needs a reason, and writes the column
and an employee_audit_logs row in one transaction. The fact reaches the agent merchant list and
the protocol directory's rig domains only while MERCHANT_IS_TEST_FILTER_ENABLED is on; with it off
both answer exactly as before.

Drives the real routers on a minimal app (never `main`'s startup) against the test DB, with real
JWTs. tests/test_admin_merchant_is_test_postgres.py runs these cases on Postgres.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.database import database  # noqa: E402

RUN = uuid.uuid4().hex[:8]
AGENT = f"agent_it_{RUN}"
M_TEST = f"merch_it_flagged_{RUN}"
M_REAL = f"merch_it_real_{RUN}"
OURS = (M_TEST, M_REAL)
TEST_HOST = f"shop-a-{RUN}.example.com"
REAL_HOST = f"shop-b-{RUN}.example.com"
FLAG = "MERCHANT_IS_TEST_FILTER_ENABLED"
_MIGRATION = Path(__file__).resolve().parent.parent / "db/migrations/282_merchant_onboarding_is_test.sql"


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


async def _has_column(table: str, column: str) -> bool:
    try:
        await database.fetch_all(f"SELECT {column} FROM {table} WHERE 1 = 0")
        return True
    except Exception:  # noqa: BLE001 - the probe only says "missing"
        return False


async def add_is_test_column() -> None:
    """The column as migration 282 makes it. On Postgres the migration file itself; SQLite has no
    IF [NOT] EXISTS on ALTER, so the same column there."""
    from db.database import IS_SQLITE
    from db.sql_migrations import split_statements

    if IS_SQLITE:
        await database.execute(
            "ALTER TABLE merchant_onboarding ADD COLUMN is_test BOOLEAN NOT NULL DEFAULT FALSE"
        )
        return
    for statement in split_statements(_MIGRATION.read_text()):
        await database.execute(statement)


def _merchant_row(mid: str, host: str):
    """An approved merchant the agent listing shows: a staff approval since the D-09 cutover
    (verified_at set, auto_approved False) and a store (the legacy single-store record)."""
    from datetime import datetime

    from db.merchant_onboarding import merchant_onboarding

    return merchant_onboarding.insert().values(
        merchant_id=mid,
        business_name=f"Biz {mid}",
        store_url=f"https://{host}",
        contact_email=f"owner@{host}",
        region="US",
        status="approved",
        psp_connected=True,
        psp_type="stripe",
        operating_mode="storefront",
        apm_enabled=False,
        auto_approved=False,
        verified_at=datetime(2026, 11, 9, 0, 0, 0),
        mcp_connected=True,
        mcp_shop_domain=host,
    )


@pytest.fixture
async def rows(monkeypatch):
    """Two approved, PSP-connected merchants. Everything this creates (tables, the column, rows)
    is removed again: the Postgres dialect gate shares one database across files."""
    import sqlalchemy

    from db.database import metadata
    from db.merchant_onboarding import merchant_onboarding
    from db.orders import orders
    from db.reviews_center import employee_audit_logs
    from services import test_merchant_policy

    monkeypatch.delenv(FLAG, raising=False)
    test_merchant_policy.reset_cache()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    created = [
        t
        for t in ("merchant_onboarding", "orders", "employee_audit_logs", "merchant_psps", "merchant_stores")
        if not await _table_exists(t)
    ]
    engine = sqlalchemy.create_engine(_sync_url())
    metadata.create_all(engine, tables=[merchant_onboarding, orders, employee_audit_logs], checkfirst=True)
    engine.dispose()
    await database.execute(
        "CREATE TABLE IF NOT EXISTS merchant_psps ("
        "psp_id VARCHAR(100) PRIMARY KEY, merchant_id VARCHAR(50), provider VARCHAR(50), "
        "status VARCHAR(50), connected_at TIMESTAMP)"
    )
    await database.execute(
        "CREATE TABLE IF NOT EXISTS merchant_stores ("
        "store_id VARCHAR(50) PRIMARY KEY, merchant_id VARCHAR(50), domain VARCHAR(255), status VARCHAR(50))"
    )
    # The listing reads merchant_stores.status (prod has it: db/schema_guard.py indexes it), and a table another
    # file left behind may not. Patch, never drop: the column is added and STAYS, the rule
    # tests/schema_guard_tables.py and tests/model_schema.py follow, because dropping a column from a table some
    # other file built is worse (and SQLite refuses it once something indexes the column). Neither helper can
    # build this table: merchant_stores has no SQLAlchemy model, and ensure_required_schema_light neither creates
    # it on SQLite nor adds `status` to one it finds. A table this fixture created is dropped whole below.
    if not await _has_column("merchant_stores", "status"):
        await database.execute("ALTER TABLE merchant_stores ADD COLUMN status TEXT")
    added_column = False
    try:
        if not await _has_column("merchant_onboarding", "is_test"):
            await add_is_test_column()
            added_column = True
        await database.execute(_merchant_row(M_TEST, TEST_HOST))
        await database.execute(_merchant_row(M_REAL, REAL_HOST))
        yield
    finally:
        params = {"a": M_TEST, "b": M_REAL}
        await database.execute(
            "DELETE FROM employee_audit_logs WHERE target_type = 'merchant' AND target_id IN (:a, :b)",
            params,
        )
        await database.execute("DELETE FROM merchant_onboarding WHERE merchant_id IN (:a, :b)", params)
        if added_column and "merchant_onboarding" not in created:
            await database.execute("ALTER TABLE merchant_onboarding DROP COLUMN is_test")
        for table in reversed(created):
            await database.execute(f"DROP TABLE IF EXISTS {table}")
        test_merchant_policy.reset_cache()
        if not was_connected and database.is_connected:
            await database.disconnect()


def _token(role: str, **extra) -> str:
    from utils.auth import create_access_token

    return create_access_token({"sub": f"u_{role}", "email": f"{role}@example.com", "role": role, **extra})


def _admin_app():
    from fastapi import FastAPI

    from routes.admin_merchant_is_test import router

    app = FastAPI()
    app.include_router(router)
    return app


def _agent_app():
    from fastapi import FastAPI

    from routes.agent_auth import AgentContext, get_agent_context
    from routes.agent_sdk_fixed import router

    app = FastAPI()
    app.include_router(router)
    ctx = AgentContext({"agent_id": AGENT, "agent_name": "t", "allowed_merchants": None}, request=None)
    app.dependency_overrides[get_agent_context] = lambda: ctx
    return app


async def _call(app, method: str, path: str, *, token=None, json=None, params=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.request(method, path, headers=headers, json=json, params=params)


async def _set(mid: str, value: bool, reason: str = "internal rig for partner-a", role: str = "admin"):
    return await _call(
        _admin_app(), "POST", f"/admin/merchants/{mid}/is-test",
        token=_token(role, employee_id="emp_1"), json={"is_test": value, "reason": reason},
    )


async def _stored(mid: str) -> bool:
    row = await database.fetch_one("SELECT is_test FROM merchant_onboarding WHERE merchant_id = :m", {"m": mid})
    return bool(row["is_test"])


async def _audit_rows(mid: str) -> list:
    rows = await database.fetch_all(
        "SELECT action, reason, actor_employee_id, actor_email, before_json, after_json "
        "FROM employee_audit_logs WHERE target_type = 'merchant' AND target_id = :m ORDER BY id",
        {"m": mid},
    )
    return [dict(r) for r in rows]


def _as_dict(value):
    if isinstance(value, str):
        import json

        return json.loads(value)
    return value


async def _listed() -> set:
    resp = await _call(_agent_app(), "GET", "/agent/v1/merchants", params={"limit": 100})
    assert resp.status_code == 200, resp.text
    return {m["merchant_id"] for m in resp.json()["merchants"]} & set(OURS)


# ── the guard ────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_anonymous_is_refused_and_nothing_changes(rows, method):
    resp = await _call(
        _admin_app(), method, f"/admin/merchants/{M_TEST}/is-test",
        json={"is_test": True, "reason": "no token"} if method == "POST" else None,
    )
    assert resp.status_code in (401, 403)
    assert await _stored(M_TEST) is False
    assert await _audit_rows(M_TEST) == []


@pytest.mark.parametrize("role", ["merchant", "agent", "employee", "outsourced"])
async def test_non_admin_roles_are_refused(rows, role):
    resp = await _set(M_TEST, True, role=role)
    assert resp.status_code == 403, resp.text
    get = await _call(_admin_app(), "GET", f"/admin/merchants/{M_TEST}/is-test", token=_token(role))
    assert get.status_code == 403
    assert await _stored(M_TEST) is False
    assert await _audit_rows(M_TEST) == []


@pytest.mark.parametrize("body", [{"is_test": True}, {"is_test": True, "reason": "   "}, {"is_test": True, "reason": "x"}, {"reason": "no value given"}])
async def test_a_reason_and_a_value_are_required(rows, body):
    resp = await _call(
        _admin_app(), "POST", f"/admin/merchants/{M_TEST}/is-test", token=_token("admin"), json=body
    )
    assert resp.status_code == 422, resp.text
    assert await _stored(M_TEST) is False
    assert await _audit_rows(M_TEST) == []


# ── the write ────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", ["admin", "super_admin"])
async def test_admin_sets_it_with_an_audit_row(rows, role):
    resp = await _set(M_TEST, True, reason="  internal rig for partner-a  ", role=role)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"merchant_id": M_TEST, "is_test": True, "changed": True}
    assert await _stored(M_TEST) is True
    assert await _stored(M_REAL) is False
    [row] = await _audit_rows(M_TEST)
    assert row["action"] == "merchant.is_test.set"
    assert row["reason"] == "internal rig for partner-a"
    assert row["actor_employee_id"] == "emp_1"
    assert row["actor_email"] == f"{role}@example.com"
    assert _as_dict(row["before_json"]) == {"is_test": False}
    assert _as_dict(row["after_json"]) == {"is_test": True}


async def test_setting_the_same_value_changes_nothing_and_writes_no_row(rows):
    assert (await _set(M_TEST, True)).status_code == 200
    again = await _set(M_TEST, True, reason="second click")
    assert again.status_code == 200
    assert again.json() == {"merchant_id": M_TEST, "is_test": True, "changed": False}
    assert len(await _audit_rows(M_TEST)) == 1
    noop_clear = await _set(M_REAL, False, reason="already real")
    assert noop_clear.json()["changed"] is False
    assert await _audit_rows(M_REAL) == []


async def test_clearing_it_is_audited_too(rows):
    await _set(M_TEST, True)
    resp = await _set(M_TEST, False, reason="real merchant after all")
    assert resp.json() == {"merchant_id": M_TEST, "is_test": False, "changed": True}
    assert await _stored(M_TEST) is False
    actions = [r["action"] for r in await _audit_rows(M_TEST)]
    assert actions == ["merchant.is_test.set", "merchant.is_test.clear"]


async def test_unknown_merchant_is_404_and_writes_nothing(rows):
    ghost = f"merch_it_ghost_{RUN}"
    resp = await _set(ghost, True)
    assert resp.status_code == 404
    assert await _audit_rows(ghost) == []
    get = await _call(_admin_app(), "GET", f"/admin/merchants/{ghost}/is-test", token=_token("admin"))
    assert get.status_code == 404


async def test_get_returns_the_value_and_its_audit_trail(rows):
    await _set(M_TEST, True, reason="rig one")
    await _set(M_TEST, False, reason="rig two")
    resp = await _call(_admin_app(), "GET", f"/admin/merchants/{M_TEST}/is-test", token=_token("admin"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["merchant_id"] == M_TEST and body["is_test"] is False
    assert [a["reason"] for a in body["audit"]] == ["rig two", "rig one"]


async def test_a_failed_audit_insert_rolls_the_change_back(rows, monkeypatch):
    real_execute = database.execute

    async def _audit_down(query, *a, **k):
        if "employee_audit_logs" in str(query):
            raise RuntimeError("relation employee_audit_logs is unavailable")
        return await real_execute(query, *a, **k)

    monkeypatch.setattr(database, "execute", _audit_down)
    resp = await _set(M_TEST, True)
    monkeypatch.undo()
    assert resp.status_code == 503
    assert "employee_audit_logs" not in resp.text
    assert await _stored(M_TEST) is False


# ── what the fact does: the agent merchant list ──────────────────────────────────────────────


async def test_flag_off_an_is_test_merchant_still_lists(rows):
    assert await _listed() == {M_TEST, M_REAL}
    await _set(M_TEST, True)
    assert await _listed() == {M_TEST, M_REAL}


async def test_flag_on_an_is_test_merchant_is_dropped_at_once(rows, monkeypatch):
    monkeypatch.setenv(FLAG, "true")
    assert await _listed() == {M_TEST, M_REAL}  # fills the cache before the write
    await _set(M_TEST, True)
    assert await _listed() == {M_REAL}  # the write invalidated this instance's cache
    await _set(M_TEST, False, reason="real merchant after all")
    assert await _listed() == {M_TEST, M_REAL}


async def test_flag_on_scope_mine_drops_it_too(rows, monkeypatch):
    from db.orders import orders
    import json

    for mid in OURS:
        await database.execute(
            orders.insert().values(
                order_id=f"ord_it_{uuid.uuid4().hex[:12]}", merchant_id=mid, agent_id=AGENT,
                customer_email="buyer@example.com", shipping_address=json.dumps({"country": "US"}),
                items=json.dumps([]), subtotal=10, total=10, currency="USD", payment_status="paid",
                is_deleted=False,
            )
        )
    try:
        await _set(M_TEST, True)
        monkeypatch.setenv(FLAG, "true")
        resp = await _call(_agent_app(), "GET", "/agent/v1/merchants", params={"scope": "mine", "limit": 100})
        assert {m["merchant_id"] for m in resp.json()["merchants"]} & set(OURS) == {M_REAL}
    finally:
        await database.execute("DELETE FROM orders WHERE agent_id = :a", {"a": AGENT})


# ── what the fact does: the protocol directory's rig domains ─────────────────────────────────


async def test_directory_rig_domains_follow_the_flag(rows, monkeypatch):
    from services import agent_merchant_directory as directory
    from services import test_merchant_policy

    await _set(M_TEST, True)
    off = await directory._rig_and_rejected_domains()
    assert TEST_HOST not in off and REAL_HOST not in off
    monkeypatch.setenv(FLAG, "true")
    test_merchant_policy.reset_cache()
    on = await directory._rig_and_rejected_domains()
    assert TEST_HOST in on and REAL_HOST not in on
    assert not directory.is_listable_domain(TEST_HOST, excluded=on)
    assert directory.is_listable_domain(REAL_HOST, excluded=on)


# ── phase B: the test-traffic classifier (services/test_traffic) ─────────────────────────────

STAMP = "TEST_TRAFFIC_STAMP_ENABLED"


def _order_data(mid: str) -> dict:
    return {
        "merchant_id": mid,
        "customer_email": "buyer@example.com",
        "shipping_address": {"country": "US"},
        "items": [{"product_id": "p1", "quantity": 1, "unit_price": 5.0, "subtotal": 5.0}],
        "subtotal": 5.0,
        "total": 5.0,
        "currency": "USD",
        "agent_id": AGENT,
        "metadata": {"pvt_click_id": f"clk_{mid}"},
    }


async def _create_and_read(mid: str) -> dict:
    """The real producer: db.orders.create_order (it reads the merchant's is_test by primary key,
    then classifies and stamps). Returns the stored metadata."""
    from db.orders import create_order

    order_id = await create_order(_order_data(mid))
    raw = await database.fetch_val("SELECT metadata FROM orders WHERE order_id = :o", {"o": order_id})
    return _as_dict(raw) or {}


async def _counted_orders() -> set:
    """Which of our merchants' orders a money read keeps (services.test_traffic.not_test_traffic_sql,
    the predicate the GMV roll-up, statements and outcomes use)."""
    from services.test_traffic import not_test_traffic_sql

    rows = await database.fetch_all(
        "SELECT DISTINCT merchant_id FROM orders WHERE agent_id = :a AND "
        + not_test_traffic_sql("metadata"),
        {"a": AGENT},
    )
    return {r["merchant_id"] for r in rows} & set(OURS)


@pytest.fixture
async def orders_cleanup(rows):
    yield
    await database.execute("DELETE FROM orders WHERE agent_id = :a", {"a": AGENT})


async def test_an_order_on_an_is_test_merchant_classifies_as_test(rows, orders_cleanup, monkeypatch):
    monkeypatch.setenv(FLAG, "true")
    monkeypatch.setenv(STAMP, "true")
    await _set(M_TEST, True)
    test_md = await _create_and_read(M_TEST)
    real_md = await _create_and_read(M_REAL)
    assert test_md == {
        "pvt_click_id": f"clk_{M_TEST}", "test_traffic": True, "test_traffic_reasons": ["test_merchant"],
    }
    assert real_md == {"pvt_click_id": f"clk_{M_REAL}"}
    # and the money read drops it, keeping the real one
    assert await _counted_orders() == {M_REAL}
    # create_order decided from its own primary-key read: it neither needed nor filled the cache
    from services import test_merchant_policy
    from services import test_traffic as tt

    assert test_merchant_policy.cached_is_test_merchant_ids() == set()
    assert tt.classify_test_traffic(
        merchant_ids=[M_TEST.upper()], synthetic_call=False, is_test_merchant_ids={M_TEST}
    ) == ["test_merchant"]
    assert tt.classify_test_traffic(merchant_ids=[M_REAL], synthetic_call=False, is_test_merchant_ids={M_TEST}) == []


@pytest.mark.parametrize("filter_flag", ["", "false"])
async def test_flag_off_an_is_test_merchants_order_is_real(rows, orders_cleanup, monkeypatch, filter_flag):
    monkeypatch.setenv(FLAG, filter_flag)
    monkeypatch.setenv(STAMP, "true")
    await _set(M_TEST, True)
    assert await _create_and_read(M_TEST) == {"pvt_click_id": f"clk_{M_TEST}"}
    assert await _counted_orders() == {M_TEST}


async def test_stamp_off_warms_nothing_and_stamps_nothing(rows, orders_cleanup, monkeypatch):
    monkeypatch.setenv(FLAG, "true")
    monkeypatch.delenv(STAMP, raising=False)
    await _set(M_TEST, True)
    from services import test_merchant_policy

    test_merchant_policy.reset_cache()
    real_fetch_all, real_fetch_one = database.fetch_all, database.fetch_one
    reads = []

    async def _spy(query, *a, **k):
        reads.append(str(query))
        return await real_fetch_all(query, *a, **k)

    async def _spy_one(query, *a, **k):
        reads.append(str(query))
        return await real_fetch_one(query, *a, **k)

    monkeypatch.setattr(database, "fetch_all", _spy)
    monkeypatch.setattr(database, "fetch_one", _spy_one)
    assert await _create_and_read(M_TEST) == {"pvt_click_id": f"clk_{M_TEST}"}
    assert not [q for q in reads if "is_test" in q]


async def test_unknown_is_not_test_when_the_column_cannot_be_read(rows, orders_cleanup, monkeypatch):
    """The billing fail direction: if the is_test read fails, the order is classified real and stays
    in the money numbers (a counted test order can be excluded later; a dropped real one is lost)."""
    monkeypatch.setenv(FLAG, "true")
    monkeypatch.setenv(STAMP, "true")
    await _set(M_TEST, True)
    from services import test_merchant_policy

    test_merchant_policy.reset_cache()
    real_fetch_all, real_fetch_one = database.fetch_all, database.fetch_one

    async def _is_test_down(query, *a, **k):
        if "is_test" in str(query):
            raise RuntimeError("column merchant_onboarding.is_test does not exist")
        return await real_fetch_all(query, *a, **k)

    async def _is_test_down_one(query, *a, **k):
        if "is_test" in str(query):
            raise RuntimeError("column merchant_onboarding.is_test does not exist")
        return await real_fetch_one(query, *a, **k)

    monkeypatch.setattr(database, "fetch_all", _is_test_down)
    monkeypatch.setattr(database, "fetch_one", _is_test_down_one)
    assert await _create_and_read(M_TEST) == {"pvt_click_id": f"clk_{M_TEST}"}
    monkeypatch.undo()
    assert await _counted_orders() == {M_TEST}


async def test_a_cleared_merchant_is_real_again_on_this_instance(rows, orders_cleanup, monkeypatch):
    monkeypatch.setenv(FLAG, "true")
    monkeypatch.setenv(STAMP, "true")
    await _set(M_TEST, True)
    assert (await _create_and_read(M_TEST)).get("test_traffic") is True
    await _set(M_TEST, False, reason="real merchant after all")
    assert "test_traffic" not in await _create_and_read(M_TEST)


async def test_turning_the_filter_off_makes_the_next_order_real_at_once(rows, orders_cleanup, monkeypatch):
    monkeypatch.setenv(FLAG, "true")
    monkeypatch.setenv(STAMP, "true")
    await _set(M_TEST, True)
    assert (await _create_and_read(M_TEST)).get("test_traffic") is True  # the cache is now filled
    monkeypatch.setenv(FLAG, "false")
    assert "test_traffic" not in await _create_and_read(M_TEST)


# ── M-05 P2-1: a stale cache never says "test" to a money writer ─────────────────────────────


async def _clear_on_another_instance(monkeypatch, mid: str) -> None:
    """The real POST, served by ANOTHER instance: the row changes, but this process's cache is
    never told (its invalidate_cache is not what runs there)."""
    from services import test_merchant_policy

    with monkeypatch.context() as m:
        m.setattr(test_merchant_policy, "invalidate_cache", lambda: None)
        resp = await _set(mid, False, reason="real merchant after all")
    assert resp.json()["changed"] is True


async def test_a_clear_on_another_instance_makes_the_next_order_real_despite_a_warm_cache(
    rows, orders_cleanup, monkeypatch
):
    from services import test_merchant_policy

    monkeypatch.setenv(FLAG, "true")
    monkeypatch.setenv(STAMP, "true")
    await _set(M_TEST, True)
    assert M_TEST in await test_merchant_policy.get_excluded_merchant_ids(database)  # warm
    await _clear_on_another_instance(monkeypatch, M_TEST)
    # This process still holds the old set: listings may hide the merchant until the TTL ...
    assert M_TEST in await test_merchant_policy.get_excluded_merchant_ids(database)
    assert M_TEST in test_merchant_policy.cached_is_test_merchant_ids()
    # ... but the order is classified from the row as it is now.
    assert "test_traffic" not in await _create_and_read(M_TEST)
    assert await _counted_orders() == {M_TEST}


async def test_a_primary_key_read_that_times_out_makes_the_order_real(rows, orders_cleanup, monkeypatch):
    import asyncio
    import time

    from services import test_merchant_policy

    monkeypatch.setenv(FLAG, "true")
    monkeypatch.setenv(STAMP, "true")
    await _set(M_TEST, True)
    monkeypatch.setattr(test_merchant_policy, "PK_READ_TIMEOUT_SECONDS", 0.05)
    real_fetch_one = database.fetch_one

    async def _hangs(query, *a, **k):
        if "is_test" in str(query):
            await asyncio.Event().wait()
        return await real_fetch_one(query, *a, **k)

    monkeypatch.setattr(database, "fetch_one", _hangs)
    started = time.monotonic()
    md = await asyncio.wait_for(_create_and_read(M_TEST), 5.0)
    assert time.monotonic() - started < 2.0
    monkeypatch.setattr(database, "fetch_one", real_fetch_one)
    assert md == {"pvt_click_id": f"clk_{M_TEST}"}


# ── M-05 P2-2: the lock-time check, on this dialect ──────────────────────────────────────────


@pytest.mark.parametrize("filter_flag", ["", "false"])
def test_lock_time_check_is_empty_with_the_filter_off(monkeypatch, filter_flag):
    from services import invoice_generation_service as inv
    from services.test_traffic import is_test_merchant_lock_sql

    monkeypatch.setenv(FLAG, filter_flag)
    assert is_test_merchant_lock_sql("x.merchant_id") == ""
    assert inv._gmv_rows_query() == inv._GMV_ROWS_QUERY


async def test_lock_time_check_drops_a_merchant_that_is_is_test_now(rows, orders_cleanup, monkeypatch):
    """History: orders written with both flags off carry no stamp. With the filter on, the
    lock-time predicate leaves the merchant out by its CURRENT is_test; off, it counts."""
    from services.test_traffic import is_test_merchant_lock_sql

    for mid in OURS:
        await _create_and_read(mid)  # flags off: unstamped history
    await _set(M_TEST, True)

    async def _locked() -> set:
        rows_ = await database.fetch_all(
            "SELECT DISTINCT merchant_id FROM orders WHERE agent_id = :a"
            + is_test_merchant_lock_sql("orders.merchant_id"),
            {"a": AGENT},
        )
        return {r["merchant_id"] for r in rows_} & set(OURS)

    assert await _locked() == {M_TEST, M_REAL}
    monkeypatch.setenv(FLAG, "true")
    assert await _locked() == {M_REAL}
    await _set(M_TEST, False, reason="real merchant after all")
    assert await _locked() == {M_TEST, M_REAL}
