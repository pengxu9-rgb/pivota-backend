"""Two live handlers named `merchant_stores` columns that do not exist.

    routes/product_sync_monitoring.py  last_sync_at  ->  last_sync   (x2)
    routes/mcp_mgmt.py                 m.email       ->  m.contact_email

`merchant_stores` is created by main.py's startup DDL and its column is
`last_sync`; Postgres even offers the hint ("Perhaps you meant to reference the
column merchant_stores.last_sync"). `merchant_onboarding` has `contact_email` and
no `email` — the mcp_mgmt statement already SELECTs `m.contact_email` and only
its GROUP BY was wrong, so that one is a plain typo that could never have run.

Both routers are mounted in main.py, so both defects are reachable in production
today. Both statements sit under `except Exception`, and `sync_health_check` is
the one that shows what that costs: it is the endpoint whose entire job is to
answer whether product sync is healthy, and the undefined column sent it down the
except arm on every single call, where it reports

    {"status": "unhealthy", "error": "column last_sync_at does not exist"}

A health check that has never once returned "healthy" is indistinguishable from a
system that has never once been healthy.

WHY EXECUTED RATHER THAN PREPARED. tests/test_repo_sql_prepare_postgres.py could
not see these statements at all (all three are function-local literals), and even
once it can, PREPARE validates TYPES and never VALUES — it would prove the
column resolves, not that the aggregate means anything. These tests drive the
real handler functions so the statement under test is the statement that ships.

Tables are built from the repo's own definitions — `merchant_stores` lifted from
main.py's startup DDL by AST, the rest compiled from `metadata` — never from DDL
hand-copied into this file, which is how a fixture drifts into agreeing with the
bug.

    createdb pivota_dialect_check
    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        pytest tests/test_store_sync_columns_postgres.py

Never point this at prod — it writes.
"""

from __future__ import annotations

import ast
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason=(
        "needs a Postgres DATABASE_URL — this is the production-dialect gate; "
        "see the module docstring for the one-line setup"
    ),
)

REPO_ROOT = Path(__file__).resolve().parents[1]

MERCHANT = f"syncol_{uuid.uuid4().hex[:8]}"
_PLATFORM = f"plat_{uuid.uuid4().hex[:6]}"  # unique, so GROUP BY platform isolates us
_EMPLOYEE = {"role": "employee", "email": "gate@example.com"}


