"""Production-dialect gate for the acp_checkout_sessions storage (migration 191).

The in-process ACP session layer (services/acp_checkout_session_service) ships
JSONB columns, a partial unique index, and a runtime self-heal
(`_ensure_acp_checkout_sessions_table`) that SQLite cannot vouch for — SQLite
compiles JSONB as JSON and enforces its own flavor of partial indexes. This
module EXECUTES the real DDL + the service's insert/select/complete SQL against
Postgres, so a statement Postgres would refuse to PREPARE (see #1588) turns the
gate red instead of prod.

    createdb pivota_dialect_check
    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        pytest tests/test_acp_checkout_sessions_postgres.py

Never point this at prod.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

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


@pytest.fixture(autouse=True)
async def _db():
    from db.database import database
    from services.acp_checkout_session_service import _ensure_acp_checkout_sessions_table

    # Connect/disconnect PER TEST: the suite runs each test on a fresh event
    # loop (asyncio_default_fixture_loop_scope=function), and an asyncpg pool
    # that outlives its loop fails with "attached to a different loop".
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    # The runtime self-heal IS the DDL under test (mirrors migration 191).
    await _ensure_acp_checkout_sessions_table()
    await database.execute(
        "DELETE FROM acp_checkout_sessions WHERE merchant_id LIKE 'merch_pgtest_%'"
    )
    yield
    if not was_connected and database.is_connected:
        await database.disconnect()


def _mk_values(merchant_id: str, *, idem: str | None = None):
    now = datetime.now(timezone.utc)
    return {
        "id": f"csn_{uuid.uuid4().hex[:14]}",
        "merchant_id": merchant_id,
        "platform": "shopify",
        "status": "ready_for_payment",
        "buyer": {"email": "buyer@example.com"},
        "items": [{"product_id": "p1", "variant_id": "v1", "quantity": 1}],
        "fulfillment_address": {"address_line1": "1 ACP Street", "city": "SF"},
        "quote": {"quote_id": "q_pg", "pricing": {"total": "45.99"}},
        "metadata": {"pvt_click_id": "clk_pg", "protocol_name": "acp"},
        "currency": "USD",
        "total_cents": 4599,
        "idempotency_key": idem,
        "created_at": now,
        "updated_at": now,
        "expires_at": now + timedelta(seconds=3600),
    }


async def test_migration_file_sql_executes():
    # The shipped migration must itself PREPARE and run (idempotently — twice).
    from db.database import database

    path = os.path.join(
        os.path.dirname(__file__), "..", "db", "migrations", "191_acp_checkout_sessions.sql"
    )
    with open(path, "r", encoding="utf-8") as fh:
        sql = fh.read()
    # Strip comment lines FIRST, then split on ';' — comment prose may contain
    # semicolons and must never be mistaken for a statement boundary.
    sql_no_comments = "\n".join(
        line for line in sql.splitlines() if not line.strip().startswith("--")
    )
    statements = [s.strip() for s in sql_no_comments.split(";") if s.strip()]
    assert statements, "migration file parsed to zero statements"
    for _ in range(2):
        for statement in statements:
            await database.execute(statement)


async def test_insert_select_complete_roundtrip():
    from db.acp_checkout_sessions import acp_checkout_sessions
    from db.database import database
    from services import acp_checkout_session_service as svc

    values = _mk_values("merch_pgtest_rt")
    await database.execute(acp_checkout_sessions.insert().values(**values))

    session = await svc.get_session(values["id"])
    assert session is not None
    assert session["merchant_id"] == "merch_pgtest_rt"
    # JSONB comes back as structured data (or a JSON string the service parses).
    assert session["metadata"]["pvt_click_id"] == "clk_pg"
    assert session["items"][0]["variant_id"] == "v1"
    assert session["total_cents"] == 4599

    # Completion write — the exact UPDATE complete_session issues.
    now = datetime.now(timezone.utc)
    completion = {"status": "completed", "order_id": "ord_pg_1", "_request_hash": "h"}
    await database.execute(
        acp_checkout_sessions.update()
        .where(acp_checkout_sessions.c.id == values["id"])
        .values(
            status="completed",
            order_id="ord_pg_1",
            completion=completion,
            idempotency_key="idem_pg_1",
            completed_at=now,
            updated_at=now,
        )
    )
    completed = await svc.peek_session(values["id"])
    assert completed["status"] == "completed"
    assert completed["order_id"] == "ord_pg_1"
    assert completed["completion"]["order_id"] == "ord_pg_1"
    assert completed["completed_at"] is not None


async def test_expired_session_treated_absent():
    from db.acp_checkout_sessions import acp_checkout_sessions
    from db.database import database
    from services import acp_checkout_session_service as svc

    values = _mk_values("merch_pgtest_exp")
    values["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    await database.execute(acp_checkout_sessions.insert().values(**values))
    assert await svc.get_session(values["id"]) is None
    assert await svc.peek_session(values["id"]) is not None


async def test_partial_unique_idempotency_index():
    from db.acp_checkout_sessions import acp_checkout_sessions
    from db.database import database

    a = _mk_values("merch_pgtest_idem", idem="idem_dup")
    b = _mk_values("merch_pgtest_idem", idem="idem_dup")
    await database.execute(acp_checkout_sessions.insert().values(**a))
    with pytest.raises(Exception):
        # Second completion claim for the same (merchant, idempotency_key) must
        # be refused by the partial unique index.
        await database.execute(acp_checkout_sessions.insert().values(**b))

    # NULL keys never collide (the index is partial on IS NOT NULL).
    c = _mk_values("merch_pgtest_idem", idem=None)
    d = _mk_values("merch_pgtest_idem", idem=None)
    await database.execute(acp_checkout_sessions.insert().values(**c))
    await database.execute(acp_checkout_sessions.insert().values(**d))
