"""The staff-set is_test leg of the rig exclusion (migration 282, MERCHANT_IS_TEST_FILTER_ENABLED).

Flag off: the column is never read and every answer is what it was before it existed.
Flag on: is_test merchants join the exclusion set; a failed is_test read keeps its last good set
for the listings and never costs the demo-domain leg. The classifier's view of the cache
(`cached_is_test_merchant_ids`) is stricter: only a fresh set, never a stale, failed or
invalidated one (M-05 P2-1).
"""

from __future__ import annotations

from typing import Any, Dict, List

import asyncio
import time

import pytest

from services import test_merchant_policy as pol

ON = {pol.IS_TEST_FILTER_ENV: "true"}


@pytest.fixture(autouse=True)
def _clear_cache():
    pol.reset_cache()
    yield
    pol.reset_cache()


class _RoutingDB:
    """fetch_all answers by which table the query reads; records every query."""

    def __init__(
        self,
        demo: List[Dict[str, Any]] | None = None,
        is_test: List[Dict[str, Any]] | None = None,
        demo_raises: bool = False,
        is_test_raises: bool = False,
    ):
        self.demo = demo or []
        self.is_test = is_test or []
        self.demo_raises = demo_raises
        self.is_test_raises = is_test_raises
        self.queries: List[str] = []

    def is_test_reads(self) -> int:
        return sum(1 for q in self.queries if "is_test" in q)

    async def fetch_all(self, query, values=None):
        self.queries.append(query)
        if "is_test" in query:
            if self.is_test_raises:
                raise RuntimeError("column merchant_onboarding.is_test does not exist")
            return self.is_test
        if self.demo_raises:
            raise RuntimeError("db down")
        return self.demo


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", " yes ", "on"])
def test_flag_truthy_values(raw):
    assert pol.merchant_is_test_filter_enabled({pol.IS_TEST_FILTER_ENV: raw}) is True


@pytest.mark.parametrize("raw", ["", "0", "false", "off", "no", "enabled"])
def test_flag_is_off_unless_truthy(raw):
    assert pol.merchant_is_test_filter_enabled({pol.IS_TEST_FILTER_ENV: raw}) is False


def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv(pol.IS_TEST_FILTER_ENV, raising=False)
    assert pol.merchant_is_test_filter_enabled() is False
    assert pol.merchant_is_test_filter_enabled({}) is False


async def test_flag_off_never_reads_the_column_and_answers_as_before():
    db = _RoutingDB(demo=[{"merchant_id": "m_demo"}], is_test=[{"merchant_id": "m_staff_test"}])
    ids = await pol.get_excluded_merchant_ids(db, env={})
    assert db.is_test_reads() == 0
    assert ids == pol.static_test_merchant_ids({}) | {"m_demo"}
    assert "m_staff_test" not in ids


async def test_flag_on_adds_is_test_merchants():
    db = _RoutingDB(demo=[{"merchant_id": "m_demo"}], is_test=[{"merchant_id": "m_staff_test"}])
    ids = await pol.get_excluded_merchant_ids(db, env=ON)
    assert {"m_demo", "m_staff_test"} <= ids
    assert pol.static_test_merchant_ids({}) <= ids


async def test_flag_on_result_is_cached_with_the_demo_set():
    db = _RoutingDB(is_test=[{"merchant_id": "m_staff_test"}])
    await pol.get_excluded_merchant_ids(db, env=ON, now=1000.0)
    ids = await pol.get_excluded_merchant_ids(db, env=ON, now=1100.0)
    assert len(db.queries) == 2  # one demo read + one is_test read, then a cache hit
    assert "m_staff_test" in ids


async def test_a_flag_off_cache_is_not_a_hit_for_a_flag_on_call():
    db = _RoutingDB(is_test=[{"merchant_id": "m_staff_test"}])
    await pol.get_excluded_merchant_ids(db, env={}, now=1000.0)
    ids = await pol.get_excluded_merchant_ids(db, env=ON, now=1001.0)
    assert db.is_test_reads() == 1
    assert "m_staff_test" in ids


async def test_flag_off_after_on_drops_the_is_test_ids_at_once():
    db = _RoutingDB(is_test=[{"merchant_id": "m_staff_test"}])
    await pol.get_excluded_merchant_ids(db, env=ON, now=1000.0)
    ids = await pol.get_excluded_merchant_ids(db, env={}, now=1001.0)
    assert "m_staff_test" not in ids


async def test_a_failed_is_test_read_keeps_the_demo_leg():
    db = _RoutingDB(demo=[{"merchant_id": "m_demo"}], is_test_raises=True)
    ids = await pol.get_excluded_merchant_ids(db, env=ON)
    assert "m_demo" in ids
    assert pol.static_test_merchant_ids({}) <= ids


