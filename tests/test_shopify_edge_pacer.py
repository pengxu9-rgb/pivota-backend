"""The shared Shopify-edge request budget (services/shopify_edge_pacer.py, migration 251).

Driven through the REAL fetch and scheduler code with only the HTTP layer stubbed:
  * `external_offers_service._fetch_html` (the refresh's fetch) and
    `external_seed_destination_liveness.probe_destination` (the destination sweep's), both through
    the real `crawl_politeness` gate;
  * the Tier B job's real `run()` and `PacedTransport`, and the purchasability sweep's real run loop;
  * two OS processes sharing one database (a SQLite file here; Postgres in the `_postgres` twin).

Pinned:
  * flag off: the same requests and the same sleeps as the pre-pacer path, no DB statement, no state;
  * non-Shopify hosts are never paced by the shared budget, flag on or off;
  * a host is paced once known Shopify-served (marked, or learned from its response headers);
  * one DB round-trip per LEASE of N slots, not per request;
  * fail open: a failing or hanging lease logs at ERROR and paces locally at the fallback rate, and
    the database is not retried for 30s;
  * two processes share ONE budget.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import subprocess
import sys
import textwrap
import time
from typing import Any, Dict, List, Optional

import httpx
import pytest

import services.crawl_politeness as cp
import services.shopify_edge_pacer as sep
from services import external_offers_service as eos
from services import external_seed_destination_liveness as live

REPO = pathlib.Path(__file__).resolve().parent.parent
_REAL_ASYNC_CLIENT = httpx.AsyncClient

SHOPIFY = {"x-shopid": "5501", "content-type": "text/html"}
PLAIN = {"server": "nginx", "content-type": "text/html"}


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    cp.reset_for_tests()
    sep.reset_for_tests()
    for name in (sep.ENABLED_ENV, sep.RATE_ENV, sep.LEASE_ENV, sep.FALLBACK_RATE_ENV,
                 sep.DB_TIMEOUT_ENV, "CRAWL_MAX_WAIT_SECONDS", "CRAWL_MAX_BACKOFF_SECONDS",
                 "CRAWL_BACKOFF_BASE_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CRAWL_ROBOTS_ENABLED", "false")
    monkeypatch.setenv("CRAWL_MIN_INTERVAL_SECONDS", "1.0")
    yield
    cp.reset_for_tests()
    sep.reset_for_tests()


class Clock:
    """One virtual clock for crawl_politeness AND the pacer; sleeping advances it."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: List[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, d: float) -> None:
        self.sleeps.append(round(d, 6))
        if d > 0:
            self.now += d
        await asyncio.sleep(0)


def _install_clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()

    class _Time:
        monotonic = staticmethod(clock.monotonic)

        def __getattr__(self, name: str) -> Any:
            return getattr(time, name)

    class _Asyncio:
        sleep = staticmethod(clock.sleep)

        def __getattr__(self, name: str) -> Any:
            return getattr(asyncio, name)

    # Swap the module REFERENCES, never the global modules (see tests/test_crawl_politeness.py).
    monkeypatch.setattr(cp, "time", _Time())
    monkeypatch.setattr(cp, "asyncio", _Asyncio())
    sep._monotonic = clock.monotonic
    sep._sleep = clock.sleep
    return clock


