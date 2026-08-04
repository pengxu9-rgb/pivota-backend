"""Production-dialect gate for the ACP delegate ALLOWANCE registry (migration 192).

The registry ships a BOOLEAN CAS (`UPDATE ... WHERE used = FALSE OR
used_by_session = :sid ... RETURNING`) and TIMESTAMPTZ expiry that SQLite cannot
vouch for — SQLite has no real boolean type and a far looser idea of what will
PREPARE. This module EXECUTES the real DDL + the service's mint/lookup/CAS SQL
against Postgres, so a statement Postgres would refuse (see #1588) turns the
gate red instead of prod.

It also asserts, against `information_schema`, the property that matters most
about this table: it has NO cardholder-data column. The retired pivota-acp
service stored raw PAN and CVC in a JSONB payload; the guard here makes the
absence enforced rather than merely intended.

    createdb pivota_dialect_check_acp
    DATABASE_URL=postgresql://localhost/pivota_dialect_check_acp \
        pytest tests/test_acp_delegate_allowances_postgres.py

Never point this at prod.
"""

from __future__ import annotations

import asyncio
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
    from services.acp_delegate_allowance_service import (
        _ensure_acp_delegate_allowances_table,
    )

    # Connect/disconnect PER TEST: the suite runs each test on a fresh event
    # loop, and an asyncpg pool that outlives its loop fails with "attached to a
    # different loop".
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    # The runtime self-heal IS the DDL under test (mirrors migration 192).
    await _ensure_acp_delegate_allowances_table()
    await database.execute(
        "DELETE FROM acp_delegate_allowances WHERE merchant_id LIKE 'merch_pgtest_%'"
    )
    yield
    if not was_connected and database.is_connected:
        await database.disconnect()


def _token() -> str:
    return f"vt_{uuid.uuid4().hex[:14]}"


async def test_migration_file_sql_executes():
    # The shipped migration must itself PREPARE and run (idempotently — twice).
    from db.database import database

    path = os.path.join(
        os.path.dirname(__file__), "..", "db", "migrations", "192_acp_delegate_allowances.sql"
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


async def test_mint_and_lookup_roundtrip_through_real_column_types():
    from services import acp_delegate_allowance_service as reg

    token_id = _token()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=900)
    await reg.mint_allowance(
        token_id=token_id,
        checkout_session_id="csn_pg_rt",
        merchant_id="merch_pgtest_rt",
        max_amount=4599,
        currency="USD",
        expires_at=expires_at,
    )
    stored = await reg.get_allowance(token_id)
    assert stored is not None
    assert stored["checkout_session_id"] == "csn_pg_rt"
    assert stored["merchant_id"] == "merch_pgtest_rt"
    assert stored["max_amount"] == 4599
    assert stored["currency"] == "USD"
    assert stored["reason"] == "one_time"
    # A REAL Postgres boolean, not SQLite's 0/1.
    assert stored["used"] is False
    assert stored["used_by_session"] is None
    # TIMESTAMPTZ roundtrips tz-aware and to the second.
    assert stored["expires_at"].tzinfo is not None
    assert abs((stored["expires_at"] - expires_at).total_seconds()) < 1


async def test_cas_bind_sql_is_single_flight_on_postgres():
    # THE statement this gate exists for: the conditional UPDATE ... RETURNING
    # must PREPARE on Postgres and let exactly one of two CONCURRENT binds win.
    from services import acp_delegate_allowance_service as reg

    token_id = _token()
    await reg.mint_allowance(
        token_id=token_id,
        checkout_session_id="csn_pg_cas",
        merchant_id="merch_pgtest_cas",
        max_amount=1000,
        currency="USD",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=900),
    )

    results = await asyncio.gather(
        reg.bind_allowance_to_session(token_id=token_id, session_id="csn_pg_a"),
        reg.bind_allowance_to_session(token_id=token_id, session_id="csn_pg_b"),
    )
    assert sorted(results) == [False, True], results

    stored = await reg.get_allowance(token_id)
    assert stored["used"] is True
    winner = stored["used_by_session"]
    assert winner in {"csn_pg_a", "csn_pg_b"}
    assert stored["used_at"] is not None

    # The winner re-binds idempotently forever (a completion retry / stale
    # resume must never be refused by its own earlier bind); the loser never can.
    loser = "csn_pg_b" if winner == "csn_pg_a" else "csn_pg_a"
    assert await reg.bind_allowance_to_session(token_id=token_id, session_id=winner) is True
    assert await reg.bind_allowance_to_session(token_id=token_id, session_id=loser) is False
    assert (await reg.get_allowance(token_id))["used_by_session"] == winner


async def test_bind_of_an_unknown_token_is_false_on_postgres():
    from services import acp_delegate_allowance_service as reg

    assert await reg.bind_allowance_to_session(
        token_id=_token(), session_id="csn_pg_missing"
    ) is False


async def test_token_id_is_a_primary_key():
    # One row per token is what the CAS contends for; a duplicate must be
    # impossible at the storage layer, not merely unlikely.
    from db.database import database
    from services import acp_delegate_allowance_service as reg

    token_id = _token()
    kwargs = dict(
        token_id=token_id,
        checkout_session_id="csn_pg_pk",
        merchant_id="merch_pgtest_pk",
        max_amount=100,
        currency="USD",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=900),
    )
    await reg.mint_allowance(**kwargs)
    with pytest.raises(Exception):
        await reg.mint_allowance(**kwargs)

    rows = await database.fetch_all(
        "SELECT indexname, indexdef FROM pg_indexes "
        "WHERE tablename = 'acp_delegate_allowances'"
    )
    defs = [str(dict(r).get("indexdef") or "") for r in rows]
    assert any(
        "UNIQUE" in d.upper() and "token_id" in d for d in defs
    ), defs
    # ...and the session lookup index the registry is queried by exists.
    assert any("checkout_session_id" in d for d in defs), defs


async def test_expires_at_is_not_null_at_the_storage_layer():
    # The retired service's biggest gap was an expiry it never enforced. Here the
    # column cannot even be absent.
    from db.database import database

    row = await database.fetch_one(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name = 'acp_delegate_allowances' AND column_name = 'expires_at'"
    )
    assert row is not None
    assert str(dict(row)["is_nullable"]).upper() == "NO"


async def test_registry_has_no_cardholder_data_columns():
    # THE schema guard. The retired pivota-acp `delegate_payment` stored the
    # whole request — raw PAN and CVC — unencrypted in a JSONB payload column
    # (PCI Req 3.2 violation by design; CVC storage is prohibited outright).
    # This table must not be ABLE to hold any of it.
    from db.database import database

    rows = await database.fetch_all(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'acp_delegate_allowances'"
    )
    names = [str(dict(r)["column_name"]).lower() for r in rows]
    assert names, "table not found"
    forbidden = ("number", "cvc", "cvv", "pan", "cryptogram")
    offenders = [n for n in names if any(bad in n for bad in forbidden)]
    assert offenders == [], offenders
    # The exact column set, so ADDING a card field is a deliberate, reviewed act.
    assert sorted(names) == sorted(
        [
            "token_id",
            "checkout_session_id",
            "merchant_id",
            "max_amount",
            "currency",
            "reason",
            "expires_at",
            "used",
            "used_at",
            "used_by_session",
            "created_at",
        ]
    ), sorted(names)
