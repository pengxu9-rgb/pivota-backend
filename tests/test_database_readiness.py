from __future__ import annotations

import asyncio
import time
import weakref

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
async def test_ensure_database_ready_reconnects_after_probe_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_db = FakeDatabase(connected=True, execute_results=[TimeoutError("stale"), 1])
    monkeypatch.setattr(readiness, "database", fake_db)

    await readiness.ensure_database_ready()

    assert fake_db.disconnect_calls == 1
    assert fake_db.connect_calls == 1
    assert fake_db.execute_calls == 2
    assert fake_db.is_connected is True


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


class SlowConnectFakeDatabase(FakeDatabase):
    """A fake whose `connect()` yields, so concurrent callers can overlap.

    The base fake's `connect()` never awaits, so N callers can never be inside
    it at once and the re-entrancy defect is invisible. Yielding once is enough
    to let every queued caller reach the same point the real
    `asyncio.wait_for(database.connect(), ...)` does.
    """

    async def connect(self) -> None:
        await asyncio.sleep(0)
        await super().connect()


@pytest.mark.asyncio
async def test_concurrent_callers_produce_exactly_one_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    """N callers hitting a disconnected database must build ONE pool.

    `databases.Database.connect()` reads `is_connected`, awaits the backend,
    and only then writes the flag, so without serialization every concurrent
    caller passes the guard and builds its own pool. All but the last are
    orphaned and can never be closed. The connection-count consequence is
    measured on real Postgres in
    `test_database_readiness_concurrent_pool_leak_postgres.py`; this pins the
    call count without needing a server.
    """
    fake_db = SlowConnectFakeDatabase(connected=False)
    monkeypatch.setattr(readiness, "database", fake_db)

    await asyncio.gather(*(readiness.ensure_database_ready() for _ in range(8)))

    assert fake_db.connect_calls == 1


@pytest.mark.asyncio
async def test_a_failing_repair_is_shared_not_repeated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Followers must adopt the leader's FAILURE, not queue up to retry it.

    This is the property a plain lock does not give you. If each queued caller
    ran its own repair, N callers against a wedged database would cost N
    serialized repairs — measured at 6s -> 123s for 20 callers against a real
    hung Postgres, and unbounded under sustained arrivals, on `/health` and the
    order path. Everyone must pay for ONE repair.
    """
    fake_db = SlowConnectFakeDatabase(
        connected=False, connect_raises=TimeoutError("db down")
    )
    monkeypatch.setattr(readiness, "database", fake_db)

    results = await asyncio.gather(
        *(readiness.ensure_database_ready() for _ in range(8)),
        return_exceptions=True,
    )

    # Every caller is told the truth...
    assert len(results) == 8
    for result in results:
        assert isinstance(result, readiness.DatabaseUnavailableError)
        assert result.phase == "connect"
        assert result.error_type == "TimeoutError"
    # ...at the cost of a single repair attempt.
    assert fake_db.connect_calls == 1


@pytest.mark.asyncio
async def test_a_failing_repair_costs_one_repair_of_wall_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same property as above, measured as LATENCY rather than call count.

    A lock gets the call count wrong only in the sense that it serializes; what
    it actually breaks is time. `connect_calls == 1` would still hold if a
    future refactor made followers wait for the leader and then re-probe on
    their own, so pin the wall clock too.
    """

    class HangingProbeFake(FakeDatabase):
        async def execute(self, _query):
            await asyncio.sleep(3600)

    fake_db = HangingProbeFake(connected=True)
    monkeypatch.setattr(readiness, "database", fake_db)

    # One repair = probe + disconnect + connect + reconnect_probe.
    budget = 0.2 + 0.1 + 0.2 + 0.2
    started = time.monotonic()
    await asyncio.gather(
        *(
            readiness.ensure_database_ready(
                connect_timeout_seconds=0.2,
                probe_timeout_seconds=0.2,
                disconnect_timeout_seconds=0.1,
            )
            for _ in range(8)
        ),
        return_exceptions=True,
    )
    elapsed = time.monotonic() - started

    # Serialized, these 8 callers would cost ~8x this. The generous ceiling is
    # deliberate: the signal is 8x, so it does not need a tight bound to be
    # unambiguous, and a tight one would flake under a loaded CI box.
    assert elapsed < budget * 2, (
        f"8 concurrent callers took {elapsed:.2f}s; one repair is ~{budget:.2f}s, "
        "so the repair is being repeated per caller rather than shared"
    )


