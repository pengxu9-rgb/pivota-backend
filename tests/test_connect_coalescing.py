"""One connect in flight per database — the coordination half.

`databases.Database.connect()` has no re-entrancy guard, so N concurrent
callers each build a pool and each assigns `backend._pool`; the losers are
unreachable for ever. What that costs in SERVER BACKENDS is measured in
`test_connect_coalescing_postgres.py`; what lives here is the contract, driven
against fakes so it needs no server.

EVERY TEST BELOW CORRESPONDS TO A MEASURED REGRESSION from an earlier attempt
at this. Two designs were written and reverted before this one, and each broke
a property that its own test suite could not see. The numbers in the docstrings
are what those attempts actually did.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from utils import database_readiness as readiness


class FakeDb:
    """A Database whose `connect()` yields, so callers can overlap inside it.

    A fake that never awaits makes every one of these tests vacuous: callers
    cannot be concurrently inside a connect that does not suspend.
    """

    def __init__(self, *, delay: float = 0.0, raises: BaseException | None = None):
        self.is_connected = False
        self.delay = delay
        self.raises = raises
        self.connect_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1
        await asyncio.sleep(self.delay)
        if self.raises is not None:
            raise self.raises
        self.is_connected = True

    async def disconnect(self) -> None:
        self.is_connected = False


@pytest.fixture(autouse=True)
def _clean_slot():
    """The slot is a module global; never let one test's attempt leak into another."""
    readiness._connect_attempt = None
    yield
    readiness._connect_attempt = None


@pytest.mark.asyncio
async def test_concurrent_callers_build_one_pool() -> None:
    """The defect itself: N callers, one connect."""
    db = FakeDb(delay=0.05)

    await asyncio.gather(*(readiness.connect_database_with_timeout(5, db=db) for _ in range(8)))

    assert db.connect_calls == 1, f"{db.connect_calls} concurrent connects — one pool each"
    assert db.is_connected is True


@pytest.mark.asyncio
async def test_a_failing_connect_costs_one_attempt_for_everyone() -> None:
    """N callers against a down database must cost ONE attempt, not N.

    Each attempt that fails strands whatever its fill had already opened, so N
    attempts is N stranded batches on exactly the outage this code exists for.
    """
    db = FakeDb(delay=0.05, raises=RuntimeError("db down"))

    results = await asyncio.gather(
        *(readiness.connect_database_with_timeout(5, db=db) for _ in range(8)),
        return_exceptions=True,
    )

    assert db.connect_calls == 1, f"{db.connect_calls} attempts for one outage"
    assert all(isinstance(r, RuntimeError) and str(r) == "db down" for r in results)
    assert len({id(r) for r in results}) == 8, "waiters shared one exception object"


@pytest.mark.asyncio
async def test_a_hasty_caller_does_not_truncate_a_patient_one() -> None:
    """THE FIRST REVERTED DESIGN'S BLOCKER.

    Call-site budgets span 2.0s (`/auth/login`) to 10s (the supervisor's timer)
    to 15s (startup). An attempt bounded by whichever caller happened to arrive
    first starved every longer-budget caller: measured 0 of 6 order requests
    served against 6 of 6 on the parent, on a database answering inside all of
    their budgets.
    """
    db = FakeDb(delay=0.5)

    hasty = asyncio.create_task(readiness.connect_database_with_timeout(0.15, db=db))
    await asyncio.sleep(0.05)
    patient = asyncio.create_task(readiness.connect_database_with_timeout(5.0, db=db))

    with pytest.raises(asyncio.TimeoutError):
        await hasty
    await asyncio.wait_for(patient, timeout=6)

    assert db.is_connected is True, "the patient caller inherited the hasty one's budget"
    assert db.connect_calls == 1, "the fill was restarted rather than shared"