class Leases:
    """A stand-in for db/crawl_egress_pacer.lease_slots with the SAME arithmetic, on the virtual
    clock. Counts round-trips."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.next_free = 0.0
        self.calls: List[int] = []

    async def __call__(self, bucket: str, *, slots: int, rate_per_s: float, horizon_s: float):
        self.calls.append(slots)
        now = self.clock.now
        span = slots / rate_per_s
        self.next_free = max(min(self.next_free, now + horizon_s), now) + span
        return self.next_free - span, now


def _mock_http(monkeypatch: pytest.MonkeyPatch, clock: Optional[Clock], headers_for) -> List[Any]:
    """Stub the HTTP layer `_fetch_html` builds its client on. Records (host, clock) per request."""
    seen: List[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        seen.append((host, clock.now if clock else None))
        status, headers = headers_for(host)
        return httpx.Response(status, headers=headers, text="<html><title>x</title></html>")

    def factory(*a: Any, **kw: Any) -> httpx.AsyncClient:
        kw["transport"] = httpx.MockTransport(handler)
        return _REAL_ASYNC_CLIENT(*a, **kw)

    monkeypatch.setattr(eos.httpx, "AsyncClient", factory)
    return seen


async def _fetch_all(urls: List[str]) -> None:
    for url in urls:
        try:
            await eos._fetch_html(url, max_wait=0)
        except eos.ExternalOfferUnavailable:
            pass


def _explode_lease(*a: Any, **k: Any):
    raise AssertionError("the shared budget must not be consulted here")


# ── flag off: the old code path ─────────────────────────────────────────────────────────────


def _scenario_urls() -> List[str]:
    return [
        "https://shop-a.example/products/1", "https://plain.example/p/1",
        "https://shop-a.example/products/2", "https://shop-a.example/products/3",
        "https://plain.example/p/2", "https://shop-a.example/products/4",
    ]


def _scenario_headers(host: str):
    # shop-a answers Shopify headers and one 429 mid-run, so the backoff path is in the trace too.
    return (200, SHOPIFY) if host == "shop-a.example" else (200, PLAIN)


async def _run_scenario(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    cp.reset_for_tests()
    sep.reset_for_tests()
    clock = _install_clock(monkeypatch)
    sep._lease_fn = _explode_lease
    seen = _mock_http(monkeypatch, clock, _scenario_headers)
    await _fetch_all(_scenario_urls())
    return {"requests": seen, "sleeps": list(clock.sleeps)}


async def test_flag_off_is_the_pre_pacer_path_exactly(monkeypatch: pytest.MonkeyPatch) -> None:
    flag_off = await _run_scenario(monkeypatch)

    # The pre-pacer code path: no second gate, and no header learning at the fetch sites.
    monkeypatch.setattr(cp, "_shopify_edge", lambda url: None)
    monkeypatch.setattr(sep, "learn_from_response", lambda url, headers: False)
    baseline = await _run_scenario(monkeypatch)

    assert flag_off == baseline
    assert sep.stats()["known_hosts"] == 0
    assert {k: v for k, v in sep.stats().items() if k in sep._zero_stats()} == sep._zero_stats()


@pytest.mark.parametrize("value", ["", "0", "false", "off", "no", "enabled", "tru", " 2 "])
async def test_only_a_truthy_flag_turns_it_on(monkeypatch, value) -> None:
    monkeypatch.setenv(sep.ENABLED_ENV, value)
    clock = _install_clock(monkeypatch)
    sep._lease_fn = _explode_lease
    _mock_http(monkeypatch, clock, lambda h: (200, SHOPIFY))
    sep.mark_shopify_host("shop-a.example")
    await _fetch_all(["https://shop-a.example/products/1", "https://shop-a.example/products/2"])
    assert not sep.enabled()
    assert sep.stats()["known_hosts"] == 0


@pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
def test_truthy_spellings_turn_it_on(monkeypatch, value) -> None:
    monkeypatch.setenv(sep.ENABLED_ENV, value)
    assert sep.enabled()


async def test_the_refresh_summary_is_unchanged_while_dark(monkeypatch) -> None:
    import jobs.external_referral_refresh as job

    async def fake_batch(**kwargs):
        return {"status": "success", "rows": 3}

    monkeypatch.setattr(job, "run_external_referral_refresh_batch", fake_batch)
    assert await job.run_daily_external_referral_refresh(limit=3) == {"status": "success", "rows": 3}
    monkeypatch.setenv(sep.ENABLED_ENV, "1")
    lit = await job.run_daily_external_referral_refresh(limit=3)
    assert lit["shopify_edge_pacer"]["enabled"] is True


async def test_the_destination_sweep_summary_is_unchanged_while_dark(monkeypatch) -> None:
    import jobs.external_seed_destination_sweep as job

    async def fake_sweep(**kwargs):
        return {"status": "success", "probed": 0}

    monkeypatch.setattr(job, "run_destination_sweep", fake_sweep)
    monkeypatch.setattr(job, "coverage_alarm", lambda s: None)
    assert await job.run_daily_destination_sweep(limit=1) == {"status": "success", "probed": 0}
    monkeypatch.setenv(sep.ENABLED_ENV, "1")
    assert "shopify_edge_pacer" in await job.run_daily_destination_sweep(limit=1)


# ── flag on ─────────────────────────────────────────────────────────────────────────────────


async def test_non_shopify_hosts_are_not_paced_by_the_shared_budget(monkeypatch) -> None:
    baseline = await _run_plain(monkeypatch, flag=False)
    lit = await _run_plain(monkeypatch, flag=True)
    assert lit == baseline
    assert sep.stats()["granted"] == 0 and sep.stats()["known_hosts"] == 0


async def _run_plain(monkeypatch, *, flag: bool):
    cp.reset_for_tests()
    sep.reset_for_tests()
    if flag:
        monkeypatch.setenv(sep.ENABLED_ENV, "1")
    else:
        monkeypatch.delenv(sep.ENABLED_ENV, raising=False)
    monkeypatch.setenv(sep.RATE_ENV, "0.1")  # a budget that would show in every sleep if it bit
    clock = _install_clock(monkeypatch)
    sep._lease_fn = _explode_lease
    seen = _mock_http(monkeypatch, clock, lambda h: (200, PLAIN))
    await _fetch_all([f"https://plain{i % 3}.example/p/{i}" for i in range(9)])
    return seen, list(clock.sleeps)


async def test_a_shopify_host_is_learned_from_its_response_then_paced(monkeypatch) -> None:
    monkeypatch.setenv(sep.ENABLED_ENV, "1")
    monkeypatch.setenv(sep.RATE_ENV, "0.5")  # one request every 2s across all Shopify hosts
    monkeypatch.setenv("CRAWL_MIN_INTERVAL_SECONDS", "0")
    clock = _install_clock(monkeypatch)
    leases = Leases(clock)
    sep._lease_fn = leases
    seen = _mock_http(monkeypatch, clock, lambda h: (200, SHOPIFY))

    await _fetch_all(["https://shop-a.example/products/0"])
    assert leases.calls == [] and sep.is_shopify_host("shop-a.example"), (
        "the first request to an unknown host is not paced by the shared budget; its answer "
        "teaches us the host is Shopify-served"
    )
    await _fetch_all([f"https://shop-a.example/products/{i}" for i in range(1, 6)])
    times = [t for _h, t in seen[1:]]
    gaps = [round(b - a, 6) for a, b in zip(times, times[1:])]
    assert gaps and min(gaps) >= 2.0, gaps
    assert leases.calls, "a paced request leased from the shared budget"


async def test_a_429_from_the_edge_still_teaches_the_host(monkeypatch) -> None:
    monkeypatch.setenv(sep.ENABLED_ENV, "1")
    _install_clock(monkeypatch)
    _mock_http(monkeypatch, None, lambda h: (429, {"x-shopify-stage": "production"}))
    await _fetch_all(["https://shop-z.example/products/0"])
    assert sep.is_shopify_host("shop-z.example")


@pytest.mark.parametrize(
    "headers,expected",
    [
        ({"x-shopid": "1"}, True),
        ({"X-ShopId": "1"}, True),
        ({"x-shopify-stage": "production"}, True),
        ({"x-sorting-hat-shopid": "9"}, True),
        ({"x-storefront-renderer-rendered": "1"}, True),
        ({"powered-by": "Shopify"}, True),
        ({"Powered-By": "Shopify, Hydrogen"}, True),
        ({"powered-by": "WooCommerce"}, False),
        ({"server": "cloudflare"}, False),
        ({}, False),
        (None, False),
    ],
)
def test_shopify_header_evidence(headers, expected) -> None:
    assert sep.looks_shopify_served(headers) is expected
    if headers:
        assert sep.looks_shopify_served(httpx.Headers(headers)) is expected


async def test_a_busy_process_leases_about_a_second_of_demand_per_statement(monkeypatch) -> None:
    """100 back-to-back requests at 4 req/s: the lease grows with demand, so statements stay near
    one per second (not one per request), and never exceed the configured max lease."""
    for max_lease in ("10", "3"):
        cp.reset_for_tests()
        sep.reset_for_tests()
        monkeypatch.setenv(sep.ENABLED_ENV, "1")
        monkeypatch.setenv(sep.RATE_ENV, "4")
        monkeypatch.setenv(sep.LEASE_ENV, max_lease)
        monkeypatch.setenv("CRAWL_MIN_INTERVAL_SECONDS", "0")
        clock = _install_clock(monkeypatch)
        leases = Leases(clock)
        sep._lease_fn = leases
        _mock_http(monkeypatch, clock, lambda h: (200, SHOPIFY))
        hosts = [f"shop{i}.example" for i in range(10)]
        for host in hosts:
            sep.mark_shopify_host(host)
        await _fetch_all([f"https://{hosts[i % 10]}/products/{i}" for i in range(100)])
        elapsed = clock.now - 1000.0
        assert elapsed >= 99 / 4.0, "the 100 requests took the budget's time"
        assert max(leases.calls) <= int(max_lease)
        assert sum(leases.calls) - 100 <= int(max_lease), "at most one lease's tail left unused"
        # About one statement per second (the stated bound, max(1, rate / max_lease) per second),
        # not one per request.
        per_second = max(1.0, 4.0 / int(max_lease))
        assert len(leases.calls) <= elapsed * per_second + 3, (len(leases.calls), elapsed)
        assert sep.stats()["granted"] == 100


async def test_a_low_demand_process_leases_one_slot_at_a_time_and_wastes_none(monkeypatch) -> None:
    """The P1 the fixed lease had: a caller spacing its own requests 3s apart (the purchasability
    sweep) reserved 10 slots and used ~3, pushing every other process back."""
    monkeypatch.setenv(sep.ENABLED_ENV, "1")
    monkeypatch.setenv(sep.RATE_ENV, "2")
    monkeypatch.setenv(sep.LEASE_ENV, "10")
    clock = _install_clock(monkeypatch)
    leases = Leases(clock)
    sep._lease_fn = leases
    for _ in range(20):
        await sep.acquire()
        clock.now += 3.0
    assert leases.calls == [1] * 20
    assert sep.stats()["expired_slots"] == 0


async def test_concurrent_callers_in_one_process_share_one_lease(monkeypatch) -> None:
    monkeypatch.setenv(sep.ENABLED_ENV, "1")
    monkeypatch.setenv(sep.LEASE_ENV, "10")
    clock = _install_clock(monkeypatch)
    gate = asyncio.Event()
    calls: List[int] = []

    async def slow_lease(bucket, *, slots, rate_per_s, **kw):
        calls.append(slots)
        await gate.wait()
        return clock.now, clock.now

    sep._lease_fn = slow_lease
    tasks = [asyncio.ensure_future(sep.acquire()) for _ in range(8)]
    await asyncio.sleep(0.01)
    assert calls == [1], "eight concurrent callers must not issue eight statements"
    gate.set()
    await asyncio.gather(*tasks)
    # The leader leased for itself; the seven that queued behind it are served by ONE more lease
    # sized for them.
    assert len(calls) == 2 and calls[1] >= 7 and sep.stats()["granted"] == 8


async def test_a_lease_leader_that_is_cancelled_does_not_strand_followers(monkeypatch) -> None:
    monkeypatch.setenv(sep.ENABLED_ENV, "1")
    clock = _install_clock(monkeypatch)
    first = asyncio.Event()
    n = {"calls": 0}

    async def lease(bucket, *, slots, rate_per_s, **kw):
        n["calls"] += 1
        if n["calls"] == 1:
            await first.wait()  # the leader hangs until cancelled
        return clock.now, clock.now

    sep._lease_fn = lease
    leader = asyncio.ensure_future(sep.acquire())
    await asyncio.sleep(0.01)
    follower = asyncio.ensure_future(sep.acquire())
    await asyncio.sleep(0.01)
    leader.cancel()
    await asyncio.wait_for(follower, timeout=2)
    assert n["calls"] == 2


async def test_slots_a_full_interval_stale_are_dropped_not_spent(monkeypatch) -> None:
    monkeypatch.setenv(sep.ENABLED_ENV, "1")
    monkeypatch.setenv(sep.RATE_ENV, "1")
    clock = _install_clock(monkeypatch)
    leases = Leases(clock)
    sep._lease_fn = leases
    await asyncio.gather(*(sep.acquire() for _ in range(4)))  # a burst of demand
    await sep.refill()  # ...and a lease sized for it, held unused
    held = len(sep._slots)
    assert held >= 1
    clock.now += 60  # idle for a minute: the held slots are now a burst nobody granted
    sleeps = len(clock.sleeps)
    await sep.acquire()
    assert sep.stats()["expired_slots"] == held
    assert clock.sleeps[sleeps:] == [], "a fresh lease starts now, not in the past and not later"


async def test_a_bounded_caller_is_refused_without_losing_the_slot(monkeypatch) -> None:
    monkeypatch.setenv(sep.ENABLED_ENV, "1")
    monkeypatch.setenv(sep.RATE_ENV, "0.1")  # 10s apart
    clock = _install_clock(monkeypatch)
    sep._lease_fn = Leases(clock)
    await sep.acquire()
    await sep.refill()  # hold the next slot locally, so the refusal is about ITS time
    with pytest.raises(sep.EdgePaced) as info:
        await sep.acquire(max_wait=5)
    assert isinstance(info.value, cp.CrawlPaced), "every caller already handles CrawlPaced"
    before = clock.now
    await sep.acquire()
    assert clock.now - before == pytest.approx(10.0), "the refused slot was the next one taken"


async def test_a_bounded_refusal_reserves_neither_slot(monkeypatch):
    """Through crawl_politeness: a bounded caller refused by the shared budget (EdgePaced, a
    CrawlPaced) leaves BOTH the host's slot and the shared slot for the next caller —
    `await_slot`'s contract that a refusal does not reserve."""
    monkeypatch.setenv(sep.ENABLED_ENV, "1")
    monkeypatch.setenv(sep.RATE_ENV, "0.1")  # 10s apart
    monkeypatch.setenv("CRAWL_MAX_WAIT_SECONDS", "5")
    monkeypatch.setenv("CRAWL_MIN_INTERVAL_SECONDS", "1")
    clock = _install_clock(monkeypatch)
    sep._lease_fn = Leases(clock)
    sep.mark_shopify_host("shop-a.example")
    await cp.before_request("https://shop-a.example/a", user_agent="PivotaBot")
    clock.now += 1.0  # the host's own interval has passed; only the shared budget is binding
    await sep.refill()
    held = list(sep._slots)
    before = cp._STATE["shop-a.example"].next_allowed
    with pytest.raises(cp.CrawlPaced):
        await cp.before_request("https://shop-a.example/b", user_agent="PivotaBot")
    assert cp._STATE["shop-a.example"].next_allowed == before, "the host slot was not reserved"
    assert list(sep._slots) == held, "the shared slot was not taken"
    await cp.before_request("https://shop-a.example/c", user_agent="PivotaBot", max_wait=0)
    assert clock.now == pytest.approx(held[0])


