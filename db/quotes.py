from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from sqlalchemy import Column, DateTime, String, Table, Text
from sqlalchemy.dialects.postgresql import JSONB

from db.database import database, metadata
from db.startup_ddl import execute_ddl
from utils.logger import logger


quotes = Table(
    "quotes",
    metadata,
    Column("quote_id", String(64), primary_key=True),
    Column("merchant_id", String(64), index=True, nullable=False),
    Column("agent_id", String(64), index=True, nullable=True),
    Column("engine", String(64), nullable=False),
    Column("engine_ref", String(256), nullable=False),
    Column("request_fingerprint", String(128), index=True, nullable=False),
    Column("request_json", JSONB, nullable=False),
    Column("snapshot_json", JSONB, nullable=False),
    Column("quote_hash_sha256", String(64), nullable=True),
    Column("status", String(32), index=True, nullable=False),  # active | consumed | expired
    Column("expires_at", DateTime(timezone=True), index=True, nullable=False),
    Column("consumed_at", DateTime(timezone=True), nullable=True),
    Column("consumed_order_id", String(64), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("debug_id", String(64), nullable=True),
    Column("notes", Text, nullable=True),
)


_QUOTES_DDL_READY = False
_QUOTES_DDL_LOCK = asyncio.Lock()


async def ensure_quotes_table() -> None:
    """
    Best-effort defensive DDL. Production normally relies on metadata.create_all(engine)
    during startup, but this avoids hard failures if that import ordering changes.
    """
    global _QUOTES_DDL_READY
    if _QUOTES_DDL_READY:
        return
    async with _QUOTES_DDL_LOCK:
        if _QUOTES_DDL_READY:
            return
    try:
        # NOTE: Keep each statement separate; some drivers reject multi-statement executes.
        statements = [
            """
            CREATE TABLE IF NOT EXISTS quotes (
              quote_id VARCHAR(64) PRIMARY KEY,
              merchant_id VARCHAR(64) NOT NULL,
              agent_id VARCHAR(64),
              engine VARCHAR(64) NOT NULL,
              engine_ref VARCHAR(256) NOT NULL,
              request_fingerprint VARCHAR(128) NOT NULL,
              request_json JSONB NOT NULL,
              snapshot_json JSONB NOT NULL,
              quote_hash_sha256 CHAR(64),
              status VARCHAR(32) NOT NULL,
              expires_at TIMESTAMPTZ NOT NULL,
              consumed_at TIMESTAMPTZ,
              consumed_order_id VARCHAR(64),
              created_at TIMESTAMPTZ NOT NULL,
              updated_at TIMESTAMPTZ NOT NULL,
              debug_id VARCHAR(64),
              notes TEXT
            );
            """,
            "ALTER TABLE quotes ADD COLUMN IF NOT EXISTS quote_hash_sha256 CHAR(64);",
            "ALTER TABLE quotes ADD COLUMN IF NOT EXISTS consumed_order_id VARCHAR(64);",
            "CREATE INDEX IF NOT EXISTS idx_quotes_merchant_id ON quotes(merchant_id);",
            "CREATE INDEX IF NOT EXISTS idx_quotes_status ON quotes(status);",
            "CREATE INDEX IF NOT EXISTS idx_quotes_expires_at ON quotes(expires_at);",
            "CREATE INDEX IF NOT EXISTS idx_quotes_request_fingerprint ON quotes(request_fingerprint);",
            "CREATE INDEX IF NOT EXISTS idx_quotes_consumed_order_id ON quotes(consumed_order_id);",
        ]
        for stmt in statements:
            # A concurrent session may win the CREATE ... IF NOT EXISTS race;
            # execute_ddl treats that as "already exists" (success).
            await execute_ddl(stmt, db=database)
    except Exception as e:
        # Best-effort; if this fails, inserts will surface the error with context.
        logger.warning("ensure_quotes_table failed (best-effort)", extra={"error": str(e)})
        return
    _QUOTES_DDL_READY = True


async def insert_quote(row: Dict[str, Any]) -> None:
    await ensure_quotes_table()
    await database.execute(quotes.insert().values(**row))


async def get_quote(quote_id: str) -> Optional[Dict[str, Any]]:
    await ensure_quotes_table()
    q = quotes.select().where(quotes.c.quote_id == quote_id)
    rec = await database.fetch_one(q)
    return dict(rec) if rec else None


async def mark_quote_consumed(quote_id: str, *, consumed_order_id: Optional[str] = None) -> bool:
    await ensure_quotes_table()
    now = datetime.now(timezone.utc)
    q = (
        quotes.update()
        .where(quotes.c.quote_id == quote_id)
        .where(quotes.c.status == "active")
        .values(
            status="consumed",
            consumed_at=now,
            consumed_order_id=consumed_order_id,
            updated_at=now,
        )
    )
    rows = await database.execute(q)
    if rows is not None:
        return True

    # Backfill linkage if an older caller consumed the quote without an order_id.
    if consumed_order_id:
        q2 = (
            quotes.update()
            .where(quotes.c.quote_id == quote_id)
            .where(quotes.c.status == "consumed")
            .where(quotes.c.consumed_order_id.is_(None))
            .values(consumed_order_id=consumed_order_id, updated_at=now)
        )
        await database.execute(q2)
    return False


async def expire_quote_if_needed(quote_id: str) -> None:
    await ensure_quotes_table()
    now = datetime.now(timezone.utc)
    q = (
        quotes.update()
        .where(quotes.c.quote_id == quote_id)
        .where(quotes.c.status == "active")
        .where(quotes.c.expires_at < now)
        .values(status="expired", updated_at=now)
    )
    await database.execute(q)


def compute_expires_at(ttl_seconds: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=max(1, int(ttl_seconds)))
