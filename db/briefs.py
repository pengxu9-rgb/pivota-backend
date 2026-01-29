from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from db.database import IS_POSTGRES, database
from utils.logger import logger


_BRIEFS_DDL_READY = False
_BRIEFS_DDL_LOCK = asyncio.Lock()


async def ensure_briefs_table() -> None:
    """
    Best-effort defensive DDL for Brief v0 persistence.

    Notes:
    - This is intentionally fail-open: API endpoints may still run on environments
      where migrations cannot be applied manually.
    - SQL is written to be compatible with both Postgres and SQLite (SQLite will
      accept unknown type names as affinities).
    """
    global _BRIEFS_DDL_READY
    if _BRIEFS_DDL_READY:
        return
    async with _BRIEFS_DDL_LOCK:
        if _BRIEFS_DDL_READY:
            return
        try:
            statements = [
                """
                CREATE TABLE IF NOT EXISTS shopping_briefs (
                  brief_id TEXT PRIMARY KEY,
                  schema_version TEXT NOT NULL,
                  agent_id TEXT NOT NULL,
                  vertical TEXT NOT NULL,
                  market TEXT,
                  locale TEXT,
                  currency TEXT,
                  raw_intent TEXT NOT NULL,
                  brief_json JSONB NOT NULL,
                  status TEXT NOT NULL DEFAULT 'active',
                  created_at TIMESTAMPTZ NOT NULL,
                  updated_at TIMESTAMPTZ NOT NULL
                );
                """,
                "CREATE INDEX IF NOT EXISTS idx_shopping_briefs_agent_id ON shopping_briefs(agent_id);",
                "CREATE INDEX IF NOT EXISTS idx_shopping_briefs_vertical ON shopping_briefs(vertical);",
                "CREATE INDEX IF NOT EXISTS idx_shopping_briefs_status ON shopping_briefs(status);",
                "CREATE INDEX IF NOT EXISTS idx_shopping_briefs_created_at ON shopping_briefs(created_at);",
            ]
            for stmt in statements:
                await database.execute(stmt)
        except Exception as e:
            logger.warning("ensure_briefs_table failed (best-effort)", extra={"error": str(e)})
            return
        _BRIEFS_DDL_READY = True


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def insert_brief(row: Dict[str, Any]) -> None:
    await ensure_briefs_table()
    params = {
        "brief_id": row.get("brief_id"),
        "schema_version": row.get("schema_version"),
        "agent_id": row.get("agent_id"),
        "vertical": row.get("vertical"),
        "market": row.get("market"),
        "locale": row.get("locale"),
        "currency": row.get("currency"),
        "raw_intent": row.get("raw_intent"),
        "brief_json": json.dumps(row.get("brief_json") or {}, ensure_ascii=False),
        "status": row.get("status") or "active",
        "created_at": row.get("created_at") or _utc_now(),
        "updated_at": row.get("updated_at") or _utc_now(),
    }

    if IS_POSTGRES:
        await database.execute(
            """
            INSERT INTO shopping_briefs
              (brief_id, schema_version, agent_id, vertical, market, locale, currency, raw_intent, brief_json, status, created_at, updated_at)
            VALUES
              (:brief_id, :schema_version, :agent_id, :vertical, :market, :locale, :currency, :raw_intent, CAST(:brief_json AS jsonb), :status, :created_at, :updated_at)
            ON CONFLICT (brief_id) DO NOTHING
            """,
            params,
        )
        return

    # SQLite/dev: no jsonb casts and use INSERT OR IGNORE for idempotency.
    await database.execute(
        """
        INSERT OR IGNORE INTO shopping_briefs
          (brief_id, schema_version, agent_id, vertical, market, locale, currency, raw_intent, brief_json, status, created_at, updated_at)
        VALUES
          (:brief_id, :schema_version, :agent_id, :vertical, :market, :locale, :currency, :raw_intent, :brief_json, :status, :created_at, :updated_at)
        """,
        params,
    )


async def get_brief(brief_id: str) -> Optional[Dict[str, Any]]:
    await ensure_briefs_table()
    row = await database.fetch_one(
        """
        SELECT brief_id, schema_version, agent_id, vertical, market, locale, currency, raw_intent, brief_json, status, created_at, updated_at
        FROM shopping_briefs
        WHERE brief_id = :brief_id
        LIMIT 1
        """,
        {"brief_id": brief_id},
    )
    if not row:
        return None
    out = dict(row)
    # Normalize brief_json for drivers that return string.
    raw = out.get("brief_json")
    if isinstance(raw, str):
        try:
            out["brief_json"] = json.loads(raw)
        except Exception:
            out["brief_json"] = {}
    return out
