from __future__ import annotations

import datetime
import asyncio
import logging
import os
from contextlib import asynccontextmanager
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
from sqlalchemy.dialects.postgresql import JSONB as _PG_JSONB

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

# Dialect-aware JSONB type: `jsonb` on Postgres, `JSON` on everything else.
#
# ⚠️ RESOLVED AT COMPILE TIME, BY DIALECT — deliberately NOT at import time from
# DATABASE_URL, which is what this used to do (`JSONB_TYPE = JSONB if
# IS_POSTGRES else JSON`). That older form was correct in each of the two pure
# configurations and WRONG in the mixed one: with a Postgres DATABASE_URL the
# `else` branch never ran, so the sqlite compiler shims below were never
# registered, and any test that builds its own SQLite fixture died with
#     CompileError: SQLiteTypeCompiler can't render element of type JSONB
# That is the trap documented at length in
# .github/workflows/postgres-dialect-gate.yml ("AND ITS MIRROR"), which is why
# adding a SQLite-fixture suite to that job's ride-along list required checking
# it under a Postgres URL first. `with_variant` has no such failure mode: the
# dialect doing the compiling picks the type, so one declaration is correct
# under both engines no matter what DATABASE_URL says.
JSONB_TYPE = JSON().with_variant(_PG_JSONB, "postgresql")

# The sqlite shims are now registered UNCONDITIONALLY, for the same reason.
# They only ever affect the *sqlite* compiler, so registering them under a
# Postgres URL is a no-op there — while NOT registering them is the bug above.
# They remain necessary because ~133 columns across the repo still declare a
# bare `postgresql.JSONB` (and some a Postgres UUID or ARRAY) rather than going
# through JSONB_TYPE; those have no variant of their own to fall back on.
try:
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
    # Optional SERVER-side per-statement ceiling (`statement_timeout`), sent
    # once per connection via asyncpg `server_settings` — zero per-query round
    # trips. This bounds how long any single statement can HOLD a pool slot,
    # which `command_timeout` cannot do: that one is a client-side await bound,
    # so with it alone a pathological plan still occupies a server backend and
    # a pool slot for the full 600s. 2026-08-21: ~6 agent searches/min whose
    # statements ran to the server's cancel point held all 20 slots for hours —
    # health 503, schedulers starved, portal key requests dead (the 2026-08-20
    # report-query wedge above was the same shape). Keep this BELOW
    # DB_COMMAND_TIMEOUT_SECONDS where both are set, so the server cancel wins
    # and the connection stays reusable; a client-side cancel poisons the
    # connection ("another operation is in progress").
    #
    # Unset (the default) keeps current behavior — flipping prod is ONE env
    # change, revertible without a deploy. The LEDGERED startup migrations are
    # not affected: db/sql_migrations.py runs on the synchronous SQLAlchemy
    # engine (CONCURRENTLY files via AUTOCOMMIT), not this pool. Route-time and
    # startup DDL that DOES go through this pool (the IF NOT EXISTS ensure_*
    # sites, schema_guard) is covered by the ceiling — deliberate: in steady
    # state those are no-ops, and one queued >30s behind a deploy-time lock is
    # exactly a statement camping on a slot. In-pool work that legitimately
    # runs longer (nightly sweeps, admin backfills) wraps the long statement in
    # `unbounded_statement_timeout()` below — and note ops scripts importing
    # this module under a prod env inherit the ceiling too.
    _statement_timeout = _env_float(
        "DB_STATEMENT_TIMEOUT_SECONDS", 0.0, min_value=0.0, max_value=600.0
    )
    if _statement_timeout > 0:
        # Floor 1s: sub-second values are misconfigurations with silent, nasty
        # shapes — 0.0005 rounds to statement_timeout='0', which Postgres reads
        # as NO ceiling while the operator believes one is on, and anything
        # under ~1s starts cancelling healthy fast-path queries.
        database_kwargs["server_settings"] = {
            "statement_timeout": str(int(max(_statement_timeout, 1.0) * 1000)),
        }

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
            _log_holders_on_starvation()
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
        _track_checkout(self)

    acquire._pivota_bounded = True  # type: ignore[attr-defined]
    PostgresConnection.acquire = acquire  # type: ignore[assignment]
    _install_release_tracking(PostgresConnection)
    return True


