"""Strict global pacing for the external-referral refresh, and the staged (cool-down) IP breaker.

2026-10-16: Cloudflare challenge-listed the crawl NAT address. Run tm899 (05:15Z) tripped the IP
breaker 37 s in with 391 `cf-mitigated: challenge` 429s and a peak of 262 distinct hosts in one
60 s window; the 14:00Z rerun zt5zz drew 359 challenges from 363 responses across 334 hosts.
`--host-concurrency 4` capped hosts in flight, but a challenged request answers instantly, so the
run swept hundreds of hosts a minute, and nothing capped the IP's total rate (10-08..10-15 nights
read 2.5-4.0 rows/s).

Driven here, with NO network: the pacer and breaker as units on a virtual clock, and the REAL batch
scheduler + the REAL fetch (`external_offers_service._fetch_html` -> `crawl_politeness` -> the
installed pacer and breaker) with only the HTTP layer stubbed (`httpx.MockTransport`).
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytest

import jobs.external_referral_refresh as job
import services.crawl_global_pacer as cgp
import services.crawl_ip_throttle as cit
import services.crawl_politeness as cp
import services.external_offers_service as svc
import services.external_referral_readiness as err

_REAL_ASYNC_CLIENT = httpx.AsyncClient

OK = (200, {"server": "nginx"})
CHALLENGE = (403, {"server": "cloudflare", "cf-mitigated": "challenge"})
CHALLENGE_429 = (429, {"server": "cloudflare", "cf-mitigated": "challenge"})
PLAIN_429 = (429, {"server": "nginx"})

_ENV = (
    "EXTERNAL_REFERRAL_REFRESH_GLOBAL_RPS",
    "EXTERNAL_REFERRAL_REFRESH_MAX_NEW_HOSTS_PER_MINUTE",
    "CRAWL_IP_CHALLENGE_TRIP_HOSTS",
    "CRAWL_IP_CHALLENGE_WINDOW_SECONDS",
    "CRAWL_IP_THROTTLE_COOLDOWN_SECONDS",
    "CRAWL_IP_THROTTLE_TRIP_HOSTS",
    "CRAWL_IP_THROTTLE_WINDOW_SECONDS",
    "CRAWL_IP_THROTTLE_BREAKER_ENABLED",
    "EXTERNAL_REFERRAL_REFRESH_HOST_BLOCK_TRIP",
    "EXTERNAL_REFERRAL_REFRESH_HOST_UNREACHABLE_TRIP",
    "EXTERNAL_REFERRAL_REFRESH_HOST_CONCURRENCY",
    "EXTERNAL_REFERRAL_REFRESH_MIN_ORIGIN_YIELD",
    "EXTERNAL_REFERRAL_REFRESH_BUDGET_SECONDS",
)


class VirtualClock:
    """One clock for the pacer and the breaker. `sleep(d)` wakes at `now + d`: the clock moves to
    that instant if it is not already past it, so a reserved start is honoured exactly."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: List[float] = []
        self.on_sleep = None

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        hook, self.on_sleep = self.on_sleep, None
        if hook is not None:
            hook()
        self.t = max(self.t, self.t + max(0.0, seconds))
        await asyncio.sleep(0)


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    cp.reset_for_tests()
    cit.reset_for_tests()
    cgp.reset_for_tests()
    monkeypatch.setenv("CRAWL_ROBOTS_ENABLED", "false")
    monkeypatch.setenv("CRAWL_MIN_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("CRAWL_BACKOFF_BASE_SECONDS", "0")
    monkeypatch.setenv("CRAWL_MAX_BACKOFF_SECONDS", "0")
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    yield
    cp.reset_for_tests()
    cit.reset_for_tests()
    cgp.reset_for_tests()


@pytest.fixture
def vclock(monkeypatch: pytest.MonkeyPatch) -> VirtualClock:
    clock = VirtualClock()
    monkeypatch.setattr(cgp, "_now", clock.now)
    monkeypatch.setattr(cgp, "_sleep", clock.sleep)
    monkeypatch.setattr(cit, "_now", clock.now)
    # Jitter pinned to its midpoint (a gap of exactly 1/rps) unless a test says otherwise.
    monkeypatch.setattr(cgp, "_random", lambda: 0.5)
    return clock


def _gaps(instants: List[float]) -> List[float]:
    ordered = sorted(instants)
    return [round(b - a, 6) for a, b in zip(ordered, ordered[1:])]


# ===================================================================== the token bucket


def test_the_bucket_spaces_every_start_across_concurrent_workers(vclock: VirtualClock) -> None:
    """Four workers, five requests each, on hosts already contacted: one shared bucket at 0.5/s
    means every start is exactly 2 s after the previous one, whichever worker took it."""
    pacer = cgp.GlobalOriginPacer(rps=0.5, max_new_hosts_per_minute=0)

    async def worker(index: int) -> None:
        for _ in range(5):
            await pacer.acquire(f"h{index}.example")

    async def run() -> None:
        await asyncio.gather(*(worker(i) for i in range(4)))

    asyncio.run(run())
    assert pacer.requests == 20 and len(pacer.grants) == 20
    assert _gaps(pacer.grants) == [2.0] * 19
    assert pacer.paces_waited_seconds > 0


def test_the_bucket_holds_in_real_time_across_concurrent_workers() -> None:
    """The same rule on the real event loop and the real clock, scaled down (40/s): the sleeps
    actually honour the reserved starts, so no two requests start closer than the lower jitter
    bound, across four concurrent workers."""
    pacer = cgp.GlobalOriginPacer(rps=40.0, max_new_hosts_per_minute=0, jitter=0.3)
    started: List[float] = []

    async def worker(index: int) -> None:
        for _ in range(4):
            await pacer.acquire(f"h{index}.example")
            started.append(time.monotonic())

    async def run() -> None:
        await asyncio.gather(*(worker(i) for i in range(4)))

    asyncio.run(run())
    assert len(started) == 16
    floor = (1 / 40.0) * 0.7
    assert min(_gaps(started)) >= floor - 0.004, _gaps(started)


@pytest.mark.parametrize("draw, factor", [(0.0, 0.7), (1.0, 1.3), (0.5, 1.0)])
def test_the_jitter_is_plus_or_minus_thirty_percent(monkeypatch, draw, factor) -> None:
    monkeypatch.setattr(cgp, "_random", lambda: draw)
    pacer = cgp.GlobalOriginPacer(rps=0.5, max_new_hosts_per_minute=0)
    assert pacer._gap() == pytest.approx(2.0 * factor)


def test_real_jitter_stays_in_bounds_and_averages_the_rate() -> None:
    pacer = cgp.GlobalOriginPacer(rps=0.5, max_new_hosts_per_minute=0)
    gaps = [pacer._gap() for _ in range(4000)]
    assert min(gaps) >= 1.4 - 1e-9 and max(gaps) <= 2.6 + 1e-9
    assert sum(gaps) / len(gaps) == pytest.approx(2.0, rel=0.03)
    assert max(gaps) - min(gaps) > 1.0, "the gaps really vary"


def test_a_jitter_of_one_or_more_cannot_allow_a_burst() -> None:
    assert cgp.GlobalOriginPacer(rps=1, max_new_hosts_per_minute=0, jitter=5).jitter == 0.9


def test_a_zero_rate_is_unpaced_and_says_so(vclock: VirtualClock) -> None:
    pacer = cgp.GlobalOriginPacer(rps=0, max_new_hosts_per_minute=0)

    async def run() -> None:
        for i in range(10):
            await pacer.acquire(f"h{i}.example")

    asyncio.run(run())
    assert vclock.t == 0.0 and pacer.requests == 10


def test_nothing_is_paced_without_an_installed_pacer(vclock: VirtualClock) -> None:
    assert asyncio.run(cgp.acquire("a.example")) == 0.0
    pacer = cgp.GlobalOriginPacer(rps=0.5, max_new_hosts_per_minute=0)
    with cgp.installed(pacer):
        asyncio.run(cgp.acquire("a.example"))
        asyncio.run(cgp.acquire("a.example"))
    assert pacer.requests == 2
    asyncio.run(cgp.acquire("a.example"))
    assert pacer.requests == 2, "uninstalled after the block"


# ===================================================================== the new-host cap


def test_the_new_host_cap_holds_when_answers_are_instant(vclock: VirtualClock) -> None:
    """20 hosts asked at once, no rate limit, instant answers: at most 6 first contacts in any
    60 s window. This is the 10-16 sweep (262 hosts in 60 s) made impossible."""
    pacer = cgp.GlobalOriginPacer(rps=0, max_new_hosts_per_minute=6)

    async def run() -> None:
        await asyncio.gather(*(pacer.acquire(f"h{i}.example") for i in range(20)))

    asyncio.run(run())
    admissions = sorted(pacer._admitted.values())
    assert admissions == [0.0] * 6 + [60.0] * 6 + [120.0] * 6 + [180.0] * 2
    for start in admissions:
        assert sum(1 for t in admissions if start <= t < start + 60) <= 6
    assert pacer.new_hosts == 20


def test_a_host_already_contacted_costs_no_new_admission(vclock: VirtualClock) -> None:
    pacer = cgp.GlobalOriginPacer(rps=0, max_new_hosts_per_minute=1)

    async def run() -> None:
        for _ in range(5):
            await pacer.acquire("same.example")
        await pacer.acquire("other.example")

    asyncio.run(run())
    assert pacer.new_hosts == 2
    assert vclock.t == 60.0, "only the second host waited for the window"


def test_new_host_wait_reports_the_room_under_the_cap(vclock: VirtualClock) -> None:
    pacer = cgp.GlobalOriginPacer(rps=0, max_new_hosts_per_minute=2)
    assert pacer.new_host_wait() == 0
    pacer.admit("a.example")
    assert pacer.new_host_wait() == 0
    pacer.admit("b.example")
    assert pacer.new_host_wait() == 60.0
    vclock.t = 59.0
    assert pacer.new_host_wait() == 1.0


# ===================================================================== pause, half rate, stop


def test_a_pause_holds_every_start_until_the_cool_down_ends(vclock: VirtualClock) -> None:
    pacer = cgp.GlobalOriginPacer(rps=0.5, max_new_hosts_per_minute=0)
    pacer.pause(900)
    asyncio.run(pacer.acquire("a.example"))
    assert pacer.grants == [900.0]
    assert pacer.cooldowns == 1 and pacer.cooldown_seconds == 900.0


def test_a_request_waiting_for_its_slot_when_a_trip_lands_waits_out_the_cool_down(
    vclock: VirtualClock,
) -> None:
    """Its slot (2 s) was reserved before the trip; it must not go out at 2 s into the cool-down."""
    pacer = cgp.GlobalOriginPacer(rps=0.5, max_new_hosts_per_minute=0)

    async def run() -> None:
        await pacer.acquire("a.example")  # t=0, no wait
        vclock.on_sleep = lambda: pacer.pause(900)  # the trip lands during the next wait
        await pacer.acquire("b.example")

    asyncio.run(run())
    assert pacer.grants[0] == 0.0
    assert pacer.grants[1] >= 900.0, pacer.grants


def test_a_halved_rate_doubles_the_gap(vclock: VirtualClock) -> None:
    pacer = cgp.GlobalOriginPacer(rps=0.5, max_new_hosts_per_minute=0)
    pacer.set_rate(pacer.rps / 2)

    async def run() -> None:
        for _ in range(3):
            await pacer.acquire("a.example")

    asyncio.run(run())
    assert _gaps(pacer.grants) == [4.0, 4.0]
    assert pacer.summary()["global_rps"] == 0.5 and pacer.summary()["final_rate"] == 0.25


def test_a_stopped_pacer_sends_nothing(vclock: VirtualClock) -> None:
    pacer = cgp.GlobalOriginPacer(rps=0.5, max_new_hosts_per_minute=0)
    pacer.stop("ip breaker tripped")
    with pytest.raises(cgp.GlobalPaceStopped):
        asyncio.run(pacer.acquire("a.example"))
    assert pacer.requests == 0


def test_a_cool_down_that_outlasts_the_budget_stops_instead_of_waiting(vclock: VirtualClock) -> None:
    pacer = cgp.GlobalOriginPacer(rps=0.5, max_new_hosts_per_minute=0, deadline=300.0)
    pacer.pause(900)
    with pytest.raises(cgp.GlobalPaceStopped):
        asyncio.run(pacer.acquire("a.example"))
    assert vclock.t == 0.0, "it did not sleep into the cool-down first"


# ===================================================================== the staged breaker


def _breaker(**kwargs: Any) -> Tuple[cit.StagedIpThrottleBreaker, List[Dict[str, Any]]]:
    events: List[Dict[str, Any]] = []
    breaker = cit.StagedIpThrottleBreaker(on_trip=lambda _b, e: events.append(e), **kwargs)
    return breaker, events


def test_three_challenge_hosts_trip_a_cool_down_and_two_do_not(vclock: VirtualClock) -> None:
    breaker, events = _breaker()
    breaker.observe("a.example", *CHALLENGE)
    breaker.observe("a.example", *CHALLENGE)  # one host twice is still one host
    breaker.observe("b.example", *CHALLENGE_429)
    assert not events and breaker.trips == 0
    breaker.observe("c.example", *CHALLENGE)
    assert breaker.trips == 1 and not breaker.tripped, "the first trip is a cool-down"
    assert events[0]["reason"] == "challenge" and events[0]["action"] == "cooldown"
    assert events[0]["challenge_hosts"] == 3
    assert breaker.cooling() and breaker.cooling_until == 900.0


def test_the_challenge_window_is_120_seconds(vclock: VirtualClock) -> None:
    breaker, _ = _breaker()
    breaker.observe("a.example", *CHALLENGE)
    vclock.t = 60.0
    breaker.observe("b.example", *CHALLENGE)
    vclock.t = 121.0  # a.example's challenge is 121 s old: out of the window
    breaker.observe("c.example", *CHALLENGE)
    assert breaker.trips == 0
    vclock.t = 179.0  # b (60) and c (121) are inside 120 s of d
    breaker.observe("d.example", *CHALLENGE)
    assert breaker.trips == 1


def test_challenges_are_counted_apart_from_plain_429s(vclock: VirtualClock) -> None:
    breaker, _ = _breaker()
    for i in range(5):
        breaker.observe(f"p{i}.example", *PLAIN_429)
    assert breaker.trips == 0, "five plain 429 hosts are not three challenges"
    breaker.observe("c.example", *CHALLENGE)
    summary = breaker.summary()
    assert summary["ip_throttle_plain_429_responses"] == 5
    assert summary["ip_throttle_challenge_responses"] == 1
    assert summary["ip_throttle_challenge_hosts"] == 1
    assert summary["throttle_diagnostics"]["cf_mitigated"] == 0, "a 403 is not a parent 429/503"


def test_ten_plain_429_hosts_still_trip_the_429_rule(vclock: VirtualClock) -> None:
    breaker, events = _breaker()
    for i in range(10):
        breaker.observe(f"p{i}.example", *PLAIN_429)
    assert breaker.trips == 1 and events[0]["reason"] == "throttle"


def test_answers_during_the_cool_down_never_count_and_the_second_trip_ends_it(
    vclock: VirtualClock,
) -> None:
    breaker, events = _breaker()
    for host in ("a", "b", "c"):
        breaker.observe(f"{host}.example", *CHALLENGE)
    assert breaker.trips == 1
    # Late answers to requests already in flight at the trip, during the cool-down.
    vclock.t = 10.0
    for host in ("d", "e", "f", "g"):
        breaker.observe(f"{host}.example", *CHALLENGE)
    assert breaker.trips == 1, "nothing seen during the cool-down counts"
    vclock.t = 901.0
    breaker.observe("h.example", *CHALLENGE)
    breaker.observe("i.example", *CHALLENGE)
    assert breaker.trips == 1, "pre-cool-down hosts are not in the new count"
    breaker.observe("j.example", *CHALLENGE)
    assert breaker.trips == 2 and breaker.tripped
    assert [e["action"] for e in events] == ["cooldown", "stop"]
    assert breaker.summary()["ip_throttled"] is True


def test_no_cool_down_makes_the_first_trip_the_last(vclock: VirtualClock) -> None:
    breaker, events = _breaker(cooldown_seconds=0)
    for host in ("a", "b", "c"):
        breaker.observe(f"{host}.example", *CHALLENGE)
    assert breaker.tripped and [e["action"] for e in events] == ["stop"]


def test_after_a_trip_a_challenged_host_is_held_back_and_a_reading_one_is_not(
    vclock: VirtualClock,
) -> None:
    breaker, _ = _breaker()
    breaker.observe("ok.example", *OK)
    breaker.observe("a.example", *CHALLENGE)
    assert not breaker.blocks("a.example"), "nothing is held back before a trip"
    breaker.observe("b.example", *CHALLENGE)
    breaker.observe("c.example", *CHALLENGE)
    assert breaker.blocks("a.example") and breaker.blocks("c.example")
    assert not breaker.blocks("ok.example")
    breaker.observe("a.example", *OK)
    assert not breaker.blocks("a.example"), "its latest answer counts"


def test_the_breaker_reads_its_env(monkeypatch: pytest.MonkeyPatch, vclock: VirtualClock) -> None:
    monkeypatch.setenv("CRAWL_IP_CHALLENGE_TRIP_HOSTS", "5")
    monkeypatch.setenv("CRAWL_IP_CHALLENGE_WINDOW_SECONDS", "300")
    monkeypatch.setenv("CRAWL_IP_THROTTLE_COOLDOWN_SECONDS", "600")
    breaker = cit.StagedIpThrottleBreaker.from_env()
    assert (breaker.challenge_trip_hosts, breaker.challenge_window_seconds, breaker.cooldown_seconds) == (
        5, 300.0, 600.0,
    )
    assert cit.StagedIpThrottleBreaker.from_env(challenge_trip_hosts=2).challenge_trip_hosts == 2
    for name in ("CRAWL_IP_CHALLENGE_TRIP_HOSTS", "CRAWL_IP_CHALLENGE_WINDOW_SECONDS",
                 "CRAWL_IP_THROTTLE_COOLDOWN_SECONDS"):
        monkeypatch.delenv(name)
    breaker = cit.StagedIpThrottleBreaker.from_env()
    assert (breaker.challenge_trip_hosts, breaker.challenge_window_seconds, breaker.cooldown_seconds) == (
        3, 120.0, 900.0,
    )


# ===================================================================== through the real batch


def _drive(
    monkeypatch: pytest.MonkeyPatch,
    clock: VirtualClock,
    rows: List[Tuple[str, Tuple[int, Dict[str, str]]]],
    *,
    concurrency: int = 1,
    robots: Optional[Dict[str, int]] = None,
    **batch_kwargs: Any,
) -> Tuple[Dict[str, Any], List[str], List[Tuple[float, str]], "cgp.GlobalOriginPacer"]:
    """Run the real batch over `rows` (host, answer), one seed per row, in queue order. Returns the
    summary, the seeds handed to the refresher (stamped), every request the stub answered as
    (virtual time, url), and the run's pacer."""
    seed_ids = [f"eps_{i}" for i in range(len(rows))]
    url_of = {s: f"https://{host}/products/p{i}" for i, (s, (host, _)) in enumerate(zip(seed_ids, rows))}
    answer_of = {url_of[s]: answer for s, (_h, answer) in zip(seed_ids, rows)}
    monkeypatch.setattr(
        err, "get_external_referral_refresh_candidate_seed_ids",
        lambda *a, **k: asyncio.sleep(0, result=seed_ids),
    )
    monkeypatch.setattr(
        err, "_fetch_refresh_candidate_hosts",
        lambda ids: asyncio.sleep(0, result={s: cp.host_of(url_of[s]) for s in ids}),
    )
    requests: List[Tuple[float, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requests.append((clock.t, url))
        if url.endswith("/robots.txt"):
            robots_seen = robots if robots is not None else {}
            host = request.url.host
            robots_seen[host] = robots_seen.get(host, 0) + 1
            return httpx.Response(404, request=request)
        status, headers = answer_of[url]
        return httpx.Response(status, headers=headers, content=b"<html></html>", request=request)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        svc.httpx, "AsyncClient", lambda *a, **kw: _REAL_ASYNC_CLIENT(*a, transport=transport, **kw)
    )
    pacers: List[cgp.GlobalOriginPacer] = []
    real_pacer = cgp.GlobalOriginPacer

    def capture(**kwargs: Any) -> cgp.GlobalOriginPacer:
        pacer = real_pacer(**kwargs)
        pacers.append(pacer)
        return pacer

    monkeypatch.setattr(err.crawl_global_pacer, "GlobalOriginPacer", capture)
    stamped: List[str] = []

    async def refresh(seed_id: str) -> Dict[str, Any]:
        stamped.append(seed_id)
        url = url_of[seed_id]
        try:
            await svc._fetch_html(url, max_wait=0)
        except svc.ExternalOfferUnavailable as exc:
            return {"status": "degraded", "error": f"destination_unavailable: http {exc.status_code}",
                    "domain": cp.host_of(url)}
        return {"status": "success", "price_refresh": {"status": "unchanged"}, "projection": {}}

    # The robots.txt fetch builds its own `httpx.AsyncClient`, which the patch above also answers.
    summary = asyncio.run(err.run_external_referral_refresh_batch(
        refresh_seed_by_id=refresh, limit=len(rows), host_concurrency=concurrency, **batch_kwargs,
    ))
    return summary, stamped, requests, pacers[0]


def test_every_origin_request_of_the_batch_takes_a_slot_across_four_workers(
    monkeypatch: pytest.MonkeyPatch, vclock: VirtualClock,
) -> None:
    rows = [(f"h{i}.example", OK) for i in range(8)] * 3
    summary, stamped, requests, pacer = _drive(
        monkeypatch, vclock, rows, concurrency=4, max_new_hosts_per_minute=0,
    )
    assert len(stamped) == len(rows) == len(requests)
    assert pacer.requests == len(requests), "every request the origin saw went through the bucket"
    assert _gaps(pacer.grants) == [2.0] * (len(rows) - 1)
    assert summary["global_rps"] == 0.5 and summary["final_rate"] == 0.5
    assert summary["paced_requests"] == len(rows)
    assert summary["paces_waited_seconds"] > 0


def test_robots_reads_and_requests_after_a_backoff_are_paced_too(
    monkeypatch: pytest.MonkeyPatch, vclock: VirtualClock,
) -> None:
    """Robots on: each new host costs its robots.txt read AND its page, both from the bucket. A
    host that 429'd is asked again on its next row, and that request pays too."""
    monkeypatch.setenv("CRAWL_ROBOTS_ENABLED", "true")
    rows = [("a.example", PLAIN_429), ("b.example", OK), ("a.example", OK)]
    robots: Dict[str, int] = {}
    summary, _stamped, requests, pacer = _drive(
        monkeypatch, vclock, rows, robots=robots, max_new_hosts_per_minute=0,
    )
    assert robots == {"a.example": 1, "b.example": 1}
    assert len(requests) == 5 and pacer.requests == 5
    assert _gaps(pacer.grants) == [2.0] * 4
    assert [t for t, _ in requests] == sorted(t for t, _ in requests)


def test_the_batch_never_contacts_more_than_six_new_hosts_a_minute(
    monkeypatch: pytest.MonkeyPatch, vclock: VirtualClock,
) -> None:
    """Instant answers, no rate limit, four workers, 20 hosts: the cap alone spaces them."""
    rows = [(f"h{i}.example", CHALLENGE_429 if i % 2 else OK) for i in range(20)]
    summary, _stamped, requests, pacer = _drive(
        monkeypatch, vclock, rows, concurrency=4, global_rps=0, ip_throttle_enabled=False,
    )
    firsts: Dict[str, float] = {}
    for at, url in requests:
        firsts.setdefault(httpx.URL(url).host, at)
    assert len(firsts) == 20
    instants = sorted(firsts.values())
    for start in instants:
        assert sum(1 for t in instants if start <= t < start + 60) <= 6, instants
    assert summary["max_new_hosts_per_minute"] == 6 and summary["new_hosts_contacted"] == 20


def test_while_the_cap_has_no_room_the_scheduler_reads_hosts_it_already_knows(
    monkeypatch: pytest.MonkeyPatch, vclock: VirtualClock,
) -> None:
    """Queue h0 h1 h2 h3, three rounds; two new hosts a minute. Parking the worker on h2's
    admission would cost 60 s per pair; instead h0 and h1 are read through first."""
    rows = [(f"h{i}.example", OK) for i in range(4)] * 3
    _summary, _stamped, requests, _pacer = _drive(
        monkeypatch, vclock, rows, global_rps=0, max_new_hosts_per_minute=2,
    )
    order = [httpx.URL(url).host.split(".")[0] for _t, url in requests]
    assert order[:6] == ["h0", "h1", "h0", "h1", "h0", "h1"], order
    assert order[6:] == ["h2", "h3", "h2", "h3", "h2", "h3"], order
    assert max(t for t, _ in requests) == 60.0


def test_a_challenge_storm_cools_down_resumes_at_half_rate_and_ends_on_the_second_trip(
    monkeypatch: pytest.MonkeyPatch, vclock: VirtualClock,
) -> None:
    rows = [("c0.example", CHALLENGE), ("c1.example", CHALLENGE), ("c2.example", CHALLENGE)]
    rows += [("ok.example", OK)] * 3
    rows += [("d0.example", CHALLENGE), ("d1.example", CHALLENGE), ("d2.example", CHALLENGE)]
    rows += [("late.example", OK)] * 5
    summary, stamped, requests, pacer = _drive(
        monkeypatch, vclock, rows, max_new_hosts_per_minute=0,
    )
    times = [t for t, _ in requests]
    hosts = [httpx.URL(u).host for _t, u in requests]
    # Three challenge hosts at 0, 2, 4 s; the trip pauses everything for 900 s from 4 s.
    assert times[:3] == [0.0, 2.0, 4.0]
    assert times[3] >= 904.0, "nothing started during the cool-down"
    # Resumed at HALF the rate: 4 s gaps from then on.
    assert _gaps(times[3:]) == [4.0] * (len(times) - 4)
    assert hosts[3:6] == ["ok.example"] * 3
    # The second trip (three NEW challenge hosts after the cool-down) ends the run.
    assert summary["breaker_trips"] == 2 and summary["cooldowns"] == 1
    assert summary["status"] == "ip_throttled" and summary["ip_throttled"] is True
    assert summary["global_rps"] == 0.5 and summary["final_rate"] == 0.25
    assert summary["cooldown_seconds"] == 900.0
    assert "late.example" not in hosts
    assert summary["skipped_for_ip_throttle"] == 5
    assert len(stamped) + summary["skipped_for_ip_throttle"] == len(rows)
    assert summary["ip_throttle_challenge_responses"] == 6


def test_rows_already_waiting_for_a_slot_send_nothing_after_the_final_trip(
    monkeypatch: pytest.MonkeyPatch, vclock: VirtualClock,
) -> None:
    """Four workers: when the second trip lands, up to three rows are already in flight, waiting
    for their slots. The run's end stops the pacer, so none of them goes out."""
    monkeypatch.setenv("CRAWL_IP_THROTTLE_COOLDOWN_SECONDS", "0")  # the first trip is the last
    rows = [(f"c{i}.example", CHALLENGE) for i in range(3)] + [(f"ok{i}.example", OK) for i in range(8)]
    summary, _stamped, requests, pacer = _drive(
        monkeypatch, vclock, rows, concurrency=4, max_new_hosts_per_minute=0,
    )
    assert summary["status"] == "ip_throttled"
    hosts = [httpx.URL(u).host for _t, u in requests]
    assert hosts == ["c0.example", "c1.example", "c2.example"], hosts
    assert pacer.stop_reason == "ip breaker tripped"


def test_a_cool_down_past_the_budget_ends_the_run_as_a_budget_stop(
    monkeypatch: pytest.MonkeyPatch, vclock: VirtualClock,
) -> None:
    rows = [(f"c{i}.example", CHALLENGE) for i in range(3)] + [("ok.example", OK)] * 4
    summary, stamped, requests, _pacer = _drive(
        monkeypatch, vclock, rows, budget_seconds=300, max_new_hosts_per_minute=0,
    )
    assert len(requests) == 3, "nothing waited out a cool-down the budget cannot cover"
    assert summary["stopped_early"] is True and summary["skipped_for_budget"] == 4
    assert summary["breaker_trips"] == 1 and summary["status"] != "ip_throttled"


# ===================================================================== flags, env, summary


def test_the_env_and_flag_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    assert err._refresh_global_rps(None) == 0.5
    assert err._refresh_max_new_hosts_per_minute(None) == 6
    monkeypatch.setenv("EXTERNAL_REFERRAL_REFRESH_GLOBAL_RPS", "0.2")
    monkeypatch.setenv("EXTERNAL_REFERRAL_REFRESH_MAX_NEW_HOSTS_PER_MINUTE", "3")
    assert err._refresh_global_rps(None) == 0.2
    assert err._refresh_max_new_hosts_per_minute(None) == 3
    assert err._refresh_global_rps(1.0) == 1.0, "the flag wins"
    assert err._refresh_max_new_hosts_per_minute(9) == 9
    assert err._refresh_global_rps(0) == 0.0, "0 is the explicit off switch"
    for bad in ("abc", "nan", "inf", "-1"):
        monkeypatch.setenv("EXTERNAL_REFERRAL_REFRESH_GLOBAL_RPS", bad)
        assert err._refresh_global_rps(None) == 0.5, f"{bad!r} must not mean unpaced"


def _main_with(monkeypatch: pytest.MonkeyPatch, summary: Dict[str, Any], argv: List[str]):
    seen: Dict[str, Any] = {}

    async def fake_run(**kwargs):
        seen.update(kwargs)
        return summary

    monkeypatch.setattr(job, "run_daily_external_referral_refresh", fake_run)
    monkeypatch.setattr(job.database, "connect", lambda: asyncio.sleep(0))
    monkeypatch.setattr(job.database, "disconnect", lambda: asyncio.sleep(0))
    monkeypatch.setattr("sys.argv", ["external_referral_refresh", "--limit", "1", *argv])
    return job.main(), seen


def test_the_cli_flags_reach_the_batch(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _code, seen = _main_with(monkeypatch, {"status": "success"}, [
        "--global-rps", "0.25", "--max-new-hosts-per-minute", "4", "--ip-challenge-trip-hosts", "2",
        "--ip-challenge-window-seconds", "60", "--ip-throttle-cooldown-seconds", "300",
    ])
    assert seen["global_rps"] == 0.25 and seen["max_new_hosts_per_minute"] == 4
    assert seen["ip_challenge_trip_hosts"] == 2 and seen["ip_challenge_window_seconds"] == 60.0
    assert seen["ip_throttle_cooldown_seconds"] == 300.0
    _code, seen = _main_with(monkeypatch, {"status": "success"}, [])
    for key in ("global_rps", "max_new_hosts_per_minute", "ip_challenge_trip_hosts",
                "ip_challenge_window_seconds", "ip_throttle_cooldown_seconds"):
        assert seen[key] is None, f"{key}: absent flag leaves the env its say"


# The summary keys a run printed before this change (prod, 2026-10-15, execution bmgwq). Every one
# must still be there, with the same meaning, for the dashboards and the IP_THROTTLE filter.
_KEYS_BEFORE = {
    "status", "status_without_ip_throttle", "attempted_count", "origin_reads", "origin_yield",
    "candidate_count", "stopped_early", "budget_seconds", "elapsed_seconds", "host_concurrency",
    "rows_started", "rows_per_second", "skipped_for_budget", "budget_reach",
    "skipped_for_host_backoff", "skipped_for_unreachable_host", "skipped_for_ip_throttle",
    "ip_throttle_skips", "ip_throttled", "ip_throttle_breaker_armed", "ip_throttle_trip_hosts",
    "ip_throttle_window_seconds", "ip_throttle_tripped_at", "ip_throttle_trip_host_count",
    "ip_throttle_trip_shopify_host_count", "ip_throttle_first_429_at", "ip_throttle_last_429_at",
    "ip_throttle_hosts", "ip_throttle_shopify_hosts", "ip_throttle_peak_hosts_in_window",
    "ip_throttle_peak_shopify_hosts_in_window", "throttle_diagnostics", "host_seconds",
    "refreshed", "refreshed_from_cache", "degraded", "failed", "price_changed", "errors",
}
_KEYS_ADDED = {"global_rps", "paces_waited_seconds", "breaker_trips", "cooldowns", "final_rate"}


def test_the_summary_is_backward_compatible_and_carries_the_new_fields(
    monkeypatch: pytest.MonkeyPatch, vclock: VirtualClock, capsys,
) -> None:
    rows = [(f"h{i}.example", OK) for i in range(3)]
    summary, _s, _r, _p = _drive(monkeypatch, vclock, rows)
    assert _KEYS_BEFORE <= set(summary), _KEYS_BEFORE - set(summary)
    assert _KEYS_ADDED <= set(summary)
    assert (summary["breaker_trips"], summary["cooldowns"]) == (0, 0)
    assert summary["status"] == "success" and summary["ip_throttled"] is False
    json.dumps(summary)  # one JSON line per run, no NaN, nothing unserialisable
    line = job.ip_throttle_line(summary)
    assert _KEYS_ADDED <= set(line)
    assert line["global_rps"] == 0.5 and line["final_rate"] == 0.5
