"""The routes written for the legacy agents table (name, company, use_case, status, request_count)
against real Postgres, on the agents table the app's own model builds (db.agents.agents, the shape
create_all gives prod) plus the email column prod carries out-of-band (probe 2026-09-24).

Pinned:
- POST /admin/fix/agents-data and GET /admin/fix/agents-status are retired: 501, agents untouched
  (the POST filled empty names/emails with company/use_case and `<company>@example.com`);
- POST /admin/fix/agents-table is retired: 501, and agents keeps its rows and its model columns
  (it ran DROP TABLE agents CASCADE and re-created the legacy shape for an admin caller);
- POST /employee/agents/create and POST /admin/init/agent-test-key are retired: 501 for any body,
  no agents row written, no plaintext ak_live_ key over a redacted:<agent_id> marker (the init
  route's UPDATE branch did exactly that to an existing agent@test.com);
- POST /admin/fix/agent-metrics and /agent-metrics-v2 are retired: 501, the agents counters are
  untouched and the agent_metrics_24h view is not swapped (their UPDATE named request_count, so it
  could never run; dropping only that column would have armed a bulk counter overwrite). The
  fixture seeds a paid order and 2xx usage logs that disagree with the seeded counters, so either
  UPDATE, run without request_count, would change them;
- every admin route here still answers 403 to a non-admin, before any 501 or any data (the GETs
  now return owner emails and per-agent GMV, which they never did while they 500'd);
- GET /agent/metrics/agents, /admin/fix/agent-metrics-status and /admin/fix/agent-orders-check
  read agent_name / owner_email / is_active / total_requests and answer with real rows (they used
  to answer {"status": "error"} or 500 on every call). A NULL is_active counts as active, as it
  does at the auth door.

    DATABASE_URL=postgresql://localhost/pivota_agents_readers_test \\
        .venv/bin/python -m pytest tests/test_agents_legacy_schema_readers_postgres.py
"""

import hashlib
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
pytestmark = pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL")

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
ADMIN = {"role": "admin", "email": "ops@example.com", "user_id": "u_ops"}

ALPHA, BRAVO, CHARLIE = "agent_legacy_alpha", "agent_legacy_bravo", "agent_legacy_charlie"

# main.py startup DDL; /admin/fix/agent-orders-check joins it.
_AGENT_MERCHANTS_DDL = """
    CREATE TABLE IF NOT EXISTS agent_merchants (
        agent_id VARCHAR(50),
        merchant_id VARCHAR(50),
        connected_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        permissions TEXT,
        PRIMARY KEY (agent_id, merchant_id)
    )
"""

_COUNTERS_SQL = """
    SELECT agent_id, api_key, total_requests, total_orders, total_gmv, success_rate, last_used_at
    FROM agents ORDER BY agent_id
"""


def _sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _assert_throwaway_database():
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to drop tables in {dbname!r}; throwaway only")


@pytest.fixture
async def db():
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateIndex, CreateTable

    import db.agents as agents_db
    from db.database import database
    from db.orders import orders as orders_table

    _assert_throwaway_database()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await database.execute("DROP VIEW IF EXISTS agent_metrics_24h CASCADE")
    for table in ("agent_merchants", "agent_usage_logs", "orders", "agents"):
        await database.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    dialect = postgresql.dialect()
    await database.execute(str(CreateTable(agents_db.agents).compile(dialect=dialect)))
    for index in agents_db.agents.indexes:  # unique=True/index=True columns: agent_id, api_key
        await database.execute(str(CreateIndex(index).compile(dialect=dialect)))
    # Prod has an email column no migration in this repo creates (probe 2026-09-24).
    await database.execute("ALTER TABLE agents ADD COLUMN email VARCHAR(255)")
    for table in (orders_table, agents_db.agent_usage_logs):
        await database.execute(str(CreateTable(table).compile(dialect=dialect)))
    await database.execute(_AGENT_MERCHANTS_DDL)
    # A paid order for ALPHA that disagrees with its seeded counters (7 requests, 2 orders, 30.00
    # GMV), so the v2 UPDATE -- run without request_count -- would rewrite them.
    await database.execute(
        """
        INSERT INTO orders (order_id, merchant_id, customer_email, shipping_address, items,
                            subtotal, total, payment_status, agent_id)
        VALUES ('ord_legacy_alpha', 'merch_legacy', 'buyer@example.com', '{}', '[]',
                45.00, 45.00, 'paid', :agent_id)
        """,
        {"agent_id": ALPHA},
    )

    for agent_id, name, is_active, email, total_requests in (
        (ALPHA, "Alpha", True, "agent@test.com", 7),
        (BRAVO, "Bravo", None, "bravo@example.com", 0),
        (CHARLIE, "Charlie", False, "charlie@example.com", 4),
    ):
        await database.execute(
            """
            INSERT INTO agents (agent_id, agent_name, agent_type, api_key, api_key_hash, is_active,
                                owner_email, email, total_requests, total_orders, total_gmv,
                                success_rate)
            VALUES (:agent_id, :name, 'custom', :api_key, :api_key_hash, :is_active,
                    :owner_email, :email, :total_requests, 2, 30.00, 50.00)
            """,
            {
                "agent_id": agent_id,
                "name": name,
                "api_key": f"redacted:{agent_id}",
                "api_key_hash": _sha(f"ak_live_{agent_id}"),
                "is_active": is_active,
                "owner_email": f"owner+{name.lower()}@example.com",
                "email": email,
                "total_requests": total_requests,
            },
        )

    now = datetime.now(timezone.utc)
    for i, (agent_id, status_code, ms, age) in enumerate(
        (
            (ALPHA, 200, 100, timedelta(hours=1)),
            (ALPHA, 201, 200, timedelta(hours=2)),
            (ALPHA, 500, 300, timedelta(hours=3)),
            (ALPHA, 200, 999, timedelta(days=3)),  # outside the 24h window
            (CHARLIE, 200, 50, timedelta(hours=1)),  # inactive agent
        )
    ):
        await database.execute(
            """
            INSERT INTO agent_usage_logs (agent_id, endpoint, method, request_id, status_code,
                                          response_time_ms, timestamp)
            VALUES (:agent_id, '/agent/v1/products/search', 'GET', :rid, :sc, :ms, :ts)
            """,
            {"agent_id": agent_id, "rid": f"req_{i}", "sc": status_code, "ms": ms, "ts": now - age},
        )

    try:
        yield database
    finally:
        await database.execute("DROP VIEW IF EXISTS agent_metrics_24h CASCADE")
        for table in ("agent_merchants", "agent_usage_logs", "orders", "agents"):
            await database.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        if not was_connected:
            await database.disconnect()