async def test_a_bounded_caller_within_patience_starts_at_the_later_of_both(monkeypatch):
    monkeypatch.setenv(sep.ENABLED_ENV, "1")
    monkeypatch.setenv(sep.RATE_ENV, "0.25")  # 4s apart
    monkeypatch.setenv("CRAWL_MIN_INTERVAL_SECONDS", "1")
    clock = _install_clock(monkeypatch)
    sep._lease_fn = Leases(clock)
    sep.mark_shopify_host("shop-a.example")
    await cp.before_request("https://shop-a.example/a", user_agent="PivotaBot")
    await cp.before_request("https://shop-a.example/b", user_agent="PivotaBot")  # 4s <= 10s
    assert clock.now - 1000.0 == pytest.approx(4.0)
    assert cp._STATE["shop-a.example"].next_allowed == pytest.approx(clock.now + 1.0)


async def test_an_unbounded_callers_host_interval_runs_from_its_real_start(monkeypatch):
    """A shared wait that pushes a request past its host slot moves the host's next slot too, so
    two requests to one host are never closer than the host interval."""
    monkeypatch.setenv(sep.ENABLED_ENV, "1")
    monkeypatch.setenv(sep.RATE_ENV, "10")
    monkeypatch.setenv("CRAWL_MIN_INTERVAL_SECONDS", "3")
    clock = _install_clock(monkeypatch)
    leases = Leases(clock)
    leases.next_free = clock.now + 2.5  # other processes hold the next 2.5s of the budget
    sep._lease_fn = leases
    seen = _mock_http(monkeypatch, clock, lambda h: (200, SHOPIFY))
    sep.mark_shopify_host("shop-a.example")
    await _fetch_all(["https://shop-a.example/products/1", "https://shop-a.example/products/2"])
    (_h1, t1), (_h2, t2) = seen
    assert t1 - 1000.0 == pytest.approx(2.5)
    assert t2 - t1 >= 3.0 - 1e-9, (t1, t2)


async def test_the_host_interval_and_the_shared_budget_compose_as_the_later_of_the_two(monkeypatch):
    monkeypatch.setenv(sep.ENABLED_ENV, "1")
    monkeypatch.setenv(sep.RATE_ENV, "2")          # shared: 0.5s
    monkeypatch.setenv("CRAWL_MIN_INTERVAL_SECONDS", "3")  # per host: 3s
    clock = _install_clock(monkeypatch)
    sep._lease_fn = Leases(clock)
    seen = _mock_http(monkeypatch, clock, lambda h: (200, SHOPIFY))
    sep.mark_shopify_host("shop-a.example")
    await _fetch_all([f"https://shop-a.example/products/{i}" for i in range(4)])
    times = [t for _h, t in seen]
    assert [round(b - a, 6) for a, b in zip(times, times[1:])] == [3.0, 3.0, 3.0]