@pytest.mark.asyncio
async def test_a_cancelled_leader_does_not_strand_its_followers(monkeypatch: pytest.MonkeyPatch) -> None:
    """A client disconnecting mid-repair must not hang everyone behind it.

    The leader is an ordinary request task and can be cancelled at any await.
    If it fails to publish an outcome on the way out, every follower waits on a
    Future that will never resolve — a worse outage than the one being
    repaired.
    """
    started = asyncio.Event()

    class HangingConnectFake(FakeDatabase):
        async def connect(self) -> None:
            started.set()
            await asyncio.sleep(3600)

    fake_db = HangingConnectFake(connected=False)
    monkeypatch.setattr(readiness, "database", fake_db)

    leader = asyncio.create_task(readiness.ensure_database_ready())
    await started.wait()
    follower = asyncio.create_task(readiness.ensure_database_ready())
    await asyncio.sleep(0)  # let the follower attach to the leader's future

    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader

    # The follower resolves promptly instead of hanging on a dead future.
    with pytest.raises(readiness.DatabaseUnavailableError) as exc_info:
        await asyncio.wait_for(follower, timeout=2.0)
    assert exc_info.value.error_type == "CancelledError"


@pytest.mark.asyncio
async def test_a_follower_gives_up_on_a_leader_that_never_publishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A follower's wait must be bounded like every other await in this module.

    The leader is an ordinary request task. `_publish_repair_outcome` runs in a
    `finally` so it should always fire — but "should" is what unbounded waits
    are always justified by, and the failure mode is a permanently wedged
    process rather than a slow one. An unpublished repair must degrade to the
    503 the call sites already handle.

    Modelled by parking a pending future in the slot with no leader behind it,
    which is precisely the state a vanished leader leaves.
    """
    loop = asyncio.get_running_loop()
    orphaned = loop.create_future()
    readiness._repair_loop = weakref.ref(loop)
    readiness._repair_inflight = orphaned

    fake_db = FakeDatabase(connected=True, execute_results=[TimeoutError("blip")])
    monkeypatch.setattr(readiness, "database", fake_db)

    try:
        with pytest.raises(readiness.DatabaseUnavailableError) as exc_info:
            # The outer wait_for turns a regression into a failure rather than
            # a hung test run.
            await asyncio.wait_for(
                readiness.ensure_database_ready(
                    connect_timeout_seconds=0.1,
                    probe_timeout_seconds=0.1,
                    disconnect_timeout_seconds=0.1,
                ),
                timeout=5.0,
            )
        assert exc_info.value.phase == "repair_wait"
        # The shared future must survive: cancelling it would take down every
        # other follower and the leader's own publish.
        assert not orphaned.cancelled()
    finally:
        readiness._publish_repair_outcome(orphaned, None)


def test_a_pending_repair_from_a_dead_loop_does_not_hang_the_next_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Future left pending on a loop that is no longer running must be dropped.

    Awaiting it from a different loop does not raise — it simply never
    resolves. A caller that adopted such a future would block for its whole
    follower budget on a repair that can never publish, every time, forever.
    The slot is therefore reset whenever the running loop changes.

    Unlike the sibling test below, this cannot be caught by simply running two
    waves: a completed repair clears the slot on its way out, so only an
    ABANDONED one leaves the stale future behind.
    """

    async def strand_a_repair() -> None:
        loop = asyncio.get_running_loop()
        readiness._repair_loop = weakref.ref(loop)
        readiness._repair_inflight = loop.create_future()  # never resolved

    asyncio.run(strand_a_repair())
    assert readiness._repair_inflight is not None
    assert not readiness._repair_inflight.done()

    async def next_loop() -> None:
        fake_db = SlowConnectFakeDatabase(connected=False)
        monkeypatch.setattr(readiness, "database", fake_db)
        await asyncio.wait_for(readiness.ensure_database_ready(), timeout=2.0)
        assert fake_db.connect_calls == 1

    asyncio.run(next_loop())


def test_the_repair_slot_is_not_bound_to_the_first_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The single-flight slot must be per-event-loop.

    Both `asyncio.Lock` and `asyncio.Future` are loop-bound. A module-level
    primitive — this repo's idiom for its ~25 `_DDL_LOCK`s — binds to the first
    event loop that CONTENDS it and then raises
    `RuntimeError: ... is bound to a different event loop` on every loop after
    (CPython 3.11); awaiting a stale Future from the wrong loop hangs instead.
    Uncontended acquires never bind, which is why the DDL locks get away with
    it — this slot is contended by design.

    It matters well beyond the test suite: every call site of
    `ensure_database_ready` (`/health`, POST /auth/login, /quotes/preview,
    /orders/create, /orders/payment/confirm) catches `DatabaseUnavailableError`
    and nothing else, so a stray RuntimeError here is a 500 on the order path
    where there should have been a 503.
    """

    async def one_contended_repair_wave() -> None:
        fake_db = SlowConnectFakeDatabase(connected=False)
        monkeypatch.setattr(readiness, "database", fake_db)
        await asyncio.gather(*(readiness.ensure_database_ready() for _ in range(4)))
        assert fake_db.connect_calls == 1

    asyncio.run(one_contended_repair_wave())
    # A brand-new event loop, exactly as the next test in the suite gets — and
    # exactly as a process that runs `asyncio.run` more than once would.
    asyncio.run(one_contended_repair_wave())

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

