from __future__ import annotations

import datetime
import os
from typing import Any, Optional

from databases import Database
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    Integer,
    JSON,
    MetaData,
    String,
    Table,
    create_engine,
)

from config.settings import settings

def _normalize_database_url(raw: str) -> str:
    url_str = (raw or "").strip()
    # Heroku/Render/Railway sometimes provide postgres:// which SQLAlchemy doesn't accept
    if url_str.startswith("postgres://"):
        url_str = url_str.replace("postgres://", "postgresql://", 1)
    return url_str


# Prefer explicit settings, else env var, else a local SQLite db for dev/tests.
DATABASE_URL = _normalize_database_url(settings.database_url or os.getenv("DATABASE_URL", ""))
if not DATABASE_URL:
    DATABASE_URL = "sqlite+aiosqlite:///./pivota.db"

lower_url = DATABASE_URL.lower()
IS_POSTGRES = lower_url.startswith("postgresql://") or lower_url.startswith("postgres://")
IS_SQLITE = lower_url.startswith("sqlite://") or lower_url.startswith("sqlite+aiosqlite://")
if not (IS_POSTGRES or IS_SQLITE):
    raise RuntimeError(
        "❌ Invalid DATABASE_URL!\n"
        f"Got: {DATABASE_URL[:80]}...\n"
        "Supported URL schemes: postgresql://, postgres://, sqlite://, sqlite+aiosqlite://"
    )

# Dialect-aware JSONB type: use JSON for non-Postgres engines.
JSONB_TYPE = JSON
if IS_POSTGRES:
    try:
        from sqlalchemy.dialects.postgresql import JSONB as _PG_JSONB

        JSONB_TYPE = _PG_JSONB
    except Exception:
        JSONB_TYPE = JSON
else:
    # Compatibility: some modules still declare columns using Postgres JSONB.
    # When running on SQLite for local dev/tests, compile that JSONB type as JSON
    # so metadata.create_all does not fail.
    try:
        from sqlalchemy.dialects.postgresql import JSONB as _PG_JSONB  # type: ignore
        from sqlalchemy.ext.compiler import compiles  # type: ignore

        @compiles(_PG_JSONB, "sqlite")  # type: ignore[misc]
        def _compile_jsonb_sqlite(_type, _compiler, **_kw):  # type: ignore[no-untyped-def]
            return "JSON"

    except Exception:
        pass

# Initialize DB connection (databases library handles pooling)
database = Database(DATABASE_URL)
# Lazy asyncpg pool for legacy helpers (Postgres only)
_asyncpg_pool: Any = None


async def get_db_pool():
    """
    Backward-compatible helper for routes that still expect an asyncpg pool.
    Lazily creates a shared pool using the configured DATABASE_URL.
    """
    if not IS_POSTGRES:
        raise RuntimeError("asyncpg pool is only available when DATABASE_URL is PostgreSQL")

    global _asyncpg_pool
    if _asyncpg_pool is None:
        # Lazy import to avoid hard-failing local dev when asyncpg wheels are broken/unavailable.
        import asyncpg  # type: ignore

        _asyncpg_pool = await asyncpg.create_pool(DATABASE_URL)
    return _asyncpg_pool


metadata = MetaData()

transactions = Table(
    "transactions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("order_id", String, unique=True, index=True),
    Column("merchant_id", String, index=True),
    Column("amount", Float),
    Column("currency", String(8)),
    Column("status", String(32), default="pending"),
    Column("psp", String(32), nullable=True),
    Column("psp_txn_id", String(128), nullable=True),
    Column("created_at", DateTime, default=datetime.datetime.utcnow),
    Column("meta", JSON, nullable=True),
)


promotions = Table(
    "promotions",
    metadata,
    Column("id", String, primary_key=True),
    Column("merchant_id", String, index=True, nullable=False),
    Column("name", String, nullable=False),
    Column("type", String, nullable=False),  # FLASH_SALE | MULTI_BUY_DISCOUNT
    Column("description", String, nullable=True),
    Column("start_at", DateTime, nullable=False),
    Column("end_at", DateTime, nullable=False),
    Column("channels", JSONB_TYPE, nullable=False),
    Column("scope", JSONB_TYPE, nullable=False),
    Column("config", JSONB_TYPE, nullable=False),
    Column("expose_to_creators", Boolean, nullable=False, default=True),
    Column("allowed_creator_ids", JSONB_TYPE, nullable=True),
    Column("human_readable_rule", String, nullable=True),
    Column("created_at", DateTime, default=datetime.datetime.utcnow, nullable=False),
    Column("updated_at", DateTime, default=datetime.datetime.utcnow, nullable=False),
    Column("deleted_at", DateTime, nullable=True),
    CheckConstraint("start_at < end_at", name="ck_promotions_time_window"),
)

# Create synchronous engine for table creation
sync_url = str(DATABASE_URL)
if IS_SQLITE and sync_url.startswith("sqlite+aiosqlite://"):
    # SQLAlchemy uses sqlite:// for sync engines; aiosqlite is for async drivers.
    sync_url = sync_url.replace("sqlite+aiosqlite://", "sqlite://", 1)

try:
    engine = create_engine(sync_url)
    # Tables will be created in main.py startup to ensure proper initialization
except Exception as err:
    # Log helpful error for connection issues
    print(f"⚠️ Could not create engine: {err}")
    # Don't raise here - let the app handle it during startup
