"""
Real-schema (SQLite, NOT FakeDB) coverage for the AP2 consent authorization gate
(#1473).

The AP2 transaction routes used to authorize a payment on consent-token validity
ALONE — `validate_consent` (action + spending-limit) and `increment_usage`
(spent-amount tracking) existed but had zero callers. `initiate`/`confirm` now
enforce them. These tests build `agent_consents` from the ACTUAL migration-021
DDL (not a FakeDB that could fabricate columns) and exercise the live service
methods against it, so a drift in `scope`/`spending_limit`/`spent_amount`/
`status`/`expires_at` breaks the test — and they check the confirm settle guard's
idempotency.
"""
import asyncio
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import databases

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

MIGRATION_021 = BACKEND_ROOT / "db" / "migrations" / "021_ap2_security.sql"


def _agent_consents_ddl_for_sqlite() -> str:
    """The REAL agent_consents CREATE TABLE from migration 021, normalized for
    SQLite. Column names come straight from the migration file, so if it changes
    columns this test table changes with it (anti-drift)."""
    sql = MIGRATION_021.read_text()
    block = re.search(
        r"CREATE TABLE IF NOT EXISTS agent_consents\s*\(.*?\n\);", sql, re.S
    ).group(0)
    block = re.sub(r"TIMESTAMP WITH TIME ZONE", "TIMESTAMP", block)
    block = re.sub(r"\bJSONB\b", "TEXT", block)
    block = re.sub(r"DEFAULT NOW\(\)", "DEFAULT CURRENT_TIMESTAMP", block)
    return block


async def _open_consents_db(path: str) -> databases.Database:
    db = databases.Database(f"sqlite+aiosqlite:///{path}")
    await db.connect()
    await db.execute(_agent_consents_ddl_for_sqlite())
    return db


async def _seed_consent(db, consent_id, actions, spending_limit=None, spent_amount=0):
    await db.execute(
        """INSERT INTO agent_consents
               (consent_id, agent_id, scope, status, spending_limit, spent_amount, expires_at)
           VALUES (:c, 'agent_a', :s, 'active', :lim, :spent, :exp)""",
        {
            "c": consent_id,
            "s": json.dumps({"actions": actions}),
            "lim": spending_limit,
            "spent": spent_amount,
            "exp": datetime.utcnow() + timedelta(hours=1),
        },
    )


def test_consent_gate_action_and_limit_against_real_schema(monkeypatch):
    """The live gate (validate_consent + increment_usage) that initiate/confirm now
    enforce, run over agent_consents built from the REAL migration-021 DDL — the
    authorization the routes previously skipped (they called only verify_consent)."""
    from db.database import database as global_db
    from services.consent_service import consent_service

    async def scenario():
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db = None
        try:
            db = await _open_consents_db(path)
            # Delegate the global singleton's methods to the real SQLite engine —
            # patch the METHODS, never swap the object.
            monkeypatch.setattr(global_db, "fetch_one", db.fetch_one)
            monkeypatch.setattr(global_db, "execute", db.execute)

            await _seed_consent(db, "c_read", ["read"])
            await _seed_consent(db, "c_pay", ["read", "create_payment"], spending_limit=100)
            await _seed_consent(db, "c_nolimit", ["create_payment"])  # NULL spending_limit

            out = {}
            out["read_pay"] = await consent_service.validate_consent("c_read", "create_payment", Decimal("10"))
            out["over_limit"] = await consent_service.validate_consent("c_pay", "create_payment", Decimal("150"))
            out["within"] = await consent_service.validate_consent("c_pay", "create_payment", Decimal("60"))
            out["nolimit"] = await consent_service.validate_consent("c_nolimit", "create_payment", Decimal("999999"))
            # accumulate spend; the remaining budget must shrink accordingly
            await consent_service.increment_usage("c_pay", Decimal("60"))
            out["after_spend"] = await consent_service.validate_consent("c_pay", "create_payment", Decimal("60"))
            row = await db.fetch_one("SELECT spent_amount FROM agent_consents WHERE consent_id='c_pay'")
            out["spent"] = row["spent_amount"]
            return out
        finally:
            if db is not None:
                await db.disconnect()
            os.unlink(path)

    r = asyncio.run(scenario())
    # AC#1 — a read-scoped consent cannot create a payment
    assert r["read_pay"][0] is False and "not permitted" in r["read_pay"][1]
    # AC#2 — an amount over the spending limit is rejected; within it passes
    assert r["over_limit"][0] is False and "spending limit" in r["over_limit"][1].lower()
    assert r["within"][0] is True
    # a NULL spending_limit means no cap (backward compatible with today's grants)
    assert r["nolimit"][0] is True
    # AC#3 — spend accumulates, shrinking the remaining budget (60 spent of 100,
    # so a further 60 is now rejected)
    assert r["after_spend"][0] is False
    assert Decimal(str(r["spent"])) == Decimal("60")


def test_confirm_settle_update_is_idempotent():
    """The confirm settle guard — `UPDATE ... WHERE status='pending' RETURNING` —
    must flip a pending transaction exactly once: a replayed/concurrent confirm
    sees no row and therefore must NOT double-debit the spending budget. (The full
    x402_transactions schema + CHECK is covered by
    tests/test_ap2_x402_transactions_schema.py; here we exercise the settle guard's
    SQL against a real engine.)"""

    async def scenario():
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db = None
        try:
            db = databases.Database(f"sqlite+aiosqlite:///{path}")
            await db.connect()
            # the columns the confirm settle path reads/writes (post-migration-185)
            await db.execute(
                """CREATE TABLE x402_transactions (
                       transaction_id TEXT PRIMARY KEY, agent_id TEXT, amount NUMERIC,
                       currency TEXT, status TEXT, wallet_address TEXT, confirmed_at TIMESTAMP)"""
            )
            await db.execute(
                "INSERT INTO x402_transactions (transaction_id, agent_id, amount, currency, status) "
                "VALUES ('t1','agent_a',25,'USD','pending')"
            )
            guard = (
                "UPDATE x402_transactions SET status='completed', wallet_address=:w, confirmed_at=:t "
                "WHERE transaction_id=:id AND status='pending' RETURNING transaction_id"
            )
            first = await db.fetch_one(guard, {"w": "0xA", "id": "t1", "t": datetime.utcnow()})
            second = await db.fetch_one(guard, {"w": "0xA", "id": "t1", "t": datetime.utcnow()})
            return first, second
        finally:
            if db is not None:
                await db.disconnect()
            os.unlink(path)

    first, second = asyncio.run(scenario())
    assert first is not None, "first confirm must settle the pending transaction"
    assert second is None, "a replayed confirm must NOT re-settle (idempotency guard)"
