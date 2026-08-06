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
async def test_a_cancelled_leader_hands_off_instead_of_failing_its_followers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client hanging up mid-repair is not a verdict about the database.

    Two things must hold. The follower must not HANG on a future the departed
    leader will never resolve — and it must not inherit `CancelledError` as a
    database fault either, which would turn one client disconnect into a 503
    for everyone behind it and surface `CancelledError` as `/health`'s
    `db_error`. Cancellation means "no outcome": the follower re-claims and
    repairs for itself.
    """
    started = asyncio.Event()

    class HangsOnceThenConnects(FakeDatabase):
        async def connect(self) -> None:
            self.connect_calls += 1
            if self.connect_calls == 1:
                started.set()
                await asyncio.sleep(3600)
            self.is_connected = True

    fake_db = HangsOnceThenConnects(connected=False)
    monkeypatch.setattr(readiness, "database", fake_db)

    leader = asyncio.create_task(readiness.ensure_database_ready())
    await started.wait()
    follower = asyncio.create_task(readiness.ensure_database_ready())
    await asyncio.sleep(0)  # let the follower attach to the leader's future

    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader

    # The follower takes over and succeeds, rather than hanging or 503ing.
    await asyncio.wait_for(follower, timeout=2.0)
    assert fake_db.connect_calls == 2, "the follower should have led its own repair"
    assert fake_db.is_connected is True


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
    # The state a vanished leader leaves: a pending future and the deadline it
    # published when it claimed the slot.
    readiness._repair_deadline = loop.time() + 0.5

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
        # Distinct from a real connect timeout, so `/health`'s db_error does
        # not conflate the two.
        assert exc_info.value.error_type == "RepairWaitTimeout"
        # The shared future must survive: cancelling it would take down every
        # other follower and the leader's own publish.
        assert not orphaned.cancelled()
    finally:
        readiness._publish_repair_outcome(orphaned, None)


@pytest.mark.asyncio
async def test_a_re_leading_follower_re_probes_before_destroying_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller that takes over from an abandoned leader must re-establish
    evidence before tearing anything down.

    Its own fast-path probe happened BEFORE the abandoned leader ran, so it
    says nothing about the pool that leader left behind. Acting on it means
    destroying a pool that was never probed: a wasted teardown and rebuild of
    a healthy pool, plus the latency of both, triggered by an unrelated client
    hanging up. The round-18 F1 rule in miniature — an ambiguous signal must
    not drive a destructive repair.
    """

    class HealsOnConnectThenHangs(FakeDatabase):
        def __init__(self) -> None:
            super().__init__(connected=True)
            self.healed = False
            self.hang_next_probe = False

        async def connect(self) -> None:
            self.connect_calls += 1
            self.is_connected = True
            self.healed = True
            # Leave the leader stalled in its reconnect_probe so it can be
            # cancelled with a HEALTHY pool already in place.
            self.hang_next_probe = True

        async def execute(self, _query):
            self.execute_calls += 1
            if self.hang_next_probe:
                self.hang_next_probe = False
                await asyncio.sleep(3600)
            if not self.healed:
                raise RuntimeError("pool is closed")
            return 1

    fake_db = HealsOnConnectThenHangs()
    monkeypatch.setattr(readiness, "database", fake_db)

    def call():
        # Small timeouts so a regression fails fast and visibly instead of
        # tripping this test's own guard rail.
        return readiness.ensure_database_ready(
            connect_timeout_seconds=0.2,
            probe_timeout_seconds=0.2,
            disconnect_timeout_seconds=0.1,
        )

    leader = asyncio.create_task(call())
    follower = asyncio.create_task(call())
    # Both fast-path probes fail on the broken pool; the leader then repairs
    # and stalls in its reconnect_probe.
    await asyncio.sleep(0.05)
    assert fake_db.connect_calls == 1, "leader should have rebuilt the pool"
    assert fake_db.disconnect_calls == 1

    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader

    # It must SUCCEED on the healthy pool, not repair it again.
    await asyncio.wait_for(follower, timeout=3.0)

    assert fake_db.disconnect_calls == 1, (
        "the re-leading follower tore down a pool it never probed — its probe "
        "predates that pool"
    )
    assert fake_db.connect_calls == 1


