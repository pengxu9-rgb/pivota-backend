from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict

from sqlalchemy import Boolean, Column, DateTime, String, Table
from sqlalchemy.sql import func

from db.database import database, metadata

logger = logging.getLogger(__name__)

DEFAULT_MERCHANT_PORTAL_PREFERENCES: Dict[str, bool] = {
    "email_orders": True,
    "email_payments": True,
    "email_inventory": False,
    "email_weekly": False,
}

merchant_portal_preferences = Table(
    "merchant_portal_preferences",
    metadata,
    Column("merchant_id", String(50), primary_key=True),
    Column("email_orders", Boolean, nullable=False, default=True),
    Column("email_payments", Boolean, nullable=False, default=True),
    Column("email_inventory", Boolean, nullable=False, default=False),
    Column("email_weekly", Boolean, nullable=False, default=False),
    Column("created_at", DateTime, server_default=func.now(), nullable=False),
    Column("updated_at", DateTime, server_default=func.now(), onupdate=func.now(), nullable=False),
)

_PREFERENCES_DDL_READY = False
_PREFERENCES_DDL_LOCK = asyncio.Lock()


async def ensure_merchant_portal_preferences_table() -> None:
    global _PREFERENCES_DDL_READY
    if _PREFERENCES_DDL_READY:
        return

    async with _PREFERENCES_DDL_LOCK:
        if _PREFERENCES_DDL_READY:
            return

        try:
            statements = [
                """
                CREATE TABLE IF NOT EXISTS merchant_portal_preferences (
                  merchant_id VARCHAR(50) PRIMARY KEY,
                  email_orders BOOLEAN NOT NULL DEFAULT TRUE,
                  email_payments BOOLEAN NOT NULL DEFAULT TRUE,
                  email_inventory BOOLEAN NOT NULL DEFAULT FALSE,
                  email_weekly BOOLEAN NOT NULL DEFAULT FALSE,
                  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """,
                "ALTER TABLE merchant_portal_preferences ADD COLUMN IF NOT EXISTS email_orders BOOLEAN NOT NULL DEFAULT TRUE;",
                "ALTER TABLE merchant_portal_preferences ADD COLUMN IF NOT EXISTS email_payments BOOLEAN NOT NULL DEFAULT TRUE;",
                "ALTER TABLE merchant_portal_preferences ADD COLUMN IF NOT EXISTS email_inventory BOOLEAN NOT NULL DEFAULT FALSE;",
                "ALTER TABLE merchant_portal_preferences ADD COLUMN IF NOT EXISTS email_weekly BOOLEAN NOT NULL DEFAULT FALSE;",
                "ALTER TABLE merchant_portal_preferences ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();",
                "ALTER TABLE merchant_portal_preferences ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();",
            ]
            for statement in statements:
                await database.execute(statement)
        except Exception as exc:
            logger.warning("ensure_merchant_portal_preferences_table failed (best-effort): %s", str(exc)[:200])
            return

        _PREFERENCES_DDL_READY = True


async def get_merchant_portal_preferences(merchant_id: str) -> Dict[str, Any]:
    await ensure_merchant_portal_preferences_table()
    row = await database.fetch_one(
        merchant_portal_preferences.select().where(
            merchant_portal_preferences.c.merchant_id == merchant_id
        )
    )
    if not row:
        return {
            "merchant_id": merchant_id,
            **DEFAULT_MERCHANT_PORTAL_PREFERENCES,
        }

    payload = dict(row)
    return {
        "merchant_id": merchant_id,
        "email_orders": bool(payload.get("email_orders")),
        "email_payments": bool(payload.get("email_payments")),
        "email_inventory": bool(payload.get("email_inventory")),
        "email_weekly": bool(payload.get("email_weekly")),
        "created_at": payload.get("created_at"),
        "updated_at": payload.get("updated_at"),
    }


async def upsert_merchant_portal_preferences(
    merchant_id: str,
    preferences: Dict[str, Any],
) -> Dict[str, Any]:
    await ensure_merchant_portal_preferences_table()
    merged = {
        **DEFAULT_MERCHANT_PORTAL_PREFERENCES,
        **{key: bool(value) for key, value in preferences.items() if key in DEFAULT_MERCHANT_PORTAL_PREFERENCES},
    }

    existing = await database.fetch_one(
        merchant_portal_preferences.select().where(
            merchant_portal_preferences.c.merchant_id == merchant_id
        )
    )

    if existing:
        await database.execute(
            merchant_portal_preferences.update()
            .where(merchant_portal_preferences.c.merchant_id == merchant_id)
            .values(**merged, updated_at=datetime.utcnow())
        )
    else:
        await database.execute(
            merchant_portal_preferences.insert().values(
                merchant_id=merchant_id,
                **merged,
                updated_at=datetime.utcnow(),
            )
        )

    return await get_merchant_portal_preferences(merchant_id)