# HOLDER TRACKING — which code took the connections that never came back.
#
# 2026-09-16: web's pool emptied again (4 instances x 13 connections, all plain
# `idle` for up to 12h, Cloud SQL CPU 0.1-0.35), the fourth time this wedge has
# cost a restart with no cause. /__pool_health's `tasks_by_frame` cannot name a
# LEAK: a connection whose task already finished has no task to park. Only the
# acquisition site can still be recovered, and only if it was recorded then.
# See db/pool_holders.py. On by default because the evidence exists only in the
# minutes before a restart; `DB_POOL_HOLDER_TRACKING=0` switches it off.
DB_POOL_HOLDER_TRACKING = (os.getenv("DB_POOL_HOLDER_TRACKING") or "1").strip().lower() not in {
    "0", "false", "no", "off"
}
DB_POOL_HOLDER_WARN_SECONDS = _env_float(
    # No legitimate request holds a connection for 5 minutes: Cloud Run cuts a
    # request at 300s and DB_COMMAND_TIMEOUT_SECONDS bounds a statement at 600s
    # only for batch work, which would log once here and be recognisable by site.
    "DB_POOL_HOLDER_WARN_SECONDS", 300.0, min_value=5.0, max_value=86400.0
)
# The checkout-timeout warning fires once per starved caller — ~800 per 10 min
# on 2026-09-16 — so the holder dump rides along at most this often.
_HOLDER_DUMP_INTERVAL_SECONDS = 60.0
_last_holder_dump = [0.0]


def _track_checkout(conn) -> None:  # type: ignore[no-untyped-def]
    """Record a successful checkout. Never lets instrumentation fail a query."""
    if not DB_POOL_HOLDER_TRACKING:
        return
    try:
        from db.pool_holders import REGISTRY, acquisition_site

        REGISTRY.record(conn, acquisition_site(start_depth=2))
        REGISTRY.warn_overdue(DB_POOL_HOLDER_WARN_SECONDS)
    except Exception:  # noqa: BLE001
        logger.debug("pool holder tracking failed on checkout", exc_info=True)


def _log_holders_on_starvation() -> None:
    if not DB_POOL_HOLDER_TRACKING:
        return
    import time as _time

    now = _time.monotonic()
    if now - _last_holder_dump[0] < _HOLDER_DUMP_INTERVAL_SECONDS:
        return
    _last_holder_dump[0] = now
    try:
        from db.pool_holders import REGISTRY

        REGISTRY.warn_overdue(DB_POOL_HOLDER_WARN_SECONDS)
        logger.warning("database pool starved; live checkouts by acquisition site: %s",
                       REGISTRY.snapshot())
    except Exception:  # noqa: BLE001
        logger.debug("pool holder dump failed", exc_info=True)


def _install_release_tracking(PostgresConnection) -> None:  # type: ignore[no-untyped-def]
    """Forget a checkout when `databases` hands the connection back."""
    if getattr(PostgresConnection.release, "_pivota_tracked", False):
        return
    original_release = PostgresConnection.release

    async def release(self) -> None:  # type: ignore[no-untyped-def]
        try:
            await original_release(self)
        finally:
            # In `finally`: a release that raised has still left `databases`
            # believing the connection is gone, and a record that outlived it
            # would be reported as a leak forever.
            if DB_POOL_HOLDER_TRACKING:
                try:
                    from db.pool_holders import REGISTRY

                    REGISTRY.forget(self)
                except Exception:  # noqa: BLE001
                    pass

    release._pivota_tracked = True  # type: ignore[attr-defined]
    PostgresConnection.release = release  # type: ignore[assignment]


if IS_POSTGRES:
    # Not best-effort. If this cannot install, every query is one slow statement
    # away from an unbounded hang, and the 2026-08-20 evidence is that such a
    # hang is silent — so failing at import is strictly better than discovering
    # it during the next incident.
    if not _install_bounded_pool_checkout():
        raise RuntimeError("db.database: bounded pool checkout failed to install")


