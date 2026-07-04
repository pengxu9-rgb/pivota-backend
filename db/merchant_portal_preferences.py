from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict

from sqlalchemy import Boolean, Column, DateTime, String, Table
from sqlalchemy.sql import func

from db.database import database, metadata

logger = logging.getLogger(__name__)

SUPPORTED_PORTAL_LANGUAGES = (
    "en",
    "zh-CN",
    "ja-JP",
    "ko-KR",
    "fr-FR",
    "de-DE",
)
DEFAULT_PORTAL_LANGUAGE = "en"

DEFAULT_MERCHANT_PORTAL_PREFERENCES: Dict[str, Any] = {
    "email_orders": True,
    "email_payments": True,
    "email_inventory": False,
    "email_weekly": False,
    "portal_language": DEFAULT_PORTAL_LANGUAGE,
    # W5 P7: per-merchant consent for audit executor dispatch. Default ON
    # preserves current behavior (existing merchants keep auto-execute);
    # merchants opt into approval mode by setting this false.
    "executor_auto_execute": True,
}


def normalize_portal_language(value: Any) -> str:
    candidate = str(value or "").strip()
    if candidate in SUPPORTED_PORTAL_LANGUAGES:
        return candidate
    return DEFAULT_PORTAL_LANGUAGE

merchant_portal_preferences = Table(
    "merchant_portal_preferences",
    metadata,
    Column("merchant_id", String(50), primary_key=True),
    Column("email_orders", Boolean, nullable=False, default=True),
    Column("email_payments", Boolean, nullable=False, default=True),
    Column("email_inventory", Boolean, nullable=False, default=False),
    Column("email_weekly", Boolean, nullable=False, default=False),
    Column("portal_language", String(16), nullable=False, default=DEFAULT_PORTAL_LANGUAGE),
    Column("executor_auto_execute", Boolean, nullable=False, default=True),
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
                  portal_language VARCHAR(16) NOT NULL DEFAULT 'en',
                  executor_auto_execute BOOLEAN NOT NULL DEFAULT TRUE,
                  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """,
                "ALTER TABLE merchant_portal_preferences ADD COLUMN IF NOT EXISTS email_orders BOOLEAN NOT NULL DEFAULT TRUE;",
                "ALTER TABLE merchant_portal_preferences ADD COLUMN IF NOT EXISTS email_payments BOOLEAN NOT NULL DEFAULT TRUE;",
                "ALTER TABLE merchant_portal_preferences ADD COLUMN IF NOT EXISTS email_inventory BOOLEAN NOT NULL DEFAULT FALSE;",
                "ALTER TABLE merchant_portal_preferences ADD COLUMN IF NOT EXISTS email_weekly BOOLEAN NOT NULL DEFAULT FALSE;",
                "ALTER TABLE merchant_portal_preferences ADD COLUMN IF NOT EXISTS portal_language VARCHAR(16) NOT NULL DEFAULT 'en';",
                "ALTER TABLE merchant_portal_preferences ADD COLUMN IF NOT EXISTS executor_auto_execute BOOLEAN NOT NULL DEFAULT TRUE;",
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
        "portal_language": normalize_portal_language(payload.get("portal_language")),
        "executor_auto_execute": bool(
            payload.get(
                "executor_auto_execute",
                DEFAULT_MERCHANT_PORTAL_PREFERENCES["executor_auto_execute"],
            )
        ),
        "created_at": payload.get("created_at"),
        "updated_at": payload.get("updated_at"),
    }


async def upsert_merchant_portal_preferences(
    merchant_id: str,
    preferences: Dict[str, Any],
) -> Dict[str, Any]:
    await ensure_merchant_portal_preferences_table()

    existing = await database.fetch_one(
        merchant_portal_preferences.select().where(
            merchant_portal_preferences.c.merchant_id == merchant_id
        )
    )

    existing_payload = dict(existing) if existing else {}
    merged = {
        "email_orders": bool(
            preferences.get(
                "email_orders",
                existing_payload.get(
                    "email_orders", DEFAULT_MERCHANT_PORTAL_PREFERENCES["email_orders"]
                ),
            )
        ),
        "email_payments": bool(
            preferences.get(
                "email_payments",
                existing_payload.get(
                    "email_payments", DEFAULT_MERCHANT_PORTAL_PREFERENCES["email_payments"]
                ),
            )
        ),
        "email_inventory": bool(
            preferences.get(
                "email_inventory",
                existing_payload.get(
                    "email_inventory", DEFAULT_MERCHANT_PORTAL_PREFERENCES["email_inventory"]
                ),
            )
        ),
        "email_weekly": bool(
            preferences.get(
                "email_weekly",
                existing_payload.get(
                    "email_weekly", DEFAULT_MERCHANT_PORTAL_PREFERENCES["email_weekly"]
                ),
            )
        ),
        "portal_language": normalize_portal_language(
            preferences.get(
                "portal_language",
                existing_payload.get(
                    "portal_language", DEFAULT_MERCHANT_PORTAL_PREFERENCES["portal_language"]
                ),
            )
        ),
        "executor_auto_execute": bool(
            preferences.get(
                "executor_auto_execute",
                existing_payload.get(
                    "executor_auto_execute",
                    DEFAULT_MERCHANT_PORTAL_PREFERENCES["executor_auto_execute"],
                ),
            )
        ),
    }

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


async def get_merchant_executor_auto_execute(merchant_id: str) -> bool:
    """W5 P7: per-merchant consent for audit executor dispatch.

    Returns True (auto-execute) when the merchant has no row or the
    read fails — the founder-locked default preserves current behavior.
    The dispatcher reads this to decide whether executor_runs land in
    `queued` (auto) or `pending_approval` (opt-in approval mode).
    """
    try:
        prefs = await get_merchant_portal_preferences(merchant_id)
    except Exception as exc:  # noqa: BLE001 — never block dispatch on a read
        logger.warning(
            "get_merchant_executor_auto_execute failed for %s (defaulting ON): %s",
            merchant_id, str(exc)[:200],
        )
        return True
    return bool(
        prefs.get(
            "executor_auto_execute",
            DEFAULT_MERCHANT_PORTAL_PREFERENCES["executor_auto_execute"],
        )
    )


async def set_merchant_executor_auto_execute(
    merchant_id: str, enabled: bool,
) -> bool:
    """Persist the executor auto-execute consent toggle. Returns the
    stored value."""
    prefs = await upsert_merchant_portal_preferences(
        merchant_id, {"executor_auto_execute": bool(enabled)},
    )
    return bool(prefs.get("executor_auto_execute", True))