@pytest.mark.asyncio
async def test_a_follower_waits_on_the_leaders_deadline_not_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wait is on someone else's repair, so it must use their bound.

    The call sites pass very different timeouts — `/health` 5/3/1, POST
    /auth/login 2/2/1 — so a follower that applies its OWN budget to the
    leader's repair gives up on one that was going to succeed. Only visible
    when leader and follower disagree, which is why every other test here
    (same timeouts on both sides) cannot see it.
    """
    started = asyncio.Event()

    class SlowConnectFake(FakeDatabase):
        async def connect(self) -> None:
            self.connect_calls += 1
            started.set()
            await asyncio.sleep(1.5)
            self.is_connected = True

    fake_db = SlowConnectFake(connected=False)
    monkeypatch.setattr(readiness, "database", fake_db)

    leader = asyncio.create_task(
        readiness.ensure_database_ready(
            connect_timeout_seconds=3.0,
            probe_timeout_seconds=3.0,
            disconnect_timeout_seconds=1.0,
        )
    )
    await started.wait()

    # This caller's own budget is ~1.05s, well short of the leader's 1.5s
    # repair. It must wait for the leader anyway.
    await asyncio.wait_for(
        readiness.ensure_database_ready(
            connect_timeout_seconds=0.01,
            probe_timeout_seconds=0.01,
            disconnect_timeout_seconds=0.01,
        ),
        timeout=6.0,
    )

    await leader
    assert fake_db.connect_calls == 1


@pytest.mark.asyncio
async def test_every_repair_publishes_a_fresh_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deadline is absolute, so it has to be rewritten on every claim.

    Publishing it once per process leaves the second and every later repair
    quoting an already-expired deadline: followers compute a zero wait, 503
    instantly with `RepairWaitTimeout`, and release the slot out from under a
    perfectly healthy leader.
    """
    fake_db = SlowConnectFakeDatabase(connected=False)
    monkeypatch.setattr(readiness, "database", fake_db)

    loop = asyncio.get_running_loop()
    deadlines = []
    for _ in range(2):
        await readiness.ensure_database_ready()
        deadlines.append(readiness._repair_deadline)
        readiness._force_mark_database_disconnected()
        await asyncio.sleep(0.05)

    assert deadlines[1] > deadlines[0], (
        f"the second repair reused the first repair's deadline ({deadlines}); "
        "every later follower would compute a zero wait"
    )
    assert deadlines[1] > loop.time(), "the published deadline is already expired"