# ── fail open ───────────────────────────────────────────────────────────────────────────────


async def test_a_failing_budget_fails_open_to_the_local_rate_loudly(monkeypatch, caplog) -> None:
    monkeypatch.setenv(sep.ENABLED_ENV, "1")
    monkeypatch.setenv(sep.RATE_ENV, "2")  # fallback = a quarter: 0.5 req/s, 2s apart
    monkeypatch.setenv(sep.LEASE_ENV, "5")
    monkeypatch.setenv("CRAWL_MIN_INTERVAL_SECONDS", "0")
    clock = _install_clock(monkeypatch)
    attempts: List[float] = []

    async def down(bucket, *, slots, rate_per_s, **kw):
        attempts.append(clock.now)
        raise ConnectionRefusedError("pivota-pg unreachable")

    sep._lease_fn = down
    seen = _mock_http(monkeypatch, clock, lambda h: (200, SHOPIFY))
    sep.mark_shopify_host("shop-a.example")
    with caplog.at_level(logging.ERROR, logger=sep.logger.name):
        await _fetch_all([f"https://shop-a.example/products/{i}" for i in range(25)])

    assert len(seen) == 25, "the crawl did not stop"
    times = [t for _h, t in seen]
    assert min(b - a for a, b in zip(times, times[1:])) >= 2.0 - 1e-9, "never unpaced"
    assert any("unreachable" in r.getMessage() and r.levelno == logging.ERROR for r in caplog.records)
    # 25 requests at 2s = 48s, one slot per lease (this caller's demand): 25 refills, but the DB
    # is asked only twice — at the start and once its 30s retry window has passed.
    assert attempts == [1000.0, 1030.0]
    assert sep.stats()["db_errors"] == 2 and sep.stats()["leases"] == 0


async def test_a_hanging_budget_times_out_and_fails_open(monkeypatch, caplog) -> None:
    monkeypatch.setenv(sep.ENABLED_ENV, "1")
    monkeypatch.setenv(sep.DB_TIMEOUT_ENV, "0.05")
    _install_clock(monkeypatch)

    async def hang(bucket, *, slots, rate_per_s, **kw):
        await asyncio.Event().wait()

    sep._lease_fn = hang
    with caplog.at_level(logging.ERROR, logger=sep.logger.name):
        await asyncio.wait_for(sep.acquire(), timeout=2)
    assert sep.stats()["db_errors"] == 1 and sep.stats()["local_slots"] > 0


async def test_the_real_db_lease_failing_fails_open(monkeypatch) -> None:
    """The real lease function (db/crawl_egress_pacer.lease_slots), its statement failing."""
    import db.crawl_egress_pacer as table
    from db.database import database

    monkeypatch.setenv(sep.ENABLED_ENV, "1")
    _install_clock(monkeypatch)
    table.reset_for_tests(ready=True)

    async def refuse(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(database, "fetch_one", refuse)
    try:
        await sep.acquire()
    finally:
        table.reset_for_tests()
    assert sep.stats()["db_errors"] == 1 and sep.stats()["local_slots"] > 0


# ── configuration guards ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [
    (None, 2.0), ("3", 3.0), ("0", 2.0), ("-1", 2.0), ("nan", 2.0), ("inf", 2.0), ("x", 2.0),
    ("0.001", 0.1), ("0.1", 0.1), ("20", 20.0), ("1e9", 20.0),
])
def test_rate_parsing(monkeypatch, raw, expected) -> None:
    if raw is None:
        monkeypatch.delenv(sep.RATE_ENV, raising=False)
    else:
        monkeypatch.setenv(sep.RATE_ENV, raw)
    assert sep.rate() == expected


@pytest.mark.parametrize("raw,expected", [
    (None, 10), ("0", 1), ("1", 1), ("2", 2), ("20", 20), ("21", 20), ("500", 20), ("x", 10),
    ("nan", 10), ("15.9", 15),
])
def test_lease_parsing(monkeypatch, raw, expected) -> None:
    if raw is None:
        monkeypatch.delenv(sep.LEASE_ENV, raising=False)
    else:
        monkeypatch.setenv(sep.LEASE_ENV, raw)
    assert sep.lease_size() == expected


def test_the_fallback_is_a_quarter_of_the_rate_and_never_faster(monkeypatch) -> None:
    monkeypatch.setenv(sep.RATE_ENV, "2")
    assert sep.fallback_rate() == 0.5
    monkeypatch.setenv(sep.FALLBACK_RATE_ENV, "1")
    assert sep.fallback_rate() == 1.0
    monkeypatch.setenv(sep.FALLBACK_RATE_ENV, "9")
    assert sep.fallback_rate() == 2.0, "failing open must never be a way to crawl faster"


def test_the_host_cache_is_bounded(monkeypatch) -> None:
    monkeypatch.setenv(sep.ENABLED_ENV, "1")
    monkeypatch.setattr(sep, "_MAX_KNOWN_HOSTS", 3)
    for i in range(5):
        sep.mark_shopify_host(f"h{i}.example")
    assert len(sep._known) <= 3


def test_mark_accepts_a_url_or_a_host(monkeypatch) -> None:
    monkeypatch.setenv(sep.ENABLED_ENV, "1")
    sep.mark_shopify_host("https://Shop-B.example/products/x")
    sep.mark_shopify_host(" SHOP-C.example ")
    assert sep.is_shopify_host("shop-b.example") and sep.is_shopify_host("shop-c.example")


# ── the destination sweep's fetch ────────────────────────────────────────────────────────────


async def test_the_destination_probe_learns_and_is_paced(monkeypatch) -> None:
    monkeypatch.setenv(sep.ENABLED_ENV, "1")
    monkeypatch.setenv(sep.RATE_ENV, "0.5")
    monkeypatch.setenv("CRAWL_MIN_INTERVAL_SECONDS", "0")
    clock = _install_clock(monkeypatch)
    leases = Leases(clock)
    sep._lease_fn = leases
    starts: List[float] = []

    def handler(request):
        starts.append(clock.now)
        return httpx.Response(200, headers=SHOPIFY, text="<html></html>")

    async with _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler)) as client:
        for i in range(4):
            await live.probe_destination(client, f"https://shop-d.example/products/{i}")
    gaps = [b - a for a, b in zip(starts[1:], starts[2:])]
    assert sep.is_shopify_host("shop-d.example") and leases.calls and min(gaps) >= 2.0


# ── Tier B and the purchasability sweep ─────────────────────────────────────────────────────