def _startup_ddl_for(table: str) -> str:
    """main.py's own `CREATE TABLE IF NOT EXISTS <table>` literal, by AST.

    The same technique tests/test_repo_sql_prepare_postgres.py uses, for the same
    reason: `merchant_stores` is created by application bootstrap rather than by
    a migration or by `metadata`, and a hand-copied copy of its DDL in this file
    would drift from the real one silently. In particular a copy that spelled the
    column `last_sync_at` would make every test below pass against the unfixed
    code.
    """
    tree = ast.parse((REPO_ROOT / "main.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and f"CREATE TABLE IF NOT EXISTS {table}" in node.value
        ):
            return node.value
    raise AssertionError(f"main.py no longer creates {table} — this fixture is stale")


@pytest.fixture(autouse=True)
async def _db():
    from sqlalchemy.schema import CreateTable
    from sqlalchemy.dialects import postgresql

    from db.database import database, metadata
    import db.merchant_onboarding  # noqa: F401  — registers the table on metadata
    import db.products  # noqa: F401  — products_cache, read by the health check

    await database.connect()
    for statement in (
        _startup_ddl_for("merchant_stores"),
        str(CreateTable(metadata.tables["merchant_onboarding"]).compile(
            dialect=postgresql.dialect())),
        str(CreateTable(metadata.tables["products_cache"]).compile(
            dialect=postgresql.dialect())),
    ):
        try:
            await database.execute(statement)
        except Exception:
            # A sibling gate file in this process already created it; same
            # source, so the shape is identical. Never DROP — this database is
            # shared with every other test_*_postgres.py file.
            pass
    try:
        yield database
    finally:
        await database.execute(
            "DELETE FROM merchant_stores WHERE merchant_id = :m", {"m": MERCHANT}
        )
        await database.execute(
            "DELETE FROM merchant_onboarding WHERE merchant_id = :m", {"m": MERCHANT}
        )
        await database.disconnect()


async def _merchant_stores_columns() -> set:
    """Which columns merchant_stores actually has, right now.

    NOT paranoia — measured. This database is shared, and `merchant_stores` is
    created by whichever gate file gets there first. Several siblings
    (test_connection_layer_postgres.py, test_priced_offer_gate_postgres.py)
    create a leaner version carrying only the columns they need, with no `name`
    column at all; main.py's DDL declares `name VARCHAR(255) NOT NULL`. Whoever
    runs first wins, because every one of these is CREATE TABLE IF NOT EXISTS.

    So this file cannot assume either shape. It writes the columns the health
    check and the statistics query actually read — store_id, merchant_id,
    platform, status, last_sync, present in both — and supplies `name` only when
    the table it found has it. Discovered by running the whole discovered gate
    set in one pytest invocation, the way CI does; a single-file run passes
    either way and would have shipped this.
    """
    from db.database import database

    rows = await database.fetch_all(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'merchant_stores'"
    )
    return {row["column_name"] for row in rows}


async def _store(store_id: str, *, synced_days_ago: float | None, status: str = "active") -> None:
    from db.database import database

    last_sync = (
        None if synced_days_ago is None
        else datetime.now(timezone.utc) - timedelta(days=synced_days_ago)
    )
    values = {
        "store_id": store_id, "merchant_id": MERCHANT, "platform": _PLATFORM,
        "status": status, "last_sync": last_sync, "name": "Gate Fixture Store",
    }
    present = await _merchant_stores_columns()
    columns = [c for c in values if c in present]
    assert {"store_id", "merchant_id", "platform", "status", "last_sync"} <= set(columns), (
        f"merchant_stores is missing a column these tests read: {sorted(present)}"
    )
    await database.execute(
        f"INSERT INTO merchant_stores ({', '.join(columns)}) "
        f"VALUES ({', '.join(':' + c for c in columns)})",
        {c: values[c] for c in columns},
    )


async def _merchant_row() -> None:
    from db.database import database
    from db.merchant_onboarding import merchant_onboarding

    await database.execute(
        merchant_onboarding.insert().values(
            merchant_id=MERCHANT,
            business_name="Gate Fixture Co",
            contact_email="ops@example.com",
            status="active",
            # Explicit because `databases` compiles the INSERT without running
            # SQLAlchemy's Python-side column defaults, so a NOT NULL column
            # carrying only `default=False` is sent as NULL.
            apm_enabled=False,
        )
    )


# ---------------------------------------------------------------------------
async def test_the_sync_health_check_can_actually_report_healthy():
    """routes/product_sync_monitoring.py::sync_health_check.

    The strongest assertion available here, and it needs no fixture arithmetic:
    on the unfixed code this endpoint returns "unhealthy" unconditionally,
    because the undefined column throws before it can reach the success arm.
    """
    from routes.product_sync_monitoring import sync_health_check

    await _store("syncol-health-1", synced_days_ago=3)

    body = await sync_health_check()

    assert body["status"] == "healthy", body.get("error")
    # ...and the stale-store count it exists to report is a real number rather
    # than the zero the except arm would never even have reached.
    assert body["stale_merchants"] >= 1


async def test_the_sync_health_check_counts_a_stale_merchant_by_last_sync():
    """The same statement's WHERE clause, which "healthy" alone does not reach.

    Asserts a DELTA, because this counter is repo-wide and this database is
    shared with forty-odd sibling test_*_postgres.py files.
    """
    from routes.product_sync_monitoring import sync_health_check

    before = (await sync_health_check())["stale_merchants"]

    # Synced an hour ago: inside the 24h window, so NOT stale.
    await _store("syncol-fresh", synced_days_ago=1.0 / 24.0)
    assert (await sync_health_check())["stale_merchants"] == before

    # Never synced: `last_sync IS NULL` is the other arm of the same predicate.
    await _store("syncol-never", synced_days_ago=None)
    assert (await sync_health_check())["stale_merchants"] == before + 1


async def test_sync_statistics_reports_time_since_sync_for_the_platform():
    """routes/product_sync_monitoring.py::get_sync_statistics — the other
    `last_sync_at` statement, which the health check does not touch.

    Unlike the health check, this handler re-raises as a 500, so on the unfixed
    code the call below does not return a wrong answer — it raises.
    """
    from routes.product_sync_monitoring import get_sync_statistics

    await _store("syncol-stats-1", synced_days_ago=2)
    await _store("syncol-stats-2", synced_days_ago=4)

    body = await get_sync_statistics(current_user=_EMPLOYEE)

    rows = {row["platform"]: row for row in body["store_health"]}
    assert _PLATFORM in rows, f"platform missing from store_health: {body}"
    ours = rows[_PLATFORM]
    assert ours["connected_stores"] == 2
    # AVG over stores synced 2 and 4 days ago = 3 days = 72 hours (the handler
    # divides the seconds it selects by 3600). Generous window: the point is
    # that a real elapsed time came back, not that the clock is exact.
    assert 60 < float(ours["avg_hours_since_sync"]) < 84


async def test_the_sync_history_endpoint_returns_its_events():
    """routes/product_sync_monitoring.py::get_merchant_sync_history — the THIRD
    `last_sync_at` statement in this file.

    Missed by the first version of this change, which fixed the two neighbours
    and declared the file clean. It matters: the router is mounted, so
    `GET /products/monitoring/merchant/{id}/sync-history` was answering

        Error fetching sync history: column "last_sync_at" does not exist
        HINT:  Perhaps you meant to reference the column "merchant_stores.last_sync".

    on every call, ~50 lines from the two that were fixed.

    WHY THE PREPARE SWEEP DID NOT CATCH IT EITHER, which is the more useful
    lesson: this statement has a subquery over `products_cache`, and the
    migrations declare ZERO columns for that table (db/products.py owns it). So
    it is never in `faithful_tables`, so `named <= faithful` fails, so the
    UndefinedColumnError was demoted to "unchecked (fixture gap)" — the masking
    behaviour that file documents, hiding a live 500. A statement that joins an
    unfaithful table is exempt from the wrong-column check no matter how wrong
    its OWN table's columns are.
    """
    from routes.product_sync_monitoring import get_merchant_sync_history

    await _store("syncol-hist-1", synced_days_ago=2)
    await _store("syncol-hist-2", synced_days_ago=40)  # outside the 30-day window

    body = await get_merchant_sync_history(MERCHANT, days=30, current_user=_EMPLOYEE)

    events = body["sync_events"]
    assert len(events) == 1, f"expected only the in-window store, got {events}"
    assert events[0]["platform"] == _PLATFORM
    assert events[0]["synced_at"] is not None, "synced_at came back null"
    assert events[0]["status"] == "active"


async def test_mcp_merchants_groups_by_the_column_it_selects():
    """routes/mcp_mgmt.py::get_mcp_merchants.

    The SELECT list says `m.contact_email`; the GROUP BY said `m.email`. There is
    no `email` column, so this statement could never have run and the endpoint
    answered from its `except` on every call.
    """
    from routes.mcp_mgmt import get_mcp_merchants

    await _merchant_row()
    await _store("syncol-mcp-1", synced_days_ago=1)
    await _store("syncol-mcp-2", synced_days_ago=5)

    body = await get_mcp_merchants(current_user=_EMPLOYEE)
    assert body["status"] == "success", body

    ours = [m for m in body["merchants"] if m["merchant_id"] == MERCHANT]
    assert len(ours) == 1, "the merchant is missing — the GROUP BY statement failed"
    row = ours[0]
    # The grouped column is the one that has to come back intact.
    assert row["email"] == "ops@example.com"
    assert row["connected_stores"] == 2
    assert row["mcp_status"] == "connected"
    # MAX(s.last_sync) — the most recent of the two stores, not the page-load
    # time. `is not None` cannot tell those apart, so assert the VALUE: the
    # newer store synced 1 day ago, so the max must sit near that and nowhere
    # near now. (This handler used to return datetime.now() here.)
    from datetime import datetime as _dt
    age_hours = (
        _dt.now(timezone.utc) - _dt.fromisoformat(row["last_sync"])
    ).total_seconds() / 3600
    assert 20 < age_hours < 28, f"last_sync is {age_hours:.1f}h old, expected ~24h"
