"""A slow statement must not camp on a pool slot — the SERVER cuts it off.

WHAT THIS PREVENTS (2026-08-21). The pool-checkout bound (#1781) made a
saturated pool FAIL instead of hang — but nothing bounded how long a statement
could keep a slot SATURATED. `DB_COMMAND_TIMEOUT_SECONDS` is a client-side
await bound: when it fires, the server backend is still executing and the slot
is still gone. So ~6 agent searches/min whose statements ran to the server's
cancel point held all 20 slots continuously: /health 503, every scheduler job
starving on `PoolCheckoutTimeout`, and the agent-portal key endpoints timing
out at the client. Same shape as the 2026-08-20 report-query wedge.

`DB_STATEMENT_TIMEOUT_SECONDS` closes that hole with a SERVER-side
`statement_timeout`, sent once per connection via asyncpg `server_settings`
(zero per-query round-trips). The server kills the statement, frees the slot,
and — unlike a client-side cancel — leaves the connection clean and reusable.

POSTGRES GATE because statement_timeout is a Postgres server setting; SQLite
has neither the setting nor a pool, so a SQLite run would assert nothing.

🚨 THESE GATE FILES SHARE ONE DATABASE. This one creates no tables and writes
no rows — it only opens its own `Database` objects against the same URL.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL — statement_timeout is a server setting",
)

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Short enough that each test stays fast, long enough that connection setup
# jitter cannot fire it spuriously before pg_sleep even starts.
CEILING_MS = 300
# Sleeps comfortably on either side of the ceiling.
SLEEP_OVER_CEILING_S = CEILING_MS / 1000 * 4
SLEEP_UNDER_CEILING_S = CEILING_MS / 1000 / 6


def _import_probe(extra_env: dict, probe: str) -> subprocess.CompletedProcess:
    """Import db.database in a FRESH interpreter and run `probe` against it.

    A subprocess is required for anything about import-time configuration:
    this interpreter has already imported the module, so an in-process check
    is satisfied by whatever env the FIRST importer happened to have — the
    exact blind spot the bounded-checkout tests documented.
    """
    env = {**os.environ, "DATABASE_URL": DATABASE_URL, **extra_env}
    env.pop("DB_STATEMENT_TIMEOUT_SECONDS", None)
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        timeout=120,
    )


def test_default_is_off_so_the_merge_changes_no_behavior() -> None:
    """Unset env must configure NO server_settings — flipping prod is an env
    decision made deliberately, never a side effect of deploying this patch."""
    proc = _import_probe(
        {},
        "import db.database as m; import sys;"
        "sys.exit(3 if 'server_settings' in m.database_kwargs else 0)",
    )
    assert proc.returncode == 0, proc.stderr.decode()[-400:]


def test_env_knob_reaches_the_pool_as_milliseconds() -> None:
    """DB_STATEMENT_TIMEOUT_SECONDS=2.5 must become statement_timeout='2500'
    in the kwargs the pool is built from — the seam `databases` 0.7.0 passes
    verbatim into `asyncpg.create_pool`."""
    proc = _import_probe(
        {"DB_STATEMENT_TIMEOUT_SECONDS": "2.5"},
        "import db.database as m; import sys;"
        "v = (m.database_kwargs.get('server_settings') or {}).get('statement_timeout');"
        "sys.exit(0 if v == '2500' else 3)",
    )
    assert proc.returncode == 0, proc.stderr.decode()[-400:]


@pytest.mark.asyncio
async def test_server_cancels_the_statement_and_the_connection_stays_usable() -> None:
    """The whole point: the SERVER frees the slot, and — unlike a client-side
    cancel, which poisons the connection ('another operation is in progress')
    — the same connection then serves the next query normally."""
    from databases import Database

    db = Database(
        DATABASE_URL,
        min_size=1,
        max_size=1,
        server_settings={"statement_timeout": str(CEILING_MS)},
    )
    await db.connect()
    try:
        with pytest.raises(Exception, match="statement timeout"):
            await db.execute(f"SELECT pg_sleep({SLEEP_OVER_CEILING_S})")
        # max_size=1: this MUST be the same connection the cancel hit.
        assert await db.fetch_val("SELECT 41 + 1") == 42
    finally:
        await db.disconnect()


@pytest.mark.asyncio
async def test_escape_hatch_lifts_the_ceiling_only_inside_its_transaction() -> None:
    """`unbounded_statement_timeout()` must lift the ceiling for the wrapped
    statement and ONLY the wrapped statement.

    HONEST LIMIT of the after-the-block leg: it does NOT distinguish SET LOCAL
    from a plain session-level SET — measured here, the mutant PASSES this
    test, because asyncpg resets session state when a connection is released
    to the pool, so the ceiling comes back either way. The leg still earns its
    place (it proves the ceiling returns at all, whatever the mechanism, and
    would catch e.g. the helper reconfiguring the pool itself), but SET LOCAL
    is defended by the helper's docstring reasoning, not by a mutant kill."""
    from databases import Database

    from db.database import unbounded_statement_timeout

    db = Database(
        DATABASE_URL,
        min_size=1,
        max_size=1,
        server_settings={"statement_timeout": str(CEILING_MS)},
    )
    await db.connect()
    try:
        with pytest.raises(Exception, match="statement timeout"):
            await db.execute(f"SELECT pg_sleep({SLEEP_OVER_CEILING_S})")

        async with unbounded_statement_timeout(db):
            await db.execute(f"SELECT pg_sleep({SLEEP_OVER_CEILING_S})")

        with pytest.raises(Exception, match="statement timeout"):
            await db.execute(f"SELECT pg_sleep({SLEEP_OVER_CEILING_S})")
        # And a fast statement still succeeds — the ceiling, not the
        # connection, is what the failures above were about.
        await db.execute(f"SELECT pg_sleep({SLEEP_UNDER_CEILING_S})")
    finally:
        await db.disconnect()


def test_a_statement_timeout_cancel_is_shed_load_not_a_code_bug() -> None:
    """Server cancels must reach clients as 503 + Retry-After via the existing
    busy vocabulary — a 500 pages as a code bug and tells agent/partner
    clients not to retry a state that is entirely retryable."""
    import asyncpg

    from utils.transient_errors import is_asyncpg_busy_error

    cancelled = asyncpg.exceptions.QueryCanceledError(
        "canceling statement due to statement timeout"
    )
    assert is_asyncpg_busy_error(cancelled)

    # Wrapped one level down, the way route code usually sees it.
    wrapper = RuntimeError("query failed")
    wrapper.__cause__ = cancelled
    assert is_asyncpg_busy_error(wrapper)


def test_a_user_requested_cancel_is_not_misread_as_capacity() -> None:
    """Same exception TYPE, different meaning: 'due to user request' is an
    explicit client-side cancel, not load-shedding. Matching on the full
    statement-timeout phrase (not just 'canceling statement') is what keeps
    these apart — this test kills the substring-shortening mutant."""
    import asyncpg

    from utils.transient_errors import is_asyncpg_busy_error

    user_cancel = asyncpg.exceptions.QueryCanceledError(
        "canceling statement due to user request"
    )
    assert not is_asyncpg_busy_error(user_cancel)