async def _tierb_run(monkeypatch, *, flag: bool):
    import jobs.tierb_cart_link_eligibility as tierb
    from services.tierb_cart_link_merchants import Merchant

    cp.reset_for_tests()
    sep.reset_for_tests()
    if flag:
        monkeypatch.setenv(sep.ENABLED_ENV, "1")
    else:
        monkeypatch.delenv(sep.ENABLED_ENV, raising=False)
    monkeypatch.setenv(sep.RATE_ENV, "0.25")  # 4s apart: slower than the job's own 1.5s
    clock = _install_clock(monkeypatch)
    leases = Leases(clock)
    sep._lease_fn = leases if flag else _explode_lease
    starts: List[float] = []

    def handler(request):
        starts.append(clock.now)
        return httpx.Response(200, headers=SHOPIFY)

    async def fake_preflight(domain, *, client, **kwargs):
        for path in ("/products.json", "/cart/1:1"):
            await client.get(f"https://{domain}{path}")
        from services.shopify_cart_link_preflight import PreflightResult, Verdict

        return PreflightResult(host=domain, verdict=next(iter(Verdict)), retryable=False)

    merchants = [
        Merchant(domain=f"m{i}.example", market="US", variant_id="1", product_handle="h")
        for i in range(2)
    ]
    await tierb.run(
        dry_run=True, merchants=merchants, preflight_fn=fake_preflight,
        transport=httpx.MockTransport(handler), clock=clock.monotonic, sleep=clock.sleep,
        environ={tierb.GATE_ENV: "1"}, emit=lambda line: None, concurrency=1,
    )
    return starts, leases


async def test_tierb_takes_shared_slots_on_top_of_its_own_spacing(monkeypatch) -> None:
    starts, leases = await _tierb_run(monkeypatch, flag=True)
    assert len(starts) == 4 and leases.calls
    assert min(b - a for a, b in zip(starts, starts[1:])) >= 4.0 - 1e-9


async def test_tierb_with_the_flag_off_keeps_its_own_spacing_only(monkeypatch) -> None:
    starts, _ = await _tierb_run(monkeypatch, flag=False)
    assert [round(b - a, 6) for a, b in zip(starts, starts[1:])] == [1.5, 1.5, 1.5]


async def test_a_shared_wait_past_the_tierb_deadline_is_budget_exhausted(monkeypatch) -> None:
    import jobs.tierb_cart_link_eligibility as tierb

    monkeypatch.setenv(sep.ENABLED_ENV, "1")
    monkeypatch.setenv(sep.RATE_ENV, "0.1")  # 10s apart
    clock = _install_clock(monkeypatch)
    sep._lease_fn = Leases(clock)
    pacer = tierb.RequestPacer(clock=clock.monotonic, sleep=clock.sleep, deadline=clock.now + 5)
    transport = tierb.PacedTransport(
        httpx.MockTransport(lambda r: httpx.Response(200)), pacer, shopify_edge=True
    )
    await transport.handle_async_request(httpx.Request("GET", "https://m.example/"))
    with pytest.raises(tierb.BudgetExhausted):
        await transport.handle_async_request(httpx.Request("GET", "https://m.example/"))
    assert len(pacer.starts) == 1, "a start refused by the shared budget is not a request"
    assert clock.now - 1000.0 < 5, "it did not sleep toward a slot past the deadline"


async def test_the_sweep_paces_only_the_crawl_egress_vantage(monkeypatch) -> None:
    import jobs.merchant_purchasability_sweep as sweep

    monkeypatch.setenv(sep.ENABLED_ENV, "1")
    monkeypatch.setenv(sep.RATE_ENV, "100")
    clock = _install_clock(monkeypatch)
    sep._lease_fn = Leases(clock)
    monkeypatch.setattr(sweep, "is_enabled", lambda: True)
    monkeypatch.setattr(sweep, "proxy_url", lambda: "http://vantage-proxy.example:3128")
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_PAUSE_MS", "0")

    async def population(batch, *, tally, sizes):
        return [sweep.Target(domain="m.example", market="US", variant_id="1")]

    monkeypatch.setattr(sweep, "load_population", population)
    monkeypatch.setattr(
        sweep, "_inner_transport",
        lambda via: httpx.MockTransport(lambda r: httpx.Response(200, headers=SHOPIFY)),
    )
    monkeypatch.setattr(sweep, "_new_pacer", lambda: sweep.RequestPacer(
        clock=clock.monotonic, sleep=clock.sleep))
    per_vantage: Dict[str, int] = {}

    async def fake_preflight(domain, *, client, **kwargs):
        before = sep.stats()["granted"]
        for _ in range(3):
            await client.get(f"https://{domain}/products.json")
        # The sweep's outermost transport is the IP-throttle watcher; the pacer is inside it.
        per_vantage[str(client._transport._inner._shopify_edge)] = sep.stats()["granted"] - before
        return None

    monkeypatch.setattr(sweep, "_preflight", fake_preflight)
    await sweep.run_merchant_purchasability_sweep()
    assert per_vantage == {"True": 3, "False": 0}


# ── two processes, one budget (SQLite file; the Postgres twin runs the same on pivota-pg's dialect) ──

_CHILD = textwrap.dedent(
    """
    import asyncio, json, os, sys, time
    sys.path.insert(0, {repo!r})
    import httpx
    from db.database import database
    from services import external_offers_service as eos
    from services import shopify_edge_pacer as sep

    starts = []
    windows = []
    real = httpx.AsyncClient

    def handler(request):
        starts.append(time.time())
        return httpx.Response(200, headers={{"x-shopid": "1"}}, text="<html></html>")

    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real(*a, **kw)

    eos.httpx.AsyncClient = factory

    async def main():
        await database.connect()
        hosts = [f"shop{{i}}.example" for i in range(6)]
        for h in hosts:
            sep.mark_shopify_host(h)
        real_lease = sep._lease_fn

        async def recording(bucket, **kw):
            start, db_now = await real_lease(bucket, **kw)
            windows.append((start, start + kw["slots"] / kw["rate_per_s"]))
            return start, db_now

        sep._lease_fn = recording
        open(sys.argv[2], "w").close()
        while not os.path.exists(sys.argv[1]):
            await asyncio.sleep(0.01)
        for i in range(int(sys.argv[3])):
            await eos._fetch_html(f"https://{{hosts[i % 6]}}/products/p{{i}}", max_wait=0)
        await database.disconnect()
        print("RESULT " + json.dumps({{"starts": starts, "windows": windows, "stats": sep.stats()}}))

    asyncio.run(main())
    """
)


def run_two_processes(tmp_path: pathlib.Path, database_url: str, *, per_process: int = 20,
                      rate: float = 10.0, lease: int = 4) -> Dict[str, Any]:
    """Two OS processes, one database, the REAL `_fetch_html` -> crawl_politeness -> pacer -> DB
    lease path in each. Returns every request start (wall clock) and each child's stats."""
    go, script = tmp_path / "go", tmp_path / "child.py"
    script.write_text(_CHILD.format(repo=str(REPO)))
    env = dict(os.environ)
    env.update({
        "DATABASE_URL": database_url, sep.ENABLED_ENV: "1", sep.RATE_ENV: str(rate),
        sep.LEASE_ENV: str(lease), "CRAWL_ROBOTS_ENABLED": "false",
        "CRAWL_MIN_INTERVAL_SECONDS": "0", "PYTHONDONTWRITEBYTECODE": "1",
    })
    procs = []
    for i in range(2):
        ready = tmp_path / f"ready{i}"
        procs.append((ready, subprocess.Popen(
            [sys.executable, str(script), str(go), str(ready), str(per_process)],
            cwd=str(REPO), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )))
    deadline = time.time() + 120
    while not all(r.exists() for r, _p in procs):
        if time.time() > deadline or any(p.poll() is not None for _r, p in procs):
            errs = [p.stderr.read()[-2000:] for _r, p in procs if p.poll() is not None]
            raise AssertionError(f"a child did not get ready: {errs}")
        time.sleep(0.05)
    go.touch()
    results = []
    for _r, p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, err[-3000:]
        line = [ln for ln in out.splitlines() if ln.startswith("RESULT ")][-1]
        results.append(json.loads(line[len("RESULT "):]))
    return {"results": results, "rate": rate, "lease": lease, "per_process": per_process}


