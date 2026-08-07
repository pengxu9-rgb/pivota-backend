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


def _env_int(name: str, default: int, *, min_value: int, max_value: int) -> int:
    raw = (os.getenv(name) or "").strip()
    try:
        value = int(raw) if raw else default
    except Exception:
        value = default
    return max(min_value, min(max_value, value))


def _env_float(name: str, default: float, *, min_value: float, max_value: float) -> float:
    raw = (os.getenv(name) or "").strip()
    try:
        value = float(raw) if raw else default
    except Exception:
        value = default
    if value != value or value in (float("inf"), float("-inf")):
        # NaN/inf survive min/max clamping (NaN comparisons are False, so
        # min(cap, nan) keeps the cap) — resolve non-finite to the default.
        value = default
    return max(min_value, min(max_value, value))


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
        from sqlalchemy.dialects.postgresql import UUID as _PG_UUID  # type: ignore
        from sqlalchemy.sql.sqltypes import ARRAY as _SA_ARRAY  # type: ignore
        from sqlalchemy.ext.compiler import compiles  # type: ignore

        @compiles(_PG_JSONB, "sqlite")  # type: ignore[misc]
        def _compile_jsonb_sqlite(_type, _compiler, **_kw):  # type: ignore[no-untyped-def]
            return "JSON"

        # Some tables declare Postgres UUID columns (e.g. merchant_audit_runs.run_id).
        # SQLite has no UUID type; store as text so metadata.create_all does not fail.
        @compiles(_PG_UUID, "sqlite")  # type: ignore[misc]
        def _compile_uuid_sqlite(_type, _compiler, **_kw):  # type: ignore[no-untyped-def]
            return "CHAR(36)"

        # Some tables declare ARRAY columns (Postgres-only). For local SQLite dev,
        # compile ARRAY as JSON so metadata.create_all does not fail.
        @compiles(_SA_ARRAY, "sqlite")  # type: ignore[misc]
        def _compile_array_sqlite(_type, _compiler, **_kw):  # type: ignore[no-untyped-def]
            return "JSON"

    except Exception:
        pass

# Initialize DB connection (databases library handles pooling)
database_kwargs = {}
if IS_POSTGRES:
    database_kwargs = {
        "min_size": _env_int("DB_POOL_MIN_SIZE", 5, min_value=1, max_value=50),
        "max_size": _env_int("DB_POOL_MAX_SIZE", 20, min_value=1, max_value=100),
        # `databases` passes this into asyncpg connect kwargs (connection timeout).
        "timeout": _env_float("DB_POOL_ACQUIRE_TIMEOUT_SECONDS", 5.0, min_value=0.1, max_value=60.0),
    }
    if database_kwargs["max_size"] < database_kwargs["min_size"]:
        database_kwargs["max_size"] = database_kwargs["min_size"]
    # Optional per-statement ceiling (asyncpg `command_timeout`). asyncpg has NO
    # default statement timeout, so a socket that dies without RST leaves an
    # await hanging forever — a CLI run over the Railway public proxy hung 36
    # minutes on 0.3s of CPU this way (2026-07-17). Unset (the default) keeps
    # current behavior everywhere, incl. prod; ops CLIs opt in via env.
    _command_timeout = _env_float(
        "DB_COMMAND_TIMEOUT_SECONDS", 0.0, min_value=0.0, max_value=600.0
    )
    # Opt-in TLS for the Railway PUBLIC proxy (self-signed cert): asyncpg
    # ignores libpq's sslmode, so a local ops CLI hitting the public URL either
    # times out (no TLS) or fails verification (ssl=true). DB_SSL_NO_VERIFY=1
    # sends TLS without cert verification — encryption without authentication,
    # acceptable for read-only ops runs, NEVER set in a deployed environment
    # (prod connects over the internal network with no TLS need).
    if str(os.getenv("DB_SSL_NO_VERIFY", "")).strip().lower() in ("1", "true", "yes"):
        if os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_SERVICE_ID"):
            raise RuntimeError(
                "DB_SSL_NO_VERIFY must never be set in a deployed environment — "
                "it disables certificate verification for the whole pool. It is "
                "for LOCAL read-only ops runs against the public proxy only."
            )
        import ssl as _ssl
        _ctx = _ssl.create_default_context()
        _ctx.check_hostname = False
        _ctx.verify_mode = _ssl.CERT_NONE
        database_kwargs["ssl"] = _ctx
    if _command_timeout > 0:
        database_kwargs["command_timeout"] = _command_timeout

database = Database(DATABASE_URL, **database_kwargs)
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

        _pool_kwargs = {}
        if database_kwargs.get("command_timeout"):
            # Same opt-in statement ceiling as the primary `database` object —
            # legacy-pool callers must not retain the infinite-hang behavior.
            _pool_kwargs["command_timeout"] = database_kwargs["command_timeout"]
        _asyncpg_pool = await asyncpg.create_pool(DATABASE_URL, **_pool_kwargs)
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
    Column("end_at", DateTime, nullable=True),
    Column("channels", JSONB_TYPE, nullable=False),
    Column("scope", JSONB_TYPE, nullable=False),
    Column("config", JSONB_TYPE, nullable=False),
    Column("expose_to_creators", Boolean, nullable=False, default=True),
    Column("allowed_creator_ids", JSONB_TYPE, nullable=True),
    Column("human_readable_rule", String, nullable=True),
    Column("created_at", DateTime, default=datetime.datetime.utcnow, nullable=False),
    Column("updated_at", DateTime, default=datetime.datetime.utcnow, nullable=False),
    Column("deleted_at", DateTime, nullable=True),
    CheckConstraint("end_at IS NULL OR start_at < end_at", name="ck_promotions_time_window"),
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
