"""
Startup DDL coordination.

Why this exists
---------------
Every boot runs a pile of best-effort DDL (``metadata.create_all``, the
``ensure_*_table()`` helpers, the ``db/migrations/*.sql`` runner). On a DB
where every relation already exists (Railway prod/staging) all of it is a
no-op. On an EMPTY database (first Cloud Run revision against a fresh Cloud
SQL instance, 2026-08-19) two or more sessions can execute
``CREATE TABLE IF NOT EXISTS x`` for the same ``x`` at the same time:
``IF NOT EXISTS`` is NOT atomic across sessions, so the loser dies with
``UniqueViolationError: duplicate key value violates unique constraint
"pg_type_typname_nsp_index"`` (or ``pg_class_relname_nsp_index``). Sources of
concurrency: Cloud Run starting >1 instance at once, and background workers
spawned during the lifespan that call lazy ``ensure_*`` helpers.

Two cooperating mechanisms, both cheap on a warm DB:

1. :func:`startup_ddl_lock` — a Postgres SESSION-scoped advisory lock
   (polled ``pg_try_advisory_lock``) on a fixed key, held for the whole
   startup DDL phase, so concurrent *processes/instances* serialize their DDL.

   It is taken by POLLING ``pg_try_advisory_lock`` rather than blocking in
   ``pg_advisory_lock``: a blocking acquire holds a statement (and its
   transaction snapshot) open on the waiter, and the holder's
   ``CREATE INDEX CONCURRENTLY`` waits for exactly that — two boots then wait
   on each other until one times out (measured: >10 minutes).

   It is held on a DEDICATED raw asyncpg connection, opened and closed by this
   module, for three reasons: it never takes a slot from (or shares a
   ``databases==0.7.0`` per-context Connection with) the app pool; closing the
   connection releases the lock even if release() is never reached; and — the
   reason it is session- and not transaction-scoped — an open transaction in
   the same database makes ``CREATE INDEX CONCURRENTLY`` (migrations 051/059)
   wait forever, which wedged boot for 10+ minutes when this held
   ``pg_advisory_xact_lock``. A session lock holds no transaction open.

   Acquisition is bounded by ``STARTUP_DDL_LOCK_TIMEOUT_SECONDS`` (default
   600s — a cold migration run takes ~125s, and a waiter that gives up too
   early runs the same DDL unlocked, which is what this module is for); on
   timeout or any lock error boot continues UNLOCKED (degraded, logged) —
   never blocks the service forever.

2. :func:`is_already_exists_error` / :func:`execute_ddl` — classify the
   catalog-level "someone else created it first" errors so an ``ensure_*``
   helper treats them as success. This covers intra-process racers that are
   not under the lock.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Any, AsyncIterator, Dict, Mapping, Optional

from db.database import database, database_kwargs

logger = logging.getLogger(__name__)

# Fixed bigint key; any 64-bit constant works as long as every instance of
# this service uses the same one. (0x50495654 = "PIVT".)
STARTUP_DDL_LOCK_KEY = 0x5049565400000001

# SQLSTATEs that mean "the object you tried to create already exists".
_ALREADY_EXISTS_SQLSTATES = frozenset(
    {
        "42P07",  # duplicate_table
        "42710",  # duplicate_object (type, index, constraint, ...)
        "42701",  # duplicate_column
        "42P06",  # duplicate_schema
        "42723",  # duplicate_function
    }
)
_UNIQUE_VIOLATION_SQLSTATE = "23505"
# The pg_catalog unique indexes a concurrent CREATE loses on.
_CATALOG_UNIQUE_INDEXES = (
    "pg_type_typname_nsp_index",
    "pg_class_relname_nsp_index",
    "pg_constraint_conrelid_contypid_conname_index",
    "pg_namespace_nspname_index",
    "pg_proc_proname_args_nsp_index",
)


def _unwrap(exc: BaseException) -> BaseException:
    # SQLAlchemy DBAPIError wraps the driver exception in .orig
    orig = getattr(exc, "orig", None)
    if isinstance(orig, BaseException):
        return orig
    return exc


def _sqlstate(exc: BaseException) -> Optional[str]:
    for attr in ("sqlstate", "pgcode"):
        value = getattr(exc, attr, None)
        if value:
            return str(value)
    diag = getattr(exc, "diag", None)
    if diag is not None:
        value = getattr(diag, "sqlstate", None)
        if value:
            return str(value)
    return None


def _constraint_name(exc: BaseException) -> str:
    for attr in ("constraint_name",):
        value = getattr(exc, attr, None)
        if value:
            return str(value)
    diag = getattr(exc, "diag", None)
    if diag is not None:
        value = getattr(diag, "constraint_name", None)
        if value:
            return str(value)
    return ""


def is_already_exists_error(exc: BaseException) -> bool:
    """
    True when ``exc`` means "the relation/type/index/column already exists",
    i.e. a concurrent session won the CREATE race or the object pre-existed.

    Accepts asyncpg, psycopg2 and SQLAlchemy-wrapped exceptions. A plain
    ``UniqueViolation`` (23505) only qualifies when it is on a pg_catalog
    unique index — a unique violation on a USER table is a real error.
    """
    inner = _unwrap(exc)
    state = _sqlstate(inner)
    if state in _ALREADY_EXISTS_SQLSTATES:
        return True
    if state == _UNIQUE_VIOLATION_SQLSTATE:
        name = _constraint_name(inner)
        if name in _CATALOG_UNIQUE_INDEXES:
            return True
        msg = str(inner)
        return any(idx in msg for idx in _CATALOG_UNIQUE_INDEXES)
    if state is not None:
        return False
    # No SQLSTATE available (e.g. sqlite in hermetic tests): fall back to text.
    msg = str(inner).lower()
    return "already exists" in msg


async def execute_ddl(
    statement: str,
    values: Optional[Mapping[str, Any]] = None,
    *,
    db: Any = None,
) -> bool:
    """
    Run one idempotent DDL statement. Returns True if it executed, False if
    it was skipped because the object already exists (someone else created
    it first). Any other error propagates unchanged.

    ``db`` lets a caller pass its own module-level ``database`` reference
    (tests monkeypatch those per module); defaults to the shared one.
    """
    target = db if db is not None else database
    try:
        await target.execute(statement, values)
        return True
    except Exception as exc:  # noqa: BLE001 - classified below
        if is_already_exists_error(exc):
            logger.debug(
                "startup_ddl: object already exists, treating as success: %s | %s",
                str(exc)[:120],
                " ".join(statement.split())[:80],
            )
            return False
        raise


# A cold first boot applies every db/migrations/*.sql file; measured at ~125s
# for 219 files against a local Postgres 15, and a second instance must be able
# to wait that out. At 120s the waiter gave up, ran the same migrations
# unlocked and deadlocked against the peer — the exact failure this module
# exists to prevent. Keep this comfortably above a full cold migration run.
DEFAULT_LOCK_TIMEOUT_SECONDS = 600.0

# Retry pacing for pg_try_advisory_lock while a peer holds it: fast at first
# so a short hold costs almost nothing, backing off so a long cold-migration
# hold does not spin.
_LOCK_POLL_MIN_SECONDS = 0.05
_LOCK_POLL_MAX_SECONDS = 1.0


def _lock_timeout_seconds() -> float:
    raw = os.getenv("STARTUP_DDL_LOCK_TIMEOUT_SECONDS")
    if raw is None or not str(raw).strip():
        return DEFAULT_LOCK_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_LOCK_TIMEOUT_SECONDS
    return max(0.0, value)


def _asyncpg_dsn() -> Optional[str]:
    """DSN for a raw asyncpg connection, derived from the app's URL.
    Returns None when the configured database is not Postgres."""
    url = getattr(database, "url", None)
    if url is None:
        return None
    raw = str(url)
    scheme = str(getattr(url, "scheme", "") or "")
    if "postgres" not in scheme.lower():
        return None
    # asyncpg does not accept SQLAlchemy's "+driver" suffix.
    return raw.replace("postgresql+asyncpg://", "postgresql://").replace(
        "postgresql+psycopg2://", "postgresql://"
    )


def _connect_kwargs() -> Dict[str, Any]:
    """The subset of db.database's asyncpg settings that apply to a single
    connection. Without the `ssl` context, a deployment using
    DB_SSL_NO_VERIFY (public proxy, self-signed cert) cannot open this
    connection at all and every boot pays the connect timeout before
    continuing UNLOCKED; without `command_timeout` a wedged statement on the
    holder would only be bounded by this module's own asyncio timeouts."""
    out: Dict[str, Any] = {}
    for key in ("ssl", "command_timeout"):
        value = database_kwargs.get(key)
        if value is not None:
            out[key] = value
    return out