def assert_one_shared_budget(run: Dict[str, Any]) -> None:
    rate, n = run["rate"], run["per_process"]
    a, b = (r["starts"] for r in run["results"])
    assert len(a) == n and len(b) == n
    merged = sorted(a + b)
    # Two processes each pacing alone would finish 2n requests in ~(n - 1) / rate seconds. One
    # shared budget needs ~(2n - 1) / rate. Allow one slot of slack for the lease-edge grace.
    assert merged[-1] - merged[0] >= (2 * n - 3) / rate, (merged[-1] - merged[0])
    # The budget itself, exactly, on the DB's own clock: the windows the two processes leased on
    # the shared timeline never overlap, so no stretch of it holds more than rate x width + 1 slots.
    # (Not a wall-clock window on request starts: an event-loop or GC stall wakes overdue requests
    # together, which says nothing about the schedule and flakes under load.)
    windows = sorted(w for r in run["results"] for w in r["windows"])
    overlaps = [(x, y) for x, y in zip(windows, windows[1:]) if y[0] < x[1] - 1e-4]
    assert not overlaps, overlaps[:3]
    # They really interleaved: each process started some of its requests while the other ran.
    assert min(b) < max(a) and min(a) < max(b)
    for r in run["results"]:
        stats = r["stats"]
        assert stats["db_errors"] == 0, stats
        # Leases are sized by demand: a serial caller getting ~rate/2 req/s leases several slots
        # per statement, so well under one statement per request, and no slot beyond its max.
        assert stats["leases"] <= n // 2 + 1, stats
        assert stats["leased_slots"] - n <= run["lease"], stats


def test_two_processes_share_one_budget_sqlite(tmp_path: pathlib.Path) -> None:
    run = run_two_processes(tmp_path, f"sqlite+aiosqlite:///{tmp_path / 'pacer.db'}")
    assert_one_shared_budget(run)


# ── the table: migration parity and the lease statement ─────────────────────────────────────

_MIGRATIONS = REPO / "db" / "migrations"


def _statement_body(sql: str) -> str:
    lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    return " ".join(" ".join(lines).split()).rstrip(";").strip()


def test_the_self_heal_create_is_the_migrations_create() -> None:
    import db.crawl_egress_pacer as table

    migration = _statement_body((_MIGRATIONS / "251_crawl_egress_pacer.sql").read_text())
    assert migration.startswith("CREATE TABLE IF NOT EXISTS crawl_egress_pacer (")
    assert _statement_body(table._CREATE) == migration


def test_the_down_drops_only_this_table() -> None:
    down = _statement_body((_MIGRATIONS / "down" / "251_crawl_egress_pacer_down.sql").read_text())
    assert down == "DROP TABLE IF EXISTS crawl_egress_pacer"


def test_no_other_migration_touches_the_table() -> None:
    touching = sorted(
        p.name for p in _MIGRATIONS.glob("*.sql")
        if "crawl_egress_pacer" in p.read_text() and p.name != "251_crawl_egress_pacer.sql"
    )
    assert touching == []


# The lease statement reads the DB clock (`clock_timestamp()` / `julianday('now')`) more than once:
# the schedule is computed from one reading and `db_now` is RETURNed from a later one, so on a loaded
# runner the two differ by however long the statement took. Every "starts at now" check below is
# against a failure that is off by a whole lease span (>= 1 s) or by the poisoned 1e6 s, so half a
# second tells them apart without timing the database.
_DB_CLOCK_SLACK = 0.5


async def test_the_lease_statement_has_no_stored_burst_and_heals_a_poisoned_row(tmp_path, monkeypatch) -> None:
    """The real SQL on the hermetic dialect: consecutive leases abut, an idle bucket restarts at
    'now' (never in the past), and every lease is one round-trip that bumps `leases`."""
    import db.crawl_egress_pacer as table
    from db.database import database

    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    try:
        await database.execute("DROP TABLE IF EXISTS crawl_egress_pacer")
        table.reset_for_tests()
        start1, now1 = await table.lease_slots("t_bucket", slots=4, rate_per_s=2.0, horizon_s=60)
        start2, _now2 = await table.lease_slots("t_bucket", slots=4, rate_per_s=2.0, horizon_s=60)
        assert start1 == pytest.approx(now1, abs=_DB_CLOCK_SLACK)
        assert start2 == pytest.approx(start1 + 2.0, abs=1e-6), "leases abut; no overlap"
        row = await database.fetch_one(
            "SELECT leases FROM crawl_egress_pacer WHERE bucket = 't_bucket'")
        assert int(row[0]) == 2
        await database.execute(
            "UPDATE crawl_egress_pacer SET next_free_epoch = 0 WHERE bucket = 't_bucket'")
        start3, now3 = await table.lease_slots("t_bucket", slots=1, rate_per_s=2.0, horizon_s=60)
        assert start3 >= now3 - _DB_CLOCK_SLACK, "an idle bucket hands out slots from now, not from the past"
        # POISONED: a typo'd rate or a clock step pushed the schedule ~11 days out. The next
        # lease starts within the horizon, and the row is healed for everyone after it.
        await database.execute(
            "UPDATE crawl_egress_pacer SET next_free_epoch = next_free_epoch + 1000000 "
            "WHERE bucket = 't_bucket'")
        start4, now4 = await table.lease_slots("t_bucket", slots=2, rate_per_s=2.0, horizon_s=60)
        assert start4 == pytest.approx(now4, abs=_DB_CLOCK_SLACK), start4 - now4
        start5, _ = await table.lease_slots("t_bucket", slots=2, rate_per_s=2.0, horizon_s=60)
        assert start5 == pytest.approx(start4 + 1.0, abs=1e-6)
        for bad in (dict(slots=0, rate_per_s=2.0, horizon_s=60),
                    dict(slots=1, rate_per_s=0.0, horizon_s=60),
                    dict(slots=1, rate_per_s=2.0, horizon_s=0)):
            with pytest.raises(ValueError):
                await table.lease_slots("t_bucket", **bad)
    finally:
        await database.execute("DROP TABLE IF EXISTS crawl_egress_pacer")
        table.reset_for_tests()
        if not was_connected:
            await database.disconnect()


async def test_a_lost_create_race_does_not_cost_the_lease(monkeypatch) -> None:
    """Two jobs starting together both run the first-use CREATE; the loser's failure must not turn
    into a fail-open when the table exists."""
    import db.crawl_egress_pacer as table
    from db.database import database

    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    try:
        table.reset_for_tests()
        assert await table.ensure_table()

        async def lost_race() -> bool:
            return False

        monkeypatch.setattr(table, "ensure_table", lost_race)
        start, now = await table.lease_slots("race_bucket", slots=2, rate_per_s=1.0, horizon_s=60)
        assert start >= now - _DB_CLOCK_SLACK
    finally:
        await database.execute("DROP TABLE IF EXISTS crawl_egress_pacer")
        table.reset_for_tests()
        if not was_connected:
            await database.disconnect()