# ---------------------------------------------------------------------------
# A cancelled request must still hand its connection back.
#
# `databases` 0.7.0 gives a pool connection back only at the END of a sequence
# of awaits, with no try/finally around them:
#   * `Connection.__aexit__` decrements the checkout counter and releases the
#     raw connection only INSIDE `async with self._connection_lock`.
#   * `Transaction.start` checks the connection out, then sends BEGIN; if BEGIN
#     raises, nothing checks it back in.
#   * `Transaction.commit` / `rollback` wait for `_transaction_lock`, send
#     COMMIT/ROLLBACK, and only then exit the connection.
# A cancellation (or error) delivered at any of those awaits skips the release.
# Nothing owns the checkout any more, so the pool slot is gone for the life of
# the process.
#
# One asyncio `task.cancel()` landing while `__aexit__` waits for a contended
# lock is enough. Two things in this app make it common:
#   * Concurrent tasks in one request share ONE `Connection` (0.7.0 keeps it in
#     a ContextVar that child tasks inherit), so the locks are contended — e.g.
#     the `asyncio.gather` of three reads in reviews_service.
#   * Starlette's BaseHTTPMiddleware runs the endpoint in an anyio task group,
#     and anyio cancellation is level-triggered: it is re-delivered at EVERY
#     await until the task leaves the scope, so it reaches the cleanup awaits
#     too, not just the query that was running.
#
# 2026-09-16: this drained web's 12-slot pools on every instance within hours
# (api.pivota.cc /health 503, uptime alert flapping). pg_stat_activity showed
# the fingerprint: connections `idle` in ClientRead for up to 12.7h whose last
# statement was an app query with no asyncpg release reset after it, CPU 0.1,
# no locks. The leaked holder's history ended in `__aexit__` raising
# CancelledError from the lock acquire with the counter still at 1. Review of
# the first fix found the Transaction path leaking the same way (12 slots in 17
# anyio-cancelled transactions), as `idle in transaction (aborted)`.
#
# The fix runs each cleanup step to completion in its own task
# (`_run_to_completion`) and re-raises the cancellation afterwards. Cancellation
# is delayed, never dropped. The delay is the cleanup itself: lock waits behind
# siblings, one COMMIT/ROLLBACK, and one release. asyncpg bounds a release by
# the checkout timeout (and terminates on expiry). DB_STATEMENT_TIMEOUT_SECONDS
# is enforced by the SERVER, so it does NOT bound a COMMIT/ROLLBACK waiting on a
# socket that has gone silent — only a client-side DB_COMMAND_TIMEOUT_SECONDS
# would, and even that not while asyncpg waits for a cancel to be acknowledged.
async def _run_to_completion(make_coro):  # type: ignore[no-untyped-def]
    """Await `make_coro()` to completion even if this task is cancelled meanwhile.

    If a cancellation arrived, it is re-raised afterwards — chained to the
    cleanup's own error when there was one, so neither is lost. Without one,
    the cleanup's result or exception is returned/raised as usual.
    """
    import anyio

    # Local strong reference: the loop only holds tasks weakly.
    task = asyncio.ensure_future(make_coro())
    interrupted: Optional[BaseException] = None
    # The anyio shield stops anyio re-delivering its cancellation on every loop
    # pass (a busy spin under BaseHTTPMiddleware); anyio re-applies it at the
    # caller's next await. Plain asyncio cancellation is not intercepted by the
    # anyio scope, so it is caught here. `asyncio.wait`, not `asyncio.shield`:
    # wait neither cancels the task when the waiter is cancelled nor raises the
    # task's exception, so a cleanup that fails AFTER a cancellation arrived
    # cannot escape the loop ahead of that cancellation.
    with anyio.CancelScope(shield=True):
        while not task.done():
            try:
                await asyncio.wait((task,))
            except asyncio.CancelledError as exc:
                interrupted = exc
    if interrupted is not None:
        if not task.cancelled() and task.exception() is not None:
            raise interrupted from task.exception()
        raise interrupted
    return task.result()


def _require_source(obj, name: str, expected: tuple) -> None:  # type: ignore[no-untyped-def]
    # Same rule as the checkout patch: refuse to wrap an implementation we have
    # not read. 0.8+ restructured connection handling; a blind wrap could hide
    # a different lifecycle.
    import inspect

    source = inspect.getsource(obj)
    for fragment in expected:
        if fragment not in source:
            raise RuntimeError(
                f"db.database: refusing to patch {name} — it no longer contains "
                f"{fragment!r}. The library changed; re-read it and update this patch."
            )


