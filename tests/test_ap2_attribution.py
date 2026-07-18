"""
Real-schema (SQLite from migrations 023 + 185) coverage for AP2 attribution parity
(#1479).

The AP2/x402 rail previously carried NO attribution linkage. Now `initiate` sets
`order_id` and stores the agent's `pvt_*` in `metadata`, and `confirm` reads them to
write a `commerce_attribution_edge` (non-custodial — ADR-016/017). This test builds
`x402_transactions` from the ACTUAL migration DDL (not a FakeDB) and asserts the
attribution-carrying columns (`order_id`, `metadata`) exist and round-trip a
`pvt_click_id` — so a schema drift that dropped them would break the confirm read.
The full x402_transactions schema/CHECK is covered by
tests/test_ap2_x402_transactions_schema.py.
"""
import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path

import databases

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

MIGRATION_023 = BACKEND_ROOT / "db" / "migrations" / "023_x402_protocol.sql"


def _x402_transactions_ddl_for_sqlite() -> str:
    """The REAL x402_transactions CREATE TABLE from migration 023, normalized for
    SQLite. Column names come straight from the migration, so an attribution-column
    drift breaks the test. The status CHECK is stripped (migration 185 widens it;
    it is exercised in test_ap2_x402_transactions_schema.py)."""
    sql = MIGRATION_023.read_text()
    block = re.search(
        r"CREATE TABLE IF NOT EXISTS x402_transactions\s*\(.*?\n\);", sql, re.S
    ).group(0)
    block = re.sub(r"\bSERIAL\b", "INTEGER", block)
    block = re.sub(r"CHECK\s*\(status IN \([^)]*\)\)", "", block)
    block = re.sub(r"TIMESTAMP WITH TIME ZONE", "TIMESTAMP", block)
    block = re.sub(r"\bJSONB\b", "TEXT", block)
    block = re.sub(r"DEFAULT NOW\(\)", "DEFAULT CURRENT_TIMESTAMP", block)
    return block


async def _open_x402_db(path: str) -> databases.Database:
    db = databases.Database(f"sqlite+aiosqlite:///{path}")
    await db.connect()
    await db.execute(_x402_transactions_ddl_for_sqlite())
    # migration 185 adds the two columns the transaction routes rely on
    await db.execute("ALTER TABLE x402_transactions ADD COLUMN product_id VARCHAR(128)")
    await db.execute("ALTER TABLE x402_transactions ADD COLUMN confirmed_at TIMESTAMP")
    return db


def test_x402_carries_attribution_fields_against_real_schema():
    """`initiate` now sets `order_id` + stores `metadata`; `confirm` reads them to
    write the attribution edge. Prove those columns exist on the real schema and
    round-trip a `pvt_click_id`, so the confirm SELECT can't silently drift."""

    async def scenario():
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db = None
        try:
            db = await _open_x402_db(path)
            cols = {c["name"] for c in await db.fetch_all("PRAGMA table_info(x402_transactions)")}
            # the columns the AP2 attribution write depends on
            assert {"order_id", "merchant_id", "product_id", "metadata"} <= cols, cols
            # initiate-shaped insert: order_id = transaction_id; pvt_* in metadata
            await db.execute(
                """INSERT INTO x402_transactions
                       (transaction_id, order_id, agent_id, merchant_id, amount, currency,
                        authorization_code, product_id, status, metadata)
                   VALUES ('ap2_txn_1','ap2_txn_1','agent_a','m_1',25,'USD','ac_1','prod_9',
                           'pending', :meta)""",
                {"meta": json.dumps({"pvt_click_id": "clk_abc", "pvt_surface": "chatgpt"})},
            )
            # the exact confirm SELECT
            row = await db.fetch_one(
                """SELECT transaction_id, order_id, agent_id, merchant_id, product_id,
                          amount, currency, status, metadata
                   FROM x402_transactions WHERE transaction_id = 'ap2_txn_1'"""
            )
            return dict(row)
        finally:
            if db is not None:
                await db.disconnect()
            os.unlink(path)

    row = asyncio.run(scenario())
    from services.commerce_attribution_service import has_attribution_signal

    assert row["order_id"] == "ap2_txn_1"          # set at initiate, keys the edge
    meta = json.loads(row["metadata"])
    assert meta["pvt_click_id"] == "clk_abc"       # the attribution signal round-trips
    # the payload confirm builds would deposit an edge (not self-gate)
    assert has_attribution_signal({**meta, "product_id": row["product_id"]})
