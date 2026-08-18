from __future__ import annotations

import asyncio

import pytest

from utils import database_readiness as readiness


class FakeDatabase:
    def __init__(
        self,
        *,
        connected: bool,
        execute_results=None,
        connect_raises: Exception | None = None,
        disconnect_raises: Exception | None = None,
    ):
        self.is_connected = connected
        self.execute_results = list(execute_results or [])
        self.connect_raises = connect_raises
        self.disconnect_raises = disconnect_raises
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.execute_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1
        if self.connect_raises is not None:
            raise self.connect_raises
        self.is_connected = True

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        if self.disconnect_raises is not None:
            raise self.disconnect_raises
        self.is_connected = False

    async def execute(self, _query):
        self.execute_calls += 1
        if self.execute_results:
            result = self.execute_results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return 1


@pytest.mark.asyncio
async def test_ensure_database_ready_connects_when_startup_left_db_disconnected(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_db = FakeDatabase(connected=False, execute_results=[1])
    monkeypatch.setattr(readiness, "database", fake_db)

    await readiness.ensure_database_ready()

    assert fake_db.connect_calls == 1
    assert fake_db.execute_calls == 1
    assert fake_db.is_connected is True


@pytest.mark.asyncio
async def test_a_probe_timeout_on_a_LIVE_pool_fails_fast_and_rebuilds_nothing() -> None:
    """The 2026-08-18 wedge, as a unit test.

    This used to assert the opposite — a bare probe timeout triggered a
    disconnect + reconnect. That is the defect: a timeout cannot distinguish a
    dead pool from a slow or merely SATURATED one (a saturated pool's probe
    queues behind `acquire` and times out identically), and rebuilding ABANDONS
    the pool, whose server connections then stay open until Postgres reaps
    them. Measured against real Postgres driving this function: 5 concurrent
    callers against a saturated max_size=2 pool stranded 9 backends permanently.

    So a timeout on a live pool must fail the request and touch nothing.
    """
    import utils.database_readiness as readiness_mod

    fake_db = FakeDatabase(connected=True, execute_results=[TimeoutError("stale"), 1])
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(readiness_mod, "database", fake_db)
        readiness_mod._reset_recovery_single_flight()
        with pytest.raises(readiness.DatabaseUnavailableError):
            await readiness.ensure_database_ready()

    assert fake_db.disconnect_calls == 0, "tore down a pool that was merely slow"
    assert fake_db.connect_calls == 0, "rebuilt a pool that was merely slow"
    assert fake_db.execute_calls == 1, "probed again instead of failing fast"
    assert fake_db.is_connected is True


@pytest.mark.asyncio
async def test_concurrent_callers_share_one_recovery_and_its_failure() -> None:
    """Single-flight: a burst must produce ONE recovery attempt, not N.

    `ensure_database_ready` runs on login/quote/order — the bursty paths.
    Without coalescing each caller runs the full teardown/rebuild, and each
    abandonment strands a pool's worth of server connections. Followers must
    adopt the leader's FAILURE too; a follower that retried on its own would
    rebuild the herd this exists to remove.
    """
    import utils.database_readiness as readiness_mod

    fake_db = FakeDatabase(connected=False, connect_raises=TimeoutError("db down"))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(readiness_mod, "database", fake_db)
        readiness_mod._reset_recovery_single_flight()
        results = await asyncio.gather(
            *[readiness.ensure_database_ready() for _ in range(5)],
            return_exceptions=True,
        )

    assert all(isinstance(r, readiness.DatabaseUnavailableError) for r in results), (
        "every caller must see the failure, not just the leader"
    )
    assert fake_db.connect_calls == 1, (
        f"expected ONE coalesced connect for 5 concurrent callers, got "
        f"{fake_db.connect_calls} — the herd is back"
    )


@pytest.mark.asyncio
async def test_ensure_database_ready_forces_reconnect_when_disconnect_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_db = FakeDatabase(
        connected=True,
        execute_results=[RuntimeError("pool is closed"), 1],
        disconnect_raises=RuntimeError("pool is closed"),
    )
    monkeypatch.setattr(readiness, "database", fake_db)

    await readiness.ensure_database_ready()

    assert fake_db.disconnect_calls == 1
    assert fake_db.connect_calls == 1
    assert fake_db.execute_calls == 2
    assert fake_db.is_connected is True


@pytest.mark.asyncio
async def test_ensure_database_ready_raises_when_connect_cannot_be_restored(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_db = FakeDatabase(connected=False, connect_raises=TimeoutError("db down"))
    monkeypatch.setattr(readiness, "database", fake_db)

    with pytest.raises(readiness.DatabaseUnavailableError) as exc_info:
        await readiness.ensure_database_ready()

    assert exc_info.value.phase == "connect"
    assert exc_info.value.error_type == "TimeoutError"


# ---------------------------------------------------------------------------
# `connect_database_with_timeout` builds the pool itself so it can terminate
# what a cancelled or failed fill already opened. That means it constructs the
# create_pool kwargs that `databases.PostgresBackend.connect()` would have
# constructed — and the two must not be allowed to drift apart silently.
#
# Whether the CLEANUP works is a question about real server backends and lives
# in `test_database_readiness_connect_timeout_leak_postgres.py`. What lives
# here is the part that needs no server: the arguments.


class _FakePool:
    """Stands in for `asyncpg.Pool`: awaitable, and awaiting it yields itself."""

    def __init__(self) -> None:
        self.terminated = False

    def __await__(self):
        async def _ready() -> "_FakePool":
            return self

        return _ready().__await__()

    def terminate(self) -> None:
        self.terminated = True


def _recording_create_pool(recorded: list):
    def create_pool(**kwargs):
        recorded.append(kwargs)
        return _FakePool()

    return create_pool


def _postgres_backend_for(url: str, **options):
    """A real `PostgresBackend`, never connected — building kwargs needs no server."""
    postgres = pytest.importorskip(
        "databases.backends.postgres", reason="needs asyncpg installed"
    )
    return postgres.PostgresBackend(url, **options)


# URL shapes that a single happy-path URL would not have caught. Production
# connects over a Railway proxy with `?sslmode=require` (note that `databases`
# forwards `ssl`, NOT `sslmode`, and drops the latter — the point here is that
# both paths drop it identically, which is what "no behaviour change" means).
# `db/database.py` supplies min_size/max_size/timeout as constructor options,
# so the prod-shaped case carries them too.
_PROD_OPTIONS = {"min_size": 5, "max_size": 20, "timeout": 5.0}
_URL_SHAPES = [
    ("plain", "postgresql://someone:secret@db.example.internal:6543/pivota_test", _PROD_OPTIONS),
    ("railway proxy", "postgresql://u:pw@x.proxy.rlwy.net:6543/railway?sslmode=require", _PROD_OPTIONS),
    ("ssl option", "postgresql://u:p@h:5432/db?ssl=true", {}),
    ("sizes in the query string", "postgresql://u:p@h:5432/db?min_size=2&max_size=9", {}),
    ("no port", "postgresql://u:p@h/db", _PROD_OPTIONS),
    ("url-encoded password", "postgresql://u:p%40ss%3Aword@h:5432/db", {}),
    ("no credentials", "postgresql://h:5432/db", {}),
    ("ipv6 host", "postgresql://u:p@[::1]:5432/db", {}),
    ("unix socket", "postgresql://u:p@/db?host=/var/run/postgresql", {}),
]


@pytest.mark.parametrize("shape,url,options", _URL_SHAPES, ids=[s[0] for s in _URL_SHAPES])
@pytest.mark.asyncio
async def test_connect_kwargs_match_the_library_backend(
    shape: str, url: str, options: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive the REAL `PostgresBackend.connect()` and ours through one stub.

    Not a lifted string and not a re-implemented oracle: the expected value is
    produced by running the library function this code stands in for. A
    `databases` upgrade that changes which kwargs reach `create_pool` fails
    here instead of silently diverging inside `_new_pool`.
    """
    import asyncpg

    recorded: list = []
    monkeypatch.setattr(asyncpg, "create_pool", _recording_create_pool(recorded))

    library_backend = _postgres_backend_for(url, **options)
    await library_backend.connect()

    ours = readiness._new_pool(_postgres_backend_for(url, **options))

    assert len(recorded) == 2, "both paths must go through asyncpg.create_pool"
    from_library, from_ours = recorded
    assert from_ours == from_library, f"{shape}: kwargs diverged from the library"
    # Guard the guard: comparing two empty dicts would pass vacuously.
    assert from_library["database"], f"{shape}: nothing meaningful was compared"
    assert isinstance(ours, _FakePool)


@pytest.mark.asyncio
async def test_connect_kwargs_carry_the_real_production_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The parametrized comparison above proves EQUALITY; this proves CONTENT.

    Two paths that both silently dropped the password and the pool sizes would
    satisfy an equality check and strand every connection attempt in
    production.
    """
    import asyncpg

    recorded: list = []
    monkeypatch.setattr(asyncpg, "create_pool", _recording_create_pool(recorded))

    readiness._new_pool(_postgres_backend_for(
        "postgresql://someone:secret@db.example.internal:6543/pivota_test",
        min_size=3, max_size=11, timeout=7.5,
    ))

    assert recorded == [{
        "host": "db.example.internal",
        "port": 6543,
        "user": "someone",
        "password": "secret",
        "database": "pivota_test",
        "min_size": 3,
        "max_size": 11,
        "timeout": 7.5,
    }]


@pytest.mark.asyncio
async def test_connect_with_timeout_falls_back_when_the_backend_is_not_postgres() -> None:
    """SQLite (local dev and most of this suite) keeps the original behaviour.

    There is no asyncpg pool to own and no server backends to strand, so the
    function must simply do what it always did rather than reaching for
    internals that are not there.
    """
    fake_db = FakeDatabase(connected=False)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(readiness, "database", fake_db)
        await readiness.connect_database_with_timeout(5)

    assert fake_db.connect_calls == 1
    assert fake_db.is_connected is True


@pytest.mark.asyncio
async def test_connect_with_timeout_is_a_no_op_when_already_connected() -> None:
    """`databases.connect()` returns early when connected; so must this.

    Building a second pool for an already-connected database would orphan the
    first one — the very leak this function exists to prevent.
    """
    fake_db = FakeDatabase(connected=True)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(readiness, "database", fake_db)
        await readiness.connect_database_with_timeout(5)

    assert fake_db.connect_calls == 0


@pytest.mark.asyncio
async def test_connect_with_timeout_refuses_to_publish_over_a_live_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard `PostgresBackend.connect()` opens with, kept.

    This function assigns `backend._pool` by hand. `is_connected` False with a
    pool still attached should not happen — `disconnect()` and
    `_force_mark_database_disconnected` both clear the two together — but if it
    ever did, assigning over the live pool would orphan it and strand its
    server connections: the exact leak this module exists to prevent, entered
    from the other end. Refuse instead, exactly as the library does.
    """
    import asyncpg

    backend = _postgres_backend_for("postgresql://someone:secret@db.example.internal:6543/pivota")
    backend._pool = object()  # a pool that is already running

    class _Wrapper:
        is_connected = False
        _backend = backend

    recorded: list = []
    monkeypatch.setattr(asyncpg, "create_pool", _recording_create_pool(recorded))

    with pytest.raises(AssertionError, match="already running"):
        await readiness.connect_database_with_timeout(5, db=_Wrapper())

    assert recorded == [], "no second pool may be built for a backend that has one"



# ---------------------------------------------------------------------------
# The `_pool_is_provably_dead()` arm of the teardown gate.
#
# Added because a mutant that deleted it — `_pool_is_provably_dead() or ...`
# -> `False or ...` — survived all 53 tests in this directory. Every
# FakeDatabase here lacks `_backend`, so the predicate returned False in every
# existing test and the arm was load-bearing but unconstrained.
#
# It is the ONLY thing that recovers `_pool is None` while the wrapper still
# says `is_connected` — the shape that raises `AssertionError: DatabaseBackend
# is not running`, which this workstream identifies as the 12.5h-outage
# fingerprint. `is_asyncpg_pool_gone_error` does NOT match it (the message has
# no "pool is closed"/"pool is closing" substring), so without this arm the
# request path would refuse to repair the very incident it was written for.
# ---------------------------------------------------------------------------


class _DeadPoolBackend:
    """A backend whose pool object is gone — `_pool_is_provably_dead()` True."""

    def __init__(self) -> None:
        self._pool = None


class _FakeDatabaseWithDeadPool:
    """`is_connected` True while the pool underneath is gone.

    Reproduces the real signature: `databases` leaves the wrapper marked
    connected, so the probe raises AssertionError rather than any asyncpg
    'pool is closed' error.
    """

    def __init__(self) -> None:
        self.is_connected = True
        self._backend = _DeadPoolBackend()
        self.connect_calls = 0
        self.disconnect_calls = 0

    async def execute(self, *_a, **_kw):
        # Exactly the real signature: `databases` asserts the backend is
        # running, and it only stops once a fresh pool exists.
        if self._backend._pool is None:
            raise AssertionError("DatabaseBackend is not running")
        return 1

    async def connect(self):
        self.connect_calls += 1
        self.is_connected = True
        self._backend._pool = object()  # a fresh pool: no longer provably dead

    async def disconnect(self):
        self.disconnect_calls += 1
        self.is_connected = False


@pytest.mark.asyncio
async def test_provably_dead_pool_is_repaired_even_though_no_asyncpg_error_matches(
    monkeypatch,
) -> None:
    """`_pool is None` must still be repaired by the request path."""
    fake = _FakeDatabaseWithDeadPool()
    monkeypatch.setattr(readiness, "database", fake)
    readiness._reset_recovery_single_flight()

    # Precondition: the predicate fires, and the error-string classifier does NOT —
    # so this arm is the only thing that can authorise the rebuild.
    assert readiness._pool_is_provably_dead() is True
    assert readiness.is_asyncpg_pool_gone_error(
        AssertionError("DatabaseBackend is not running")
    ) is False

    await readiness.ensure_database_ready()

    assert fake.connect_calls == 1, "a provably dead pool was not rebuilt"


@pytest.mark.asyncio
async def test_a_live_pool_that_merely_times_out_is_never_torn_down(monkeypatch) -> None:
    """The saturation case: alive pool, slow probe — must NOT rebuild.

    This is the 2026-08-18 wedge in one test. Paired with the test above so the
    gate is driven BOTH ways: rebuild on local fact, refuse on inference.
    """

    class _LivePoolBackend:
        def __init__(self) -> None:
            self._pool = object()  # alive, not closed

    class _SaturatedDatabase:
        def __init__(self) -> None:
            self.is_connected = True
            self._backend = _LivePoolBackend()
            self.connect_calls = 0
            self.disconnect_calls = 0

        async def execute(self, *_a, **_kw):
            raise asyncio.TimeoutError()

        async def connect(self):
            self.connect_calls += 1

        async def disconnect(self):
            self.disconnect_calls += 1

    fake = _SaturatedDatabase()
    monkeypatch.setattr(readiness, "database", fake)
    readiness._reset_recovery_single_flight()

    assert readiness._pool_is_provably_dead() is False

    with pytest.raises(readiness.DatabaseUnavailableError):
        await readiness.ensure_database_ready()

    assert fake.disconnect_calls == 0, "tore down a pool that was merely saturated"
    assert fake.connect_calls == 0, "rebuilt a pool that was merely saturated"


@pytest.mark.asyncio
async def test_a_cancelled_leader_does_not_cancel_its_followers(monkeypatch) -> None:
    """A cancelled leader must hand followers a 503, not its own cancellation.

    Single-flight COUPLES callers that used to be independent, so the leader's
    task state must not leak into them. Storing `CancelledError` on the shared
    future makes `asyncio.shield` re-raise it inside follower tasks that were
    never cancelled: they unwind with no response body and no `Retry-After`,
    instead of the `DatabaseUnavailableError` -> 503 contract that
    routes/auth.py, routes/quote_routes.py and routes/order_routes.py are all
    written against. One cancelled leader (a deploy landing mid-outage, a
    TaskGroup sibling failing) would otherwise take N healthy requests with it.
    """
    started = asyncio.Event()

    class _HangingDatabase:
        def __init__(self) -> None:
            self.is_connected = False

        async def connect(self):
            started.set()
            await asyncio.Event().wait()  # never completes; the leader is cancelled

        async def execute(self, *_a, **_kw):
            return 1

        async def disconnect(self):
            self.is_connected = False

    monkeypatch.setattr(readiness, "database", _HangingDatabase())
    readiness._reset_recovery_single_flight()

    leader = asyncio.create_task(readiness.ensure_database_ready())
    await asyncio.wait_for(started.wait(), timeout=5)

    followers = [asyncio.create_task(readiness.ensure_database_ready()) for _ in range(3)]
    await asyncio.sleep(0.05)  # let them queue behind the leader

    leader.cancel()
    results = await asyncio.wait_for(
        asyncio.gather(*followers, return_exceptions=True), timeout=5
    )

    assert all(
        isinstance(r, readiness.DatabaseUnavailableError) for r in results
    ), f"followers must get the domain error, got {[type(r).__name__ for r in results]}"