class TransactionEndedOutOfOrder(RuntimeError):
    """A `databases` transaction ended while it was not the innermost one open.

    Replaces 0.7.0's bare `assert` (stripped under `python -O`, and raised
    before the connection checkout was returned).
    """


def _install_cancellation_safe_connection_exit() -> bool:
    """Make `databases` return connections despite cancellation. Returns True if installed."""
    from databases.core import Connection, Transaction

    if getattr(Connection.__aexit__, "_pivota_cancel_safe", False):
        return True

    _require_source(
        Connection.__aexit__,
        "databases.core.Connection.__aexit__",
        (
            "async with self._connection_lock",
            "self._connection_counter -= 1",
            "await self._connection.release()",
        ),
    )
    _require_source(
        Transaction.start,
        "databases.core.Transaction.start",
        (
            "async with self._connection._transaction_lock",
            "await self._connection.__aenter__()",
            "await self._transaction.start(",
            "self._connection._transaction_stack.append(self)",
        ),
    )
    for method in (Transaction.commit, Transaction.rollback):
        _require_source(
            method,
            f"databases.core.Transaction.{method.__name__}",
            (
                "async with self._connection._transaction_lock",
                "self._connection._transaction_stack.pop()",
                f"await self._transaction.{method.__name__}()",
                "await self._connection.__aexit__()",
            ),
        )

    original_exit = Connection.__aexit__

    async def __aexit__(self, exc_type=None, exc_value=None, traceback=None):  # type: ignore[no-untyped-def]
        await _run_to_completion(lambda: original_exit(self, exc_type, exc_value, traceback))

    async def start(self):  # type: ignore[no-untyped-def]
        # 0.7.0 body, plus: a failed or cancelled BEGIN checks the connection
        # back in. Checking in is NOT a rollback: while a sibling still holds
        # the shared Connection the counter stays above zero, nothing is
        # released, and no asyncpg reset runs. Undoing what BEGIN left on the
        # server is the backend's job — see `_install_failed_begin_cleanup`.
        self._connection = self._connection_callable()
        self._transaction = self._connection._connection.transaction()

        async with self._connection._transaction_lock:
            is_root = not self._connection._transaction_stack
            await self._connection.__aenter__()
            try:
                await self._transaction.start(
                    is_root=is_root, extra_options=self._extra_options
                )
            except BaseException:
                await self._connection.__aexit__()
                raise
            self._connection._transaction_stack.append(self)
        return self

    async def _end(self, action: str) -> None:  # type: ignore[no-untyped-def]
        # 0.7.0 body, plus: once this transaction is known to hold a checkout
        # (it is on the stack), the connection exits whatever happens — even if
        # COMMIT/ROLLBACK raises, and even if it is not the innermost one.
        #
        # 0.7.0 asserted `stack[-1] is self` BEFORE anything that exits the
        # connection. Sibling tasks sharing one Connection can end their
        # transactions out of order, and that assertion then leaked a pool
        # slot for good. An out-of-order end is still an error, raised after
        # the release, and it ROLLS BACK rather than doing what was asked:
        #   * COMMIT of a lower level commits (root) or folds in (savepoint)
        #     the still-open levels above it. An error must mean "not
        #     committed", or a caller that retries writes twice.
        #   * Doing nothing leaves the BEGIN/SAVEPOINT open under the siblings.
        #     Theirs then "commit" into it and vanish at the release reset —
        #     silently, which is the worst outcome.
        #   * ROLLBACK discards the levels above too, so each sibling's own end
        #     then fails loudly on the server (no such transaction/savepoint).
        #     Not correct either: a sibling's statements between this ROLLBACK
        #     and its own end run with no enclosing transaction (autocommit
        #     after a root rollback). Concurrent transactions on one shared
        #     Connection cannot be made correct here — only loud and leak-free.
        # A transaction that is not on the stack at all holds no checkout
        # (already ended, or its BEGIN failed): raise, and exit nothing.
        async with self._connection._transaction_lock:
            stack = self._connection._transaction_stack
            depth = next((i for i, t in enumerate(stack) if t is self), None)
            if depth is None:
                raise TransactionEndedOutOfOrder(
                    f"cannot {action}: this transaction is not open on its connection "
                    "(already committed/rolled back, or never started)"
                )
            above = len(stack) - 1 - depth
            del stack[depth]
            if not above:
                try:
                    await getattr(self._transaction, action)()
                finally:
                    await self._connection.__aexit__()
                return
            message = (
                f"cannot {action}: {above} transaction(s) started after this one on "
                "the same connection are still open — concurrent tasks are sharing "
                "one databases Connection. Rolled this transaction back instead "
                "(which also discards the open ones above it) and released its "
                "connection checkout."
            )
            try:
                try:
                    await self._transaction.rollback()
                finally:
                    await self._connection.__aexit__()
            except Exception as exc:
                raise TransactionEndedOutOfOrder(message) from exc
            raise TransactionEndedOutOfOrder(message)

    async def commit(self) -> None:  # type: ignore[no-untyped-def]
        await _run_to_completion(lambda: _end(self, "commit"))

    async def rollback(self) -> None:  # type: ignore[no-untyped-def]
        await _run_to_completion(lambda: _end(self, "rollback"))

    Transaction.start = start  # type: ignore[assignment]
    Transaction.commit = commit  # type: ignore[assignment]
    Transaction.rollback = rollback  # type: ignore[assignment]
    __aexit__._pivota_cancel_safe = True  # type: ignore[attr-defined]
    Connection.__aexit__ = __aexit__  # type: ignore[assignment]
    return True