async def test_a_failed_is_test_read_serves_the_last_good_set(caplog):
    good = _RoutingDB(is_test=[{"merchant_id": "m_staff_test"}])
    await pol.get_excluded_merchant_ids(good, env=ON, now=0.0)
    bad = _RoutingDB(is_test_raises=True)
    ids = await pol.get_excluded_merchant_ids(bad, env=ON, now=1000.0)  # cache expired
    assert bad.is_test_reads() == 1
    assert "m_staff_test" in ids
    assert any("is_test resolve FAILED" in r.getMessage() for r in caplog.records)


async def test_unknown_is_not_test_when_the_column_was_never_readable():
    # The fail direction: a merchant the read could not see lists as before; static ids still apply.
    db = _RoutingDB(is_test_raises=True)
    ids = await pol.get_excluded_merchant_ids(db, env=ON)
    assert ids == pol.static_test_merchant_ids({})


async def test_is_test_warning_is_rate_limited(caplog):
    bad = _RoutingDB(is_test_raises=True)
    await pol.get_excluded_merchant_ids(bad, env=ON, now=0.0)
    pol.invalidate_cache()
    await pol.get_excluded_merchant_ids(bad, env=ON, now=10.0)
    assert bad.is_test_reads() == 2
    warnings = [r for r in caplog.records if "is_test resolve FAILED" in r.getMessage()]
    assert len(warnings) == 1


async def test_invalidate_forces_a_reread_and_keeps_the_last_good_sets():
    db = _RoutingDB(demo=[{"merchant_id": "m_demo"}], is_test=[{"merchant_id": "m_a"}])
    await pol.get_excluded_merchant_ids(db, env=ON, now=1000.0)
    db.is_test = [{"merchant_id": "m_b"}]
    pol.invalidate_cache()
    ids = await pol.get_excluded_merchant_ids(db, env=ON, now=1001.0)  # inside the TTL
    assert "m_b" in ids and "m_a" not in ids
    # A failed re-read after an invalidate still serves the last good sets.
    pol.invalidate_cache()
    db.demo_raises = True
    ids = await pol.get_excluded_merchant_ids(db, env=ON, now=1002.0)
    assert {"m_demo", "m_b"} <= ids


async def test_blank_ids_from_the_column_are_ignored():
    db = _RoutingDB(is_test=[{"merchant_id": " "}, {"merchant_id": None}, {"merchant_id": "m_x "}])
    ids = await pol.get_excluded_merchant_ids(db, env=ON)
    assert "m_x" in ids and "" not in ids and " " not in ids


# ── phase B: the sync read the test-traffic classifier uses ───────────────────────────────────


async def test_cached_set_is_empty_until_an_async_read():
    assert pol.cached_is_test_merchant_ids(ON) == set()
    db = _RoutingDB(is_test=[{"merchant_id": "m_staff_test"}])
    await pol.refresh_is_test_merchant_ids(db, env=ON)
    assert pol.cached_is_test_merchant_ids(ON) == {"m_staff_test"}


async def test_cached_set_is_empty_with_the_flag_off_even_when_filled():
    db = _RoutingDB(is_test=[{"merchant_id": "m_staff_test"}])
    await pol.refresh_is_test_merchant_ids(db, env=ON)
    assert pol.cached_is_test_merchant_ids({}) == set()
    assert pol.cached_is_test_merchant_ids({pol.IS_TEST_FILTER_ENV: "false"}) == set()


async def test_refresh_with_the_flag_off_touches_no_database():
    db = _RoutingDB(is_test=[{"merchant_id": "m_staff_test"}])
    await pol.refresh_is_test_merchant_ids(db, env={})
    assert db.queries == []
    await pol.refresh_is_test_merchant_ids(None, env=ON)  # no database: nothing to do, no raise


async def test_refresh_is_cached_and_never_raises():
    db = _RoutingDB(is_test=[{"merchant_id": "m_a"}])
    await pol.refresh_is_test_merchant_ids(db, env=ON)
    await pol.refresh_is_test_merchant_ids(db, env=ON)
    assert db.is_test_reads() == 1
    down = _RoutingDB(demo_raises=True, is_test_raises=True)
    pol.invalidate_cache()
    await pol.refresh_is_test_merchant_ids(down, env=ON)
    # The listings keep the last good set; the classifier is told nothing (unknown is not test).
    assert "m_a" in await pol.get_excluded_merchant_ids(down, env=ON)
    assert pol.cached_is_test_merchant_ids(ON) == set()