class StartupDdlLock:
    """
    Serializes the startup DDL phase across processes/instances.

    Holds a session-scoped advisory lock on ``key`` on its own raw asyncpg
    connection until :meth:`release`, which unlocks and closes the connection.
    ``release()`` is idempotent and safe to call from a ``finally``. Both
    methods are best-effort: they never raise, because a lock problem must
    degrade boot, not break it.
    """

    def __init__(self, *, key: int = STARTUP_DDL_LOCK_KEY, timeout_seconds: Optional[float] = None) -> None:
        self._key = int(key)
        self._timeout_seconds = (
            _lock_timeout_seconds() if timeout_seconds is None else max(0.0, float(timeout_seconds))
        )
        self._conn: Any = None
        self.held: bool = False

    async def _poll_for_lock(self, conn: Any) -> bool:
        """Poll ``pg_try_advisory_lock`` instead of blocking in
        ``pg_advisory_lock``.

        A *blocking* acquire keeps a statement — and therefore a transaction
        snapshot — open on the waiter for as long as it waits, and
        ``CREATE INDEX CONCURRENTLY`` on the holder waits for every such
        transaction to end. Two boots then wait on each other until one times
        out: measured as a >10-minute wedge with a blocking acquire, ~seconds
        with this poll. Each try is a single instant statement.
        """
        deadline = asyncio.get_running_loop().time() + self._timeout_seconds
        waited_logged = False
        interval = _LOCK_POLL_MIN_SECONDS
        while True:
            got = await asyncio.wait_for(
                conn.fetchval("SELECT pg_try_advisory_lock($1)", self._key), timeout=30.0
            )
            if got:
                return True
            if asyncio.get_running_loop().time() >= deadline:
                return False
            if not waited_logged:
                logger.info(
                    "startup_ddl: another instance holds the startup DDL lock; "
                    "waiting up to %.0fs",
                    self._timeout_seconds,
                )
                waited_logged = True
            await asyncio.sleep(interval)
            interval = min(_LOCK_POLL_MAX_SECONDS, interval * 2)

    async def acquire(self) -> bool:
        """True when the lock is held; False when boot should continue
        UNLOCKED (not connected, non-Postgres, timeout, or lock error)."""
        if self._conn is not None:
            return self.held
        if not getattr(database, "is_connected", False):
            return False
        dsn = _asyncpg_dsn()
        if not dsn:
            return False

        conn = None
        try:
            import asyncpg  # imported lazily: sqlite/dev installs may not have it

            connect_timeout = max(5.0, min(30.0, self._timeout_seconds))
            conn = await asyncio.wait_for(
                asyncpg.connect(dsn, **_connect_kwargs()), timeout=connect_timeout
            )
            if not await self._poll_for_lock(conn):
                logger.warning(
                    "startup_ddl: advisory lock not acquired within %.0fs "
                    "(continuing UNLOCKED, concurrent first-boot DDL may race)",
                    self._timeout_seconds,
                )
                try:
                    await conn.close()
                except Exception:  # noqa: BLE001
                    pass
                return False
        except Exception as exc:  # noqa: BLE001 - boot must not die on lock errors
            logger.warning(
                "startup_ddl: could not acquire advisory lock (continuing UNLOCKED, "
                "concurrent first-boot DDL may race): %s",
                exc,
            )
            if conn is not None:
                try:
                    await conn.close()
                except Exception:  # noqa: BLE001
                    pass
            return False

        self._conn = conn
        self.held = True
        logger.info("startup_ddl: advisory lock acquired (key=%d)", self._key)
        return True

    async def release(self) -> None:
        """Idempotent. Unlocks and closes the holder connection (closing alone
        would also drop a session-scoped lock).

        Raises only ``CancelledError``, and only after the session is already
        gone: a SIGTERM during boot cancels this coroutine mid-await, and
        ``CancelledError`` is a BaseException, so an ``except Exception`` here
        would skip the close and strand the lock on a connection this object
        no longer references. ``terminate()`` is synchronous, so it works on
        that path.
        """
        conn = self._conn
        if conn is None:
            return
        self._conn = None
        was_held = self.held
        self.held = False
        try:
            try:
                await asyncio.wait_for(
                    conn.execute("SELECT pg_advisory_unlock($1)", self._key), timeout=10.0
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "startup_ddl: advisory unlock failed (closing connection): %s", exc
                )
            await asyncio.wait_for(conn.close(), timeout=10.0)
        except BaseException as exc:  # noqa: BLE001
            try:
                conn.terminate()
            except Exception:  # noqa: BLE001
                pass
            if isinstance(exc, asyncio.CancelledError):
                raise
        if was_held:
            logger.info("startup_ddl: advisory lock released")


@contextlib.asynccontextmanager
async def startup_ddl_lock(
    *, key: int = STARTUP_DDL_LOCK_KEY, timeout_seconds: Optional[float] = None
) -> AsyncIterator[bool]:
    """
    Serialize the startup DDL phase across processes/instances.

    Yields True when the advisory lock is held, False when boot continues
    unlocked (DB not connected, non-Postgres URL, timeout, or lock error).

    The lock is SESSION-scoped (see the module docstring for why it must not
    be transaction-scoped) on a connection this module owns, and `release()`
    closes that connection, so the lock cannot outlive this context: even a
    cancelled release terminates the session, and a hard kill drops it with
    the socket.
    """
    lock = StartupDdlLock(key=key, timeout_seconds=timeout_seconds)
    acquired = await lock.acquire()
    try:
        yield acquired
    finally:
        await lock.release()