@pytest.mark.asyncio
async def test_a_short_waiter_timing_out_does_not_cancel_a_co_waiter() -> None:
    """`asyncio.shield` on the shared future is load-bearing.

    `wait_for` cancels what it waits on, and the future is SHARED — so an
    unshielded wait lets the first waiter to give up cancel it out from under
    every co-waiter, who then see `CancelledError`. That is a `BaseException`
    which `accounts_orders_api`'s `except Exception` does not catch, so the
    request is torn down instead of returning 503. Two waiters with DIFFERENT
    budgets is the only shape that detects it.
    """
    db = FakeDb(delay=0.4)

    short = asyncio.create_task(readiness.connect_database_with_timeout(0.1, db=db))
    await asyncio.sleep(0.02)
    long = asyncio.create_task(readiness.connect_database_with_timeout(5, db=db))

    with pytest.raises(asyncio.TimeoutError):
        await short
    await asyncio.wait_for(long, timeout=5)   # a clean return, NOT CancelledError
    assert db.is_connected is True


@pytest.mark.asyncio
async def test_a_wedged_attempt_is_retried_under_sustained_arrivals() -> None:
    """THE SECOND REVERTED DESIGN'S BLOCKER.

    That version widened the deadline by each new arrival's budget measured
    from ITS arrival, so under traffic arriving closer together than the budget
    the attempt was never cancelled and no second one ever started: measured 13
    of 24 requests served against 23 of 24 on the parent, first success at
    8.01s against 0.51s, and monotonically worse the longer the wedge.

    THE ARRIVALS MUST OVERLAP. A sequential loop of callers never has two
    waiters at once, so the refcount drops to zero between them and the fill is
    cancelled for that reason instead — which is why an earlier version of this
    test passed against the very mutant it was written to catch. Spawn them.
    """
    attempts = []

    class Wedged(FakeDb):
        async def connect(self) -> None:
            attempts.append(1)
            await asyncio.sleep(30 if len(attempts) == 1 else 0)
            self.is_connected = True

    db = Wedged()

    async def arrival() -> None:
        try:
            await readiness.connect_database_with_timeout(3.0, db=db)
        except Exception:
            pass

    # Overlapping: each caller's 3.0s budget covers the next several arrivals.
    callers = []
    for _ in range(12):
        callers.append(asyncio.create_task(arrival()))
        await asyncio.sleep(0.25)
    await asyncio.gather(*callers, return_exceptions=True)

    assert len(attempts) >= 2, (
        f"{len(attempts)} attempt(s) across 3s of overlapping arrivals — the "
        "deadline was pushed out by each arrival, so the wedged attempt was "
        "never retired and nothing retried it"
    )


@pytest.mark.asyncio
async def test_a_late_joiner_with_budget_left_gets_its_own_attempt() -> None:
    """An attempt running out of road is not the same as the CALLER running out.

    A caller that joins late is covered by the attempt's deadline only up to
    that attempt's start; past it, the caller still has budget the attempt does
    not. Adopting the attempt's `TimeoutError` there makes joining strictly
    worse than connecting alone — the property the whole design rests on.
    """
    attempts = []

    class WedgedOnce(FakeDb):
        async def connect(self) -> None:
            attempts.append(1)
            await asyncio.sleep(30 if len(attempts) == 1 else 0)
            self.is_connected = True

    db = WedgedOnce()

    first = asyncio.create_task(readiness.connect_database_with_timeout(0.5, db=db))
    await asyncio.sleep(0.4)
    # Joins at +0.4s with 3.0s of its own: the attempt dies at +3.0, this
    # caller is good until +3.4 and must spend the difference on a real try.
    late = asyncio.create_task(readiness.connect_database_with_timeout(3.0, db=db))

    await asyncio.gather(first, return_exceptions=True)
    await asyncio.wait_for(late, timeout=6)

    assert db.is_connected is True, "the late joiner adopted an attempt timeout it had budget to survive"
    assert len(attempts) == 2, f"{len(attempts)} attempts — the late joiner never got its own"


@pytest.mark.asyncio
async def test_a_caller_never_waits_longer_than_its_own_budget() -> None:
    """Joining must never be slower than connecting alone.

    A caller that inherits the attempt's deadline instead of its own turns one
    slow startup connect into a Railway healthcheck failure.
    """
    db = FakeDb(delay=30)

    started = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        await readiness.connect_database_with_timeout(0.3, db=db)
    elapsed = time.monotonic() - started

    assert elapsed < 3, f"waited {elapsed:.1f}s on a 0.3s budget"