async def test_classifier_reads_the_cached_set_only_with_the_flag_on(monkeypatch):
    from services import test_traffic as tt

    db = _RoutingDB(is_test=[{"merchant_id": "m_staff_test"}])
    monkeypatch.setenv(pol.IS_TEST_FILTER_ENV, "true")
    await pol.refresh_is_test_merchant_ids(db)
    assert tt.classify_test_traffic(merchant_ids=["M_STAFF_TEST"], synthetic_call=False) == ["test_merchant"]
    monkeypatch.delenv(pol.IS_TEST_FILTER_ENV)
    assert tt.classify_test_traffic(merchant_ids=["m_staff_test"], synthetic_call=False) == []


# ── M-05 P2-1: what the classifier may take from the cache ───────────────────────────────────


class _GatedDB(_RoutingDB):
    """The is_test read waits on `gate` once it has started (`started`), so a test can act while it
    is in flight. It returns the rows as they were when it STARTED, like a read from a snapshot."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.gate = asyncio.Event()
        self.started = asyncio.Event()

    async def fetch_all(self, query, values=None):
        if "is_test" in query and not self.gate.is_set():
            self.queries.append(query)
            snapshot = list(self.is_test)
            self.started.set()
            await self.gate.wait()
            return snapshot
        return await super().fetch_all(query, values)


async def test_an_in_flight_read_cannot_refresh_a_cache_invalidated_after_it_began():
    db = _GatedDB(is_test=[{"merchant_id": "m_cleared"}])
    leader = asyncio.ensure_future(pol.get_excluded_merchant_ids(db, env=ON))
    await asyncio.wait_for(db.started.wait(), 1.0)
    # the staff clear commits and this instance invalidates, while the read is still running
    db.is_test = []
    pol.invalidate_cache()
    db.gate.set()
    await asyncio.wait_for(leader, 1.0)
    assert pol.cached_is_test_merchant_ids(ON) == set()  # the pre-write answer is not served
    ids = await pol.get_excluded_merchant_ids(db, env=ON)  # not a cache hit: it reads again
    assert db.is_test_reads() == 2
    assert "m_cleared" not in ids
    assert pol.cached_is_test_merchant_ids(ON) == set()


async def test_a_read_not_raced_by_an_invalidate_is_served():
    db = _GatedDB(is_test=[{"merchant_id": "m_a"}])
    leader = asyncio.ensure_future(pol.get_excluded_merchant_ids(db, env=ON))
    await asyncio.wait_for(db.started.wait(), 1.0)
    db.gate.set()
    await asyncio.wait_for(leader, 1.0)
    assert pol.cached_is_test_merchant_ids(ON) == {"m_a"}


async def test_a_stale_set_is_unknown_to_the_classifier():
    db = _RoutingDB(is_test=[{"merchant_id": "m_a"}])
    await pol.get_excluded_merchant_ids(db, env=ON, now=1000.0)
    assert pol.cached_is_test_merchant_ids(ON, now=1000.0 + pol._CACHE_TTL_SECONDS - 1) == {"m_a"}
    assert pol.cached_is_test_merchant_ids(ON, now=1000.0 + pol._CACHE_TTL_SECONDS) == set()


async def test_an_invalidate_makes_the_set_unknown_until_a_new_read():
    db = _RoutingDB(is_test=[{"merchant_id": "m_a"}])
    await pol.refresh_is_test_merchant_ids(db, env=ON)
    pol.invalidate_cache()
    assert pol.cached_is_test_merchant_ids(ON) == set()
    await pol.refresh_is_test_merchant_ids(db, env=ON)
    assert pol.cached_is_test_merchant_ids(ON) == {"m_a"}


async def test_a_persistently_failing_read_is_not_test_to_the_classifier_but_keeps_listings():
    from services import test_traffic as tt

    good = _RoutingDB(is_test=[{"merchant_id": "m_a"}])
    await pol.refresh_is_test_merchant_ids(good, env=ON)
    bad = _RoutingDB(is_test_raises=True)
    for _ in range(3):
        pol.invalidate_cache()
        assert "m_a" in await pol.get_excluded_merchant_ids(bad, env=ON)  # the listing: last good
        assert pol.cached_is_test_merchant_ids(ON) == set()  # the classifier: unknown
    assert bad.is_test_reads() == 3
    assert tt.classify_test_traffic(merchant_ids=["m_a"], synthetic_call=False) == []


async def test_a_writers_primary_key_answer_replaces_the_cache(monkeypatch):
    """The cache says test (fresh, but another instance cleared the merchant): the writer's own
    read says not, and the writer's read wins."""
    from services import test_traffic as tt

    monkeypatch.setenv(pol.IS_TEST_FILTER_ENV, "true")
    await pol.refresh_is_test_merchant_ids(_RoutingDB(is_test=[{"merchant_id": "m_a"}]))
    assert tt.classify_test_traffic(merchant_ids=["m_a"], synthetic_call=False) == ["test_merchant"]
    assert tt.classify_test_traffic(merchant_ids=["m_a"], synthetic_call=False, is_test_merchant_ids=set()) == []
    assert tt.classify_test_traffic(
        merchant_ids=["M_B"], synthetic_call=False, is_test_merchant_ids={"m_b"}
    ) == ["test_merchant"]


