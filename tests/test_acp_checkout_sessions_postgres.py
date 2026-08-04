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
        "agent_id": "agent_pg",
        "platform": "shopify",
        "status": "ready_for_payment",
        "buyer": {"email": "buyer@example.com"},
        "items": [{"product_id": "p1", "variant_id": "v1", "quantity": 1}],
        "fulfillment_address": {
            "name": "PG Buyer",
            "address_line1": "500 Dialect Ave",
            "city": "San Francisco",
            "postal_code": "94110",
            "country": "US",
        },
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
    assert session["agent_id"] == "agent_pg"
    # JSONB comes back as structured data (or a JSON string the service parses).
    assert session["metadata"]["pvt_click_id"] == "clk_pg"
    assert session["items"][0]["variant_id"] == "v1"
    assert session["total_cents"] == 4599

    # The single-flight claim SQL (conditional UPDATE ... RETURNING, with the
    # attempt-scoped key built by SQL string concat + CAST) must PREPARE and
    # behave on Postgres: first claim wins, second loses.
    claim = await svc._claim_session(values["id"], "idem_claim")
    assert claim is not None
    assert claim.capture_attempt == 1
    assert claim.psp_idempotency_key == f"acp_complete:{values['id']}:a1"
    assert await svc._claim_session(values["id"], "idem_claim") is None
    row = await svc.peek_session(values["id"])
    assert row["status"] == "completing"
    assert row["idempotency_key"] == "idem_claim"
    # The attempt + key roundtrip through the real column types.
    assert row["capture_attempt"] == 1
    assert row["psp_idempotency_key"] == claim.psp_idempotency_key
    # Release-claim UPDATE too — and the NEXT claim mints the NEXT attempt key
    # (this is what lets a definitively declined session be retried).
    await svc._revert_claim_to_ready(values["id"])
    row = await svc.peek_session(values["id"])
    assert row["status"] == "ready_for_payment"
    second = await svc._claim_session(values["id"], "idem_claim_2")
    assert second is not None
    assert second.capture_attempt == 2
    assert second.psp_idempotency_key == f"acp_complete:{values['id']}:a2"
    await svc._revert_claim_to_ready(values["id"])


async def test_stale_takeover_sql_is_single_flight_on_postgres():
    # N4 on the production dialect: the takeover puts BOTH the status and the
    # staleness comparison (TIMESTAMPTZ vs a bound datetime) in the WHERE
    # clause, so exactly one of N concurrent resumers can proceed.
    from datetime import datetime as _dt

    from db.acp_checkout_sessions import acp_checkout_sessions
    from db.database import database
    from services import acp_checkout_session_service as svc

    values = _mk_values("merch_pgtest_takeover")
    await database.execute(acp_checkout_sessions.insert().values(**values))
    claim = await svc._claim_session(values["id"], "idem_takeover")
    assert claim is not None

    now = _dt.now(timezone.utc)
    # A FRESH completing row is never taken over (it is an in-flight attempt).
    assert await svc._take_over_stale_completing(
        values["id"], now - timedelta(seconds=60)
    ) is None

    # Age it, then race two takeovers: the first moves updated_at out of the
    # stale window, so the second must miss.
    await database.execute(
        acp_checkout_sessions.update()
        .where(acp_checkout_sessions.c.id == values["id"])
        .values(updated_at=now - timedelta(seconds=600))
    )
    stale_cutoff = _dt.now(timezone.utc) - timedelta(seconds=60)
    first = await svc._take_over_stale_completing(values["id"], stale_cutoff)
    second = await svc._take_over_stale_completing(values["id"], stale_cutoff)
    assert first is not None
    assert second is None
    # The resume replays the STORED key and never bumps the attempt counter.
    assert first.capture_attempt == 1
    assert first.psp_idempotency_key == f"acp_complete:{values['id']}:a1"
    row = await svc.peek_session(values["id"])
    assert row["capture_attempt"] == 1
    assert row["psp_idempotency_key"] == first.psp_idempotency_key

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


async def test_idempotency_key_index_is_plain_lookup_not_unique():
    # Review F2 item 5: the idempotency-key index must NOT be unique — duplicate
    # keys are legal at the storage layer (the service answers cross-session
    # reuse with a SELECT + 409 BEFORE charging). A unique index here would
    # abort the completion write only AFTER the charge (retry-recharge wedge).
    from db.acp_checkout_sessions import acp_checkout_sessions
    from db.database import database

    a = _mk_values("merch_pgtest_idem", idem="idem_dup")
    b = _mk_values("merch_pgtest_idem", idem="idem_dup")
    await database.execute(acp_checkout_sessions.insert().values(**a))
    await database.execute(acp_checkout_sessions.insert().values(**b))  # must NOT raise

    # And no unique index on (merchant_id, idempotency_key) exists at all.
    rows = await database.fetch_all(
        "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'acp_checkout_sessions'"
    )
    for row in rows:
        d = dict(row)
        if "idempotency_key" in str(d.get("indexdef") or ""):
            assert "UNIQUE" not in str(d.get("indexdef") or "").upper(), d

    # The cross-session lookup the service runs pre-charge PREPAREs fine.
    hit = await database.fetch_one(
        "SELECT id FROM acp_checkout_sessions "
        "WHERE merchant_id = :merchant_id AND idempotency_key = :key AND id != :sid "
        "LIMIT 1",
        {"merchant_id": "merch_pgtest_idem", "key": "idem_dup", "sid": a["id"]},
    )
    assert hit is not None

    # NULL keys are fine in any multiplicity.
    c = _mk_values("merch_pgtest_idem", idem=None)
    d = _mk_values("merch_pgtest_idem", idem=None)
    await database.execute(acp_checkout_sessions.insert().values(**c))
    await database.execute(acp_checkout_sessions.insert().values(**d))