async def test_turning_the_flag_off_stops_pacing_a_known_host_at_once(monkeypatch) -> None:
    monkeypatch.setenv(sep.ENABLED_ENV, "1")
    clock = _install_clock(monkeypatch)
    sep._lease_fn = Leases(clock)
    sep.mark_shopify_host("shop-a.example")
    await sep.acquire_for_url("https://shop-a.example/x")
    monkeypatch.delenv(sep.ENABLED_ENV)
    sep._lease_fn = _explode_lease
    granted = sep.stats()["granted"]
    for _ in range(5):
        assert await sep.acquire_for_url("https://shop-a.example/x") == 0.0
    assert sep.stats()["granted"] == granted


async def test_a_db_lease_after_a_local_run_keeps_its_db_times(monkeypatch) -> None:
    """Recovery does not shift the DB's slots past the local fallback schedule: those windows
    belong to other processes, and moving a lease into them doubles the edge's rate for a lease."""
    monkeypatch.setenv(sep.ENABLED_ENV, "1")
    monkeypatch.setenv(sep.RATE_ENV, "1")
    monkeypatch.setenv(sep.FALLBACK_RATE_ENV, "0.1")  # local slots 10s apart
    clock = _install_clock(monkeypatch)
    healthy = Leases(clock)
    state = {"down": True}

    async def flaky(bucket, *, slots, rate_per_s, **kw):
        if state["down"]:
            state["down"] = False
            raise OSError("blip")
        return await healthy(bucket, slots=slots, rate_per_s=rate_per_s, **kw)

    sep._lease_fn = flaky
    await sep.acquire()  # local slot at 1000; the local schedule's next is 1010
    monkeypatch.setattr(sep, "_db_down_until", 0.0)  # the DB is back
    clock.now += 1.0
    await sep.acquire()
    assert clock.now == pytest.approx(1001.0), "the DB's slot starts at the DB's time"



async def test_the_destination_catalogue_read_learns_and_its_later_pages_are_paced(monkeypatch):
    monkeypatch.setenv(sep.ENABLED_ENV, "1")
    monkeypatch.setenv(sep.RATE_ENV, "0.5")
    monkeypatch.setenv("CRAWL_MIN_INTERVAL_SECONDS", "0")
    clock = _install_clock(monkeypatch)
    leases = Leases(clock)
    sep._lease_fn = leases
    starts: List[float] = []

    def handler(request):
        starts.append(clock.now)
        page = int(request.url.params.get("page", "1"))
        products = [{"id": page, "handle": f"h{page}", "title": f"t{page}"}] if page <= 3 else []
        return httpx.Response(200, headers={"x-shopid": "7"}, json={"products": products})

    async with _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler)) as client:
        read = await live.read_brand_catalogue(client, "shop-e.example")
    assert read.handles == {"h1", "h2", "h3"}
    assert sep.is_shopify_host("shop-e.example") and leases.calls
    assert min(b - a for a, b in zip(starts[1:], starts[2:])) >= 2.0


# ── bounded waits: a tiny rate, a DB clock jump, the horizon ────────────────────────────────


async def test_a_typod_tiny_rate_is_floored(monkeypatch) -> None:
    """`CRAWL_SHOPIFY_EDGE_RPS=0.001` would have been 1,000s per request, stored in the shared row
    for every later run; it is floored at 0.1 and the horizon bounds any wait."""
    monkeypatch.setenv(sep.ENABLED_ENV, "1")
    monkeypatch.setenv(sep.RATE_ENV, "0.001")
    clock = _install_clock(monkeypatch)
    leases = Leases(clock)
    sep._lease_fn = leases
    for _ in range(3):
        await sep.acquire()
    assert clock.now - 1000.0 == pytest.approx(20.0)  # 10s apart, not 1,000s


async def test_a_lease_past_the_horizon_fails_open(monkeypatch, caplog) -> None:
    """A DB clock step (or a row the SQL clamp did not heal) hands back a lease a day out: that is
    a failure, not a day-long sleep."""
    monkeypatch.setenv(sep.ENABLED_ENV, "1")
    clock = _install_clock(monkeypatch)

    async def jumped(bucket, *, slots, rate_per_s, horizon_s):
        return clock.now + 86_400.0, clock.now

    sep._lease_fn = jumped
    with caplog.at_level(logging.ERROR, logger=sep.logger.name):
        waited = await sep.acquire()
    assert waited < 1.0 and sep.stats()["db_errors"] == 1 and sep.stats()["local_slots"] >= 1
    assert any("horizon" in r.getMessage() for r in caplog.records)


async def test_the_horizon_is_passed_to_the_lease_and_bounded(monkeypatch) -> None:
    monkeypatch.setenv(sep.ENABLED_ENV, "1")
    clock = _install_clock(monkeypatch)
    seen = []

    async def lease(bucket, *, slots, rate_per_s, horizon_s):
        seen.append(horizon_s)
        return clock.now, clock.now

    sep._lease_fn = lease
    await sep.acquire()
    assert seen == [80.0]  # max(60, 16 * 10 / 2.0)
    monkeypatch.setenv(sep.RATE_ENV, "0.1")
    assert sep.horizon() == 1600.0
    monkeypatch.setenv(sep.LEASE_ENV, "20")
    assert sep.horizon() == 3200.0
    monkeypatch.setenv(sep.RATE_ENV, "20")
    monkeypatch.setenv(sep.LEASE_ENV, "10")
    assert sep.horizon() == 60.0
    monkeypatch.setenv(sep.RATE_ENV, "20")
    assert sep.horizon() == 60.0


async def test_learning_keys_on_the_host_that_answered(monkeypatch) -> None:
    monkeypatch.setenv(sep.ENABLED_ENV, "1")
    _install_clock(monkeypatch)

    def handler(request):
        if request.url.host == "shop-r.example":
            return httpx.Response(301, headers={"location": "https://www.shop-r.example/p"})
        return httpx.Response(200, headers=SHOPIFY, text="<html></html>")

    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return _REAL_ASYNC_CLIENT(*a, **kw)

    monkeypatch.setattr(eos.httpx, "AsyncClient", factory)
    await _fetch_all(["https://shop-r.example/p"])
    assert sep.is_shopify_host("www.shop-r.example")
    assert not sep.is_shopify_host("shop-r.example")


async def test_tierb_spaces_its_next_start_from_the_real_one(monkeypatch) -> None:
    """A request the shared budget held back went out LATER than the run's pacer granted it; the
    run's own 1.5s spacing is measured from when it really went out."""
    import jobs.tierb_cart_link_eligibility as tierb

    monkeypatch.setenv(sep.ENABLED_ENV, "1")
    monkeypatch.setenv(sep.RATE_ENV, "10")
    clock = _install_clock(monkeypatch)
    leases = Leases(clock)
    leases.next_free = clock.now + 1.0  # other processes hold the next second of the budget
    sep._lease_fn = leases
    starts: List[float] = []

    def handler(request):
        starts.append(clock.now)
        return httpx.Response(200)

    pacer = tierb.RequestPacer(clock=clock.monotonic, sleep=clock.sleep)
    transport = tierb.PacedTransport(httpx.MockTransport(handler), pacer, shopify_edge=True)
    for _ in range(2):
        await transport.handle_async_request(httpx.Request("GET", "https://m.example/"))
    assert starts[0] - 1000.0 == pytest.approx(1.0)
    assert starts[1] - starts[0] >= 1.5 - 1e-9, starts
    assert pacer.starts == starts