class _PkDB:
    def __init__(self, rows=None, raises=False, hangs=False):
        self.rows = rows or {}
        self.raises = raises
        self.hangs = hangs
        self.queries = []

    async def fetch_one(self, query, values=None):
        self.queries.append((query, dict(values or {})))
        if self.hangs:
            await asyncio.Event().wait()
        if self.raises:
            raise RuntimeError("column merchant_onboarding.is_test does not exist")
        mid = (values or {}).get("m")
        return {"is_test": self.rows[mid]} if mid in self.rows else None


async def test_primary_key_read_answers_per_merchant():
    db = _PkDB(rows={"m_t": True, "m_r": False})
    assert await pol.read_is_test_merchant_ids(db, ["m_t", "m_r", "m_ghost", None, " ", "m_t"]) == {"m_t"}
    assert [v["m"] for _, v in db.queries] == ["m_t", "m_r", "m_ghost"]  # one read per distinct id
    assert all("WHERE merchant_id = :m" in q for q, _ in db.queries)


@pytest.mark.parametrize("db", [_PkDB(raises=True), _PkDB(hangs=True)], ids=["raises", "hangs"])
async def test_primary_key_read_failure_or_timeout_is_not_test(db):
    started = time.monotonic()
    out = await asyncio.wait_for(pol.read_is_test_merchant_ids(db, ["m_t"], timeout=0.05), 2.0)
    assert out == set()
    assert time.monotonic() - started < 1.0


async def test_primary_key_read_uses_the_module_timeout_by_default(monkeypatch):
    monkeypatch.setattr(pol, "PK_READ_TIMEOUT_SECONDS", 0.05)
    out = await asyncio.wait_for(pol.read_is_test_merchant_ids(_PkDB(hangs=True), ["m_t"]), 2.0)
    assert out == set()


# ── M-05 P3-5: the leader's reads are bounded once the flag is on ─────────────────────────────


class _HangingDB(_RoutingDB):
    def __init__(self, hang_on: str, **kw):
        super().__init__(**kw)
        self.hang_on = hang_on

    async def fetch_all(self, query, values=None):
        if (self.hang_on == "is_test") == ("is_test" in query):
            self.queries.append(query)
            await asyncio.Event().wait()
        return await super().fetch_all(query, values)


@pytest.mark.parametrize("hang_on", ["is_test", "demo"])
async def test_a_stuck_leader_read_is_bounded_with_the_flag_on(monkeypatch, hang_on):
    monkeypatch.setattr(pol, "_LEADER_READ_TIMEOUT_SECONDS", 0.05)
    db = _HangingDB(hang_on, demo=[{"merchant_id": "m_demo"}], is_test=[{"merchant_id": "m_a"}])
    started = time.monotonic()
    ids = await asyncio.wait_for(pol.get_excluded_merchant_ids(db, env=ON), 2.0)
    assert time.monotonic() - started < 1.0
    assert pol.static_test_merchant_ids({}) <= ids
    assert pol.cached_is_test_merchant_ids(ON) == set()


# ── M-05 delta review P3-E: the primary-key read's warning is one short line, rate-limited ───


async def test_primary_key_read_failures_log_the_type_once_per_interval(caplog):
    db = _PkDB(raises=True)
    with caplog.at_level("WARNING", logger=pol.logger.name):
        for _ in range(3):
            assert await pol.read_is_test_merchant_ids(db, ["m_t"]) == set()
        lines = [r for r in caplog.records if "primary-key read FAILED" in r.getMessage()]
        assert len(lines) == 1  # not one per order
        assert "(RuntimeError)" in lines[0].getMessage()
        assert "0 earlier failures suppressed" in lines[0].getMessage()
        assert lines[0].exc_info is None  # the type only, no traceback
        assert "is_test does not exist" not in lines[0].getMessage()
        # after the interval, one more line, carrying the count it suppressed
        pol._pk_last_warn_at -= pol._WARN_INTERVAL_SECONDS
        await pol.read_is_test_merchant_ids(db, ["m_t"])
        lines = [r for r in caplog.records if "primary-key read FAILED" in r.getMessage()]
        assert len(lines) == 2
        assert "2 earlier failures suppressed" in lines[1].getMessage()


async def test_a_primary_key_timeout_is_named_in_the_warning(caplog):
    with caplog.at_level("WARNING", logger=pol.logger.name):
        await pol.read_is_test_merchant_ids(_PkDB(hangs=True), ["m_t"], timeout=0.05)
    [line] = [r for r in caplog.records if "primary-key read FAILED" in r.getMessage()]
    assert "(TimeoutError)" in line.getMessage() and line.exc_info is None