@pytest.fixture
def user():
    return dict(ADMIN)


@pytest.fixture
async def client(db, user):
    import httpx
    from fastapi import FastAPI

    from routes.admin_fix_agent_metrics import router as admin_fix_router
    from routes.admin_fix_agent_metrics_v2 import router as admin_fix_v2_router
    from routes.admin_fix_agents import router as admin_fix_agents_router
    from routes.agent_metrics import router as agent_metrics_router
    from routes.employee_agent_mgmt import router as employee_agent_router
    from routes.fix_agents_table import router as fix_agents_table_router
    from routes.init_agent_key import router as init_agent_key_router
    from utils.auth import get_current_user, require_admin_or_key

    app = FastAPI()
    for router in (
        employee_agent_router,
        init_agent_key_router,
        agent_metrics_router,
        admin_fix_router,
        admin_fix_v2_router,
        fix_agents_table_router,
        admin_fix_agents_router,
    ):
        app.include_router(router)
    # require_admin is NOT overridden: it runs its real role check on the overridden user.
    # require_admin_or_key is (it also reads X-ADMIN-KEY), so init's guard is pinned elsewhere
    # (tests/test_unauthenticated_admin_ops_routes.py).
    for dep in (get_current_user, require_admin_or_key):
        app.dependency_overrides[dep] = lambda: user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _counters(db):
    return [dict(r) for r in await db.fetch_all(_COUNTERS_SQL)]


async def _view_exists(db):
    return await db.fetch_val(
        "SELECT EXISTS (SELECT 1 FROM information_schema.views WHERE table_name = 'agent_metrics_24h')"
    )


@pytest.mark.parametrize(
    "body",
    [
        # CreateAgentRequest's shape: this reached the INSERT and 500'd on the missing columns.
        {"name": "New", "email": "new@example.com", "company": "Co", "use_case": "shopping"},
        # The employee portal's createAgent body: this 422'd on company / use_case.
        {"name": "New", "email": "new@example.com", "phone": "+1 555 0100"},
    ],
)
async def test_employee_create_agent_is_retired_and_writes_nothing(client, db, body):
    before = await _counters(db)

    resp = await client.post("/employee/agents/create", json=body)

    assert resp.status_code == 501, resp.text
    assert "/agent/account/register" in resp.json()["detail"]
    assert await _counters(db) == before
    assert await db.fetch_val("SELECT COUNT(*) FROM agents WHERE email = 'new@example.com'") == 0


async def test_employee_create_agent_still_refuses_non_staff(client, user):
    user["role"] = "merchant"

    resp = await client.post("/employee/agents/create", json={})

    assert resp.status_code == 403, resp.text


async def test_init_agent_test_key_is_retired_and_keeps_the_redacted_marker(client, db):
    # ALPHA is agent@test.com, so the old handler took its UPDATE branch and wrote a plaintext
    # ak_live_ key over redacted:agent_legacy_alpha.
    before = await _counters(db)

    resp = await client.post("/admin/init/agent-test-key")

    assert resp.status_code == 501, resp.text
    assert await _counters(db) == before
    assert await db.fetch_val(
        "SELECT api_key FROM agents WHERE agent_id = :a", {"a": ALPHA}
    ) == f"redacted:{ALPHA}"
    assert await db.fetch_val("SELECT COUNT(*) FROM agents WHERE api_key LIKE 'ak_live_%'") == 0


