"""
Schema-contract regression test for x402_transactions (migration 185).

Every other AP2 test uses an in-memory FakeDB, so none exercise the REAL schema —
which is exactly why the drift between migration 023 and the transaction routes
(routes/ap2_routes.py) went unnoticed: initiate/confirm/read would 500 against a
fresh 023-provisioned database. This runs the routes' actual INSERT/UPDATE/SELECT
against a real SQL engine (SQLite, which enforces NOT NULL + CHECK + column
existence) and asserts they FAIL on the committed-023 shape and SUCCEED on the
reconciled (post-185) shape.

The DDL below mirrors migration 023 (OLD) and 023+185 (FIXED); a separate test
guards that migration 185 actually makes those changes.
"""
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# --- schemas (SQLite-translated; TEXT for VARCHAR/timestamps, NUMERIC for DECIMAL) ---

_OLD_DDL = """
CREATE TABLE x402_transactions (
    id INTEGER PRIMARY KEY,
    transaction_id TEXT UNIQUE NOT NULL,
    order_id TEXT,
    agent_id TEXT,
    merchant_id TEXT,
    amount NUMERIC NOT NULL,
    currency TEXT NOT NULL,
    authorization_code TEXT NOT NULL,
    wallet_network TEXT,
    wallet_address TEXT,
    tx_hash TEXT,
    status TEXT CHECK (status IN ('authorized','captured','settled','refunded','voided','failed')),
    created_at TEXT,
    updated_at TEXT,
    metadata TEXT
);
"""

_FIXED_DDL = """
CREATE TABLE x402_transactions (
    id INTEGER PRIMARY KEY,
    transaction_id TEXT UNIQUE NOT NULL,
    order_id TEXT,
    agent_id TEXT,
    merchant_id TEXT,
    amount NUMERIC NOT NULL,
    currency TEXT NOT NULL,
    authorization_code TEXT,
    wallet_network TEXT,
    wallet_address TEXT,
    tx_hash TEXT,
    status TEXT CHECK (status IN ('pending','completed','failed','cancelled','authorized','captured','settled','refunded','voided')),
    created_at TEXT,
    updated_at TEXT,
    metadata TEXT,
    product_id TEXT,
    confirmed_at TEXT
);
"""

# --- the routes' actual statements (NOW() -> CURRENT_TIMESTAMP for SQLite) ---

_INITIATE_INSERT = """
    INSERT INTO x402_transactions (
        transaction_id, agent_id, merchant_id, amount, currency,
        product_id, status, metadata, created_at
    ) VALUES (
        :transaction_id, :agent_id, :merchant_id, :amount, :currency,
        :product_id, 'pending', :metadata, CURRENT_TIMESTAMP
    )
"""

_CONFIRM_UPDATE = """
    UPDATE x402_transactions
    SET status = 'completed', wallet_address = :wallet_address, confirmed_at = CURRENT_TIMESTAMP
    WHERE transaction_id = :transaction_id
"""

_GET_SELECT = """
    SELECT transaction_id, agent_id, merchant_id, amount, currency,
           status, created_at, confirmed_at
    FROM x402_transactions
    WHERE transaction_id = :transaction_id
"""

# admin_protocol_sync.cancel_transaction — only reachable once a 'pending' row can
# exist, which this migration is what first makes possible.
_CANCEL_UPDATE = """
    UPDATE x402_transactions
    SET status = 'cancelled'
    WHERE transaction_id = :transaction_id AND status = 'pending'
"""


def _params():
    return {"transaction_id": "t1", "agent_id": "a1", "merchant_id": "m1",
            "amount": 10.0, "currency": "USD", "product_id": "p1", "metadata": None}


def _conn(ddl):
    conn = sqlite3.connect(":memory:")
    conn.executescript(ddl)
    return conn


def test_committed_023_schema_breaks_the_transaction_route():
    # product_id column absent (and status='pending' violates the CHECK, and
    # authorization_code is NOT NULL but omitted) — the route INSERT cannot run.
    conn = _conn(_OLD_DDL)
    with pytest.raises(sqlite3.Error):
        conn.execute(_INITIATE_INSERT, _params())


def test_reconciled_schema_supports_the_full_lifecycle():
    conn = _conn(_FIXED_DDL)
    conn.execute(_INITIATE_INSERT, _params())
    conn.commit()
    conn.execute(_CONFIRM_UPDATE, {"wallet_address": "0xW", "transaction_id": "t1"})
    conn.commit()
    row = conn.execute(_GET_SELECT, {"transaction_id": "t1"}).fetchone()
    assert row is not None
    assert row[5] == "completed"      # status
    assert row[7] is not None         # confirmed_at set


def test_reconciled_schema_permits_cancelling_a_pending_transaction():
    # The migration first makes 'pending' rows possible; the cancel path must then
    # be able to move one to 'cancelled' without tripping the status CHECK.
    conn = _conn(_FIXED_DDL)
    conn.execute(_INITIATE_INSERT, _params())
    conn.commit()
    conn.execute(_CANCEL_UPDATE, {"transaction_id": "t1"})
    conn.commit()
    row = conn.execute(_GET_SELECT, {"transaction_id": "t1"}).fetchone()
    assert row[5] == "cancelled"      # status


def test_migration_185_reconciles_the_schema():
    sql = (BACKEND_ROOT / "db" / "migrations" /
           "185_x402_transactions_reconcile.sql").read_text()
    assert "ADD COLUMN IF NOT EXISTS product_id" in sql
    assert "ADD COLUMN IF NOT EXISTS confirmed_at" in sql
    assert "authorization_code DROP NOT NULL" in sql
    # status CHECK must now permit every status the AP2 routes write:
    # initiate 'pending', confirm 'completed', admin cancel 'cancelled'.
    assert "'pending'" in sql and "'completed'" in sql and "'cancelled'" in sql