# Unconditional, unlike the checkout bound: the defect is in `databases` core,
# not in the asyncpg backend, so it applies to every engine.
if not _install_cancellation_safe_connection_exit():
    raise RuntimeError("db.database: cancellation-safe connection exit failed to install")


# ---------------------------------------------------------------------------
# A failed root BEGIN must not leave a transaction nobody owns.
#
# asyncpg's `Transaction.start` records itself as the connection's `_top_xact`
# BEFORE it sends BEGIN, and nothing clears that if BEGIN raises or is
# cancelled. Cancelled after the server ran BEGIN, the session is left inside a
# server-side transaction that no code will ever commit.
#
# When the connection is released right away, asyncpg's reset rolls that back
# and nothing is lost. But sibling tasks in one request share one `Connection`,
# and while one of them still holds it, nothing is released. The request's
# next `transaction()` then sees `_top_xact` set, becomes a SAVEPOINT inside
# the orphan, and reports a successful commit. The release reset rolls all of
# it back later, so the rows never persist and no error is ever raised.
# Reproduced on Postgres 15 with 0.7.0 + asyncpg 0.31 and the cancellation-safe
# exit above installed: 0 rows, no error. Without that exit the same run also
# leaks the slot.
#
# The fix, in the backend where asyncpg's state lives: when a ROOT BEGIN
# fails, clear `_top_xact` and send ROLLBACK, run to completion despite
# cancellation. ROLLBACK outside a transaction is only a server WARNING, so it
# is safe whether or not BEGIN reached the server. `_top_xact` is checked
# first, so a transaction someone started by hand (asyncpg raises before
# claiming `_top_xact`) is never rolled back. A failed SAVEPOINT is left
# alone: an empty savepoint changes nothing, and the enclosing transaction
# still has an owner who ends it.
#
# The ROLLBACK has a DEADLINE, and the connection is terminated when it runs
# out. It runs uncancellably (`_run_to_completion`), and asyncpg sends nothing
# until the server acknowledges the cancel of the BEGIN — a wait no
# command_timeout covers. On a socket that has gone silent (failover, dropped
# NAT entry) an unbounded ROLLBACK therefore never returns and holds the slot
# forever, where the plain release it runs before would have given up and
# terminated. The deadline is the one that release uses: asyncpg bounds a
# release by the acquire timeout, which is DB_POOL_CHECKOUT_TIMEOUT_SECONDS.
async def _abandon_failed_begin(xact) -> None:  # type: ignore[no-untyped-def]
    con = xact._connection
    if xact._nested or con._top_xact is not xact:
        return
    con._top_xact = None
    if con.is_closed():
        return  # nothing to roll back; the pool replaces a closed connection
    try:
        await asyncio.wait_for(con.execute("ROLLBACK;"), DB_POOL_CHECKOUT_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        logger.warning(
            "no answer to ROLLBACK after a failed BEGIN within %.1fs — terminating the connection",
            DB_POOL_CHECKOUT_TIMEOUT_SECONDS,
        )
        con.terminate()
    except Exception:
        # Still safe: with `_top_xact` cleared, a server left in the orphan
        # makes asyncpg REFUSE the next `transaction()` ("manually started
        # transaction"), so the next write fails loudly instead of vanishing.
        logger.warning("could not roll back after a failed BEGIN", exc_info=True)


def _install_failed_begin_cleanup() -> bool:
    """Make a failed root BEGIN undo itself on the connection. Returns True if installed."""
    import asyncpg.transaction
    from databases.backends.postgres import PostgresTransaction

    if getattr(PostgresTransaction.start, "_pivota_failed_begin_cleanup", False):
        return True

    _require_source(
        PostgresTransaction.start,
        "databases.backends.postgres.PostgresTransaction.start",
        (
            "self._connection._connection.transaction(**extra_options)",
            "await self._transaction.start()",
        ),
    )
    _require_source(
        asyncpg.transaction.Transaction.start,
        "asyncpg.transaction.Transaction.start",
        (
            "con._top_xact = self",
            "self._nested = True",
            "self._state = TransactionState.FAILED",
        ),
    )

    async def start(self, is_root, extra_options):  # type: ignore[no-untyped-def]
        # 0.7.0 body, plus the failed-BEGIN cleanup. An explicit raise, not
        # an assert, for the same `python -O` reason as the checkout patch.
        if self._connection._connection is None:
            raise AssertionError("Connection is not acquired")
        self._transaction = self._connection._connection.transaction(**extra_options)
        try:
            await self._transaction.start()
        except BaseException:
            xact = self._transaction
            await _run_to_completion(lambda: _abandon_failed_begin(xact))
            raise

    start._pivota_failed_begin_cleanup = True  # type: ignore[attr-defined]
    PostgresTransaction.start = start  # type: ignore[assignment]
    return True


# Unconditional, like the exit above: it changes nothing until a Postgres BEGIN
# fails, and `Database` objects are not tied to the module's DATABASE_URL.
# asyncpg is a hard requirement (requirements.txt).
if not _install_failed_begin_cleanup():
    raise RuntimeError("db.database: failed-BEGIN cleanup failed to install")


database = Database(DATABASE_URL, **database_kwargs)


@asynccontextmanager
async def unbounded_statement_timeout(db: Optional[Database] = None):
    """Escape hatch from DB_STATEMENT_TIMEOUT_SECONDS for ONE long statement.

    Opens a transaction and issues `SET LOCAL statement_timeout = 0`, so every
    statement executed inside the `async with` block (same task = same pooled
    connection under `databases` 0.7.0) runs without the server-side ceiling.
    SET LOCAL rather than a plain SET on principle: asyncpg's pool DOES reset
    session state on release (verified — a session-level SET here still came
    back to the ceiling on the next checkout), but that net exists only at
    release time; SET LOCAL also covers the same connection being reused
    within one task before release, and does not depend on pool internals
    staying that way.

    Two scope caveats, both load-bearing:

    * CALL THIS OUTSIDE ANY TRANSACTION. Under `databases` 0.7.0 a nested
      `transaction()` is an asyncpg SAVEPOINT, and `SET LOCAL` inside a
      savepoint survives its RELEASE — the lifted ceiling would persist to
      the end of the CALLER's outer transaction, not this block.
    * "Unbounded" lifts only the SERVER ceiling. Where the pool also runs
      with DB_COMMAND_TIMEOUT_SECONDS (prod: 600), work past that bound gets
      a CLIENT-side cancel mid-transaction — a poisoned connection and a
      rollback attempt on it. This hatch does not extend that budget.

    Scope it to the individual long-running statement, not a whole job tick:
    the block holds one pool slot for its full duration, and it makes the
    wrapped work a single transaction (one failure rolls back all of it).

    No-op when the TARGET database is not Postgres (judged from its URL, not
    from the module-level default), which has no statement_timeout.
    """
    target = db if db is not None else database
    target_url = str(getattr(target, "url", "")).lower()
    if not (
        target_url.startswith("postgresql://") or target_url.startswith("postgres://")
    ):
        yield
        return
    async with target.transaction():
        await target.execute("SET LOCAL statement_timeout = 0")
        yield


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