async def test_init_agent_test_key_writes_no_row_when_the_test_agent_is_absent(client, db):
    await db.execute("DELETE FROM agents WHERE email = 'agent@test.com'")
    before = await _counters(db)

    resp = await client.post("/admin/init/agent-test-key")

    assert resp.status_code == 501, resp.text
    assert await _counters(db) == before


async def _agents_table_snapshot(db):
    """Every row and column value, plus the column types / nullability and the indexes: a handler
    that UPDATEs, ALTERs or re-creates agents before its 501 changes at least one of these."""
    rows = [
        tuple(sorted((k, str(v)) for k, v in dict(r).items()))
        for r in await db.fetch_all("SELECT * FROM agents ORDER BY agent_id")
    ]
    columns = [
        tuple(dict(r).values())
        for r in await db.fetch_all(
            "SELECT column_name, data_type, is_nullable, character_maximum_length "
            "FROM information_schema.columns WHERE table_name = 'agents' ORDER BY ordinal_position"
        )
    ]
    indexes = [
        tuple(dict(r).values())
        for r in await db.fetch_all(
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'agents' ORDER BY indexname"
        )
    ]
    return rows, columns, indexes


async def test_fix_agents_table_is_retired_and_keeps_the_table(client, db):
    before = await _agents_table_snapshot(db)
    assert len(before[0]) == 3 and before[2]  # seeded rows and the model's indexes are there

    resp = await client.post("/admin/fix/agents-table")

    assert resp.status_code == 501, resp.text
    assert await _agents_table_snapshot(db) == before


@pytest.mark.parametrize("method,path", [("POST", "/admin/fix/agents-data"), ("GET", "/admin/fix/agents-status")])
async def test_admin_fix_agents_routes_are_retired_and_write_nothing(client, db, method, path):
    # An agent with no email is exactly what the old POST "fixed" with a made-up address.
    await db.execute("UPDATE agents SET email = NULL WHERE agent_id = :a", {"a": BRAVO})
    before = await _agents_table_snapshot(db)

    resp = await client.request(method, path)

    assert resp.status_code == 501, resp.text
    assert await _agents_table_snapshot(db) == before


@pytest.mark.parametrize("path", ["/admin/fix/agent-metrics", "/admin/fix/agent-metrics-v2"])
async def test_admin_fix_metrics_posts_are_retired_and_write_nothing(client, db, path):
    before = await _counters(db)

    resp = await client.post(path)

    assert resp.status_code == 501, resp.text
    assert await _counters(db) == before
    assert await _view_exists(db) is False


async def test_agent_metrics_agents_reads_the_real_columns(client, db):
    resp = await client.get("/agent/metrics/agents")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "error" not in body, body
    by_id = {a["agent_id"]: a for a in body["agents"]}
    # CHARLIE is inactive; BRAVO's NULL is_active is active, as at the auth door.
    assert set(by_id) == {ALPHA, BRAVO}
    alpha = by_id[ALPHA]
    assert alpha["name"] == "Alpha"
    assert alpha["owner_email"] == "owner+alpha@example.com"
    assert alpha["status"] == "active"
    assert alpha["metrics_24h"]["request_count"] == 3
    assert alpha["metrics_24h"]["avg_response_time_ms"] == 200.0
    assert alpha["metrics_24h"]["success_rate"] == pytest.approx(66.67, abs=0.01)
    assert by_id[BRAVO]["name"] == "Bravo"
    assert by_id[BRAVO]["metrics_24h"]["request_count"] == 0
    assert body["agents"][0]["agent_id"] == ALPHA  # ORDER BY request_count DESC


async def test_admin_fix_metrics_status_reads_the_real_columns(client, db):
    resp = await client.get("/admin/fix/agent-metrics-status")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["view_exists"] is False
    assert body["agents"] == {"total": 3, "total_requests_sum": 11}
    assert body["usage_logs"]["agents_with_logs"] == 2
    assert body["usage_logs"]["total_logs"] == 5


async def test_admin_fix_agent_orders_check_reads_the_real_columns(client, db):
    resp = await client.get("/admin/fix/agent-orders-check", params={"agent_id": ALPHA})

    assert resp.status_code == 200, resp.text
    stats = resp.json()["current_agent_stats"]
    assert "request_count" not in stats
    assert stats["total_requests"] == 7
    assert stats["total_orders"] == 2
    assert resp.json()["direct_orders"]["total_orders"] == 1
    assert resp.json()["usage_logs_data"]["total_logs"] == 4


@pytest.mark.parametrize("role", ["merchant", "employee"])
@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/agent/metrics/agents"),
        ("GET", "/admin/fix/agent-metrics-status"),
        ("GET", f"/admin/fix/agent-orders-check?agent_id={ALPHA}"),
        ("POST", "/admin/fix/agent-metrics"),
        ("POST", "/admin/fix/agent-metrics-v2"),
        ("POST", "/admin/fix/agents-data"),
        ("GET", "/admin/fix/agents-status"),
    ],
)
async def test_admin_routes_refuse_a_non_admin_before_anything_else(client, db, user, role, method, path):
    user["role"] = role
    before = await _counters(db)

    resp = await client.request(method, path)

    assert resp.status_code == 403, resp.text
    assert await _counters(db) == before
    assert await _view_exists(db) is False