async def test_a_bounded_caller_rechecks_its_host_after_waiting_on_a_lease(monkeypatch):
    """While a bounded caller waits on a lease refill, another caller may reserve the same host far
    out. The bounded caller re-reads the host slot and is refused rather than sleeping past its
    patience."""
    monkeypatch.setenv(sep.ENABLED_ENV, "1")
    monkeypatch.setenv("CRAWL_MIN_INTERVAL_SECONDS", "30")
    clock = _install_clock(monkeypatch)
    gate = asyncio.Event()
    inner = Leases(clock)

    async def slow(bucket, **kw):
        await gate.wait()
        return await inner(bucket, **kw)

    sep._lease_fn = slow
    sep.mark_shopify_host("shop-a.example")
    bounded = asyncio.ensure_future(
        cp.before_request("https://shop-a.example/a", user_agent="PivotaBot", max_wait=10))
    await asyncio.sleep(0.01)  # the bounded caller is now waiting on the lease
    unbounded = asyncio.ensure_future(
        cp.before_request("https://shop-a.example/b", user_agent="PivotaBot", max_wait=0))
    await asyncio.sleep(0.01)  # ...and the unbounded one has reserved the host's next slot
    gate.set()
    with pytest.raises(cp.CrawlPaced):
        await bounded
    await unbounded
    assert clock.now - 1000.0 < 10.0



def _pacer_copy(name: str):
    """A second, independent copy of the pacer module: one "process" of a multi-process test."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, REPO / "services" / "shopify_edge_pacer.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_a_burst_of_concurrent_callers_stays_in_budget_without_healing(monkeypatch) -> None:
    """Four processes each bursting 50 concurrent callers. Without the lookahead cap every caller
    got a future slot at once, the shared backlog outran the horizon, and the SQL heal restarted the
    schedule underneath slots already handed out: 3.17 req/s against a 2.0 budget in the reviewer's
    simulation. Real time, scaled: rate 100/s, horizon floor 1.2s.

    ASSERTED ON THE SCHEDULE, NOT ON WAKE TIMES. At 100/s slots are 10ms apart, so any event-loop
    stall (a full GC on a 30k-test shard's heap takes ~320ms) wakes every overdue caller together; a
    wall-clock window check flaked 3 runs in 30. What the budget promises is the SCHEDULE: the
    leased windows on the shared timeline never overlap (so no stretch of it holds more than rate x
    width + 1 slots), no lease was healed on top of another, and the slots handed to callers span at
    least the budget's time. All exact, whatever the scheduler does.
    """
    import gc

    rate, per_process, procs = 100.0, 50, 4
    monkeypatch.setenv(sep.ENABLED_ENV, "1")
    monkeypatch.setenv(sep.RATE_ENV, str(rate))  # each copy's rate cap is raised to allow it
    db = {"next": 0.0, "heals": 0, "windows": []}

    async def lease(bucket, *, slots, rate_per_s, horizon_s):
        now = time.monotonic()
        span = slots / rate_per_s
        if db["next"] > now + horizon_s:
            db["heals"] += 1
            base = now
        else:
            base = max(db["next"], now)
        db["next"] = base + span
        db["windows"].append((base, base + span))
        return base, now

    handed: List[float] = []
    copies = []
    for i in range(procs):
        m = _pacer_copy(f"_edge_pacer_proc{i}")
        m._RATE_MAX = rate
        m._HORIZON_MIN = 1.2
        m._DEMAND_WINDOW = 0.02  # the 1s demand window at this 50x time scale
        m._lease_fn = lease
        real_take = m.take_nowait

        def take(*, max_wait=None, _real=real_take):
            slot = _real(max_wait=max_wait)
            if slot is not None:
                handed.append(slot)
            return slot

        m.take_nowait = take
        copies.append(m)

    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        await asyncio.wait_for(
            asyncio.gather(*(m.acquire() for m in copies for _ in range(per_process))), timeout=60)
    finally:
        if gc_was_enabled:
            gc.enable()

    total = procs * per_process
    assert db["heals"] == 0, db["heals"]
    windows = sorted(db["windows"])
    overlaps = [(a, b) for a, b in zip(windows, windows[1:]) if b[0] < a[1] - 1e-9]
    assert not overlaps, overlaps[:3]
    assert len(handed) == total
    handed.sort()
    assert handed[-1] - handed[0] >= (total - 3) / rate, handed[-1] - handed[0]



async def test_time_spent_on_a_refill_counts_against_a_bounded_callers_patience(monkeypatch):
    monkeypatch.setenv(sep.ENABLED_ENV, "1")
    clock = _install_clock(monkeypatch)

    async def slow(bucket, *, slots, rate_per_s, horizon_s):
        clock.now += 4.0  # a slow refill (or a lookahead hold)
        return clock.now + 2.0, clock.now  # ...then a slot 2s out: 6s after the call

    sep._lease_fn = slow
    with pytest.raises(sep.EdgePaced):
        await sep.acquire(max_wait=5.0)
    sep.reset_for_tests()
    clock = _install_clock(monkeypatch)
    sep._lease_fn = slow
    assert await sep.acquire(max_wait=7.0) == pytest.approx(2.0)


async def test_a_refill_counts_against_the_gates_bounded_patience(monkeypatch):
    monkeypatch.setenv(sep.ENABLED_ENV, "1")
    monkeypatch.setenv("CRAWL_MAX_WAIT_SECONDS", "5")
    monkeypatch.setenv("CRAWL_MIN_INTERVAL_SECONDS", "0")
    clock = _install_clock(monkeypatch)

    async def slow(bucket, *, slots, rate_per_s, horizon_s):
        clock.now += 4.0
        return clock.now + 2.0, clock.now

    sep._lease_fn = slow
    sep.mark_shopify_host("shop-a.example")
    with pytest.raises(cp.CrawlPaced):
        await cp.before_request("https://shop-a.example/a", user_agent="PivotaBot")


async def test_a_host_slot_taken_during_a_refill_is_judged_against_the_original_deadline(monkeypatch):
    """A bounded caller (5s) waits 4s on a refill while another caller reserves the host 8s out.
    Measured from now the host is 4s away (inside 5s); measured from the call it is 8s (outside).
    The patience is the call's, so it is refused."""
    monkeypatch.setenv(sep.ENABLED_ENV, "1")
    monkeypatch.setenv("CRAWL_MIN_INTERVAL_SECONDS", "8")
    clock = _install_clock(monkeypatch)
    gate = asyncio.Event()

    async def slow(bucket, *, slots, rate_per_s, horizon_s):
        await gate.wait()
        clock.now += 4.0
        return clock.now, clock.now

    sep._lease_fn = slow
    sep.mark_shopify_host("shop-a.example")
    bounded = asyncio.ensure_future(
        cp.before_request("https://shop-a.example/a", user_agent="PivotaBot", max_wait=5))
    await asyncio.sleep(0.01)
    unbounded = asyncio.ensure_future(
        cp.before_request("https://shop-a.example/b", user_agent="PivotaBot", max_wait=0))
    await asyncio.sleep(0.01)
    gate.set()
    with pytest.raises(cp.CrawlPaced):
        await bounded
    await unbounded
