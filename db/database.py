from __future__ import annotations

import datetime
import asyncio
import logging
import os
from typing import Optional

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

from config.platform import is_deployed
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
from utils.transient_errors import PoolCheckoutTimeout  # noqa: E402

logger = logging.getLogger("db.database")

database_kwargs = {}
if IS_POSTGRES:
    database_kwargs = {
        "min_size": _env_int("DB_POOL_MIN_SIZE", 5, min_value=1, max_value=50),
        "max_size": _env_int("DB_POOL_MAX_SIZE", 20, min_value=1, max_value=100),
        # ⚠️ THIS NAME LIES AND THE NAME IS LOAD-BEARING IN AN INCIDENT.
        # `asyncpg.create_pool` has NO `timeout` parameter of its own; it
        # forwards this into `connect()`, so it bounds CONNECTION
        # ESTABLISHMENT, not waiting for a free pool slot. Checking it out is
        # bounded by DB_POOL_CHECKOUT_TIMEOUT_SECONDS below. Renaming this one
        # would silently change the connect budget, so it keeps its name and
        # gets this comment instead.
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
        if is_deployed():
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

# ---------------------------------------------------------------------------
# Bound the wait for a free pool slot.
#
# `databases` 0.7.0 checks a connection out with a bare
# `await self._database._pool.acquire()` (backends/postgres.py,
# `PostgresConnection.acquire`), and `asyncpg.Pool.acquire` defaults to
# `timeout=None` — WAIT FOREVER. So a saturated pool does not degrade, it
# stops: callers queue with no deadline and no error.
#
# That is not theoretical. 2026-08-20: one report query averaging 125s over
# 1,374 calls filled the 20 slots, and every scheduler job then hung silently
# until it burned its own deadline — 705 `maximum number of running instances`,
# 66 `JobDeadlineExceeded`, and ZERO database errors, because nothing ever
# failed; it just never returned. HTTP starved alongside them and the sitemap
# cron took a 504. The slow query is fixed (#1779), but the NEXT slow query
# does the same thing, which is why this is worth patching a pinned library for.
#
# asyncpg's own `acquire(timeout=)` is used rather than wrapping the call in
# `asyncio.wait_for`: cancelling mid-acquire is exactly how a connection gets
# left half-checked-out ("Connection is already acquired"), and the driver
# handles its own timeout without that hazard.
#
# The two asserts are preserved verbatim. "DatabaseBackend is not running" in
# particular is the signal `utils/database_readiness._pool_is_provably_dead`
# reads to decide a pool is genuinely dead, so losing it would break recovery.
DB_POOL_CHECKOUT_TIMEOUT_SECONDS = _env_float(
    # 120s, not the 4s an HTTP request can afford, and that asymmetry is the point.
    #
    # Callers already bound THEMSELVES where they need to: the canonical route
    # gives a query 4s (`CANONICAL_PRODUCTS_DB_TIMEOUT_SECONDS`) and the edge
    # gives up sooner still. So this deadline is not there to make HTTP fail
    # fast — HTTP fails fast on its own — it exists solely to stop an INFINITE
    # wait. Scheduler jobs are the constraint in the other direction: their run
    # deadlines are 600-14400s (`services/audit_scheduler._JOB_RUN_DEADLINES`),
    # and a nightly sweep that would legitimately queue 90s for a slot and then
    # run fine must not be converted into a failure. A tight global bound would
    # invent a new failure class for batch work that previously merely ran late.
    "DB_POOL_CHECKOUT_TIMEOUT_SECONDS", 120.0, min_value=0.5, max_value=600.0
)


def _install_bounded_pool_checkout() -> bool:
    """Give `PostgresConnection.acquire` a deadline. Returns True if installed."""
    from databases.backends.postgres import PostgresConnection

    if getattr(PostgresConnection.acquire, "_pivota_bounded", False):
        return True

    # Refuse to patch an implementation we have not read. Overwriting blindly
    # would silently reinstate 0.7.0 semantics over a newer `databases` — and
    # the per-task connection map in 0.8+ is a stated future direction here, so
    # that is a live risk, not a hypothetical one.
    import inspect

    original = inspect.getsource(PostgresConnection.acquire)
    for expected in (
        "Connection is already acquired",
        "DatabaseBackend is not running",
        "self._database._pool.acquire()",
    ):
        if expected not in original:
            raise RuntimeError(
                "db.database: refusing to bound pool checkout — "
                f"databases.PostgresConnection.acquire no longer contains {expected!r}. "
                "The library changed; re-read it and update this patch."
            )

    async def acquire(self) -> None:  # type: ignore[no-untyped-def]
        # Explicit raises, not bare asserts: `python -O` strips asserts, and
        # "DatabaseBackend is not running" is the signal
        # `utils/database_readiness._pool_is_provably_dead` reads to tell a dead
        # pool from a slow one. Same reasoning as that module's own guard.
        if self._connection is not None:
            raise AssertionError("Connection is already acquired")
        if self._database._pool is None:
            raise AssertionError("DatabaseBackend is not running")
        try:
            self._connection = await self._database._pool.acquire(
                timeout=DB_POOL_CHECKOUT_TIMEOUT_SECONDS
            )
        except (asyncio.TimeoutError, TimeoutError) as exc:
            pool = self._database._pool
            # Attribution, which the bare TimeoutError cannot carry: an empty
            # message in a 5xx tells an operator nothing about WHY.
            logger.warning(
                "database pool checkout timed out after %.1fs "
                "(pool size=%s, free=%s) — the pool is saturated, not broken",
                DB_POOL_CHECKOUT_TIMEOUT_SECONDS,
                getattr(pool, "_maxsize", "?"),
                getattr(getattr(pool, "_queue", None), "qsize", lambda: "?")(),
            )
            raise PoolCheckoutTimeout(
                "timed out waiting %.1fs for a database connection"
                % DB_POOL_CHECKOUT_TIMEOUT_SECONDS
            ) from exc

    acquire._pivota_bounded = True  # type: ignore[attr-defined]
    PostgresConnection.acquire = acquire  # type: ignore[assignment]
    return True


if IS_POSTGRES:
    # Not best-effort. If this cannot install, every query is one slow statement
    # away from an unbounded hang, and the 2026-08-20 evidence is that such a
    # hang is silent — so failing at import is strictly better than discovering
    # it during the next incident.
    if not _install_bounded_pool_checkout():
        raise RuntimeError("db.database: bounded pool checkout failed to install")


database = Database(DATABASE_URL, **database_kwargs)
# THE SECOND POOL IS GONE (2026-08-20). `get_db_pool()` lazily built its own
# `asyncpg.create_pool(DATABASE_URL)` for "routes that still expect an asyncpg
# pool". By the end it had exactly ONE caller, and it carried two hazards the
# primary pool no longer has:
#   * asyncpg's create_pool defaults are min_size=max_size=10, so it opened TEN
#     connections eagerly, entirely outside the DB_POOL_MAX_SIZE budget — the
#     capacity everything else is sized against was quietly wrong;
#   * its `pool.acquire()` took no deadline, i.e. the unbounded wait #1781
#     removed from the primary pool was still live here.
# Bounding it would have kept both a second pool and a second thing to remember.
# Its one caller, POST /admin/cleanup/phase5-data, was deleted rather than
# ported: measured against production, its FIRST statement was
# `DELETE FROM agent_routing_history`, and that table does not exist there — nor
# does `dual_sided_revenue`, which it also counted. `revenue_matching_logs` has
# no `revenue_id` column and `agent_integration_logs` has no `event_data`
# column, so three of its four DELETEs and two of its three COUNTs could not
# run either. The endpoint could only ever have returned its blanket 500. No
# repo referenced it. So there is now one pool, one budget, one deadline. Do
# not reintroduce a private pool: add what you need to `database_kwargs`
# instead.

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
