"""
GET /ap2/wallet/balance is an honest 501 not-implemented stub.

`agent_wallets` (migration 022) is an ADDRESS REGISTRY with no balance column,
and no balance source is wired anywhere in this codebase (no on-chain RPC, no
custodian client), so the route fails closed with 501 instead of 500-ing on a
query for columns that do not exist. The drift that shipped was:

    SELECT wallet_id, balance, currency, status, last_updated
    FROM agent_wallets WHERE agent_id = :agent_id AND wallet_address = :wallet_address

— `balance`/`currency`/`last_updated`/`wallet_address` are all absent (the real
ownership column is `address`). Its unit test passed only because a FakeDB
fabricated the row; see docs/AP2_ENABLEMENT.md §6.

The real-schema tests below build `agent_wallets` from the ACTUAL migration 022
DDL on a real SQLite engine (NOT a FakeDB) and exercise the live
`WalletService.verify_agent_wallet` query against it — the kind of test that
catches this class of schema drift, which a FakeDB hides.
"""
import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path

import databases
from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

MIGRATION_022 = BACKEND_ROOT / "db" / "migrations" / "022_wallet_infrastructure.sql"

NOT_IMPLEMENTED_DETAIL = "Wallet balance is not available (no balance source wired)"


# ---------------------------------------------------------------------------
# Behavioral: the route is an honest 501 (no balance source), never a 500.
# ---------------------------------------------------------------------------
def _client() -> TestClient:
    from routes.ap2_routes import router as ap2_router

    app = FastAPI()
    app.include_router(ap2_router)
    return TestClient(app)


def test_wallet_balance_returns_501_not_implemented():
    res = _client().get(
        "/ap2/wallet/balance",
        headers={"X-Agent-Consent": "consent", "X-Wallet-Address": "0xABC"},
    )
    assert res.status_code == 501, res.text
    assert res.json()["detail"] == NOT_IMPLEMENTED_DETAIL


def test_wallet_balance_501_even_without_headers():
    # It is a bare stub (like x402/exchange): it does no auth work of its own and
    # never touches the DB, so it cannot 500 on a bad query regardless of input.
    # The consent-only gate lives in the middleware (see test_ap2_routes_wiring).
    res = _client().get("/ap2/wallet/balance")
    assert res.status_code == 501, res.text


# ---------------------------------------------------------------------------
# Real-schema (SQLite, NOT FakeDB): build agent_wallets from the actual
# migration-022 DDL and exercise the live wallet query against it. This is the
# test that catches the schema drift a FakeDB hides.
# ---------------------------------------------------------------------------
def _agent_wallets_ddl_for_sqlite() -> str:
    """The REAL agent_wallets CREATE TABLE from migration 022, normalized for
    SQLite. Column names come straight from the migration file, so if the
    migration changes columns this test table changes with it (anti-drift)."""
    sql = MIGRATION_022.read_text()
    block = re.search(
        r"CREATE TABLE IF NOT EXISTS agent_wallets\s*\(.*?\n\);", sql, re.S
    ).group(0)
    block = re.sub(r"TIMESTAMP WITH TIME ZONE", "TIMESTAMP", block)
    block = re.sub(r"\bJSONB\b", "TEXT", block)
    block = re.sub(r"DEFAULT NOW\(\)", "DEFAULT CURRENT_TIMESTAMP", block)
    return block


async def _open_seeded_db(path: str) -> databases.Database:
    db = databases.Database(f"sqlite+aiosqlite:///{path}")
    await db.connect()
    try:
        await db.execute(_agent_wallets_ddl_for_sqlite())
        # active wallet owned by agent_a
        await db.execute(
            "INSERT INTO agent_wallets (wallet_id, agent_id, network, address, status) "
            "VALUES ('w_active', 'agent_a', 'ethereum', '0xAAA', 'active')"
        )
        # a non-active (pending) wallet owned by agent_a
        await db.execute(
            "INSERT INTO agent_wallets (wallet_id, agent_id, network, address, status) "
            "VALUES ('w_pending', 'agent_a', 'ethereum', '0xBBB', 'pending')"
        )
        # active wallet owned by a DIFFERENT agent
        await db.execute(
            "INSERT INTO agent_wallets (wallet_id, agent_id, network, address, status) "
            "VALUES ('w_other', 'agent_b', 'ethereum', '0xCCC', 'active')"
        )
        return db
    except Exception:
        # If seeding fails after connect, close the connection before propagating
        # so the caller's temp-file cleanup isn't left holding an open handle.
        await db.disconnect()
        raise


def test_agent_wallets_real_schema_is_an_address_registry():
    """migration-022 `agent_wallets` is an ADDRESS REGISTRY: it has
    `address`/`status`/`network` (what the confirm path reads) and NONE of the
    columns the buggy balance handler selected. Pins the factual basis for the
    501 and catches anyone adding a fictional balance column without wiring a
    source to populate it."""

    async def scenario():
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db = None
        try:
            db = await _open_seeded_db(path)
            rows = await db.fetch_all("PRAGMA table_info(agent_wallets)")
            return {r["name"] for r in rows}
        finally:
            if db is not None:
                await db.disconnect()
            os.unlink(path)

    cols = asyncio.run(scenario())
    assert {"agent_id", "address", "status", "network"} <= cols
    assert not ({"balance", "currency", "last_updated", "wallet_address"} & cols), (
        "agent_wallets has no balance store; the balance route must not SELECT these "
        "columns (that is the drift that 500'd in prod)"
    )


def test_verify_agent_wallet_runs_against_real_schema(monkeypatch):
    """The live confirm-path query (`WalletService.verify_agent_wallet`) must
    work against the REAL agent_wallets schema. Runs the actual service — not a
    copied query string — over a real SQLite engine built from migration 022. If
    the query ever references a column the migration lacks, this fails (where a
    FakeDB silently passes)."""
    from db.database import database as global_db
    from services.wallet_service import wallet_service

    async def scenario():
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db = None
        try:
            db = await _open_seeded_db(path)
            # Delegate the global singleton's method to the real SQLite engine —
            # patch the METHOD on the object, never swap the object (a rebind
            # would poison the module-level binder session-wide).
            monkeypatch.setattr(global_db, "fetch_one", db.fetch_one)
            return {
                "owned_active": await wallet_service.verify_agent_wallet("agent_a", "0xAAA"),
                "owned_pending": await wallet_service.verify_agent_wallet("agent_a", "0xBBB"),
                "foreign": await wallet_service.verify_agent_wallet("agent_a", "0xCCC"),
                "absent": await wallet_service.verify_agent_wallet("agent_a", "0xZZZ"),
            }
        finally:
            if db is not None:
                await db.disconnect()
            os.unlink(path)

    r = asyncio.run(scenario())
    assert r["owned_active"] is True, "active wallet owned by the agent must authorize"
    assert r["owned_pending"] is False, "a non-active wallet must NOT authorize"
    assert r["foreign"] is False, "another agent's wallet must NOT authorize"
    assert r["absent"] is False, "an unknown address must NOT authorize"