@pytest.mark.asyncio
async def test_repeated_abandonment_gives_up_instead_of_spinning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The re-claim loop is bounded, and says so when it runs out.

    Every leader here is cancelled, so a caller that kept re-claiming would
    spin for as long as the churn lasts. Two attempts, then an honest 503.
    """
    class AlwaysBroken(FakeDatabase):
        async def execute(self, _query):
            self.execute_calls += 1
            raise RuntimeError("pool is closed")

    fake_db = AlwaysBroken(connected=True)
    monkeypatch.setattr(readiness, "database", fake_db)

    loop = asyncio.get_running_loop()
    attempts = 0
    real_claim = readiness._claim_repair

    def always_a_departed_leader(budget):
        """Hand back a future that is already resolved as abandoned."""
        nonlocal attempts
        attempts += 1
        settled = loop.create_future()
        settled.set_result(readiness._REPAIR_ABANDONED)
        return False, settled, loop.time() + budget

    monkeypatch.setattr(readiness, "_claim_repair", always_a_departed_leader)
    try:
        with pytest.raises(readiness.DatabaseUnavailableError) as exc_info:
            await asyncio.wait_for(
                readiness.ensure_database_ready(
                    connect_timeout_seconds=0.1,
                    probe_timeout_seconds=0.1,
                    disconnect_timeout_seconds=0.1,
                ),
                timeout=5.0,
            )
    finally:
        monkeypatch.setattr(readiness, "_claim_repair", real_claim)

    assert exc_info.value.error_type == "RepairAbandoned"
    assert attempts == 2, f"expected exactly two attempts, made {attempts}"


@pytest.mark.asyncio
async def test_a_departed_leader_cannot_clear_a_later_leaders_slot() -> None:
    """`_release_repair_slot` must only release the slot it was given.

    A hung leader whose followers already gave up and freed the slot will
    still run its own `finally` eventually. If that publish cleared whatever
    is in the slot rather than only its own future, it would evict the CURRENT
    leader — admitting a second concurrent leader and a second pool, which is
    the defect this whole module exists to prevent.
    """
    loop = asyncio.get_running_loop()
    departed = loop.create_future()
    readiness._repair_loop = weakref.ref(loop)
    readiness._repair_inflight = departed

    # Its followers time out and free the slot; a new leader claims it.
    readiness._release_repair_slot(departed)
    assert readiness._repair_inflight is None
    incumbent = loop.create_future()
    readiness._repair_inflight = incumbent
    readiness._repair_deadline = loop.time() + 10

    # Now the departed leader finally reaches its publish.
    readiness._publish_repair_outcome(departed, None)

    assert readiness._repair_inflight is incumbent, (
        "a departed leader evicted the current leader from the slot"
    )
    readiness._publish_repair_outcome(incumbent, None)


def test_the_leader_budget_counts_every_await_the_leader_can_make() -> None:
    """Followers wait this long, so it must not under-count the leader.

    The leader calls `_connect_once` TWICE — once when it enters disconnected,
    again after the teardown — and probes twice. An earlier version counted
    connect once: at production defaults that is an 11.0s budget against a
    13.0s worst case, and it was measured turning two of three `/orders/create`
    callers away 0.9s before a recovery that succeeded. The parent served all
    three.

    Pinned as arithmetic rather than as a wall-clock test because the shrinking
    mutations that matter (`* 2` -> `* 1`, slack -> ~0) are invisible to a
    timing test whose slack dominates the difference.
    """
    connect, probe, disconnect = 3.0, 3.0, 1.0
    leader_worst_case = connect * 2 + probe * 2 + disconnect  # 13.0s

    budget = readiness._leader_budget(
        connect_timeout_seconds=connect,
        probe_timeout_seconds=probe,
        disconnect_timeout_seconds=disconnect,
    )

    assert budget > leader_worst_case, (
        f"budget {budget}s is under the leader's {leader_worst_case}s worst case; "
        "followers will 503 on repairs that were about to succeed"
    )
    # The margin is scheduling slack: these bounds are per-await and the leader
    # has to be resumed between them. Shrinking it to nothing reintroduces the
    # same failure at the margin, so it is pinned deliberately.
    assert budget == pytest.approx(leader_worst_case + 1.0)


@pytest.mark.asyncio
async def test_a_stale_slot_is_reclaimed_rather_than_wedging_the_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Giving up on a leader must free the slot, not just the caller.

    Bounding the follower's wait bounds the REQUEST; if the slot stays occupied
    the REPAIR PATH is dead for the life of the process, and every later caller
    503s without anything ever attempting recovery again — visible only once
    the database actually needs repairing, because the fast path masks it while
    things are healthy. `asyncio.wait_for` does not hard-bound a leader whose
    own cancellation hangs, so this state is reachable.
    """
    loop = asyncio.get_running_loop()
    abandoned = loop.create_future()
    readiness._repair_loop = weakref.ref(loop)
    readiness._repair_inflight = abandoned
    readiness._repair_deadline = loop.time() + 0.2

    fake_db = FakeDatabase(connected=True, execute_results=[TimeoutError("blip")])
    monkeypatch.setattr(readiness, "database", fake_db)

    with pytest.raises(readiness.DatabaseUnavailableError):
        await asyncio.wait_for(
            readiness.ensure_database_ready(
                connect_timeout_seconds=0.1,
                probe_timeout_seconds=0.1,
                disconnect_timeout_seconds=0.1,
            ),
            timeout=5.0,
        )

    assert readiness._repair_inflight is None, "the stale slot was never freed"

    # And the proof that matters: the very next caller actually repairs.
    healthy = SlowConnectFakeDatabase(connected=False)
    monkeypatch.setattr(readiness, "database", healthy)
    await asyncio.wait_for(readiness.ensure_database_ready(), timeout=2.0)
    assert healthy.connect_calls == 1


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
