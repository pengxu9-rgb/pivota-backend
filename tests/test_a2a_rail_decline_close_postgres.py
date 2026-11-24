"""The decline close's tests on Postgres: the SAME tests as tests/test_a2a_rail_decline_close.py (one source,
dialect-aware), collected here so the Postgres dialect gate runs them, plus the schema parity check.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_dialect_check \\
        pytest tests/test_a2a_rail_decline_close_postgres.py
"""

from __future__ import annotations

import os

import pytest

from tests.test_a2a_rail_decline_close import *  # noqa: F401,F403 - the tests
from tests.test_a2a_rail_decline_close import _db, _dials  # noqa: F401 - the autouse fixtures
from tests.test_a2a_rail_decline_close import _apply, _audit, _declined, _fence  # noqa: E402 - helpers

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_DBNAME = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]

pytestmark = pytest.mark.skipif(
    not _IS_PG or not any(m in _DBNAME or m in DATABASE_URL for m in _SAFE_DB_MARKERS),
    reason="needs a throwaway Postgres DATABASE_URL",
)



async def test_migration_296_builds_the_table_and_trigger_and_its_down_removes_them():
    from db.database import database
    import db.a2a_rail_ledger as ledger
    from tests.test_a2a_rail_decline_close import _DOWN, _apply_migration_296, _split

    assert await ledger.manual_resolution_audit_ready()  # the fixture applied 296
    for statement in _split(_DOWN.read_text("utf-8")):
        await database.execute(statement)
    assert not await ledger.manual_resolution_audit_ready()
    row = await database.fetch_one("SELECT NOT EXISTS (SELECT 1 FROM pg_proc WHERE proname = "
                                   "'prevent_a2a_rail_manual_resolution_audit_mutation') AS gone")
    assert row["gone"]
    await _apply_migration_296()
    assert await ledger.manual_resolution_audit_ready()


# ── the gate reads the trigger's ENABLED state, not only its presence ─────────────────────────────────
#
# `ALTER TABLE ... DISABLE TRIGGER` keeps the pg_trigger row (tgenabled 'D'), so a presence-only gate read a
# disabled guard as ready. 'R' (ENABLE REPLICA) fires only under session_replication_role = replica, so an
# ordinary session is unguarded too. Only 'O' (origin, the default) and 'A' (always) count.

_AUDIT_TRIGGER = "a2a_rail_manual_resolution_audit_immutable"


async def _tgenabled():
    from db.database import database

    row = await database.fetch_one(
        "SELECT tgenabled::text AS e FROM pg_trigger WHERE tgrelid = to_regclass('a2a_rail_manual_resolution_audit') "
        "AND tgname = :g AND NOT tgisinternal", {"g": _AUDIT_TRIGGER})
    return row["e"] if row else None


@pytest.mark.parametrize("alter, enabled, ready", [
    ("DISABLE TRIGGER", "D", False),
    ("ENABLE REPLICA TRIGGER", "R", False),
    ("ENABLE ALWAYS TRIGGER", "A", True),
    ("ENABLE TRIGGER", "O", True),
])
async def test_the_audit_gate_reads_the_trigger_enabled_state(alter, enabled, ready):
    from db.database import database
    import db.a2a_rail_ledger as ledger

    assert (await _tgenabled(), await ledger.manual_resolution_audit_ready()) == ("O", True)  # 296 as applied
    await database.execute(f"ALTER TABLE a2a_rail_manual_resolution_audit {alter} {_AUDIT_TRIGGER}")
    assert (await _tgenabled(), await ledger.manual_resolution_audit_ready()) == (enabled, ready)


async def test_a_disabled_audit_trigger_then_reenabled_reads_ready_again():
    from db.database import database
    import db.a2a_rail_ledger as ledger

    await database.execute(f"ALTER TABLE a2a_rail_manual_resolution_audit DISABLE TRIGGER {_AUDIT_TRIGGER}")
    assert not await ledger.manual_resolution_audit_ready()
    await database.execute(f"ALTER TABLE a2a_rail_manual_resolution_audit ENABLE TRIGGER {_AUDIT_TRIGGER}")
    assert await ledger.manual_resolution_audit_ready()
    await database.execute(f"DROP TRIGGER {_AUDIT_TRIGGER} ON a2a_rail_manual_resolution_audit")
    assert (await _tgenabled(), await ledger.manual_resolution_audit_ready()) == (None, False)


async def test_a_disabled_audit_trigger_lets_a_delete_through():
    """Why a disabled trigger must read not ready: the audit row is no longer append-only."""
    from db.database import database

    pid = await _declined()
    assert (await _apply(pid))[1]["outcome"] == "resolved"
    await database.execute(f"ALTER TABLE a2a_rail_manual_resolution_audit DISABLE TRIGGER {_AUDIT_TRIGGER}")
    await database.execute("DELETE FROM a2a_rail_manual_resolution_audit WHERE purchase_id = :p", {"p": pid})
    assert await _audit(pid) is None


async def test_the_close_refuses_while_the_audit_trigger_is_disabled():
    from db.database import database
    import db.a2a_rail_ledger as ledger
    from services.payment_orchestration import a2a_rail_worker as worker

    pid = await _declined()
    _, preview = await _fence(pid)  # previewed while the guard was on
    await database.execute(f"ALTER TABLE a2a_rail_manual_resolution_audit DISABLE TRIGGER {_AUDIT_TRIGGER}")
    for code, result in ((await _fence(pid)), (await _apply(pid, fence=preview["fence"]))[:2]):
        assert (code, result["reason"]) == (3, "manual_resolution_audit_not_ready")
    row = await ledger.get_decline_candidate(pid)
    assert (row["state"], row["last_error_code"]) == ("paying", worker.CHECKOUT_PAYMENT_REACHED_DECLINED)
    assert await _audit(pid) is None
