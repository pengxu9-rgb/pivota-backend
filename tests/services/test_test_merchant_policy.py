"""Test/demo merchant exclusion — the serving-side rig gate.

Covers the id denylist, the additive-only env hatch, the fail-soft cached
demo-domain resolver, and the product filter (dict + object, missing id kept).
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import pytest

from services import test_merchant_policy as pol


@pytest.fixture(autouse=True)
def _clear_cache():
    pol.reset_cache()
    yield
    pol.reset_cache()


class _FakeDB:
    """Minimal async database stub: fetch_all returns preset rows or raises."""

    def __init__(self, rows: List[Dict[str, Any]] | None = None, raise_exc: bool = False):
        self._rows = rows or []
        self._raise = raise_exc
        self.calls = 0

    async def fetch_all(self, query, values=None):
        self.calls += 1
        if self._raise:
            raise RuntimeError("db down")
        return self._rows


def test_static_set_bakes_in_all_four_rigs():
    ids = pol.static_test_merchant_ids({})
    assert "merch_efbc46b4619cfbdf" in ids
    assert "merch_shopify_0584b37f7a8be00a5223" in ids
    assert "merch_shopify_00d4a720d67d96c5dcba" in ids
    assert "merch_bbd34645bc1950cc" in ids


def test_static_set_bakes_in_review_demo_3():
    """pivota-review-demo-3 was a known rig in the agent repo's metrics
    exclusion list but was never added to the serving denylists. It has no
    merchant_stores row, so the domain resolver cannot reach it either."""
    assert "merch_shopify_b20b5797f4181983c177" in pol.static_test_merchant_ids({})


def test_static_set_bakes_in_the_ownist_fixture_merchant():
    """ADR-018 census (#1595): a fixture catalog with 4 serving_eligible rows
    and NO merchant_stores row, so the demo-domain resolver can never reach it
    — the id denylist is the only leg that excludes it."""
    assert "merch_test_ownist_001" in pol.static_test_merchant_ids({})


async def test_ownist_is_excluded_without_any_store_row():
    # Resolver returns nothing (no merchant_stores row for this rig at all);
    # the exclusion must still hold from the static denylist.
    db = _FakeDB(rows=[])
    ids = await pol.get_excluded_merchant_ids(db, env={})
    assert "merch_test_ownist_001" in ids


def test_env_hatch_is_additive_only():
    ids = pol.static_test_merchant_ids({"PIVOTA_TEST_MERCHANT_IDS": "merch_new, merch_two"})
    assert "merch_new" in ids and "merch_two" in ids
    # cannot un-exclude a baked-in id
    assert "merch_efbc46b4619cfbdf" in pol.static_test_merchant_ids({"PIVOTA_TEST_MERCHANT_IDS": ""})


async def test_resolver_unions_demo_domain_merchants():
    db = _FakeDB(rows=[{"merchant_id": "merch_reconnected_demo"}])
    ids = await pol.get_excluded_merchant_ids(db, env={})
    assert "merch_reconnected_demo" in ids          # resolved by domain
    assert "merch_efbc46b4619cfbdf" in ids          # still has the static set


async def test_resolver_is_fail_soft_on_db_error():
    db = _FakeDB(raise_exc=True)
    ids = await pol.get_excluded_merchant_ids(db, env={})
    # DB blew up → only the static denylist, never a crash, never an empty leak
    assert "merch_efbc46b4619cfbdf" in ids
    assert "merch_reconnected_demo" not in ids


async def test_a_failed_resolve_is_not_cached():
    # A cached failure would let demo rigs list for a whole TTL window after one blip.
    down = _FakeDB(raise_exc=True)
    await pol.get_excluded_merchant_ids(down, env={}, now=1000.0)
    up = _FakeDB(rows=[{"merchant_id": "m_demo"}])
    later = 1000.0 + pol._FAILURE_BACKOFF_SECONDS + 1  # past the backoff, well inside the TTL
    ids = await pol.get_excluded_merchant_ids(up, env={}, now=later)
    assert up.calls == 1                                                 # retried, not cached
    assert "m_demo" in ids


async def test_an_empty_successful_resolve_is_cached():
    # "No demo stores" is a real answer, unlike a failure: one scan per TTL.
    db = _FakeDB(rows=[])
    await pol.get_excluded_merchant_ids(db, env={}, now=1000.0)
    await pol.get_excluded_merchant_ids(db, env={}, now=1100.0)
    assert db.calls == 1


async def test_a_failed_resolve_backs_off_before_retrying():
    good = _FakeDB(rows=[{"merchant_id": "m_demo"}])
    await pol.get_excluded_merchant_ids(good, env={}, now=0.0)
    down = _FakeDB(raise_exc=True)
    t0 = 1000.0                                   # cache expired
    await pol.get_excluded_merchant_ids(down, env={}, now=t0)
    assert down.calls == 1
    inside = t0 + pol._FAILURE_BACKOFF_SECONDS - 0.5
    ids = await pol.get_excluded_merchant_ids(down, env={}, now=inside)
    assert down.calls == 1                        # no second DB attempt inside the window
    assert "m_demo" in ids                        # last good set still served
    assert "merch_efbc46b4619cfbdf" in ids
    after = t0 + pol._FAILURE_BACKOFF_SECONDS + 0.5
    await pol.get_excluded_merchant_ids(down, env={}, now=after)
    assert down.calls == 2                        # retried once the window passed
    up = _FakeDB(rows=[{"merchant_id": "m_demo2"}])
    ids = await pol.get_excluded_merchant_ids(up, env={}, now=after + pol._FAILURE_BACKOFF_SECONDS + 1)
    assert up.calls == 1 and "m_demo2" in ids     # recovery clears the failure state
    await pol.get_excluded_merchant_ids(up, env={}, now=after + pol._FAILURE_BACKOFF_SECONDS + 2)
    assert up.calls == 1                          # and the good result is cached again


async def test_a_persistent_failure_warns_once_per_interval(caplog):
    import logging

    down = _FakeDB(raise_exc=True)
    step = pol._FAILURE_BACKOFF_SECONDS + 1
    with caplog.at_level(logging.WARNING, logger=pol.__name__):
        for i in range(4):                        # 4 real attempts inside one warn interval
            await pol.get_excluded_merchant_ids(down, env={}, now=1000.0 + i * step)
        assert down.calls == 4
        warned = [r for r in caplog.records if "demo-domain resolve FAILED" in r.getMessage()]
        assert len(warned) == 1
        await pol.get_excluded_merchant_ids(down, env={}, now=1000.0 + pol._WARN_INTERVAL_SECONDS + 1)
        warned = [r for r in caplog.records if "demo-domain resolve FAILED" in r.getMessage()]
        assert len(warned) == 2
        assert "3 earlier failures suppressed" in warned[1].getMessage()
        assert warned[0].exc_info is not None and warned[1].exc_info is not None  # traceback kept
        # The suppressed count restarts after each emitted warning.
        t = 1000.0 + pol._WARN_INTERVAL_SECONDS + 1
        for i in (1, 2):
            await pol.get_excluded_merchant_ids(down, env={}, now=t + i * step)
        await pol.get_excluded_merchant_ids(down, env={}, now=t + 2 * pol._WARN_INTERVAL_SECONDS)
        warned = [r for r in caplog.records if "demo-domain resolve FAILED" in r.getMessage()]
        assert len(warned) == 3
        assert "2 earlier failures suppressed" in warned[2].getMessage()


async def test_a_failed_resolve_keeps_the_last_good_set():
    good = _FakeDB(rows=[{"merchant_id": "m_demo"}])
    await pol.get_excluded_merchant_ids(good, env={}, now=1000.0)
    down = _FakeDB(raise_exc=True)
    ids = await pol.get_excluded_merchant_ids(down, env={}, now=1400.0)  # TTL expired, DB down
    assert down.calls == 1
    assert "m_demo" in ids                      # last good set still applies
    assert "merch_efbc46b4619cfbdf" in ids      # static set always


async def test_resolver_caches_within_ttl():
    db = _FakeDB(rows=[{"merchant_id": "m_demo"}])
    await pol.get_excluded_merchant_ids(db, env={}, now=1000.0)
    await pol.get_excluded_merchant_ids(db, env={}, now=1100.0)  # < 300s later
    assert db.calls == 1                                          # served from cache
    await pol.get_excluded_merchant_ids(db, env={}, now=1400.0)  # > 300s later
    assert db.calls == 2


def test_none_db_returns_static_only():
    import asyncio

    # `asyncio.run`, not `get_event_loop().run_until_complete`: since 3.12 the
    # latter no longer auto-creates a loop off the main thread, so it raises
    # DeprecationWarning on 3.12 and RuntimeError on 3.14 — verified failing on
    # 3.14 locally while CI's 3.11 stayed green. Wiring this file into the
    # reliability suite (which is the point of the change shipping alongside)
    # would otherwise plant a landmine that detonates on the next Python bump,
    # in a test whose whole job is to be the tripwire.
    ids = asyncio.run(pol.get_excluded_merchant_ids(None, env={}))
    assert ids == pol.static_test_merchant_ids({})


def test_filter_drops_rigs_keeps_real_and_unscoped():
    excluded = {"merch_efbc46b4619cfbdf"}
    products = [
        {"merchant_id": "merch_efbc46b4619cfbdf", "title": "Winona test"},
        {"merchant_id": "merch_obs_cosrx", "title": "real"},
        {"title": "external_seed canonical (no merchant scope)"},
    ]
    kept = pol.filter_out_test_merchants(products, excluded)
    titles = [p.get("title") for p in kept]
    assert "Winona test" not in titles
    assert "real" in titles
    assert "external_seed canonical (no merchant scope)" in titles


def test_filter_handles_object_products():
    class _P:
        def __init__(self, mid):
            self.merchant_id = mid

    kept = pol.filter_out_test_merchants(
        [_P("merch_efbc46b4619cfbdf"), _P("merch_real")],
        {"merch_efbc46b4619cfbdf"},
    )
    assert len(kept) == 1 and kept[0].merchant_id == "merch_real"


def test_filter_empty_input():
    assert pol.filter_out_test_merchants(None, {"x"}) == []
    assert pol.filter_out_test_merchants([], {"x"}) == []


# The two repos' rig lists must hold the SAME ids: both services write
# catalog_row_trust from the same policy, so an id present in one and not the
# other makes them derive different serving_decisions for the same row and flap
# it public<->blocked on the live surface. Nothing can enforce that across repo
# boundaries, so each side pins the exact set: adding a rig here fails this test
# until the author consciously updates it, and the message says where else to go.
EXPECTED_RIG_IDS = {
    "merch_efbc46b4619cfbdf",
    "merch_shopify_0584b37f7a8be00a5223",
    "merch_shopify_00d4a720d67d96c5dcba",
    "merch_bbd34645bc1950cc",
    "merch_shopify_b20b5797f4181983c177",
    "merch_test_ownist_001",
}


def test_rig_list_is_pinned_and_mirrors_the_gateway_twin():
    assert set(pol.KNOWN_TEST_MERCHANT_IDS) == EXPECTED_RIG_IDS, (
        "Rig list changed. Mirror it in PIVOTA-Agent "
        "src/services/testMerchantPolicy.js TEST_MERCHANT_IDS (and its pinned "
        "test) IN THE SAME DEPLOY — the two services share catalog_row_trust, "
        "so a one-sided change flaps rows public<->blocked."
    )


class _FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def monotonic(self) -> float:
        return self.t


async def test_a_slow_failure_backs_off_from_when_it_failed(monkeypatch):
    # Pool saturation: the attempt fails only after a 120 s checkout wait. The backoff
    # must run from the failure, not from the attempt's start.
    clock = _FakeClock()
    monkeypatch.setattr(pol, "time", clock)

    class _SlowDown(_FakeDB):
        async def fetch_all(self, query, values=None):
            self.calls += 1
            clock.t += 120.0
            raise RuntimeError("pool checkout timed out")

    db = _SlowDown()
    await pol.get_excluded_merchant_ids(db, env={})
    assert db.calls == 1
    clock.t += 1.0
    ids = await pol.get_excluded_merchant_ids(db, env={})
    assert db.calls == 1                          # still inside the window that began at the failure
    assert "merch_efbc46b4619cfbdf" in ids
    clock.t += pol._FAILURE_BACKOFF_SECONDS
    await pol.get_excluded_merchant_ids(db, env={})
    assert db.calls == 2


async def _prime_expired_cache(demo_id: str) -> None:
    """A last-good demo set that is already past its TTL on the clock the policy reads.

    Relative to `pol.time.monotonic()`, NOT an absolute 0.0: on Linux monotonic counts
    from boot, so on a freshly booted CI runner t=0 can still be inside the 300 s TTL.
    The cache then looked fresh, the leader never queried, and `started.wait()` hung
    the sweep shard for its whole 30 min budget."""
    stale = pol.time.monotonic() - pol._CACHE_TTL_SECONDS - 1
    await pol.get_excluded_merchant_ids(_FakeDB(rows=[{"merchant_id": demo_id}]), env={}, now=stale)


class _GatedDB(_FakeDB):
    """fetch_all waits for `release`, so callers can pile up behind one attempt."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.release = asyncio.Event()
        self.started = asyncio.Event()

    async def fetch_all(self, query, values=None):
        self.calls += 1
        self.started.set()
        await self.release.wait()
        if self._raise:
            raise RuntimeError("db down")
        return self._rows


async def _pile_up(db, n=50):
    leader = asyncio.create_task(pol.get_excluded_merchant_ids(db, env={}))
    await asyncio.wait_for(db.started.wait(), 5.0)
    followers = [asyncio.create_task(pol.get_excluded_merchant_ids(db, env={})) for _ in range(n - 1)]
    await asyncio.sleep(0)
    db.release.set()
    # Bounded: a future that never resolves must fail the test, not hang the suite.
    return await asyncio.wait_for(asyncio.gather(leader, *followers), 5.0)


async def test_concurrent_callers_share_one_failing_attempt():
    db = _GatedDB(raise_exc=True)
    results = await _pile_up(db)
    assert db.calls == 1
    assert all("merch_efbc46b4619cfbdf" in r for r in results)


async def test_concurrent_callers_share_one_successful_attempt():
    # A cold start must not hand the followers static-only: they get the demo leg too.
    db = _GatedDB(rows=[{"merchant_id": "m_demo"}])
    results = await _pile_up(db)
    assert db.calls == 1
    assert all("m_demo" in r and "merch_efbc46b4619cfbdf" in r for r in results)


async def test_a_cancelled_follower_does_not_cancel_the_shared_attempt():
    db = _GatedDB(rows=[{"merchant_id": "m_demo"}])
    leader = asyncio.create_task(pol.get_excluded_merchant_ids(db, env={}))
    await asyncio.wait_for(db.started.wait(), 5.0)
    follower = asyncio.create_task(pol.get_excluded_merchant_ids(db, env={}))
    await asyncio.sleep(0)
    follower.cancel()
    db.release.set()
    assert "m_demo" in await asyncio.wait_for(leader, 5.0)
    with pytest.raises(asyncio.CancelledError):
        await follower
    assert db.calls == 1


async def test_cancelling_one_follower_leaves_the_other_followers_served():
    # Without shield, one cancellation would cancel the shared future and hand every
    # other follower a CancelledError, which `except Exception` callers do not catch.
    db = _GatedDB(rows=[{"merchant_id": "m_demo"}])
    leader = asyncio.create_task(pol.get_excluded_merchant_ids(db, env={}))
    await asyncio.wait_for(db.started.wait(), 5.0)
    followers = [asyncio.create_task(pol.get_excluded_merchant_ids(db, env={})) for _ in range(3)]
    await asyncio.sleep(0)
    followers[0].cancel()
    db.release.set()
    results = await asyncio.wait_for(asyncio.gather(leader, *followers[1:]), 5.0)
    assert all("m_demo" in r for r in results)
    assert db.calls == 1


async def test_followers_of_a_failed_attempt_get_the_last_good_set_and_do_not_warn(caplog):
    import logging

    await _prime_expired_cache("m_old")
    db = _GatedDB(raise_exc=True)
    with caplog.at_level(logging.WARNING, logger=pol.__name__):
        results = await _pile_up(db, n=10)
    assert db.calls == 1
    assert all("m_old" in r and "merch_efbc46b4619cfbdf" in r for r in results)
    warned = [r for r in caplog.records if "demo-domain resolve FAILED" in r.getMessage()]
    assert len(warned) == 1                       # the leader's failure only


async def test_a_follower_stops_waiting_after_the_bound(monkeypatch):
    # The leader's connection never answers. Followers must not wait with it.
    monkeypatch.setattr(pol, "_FOLLOWER_WAIT_SECONDS", 0.2)
    await _prime_expired_cache("m_old")
    db = _GatedDB(rows=[{"merchant_id": "m_new"}])
    leader = asyncio.create_task(pol.get_excluded_merchant_ids(db, env={}))
    await asyncio.wait_for(db.started.wait(), 5.0)
    ids = await asyncio.wait_for(pol.get_excluded_merchant_ids(db, env={}), 2.0)
    assert "merch_efbc46b4619cfbdf" in ids and "m_old" in ids   # static + last good
    assert db.calls == 1                          # no second attempt on timeout
    assert not leader.done()                      # the leader is unaffected
    db.release.set()
    assert "m_new" in await asyncio.wait_for(leader, 5.0)


async def test_the_inflight_slot_is_cleared_after_every_attempt():
    await pol.get_excluded_merchant_ids(_FakeDB(rows=[]), env={}, now=0.0)
    assert pol._inflight is None
    await pol.get_excluded_merchant_ids(_FakeDB(raise_exc=True), env={}, now=1000.0)
    assert pol._inflight is None


async def test_a_stale_inflight_from_another_loop_is_not_joined():
    other = asyncio.new_event_loop()
    try:
        pol._inflight = other.create_future()     # pending, but not on this loop
        db = _FakeDB(rows=[{"merchant_id": "m_demo"}])
        ids = await asyncio.wait_for(pol.get_excluded_merchant_ids(db, env={}), 2.0)
        assert db.calls == 1 and "m_demo" in ids  # ran its own lookup instead
    finally:
        pol.reset_cache()
        other.close()


def test_reset_cache_clears_the_inflight_slot():
    loop = asyncio.new_event_loop()
    try:
        pol._inflight = loop.create_future()
        pol.reset_cache()
        assert pol._inflight is None
    finally:
        loop.close()