@pytest.mark.asyncio
async def test_two_databases_do_not_share_a_deadline() -> None:
    """The deadline is per-attempt, not a module global.

    With a shared global, an uncoalesced fill for a second database was bounded
    by the first database's deadline: measured, a caller asking for 10s for a
    1.0s connect was truncated at 0.45s.
    """
    first = FakeDb(delay=1.0)
    second = FakeDb(delay=1.0)

    hasty = asyncio.create_task(readiness.connect_database_with_timeout(0.3, db=first))
    await asyncio.sleep(0.05)
    other = asyncio.create_task(readiness.connect_database_with_timeout(10.0, db=second))

    await asyncio.gather(hasty, return_exceptions=True)
    await asyncio.wait_for(other, timeout=8)

    assert second.is_connected is True, "one database's budget bounded another's fill"


@pytest.mark.asyncio
async def test_a_live_registration_is_never_evicted() -> None:
    """A newcomer for a DIFFERENT database must not take the slot.

    Evicting a live registration leaves its database with none, so its next
    caller starts a SECOND fill behind the first one's back — the very defect
    the slot exists to prevent. Measured on a version that evicted: 25 orphaned
    backends, still open after a clean disconnect.
    """
    held = FakeDb(delay=0.4)
    other = FakeDb(delay=0.0)

    holder = asyncio.create_task(readiness.connect_database_with_timeout(5, db=held))
    await asyncio.sleep(0.05)
    registered = readiness._connect_attempt
    assert registered is not None and not registered.future.done()

    await readiness.connect_database_with_timeout(5, db=other)   # uncoalesced
    assert other.connect_calls == 1
    assert readiness._connect_attempt is registered, (
        "an uncoalesced connect evicted another database's live registration"
    )

    await holder
    assert held.connect_calls == 1


@pytest.mark.asyncio
async def test_a_registration_from_a_dead_loop_does_not_wedge_the_slot() -> None:
    """The slot must be usable again after the loop that owned it is closed.

    A Future from a closed loop is never `done()` and can never be awaited from
    another loop, so a slot that refuses to drop it disables coalescing for the
    life of the process — every later caller takes the uncoalesced path.
    Measured on a version without this eviction: 6 attempts for 6 callers where
    1 was expected, and 25 orphaned backends on real Postgres.

    The stale registration is CONSTRUCTED rather than produced by running a
    loop to death: this implementation retires its own slot on cleanup, so a
    naturally-abandoned attempt leaves nothing behind and a test that waited
    for one would assert on a state that never occurs. What needs proving is
    the eviction branch itself.
    """
    dead = asyncio.new_event_loop()
    try:
        stale_db = FakeDb()
        stale = readiness._ConnectAttempt(dead, stale_db, 30.0)
        assert not stale.future.done(), "a pending future is the wedging shape"
        readiness._connect_attempt = stale

        db = FakeDb(delay=0.2)
        await asyncio.gather(
            *(readiness.connect_database_with_timeout(5, db=db) for _ in range(6))
        )

        assert db.connect_calls == 1, (
            f"{db.connect_calls} attempts for 6 callers — the dead loop's "
            "registration disabled coalescing"
        )
        assert readiness._connect_attempt is not stale, "the stale slot was never dropped"
    finally:
        dead.close()


@pytest.mark.asyncio
async def test_an_already_connected_database_never_touches_the_slot() -> None:
    """The fast path is every call on every hot request path."""
    db = FakeDb()
    db.is_connected = True

    parked = asyncio.get_running_loop().create_future()
    readiness._connect_attempt = parked   # anything truthy the fast path must ignore

    await asyncio.wait_for(readiness.connect_database_with_timeout(5, db=db), timeout=2)

    assert db.connect_calls == 0
    assert readiness._connect_attempt is parked, "the fast path touched the slot"
