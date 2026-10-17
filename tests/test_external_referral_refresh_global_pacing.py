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
# A new-host cap far above anything a test reaches. Since 2026-10-17 a cap of 0 is refused (it used
# to mean "off"), so a test that isolates the RATE passes this instead.
_NO_CAP = 10_000

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
    "EXTERNAL_REFERRAL_REFRESH_MIN_BUDGET_REACH",
    "EXTERNAL_REFERRAL_REFRESH_GLOBAL_PACER_ENABLED",  # dropped alias: must stay unread
    "CRAWL_GLOBAL_PACER_DISABLED",
    "CRAWL_IP_CHALLENGE_EXCLUDED_HOSTS",
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
    pacer = cgp.GlobalOriginPacer(rps=0.5, max_new_hosts_per_minute=_NO_CAP)

    async def worker(index: int) -> None:
        for _ in range(5):
            await pacer.acquire(f"h{index}.example")

    async def run() -> None:
        await asyncio.gather(*(worker(i) for i in range(4)))

    asyncio.run(run())
    assert pacer.requests == 20 and len(pacer.grants) == 20
    assert _gaps(pacer.grants) == [2.0] * 19
    assert pacer.paces_waited_seconds > 0


def test_the_bucket_holds_in_real_time_across_concurrent_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same rule on the real event loop and the real clock, scaled down (40/s): the sleeps
    actually honour the reserved starts, so no two requests start closer than the lower jitter
    bound, across four concurrent workers. (The 1.0/s ceiling is lifted for the scale-down only.)"""
    monkeypatch.setattr(cgp, "RPS_MAX", 100.0)
    pacer = cgp.GlobalOriginPacer(rps=40.0, max_new_hosts_per_minute=_NO_CAP, jitter=0.3)
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
    pacer = cgp.GlobalOriginPacer(rps=0.5, max_new_hosts_per_minute=_NO_CAP)
    assert pacer._gap() == pytest.approx(2.0 * factor)


def test_real_jitter_stays_in_bounds_and_averages_the_rate() -> None:
    pacer = cgp.GlobalOriginPacer(rps=0.5, max_new_hosts_per_minute=_NO_CAP)
    gaps = [pacer._gap() for _ in range(4000)]
    assert min(gaps) >= 1.4 - 1e-9 and max(gaps) <= 2.6 + 1e-9
    assert sum(gaps) / len(gaps) == pytest.approx(2.0, rel=0.03)
    assert max(gaps) - min(gaps) > 1.0, "the gaps really vary"


def test_a_jitter_of_one_or_more_cannot_allow_a_burst() -> None:
    assert cgp.GlobalOriginPacer(rps=1, max_new_hosts_per_minute=_NO_CAP, jitter=5).jitter == 0.9


@pytest.mark.parametrize("bad", [0, -0.0, -1, float("nan"), float("inf"), "abc", None])
def test_a_zero_or_garbage_rate_is_refused_and_paced_at_the_default(vclock: VirtualClock, bad, caplog) -> None:
    """2026-10-17 review, M3: 0 used to mean UNPACED. Now 0, -0, negative, NaN, inf and garbage are
    an ERROR and the 0.5/s default, in the constructor as in every flag."""
    with caplog.at_level("ERROR"):
        pacer = cgp.GlobalOriginPacer(rps=bad, max_new_hosts_per_minute=_NO_CAP)

    async def run() -> None:
        for i in range(10):
            await pacer.acquire(f"h{i}.example")

    asyncio.run(run())
    assert pacer.initial_rps == 0.5 and _gaps(pacer.grants) == [2.0] * 9
    if bad is not None:
        assert any(r.levelname == "ERROR" for r in caplog.records), "a refused value is logged at ERROR"


@pytest.mark.parametrize("given, used", [(0.01, 0.05), (5.0, 1.0), (0.3, 0.3)])
def test_the_rate_is_clamped_to_its_band(given, used) -> None:
    assert cgp.GlobalOriginPacer(rps=given).initial_rps == used
    assert cgp.resolve_rps(given, None) == used


@pytest.mark.parametrize("given, used", [(0.9, 1), (2.1, 3), (6, 6), (0, 6), (-3, 6), ("x", 6)])
def test_the_new_host_cap_rounds_up_and_is_at_least_one(given, used) -> None:
    """`int()` used to truncate 0.9 to 0, which switched the cap off."""
    assert cgp.resolve_new_host_cap(given, None) == used
    assert cgp.GlobalOriginPacer(max_new_hosts_per_minute=given).max_new_hosts_per_minute == used


def test_a_halved_rate_can_go_below_the_floor_but_never_off() -> None:
    pacer = cgp.GlobalOriginPacer(rps=0.05)
    pacer.set_rate(0.025)
    assert pacer.rps == 0.025, "slower is always allowed"
    for bad in (0, -1, float("nan"), "x"):
        pacer.set_rate(bad)
        assert pacer.rps == 0.025, f"set_rate({bad!r}) must never turn the rate off"
    pacer.set_rate(9)
    assert pacer.rps == 1.0


def test_only_the_kill_switch_runs_unpaced_and_it_still_pauses_and_stops(vclock: VirtualClock) -> None:
    pacer = cgp.GlobalOriginPacer(unpaced=True)

    async def run() -> None:
        for i in range(10):
            await pacer.acquire(f"h{i}.example")

    asyncio.run(run())
    assert vclock.t == 0.0 and pacer.requests == 10
    assert pacer.summary()["global_rps"] is None and pacer.summary()["global_pacer_disabled"] is True
    pacer.pause(900)
    asyncio.run(pacer.acquire("h0.example"))
    assert vclock.t == 900.0, "an unpaced pacer still carries the breaker's cool-down"
    pacer.stop("ip breaker tripped")
    with pytest.raises(cgp.GlobalPaceStopped):
        asyncio.run(pacer.acquire("h0.example"))


@pytest.mark.parametrize("env, value, disabled, unreadable", [
    ("CRAWL_GLOBAL_PACER_DISABLED", "true", True, False),
    ("CRAWL_GLOBAL_PACER_DISABLED", " TRUE ", True, False),
    ("CRAWL_GLOBAL_PACER_DISABLED", "True", True, False),
    # Only the exact word disables (controller review, 2026-10-17): anything else is unreadable,
    # logged at ERROR, and the run stays PACED.
    ("CRAWL_GLOBAL_PACER_DISABLED", "1", False, True),
    ("CRAWL_GLOBAL_PACER_DISABLED", "yes", False, True),
    ("CRAWL_GLOBAL_PACER_DISABLED", "on", False, True),
    ("CRAWL_GLOBAL_PACER_DISABLED", "false", False, True),
    ("CRAWL_GLOBAL_PACER_DISABLED", "maybe", False, True),
    ("CRAWL_GLOBAL_PACER_DISABLED", "", False, False),
    # The dropped per-lane alias is not read at all, in either polarity.
    ("EXTERNAL_REFERRAL_REFRESH_GLOBAL_PACER_ENABLED", "false", False, False),
    ("EXTERNAL_REFERRAL_REFRESH_GLOBAL_PACER_ENABLED", "0", False, False),
])
def test_the_kill_switch_env(monkeypatch: pytest.MonkeyPatch, caplog, env, value, disabled, unreadable) -> None:
    monkeypatch.setenv(env, value)
    with caplog.at_level("ERROR"):
        assert err._refresh_global_pacer_disabled() is disabled
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert any("DISABLED for" in m for m in errors) is disabled
    assert any("unreadable" in m for m in errors) is unreadable


def test_the_batch_runs_unpaced_under_the_kill_switch_and_logs_it_every_run(
    monkeypatch: pytest.MonkeyPatch, vclock: VirtualClock, caplog,
) -> None:
    monkeypatch.setenv("CRAWL_GLOBAL_PACER_DISABLED", "true")
    rows = [(f"h{i}.example", OK) for i in range(12)]
    with caplog.at_level("ERROR"):
        summary, stamped, requests, pacer = _drive(monkeypatch, vclock, rows, concurrency=4)
    assert len(stamped) == 12 and pacer.unpaced and vclock.t == 0.0
    assert summary["global_pacer_disabled"] is True and summary["global_rps"] is None
    assert any("DISABLED" in r.getMessage() for r in caplog.records if r.levelname == "ERROR")


def test_a_slot_past_the_deadline_stops_instead_of_sleeping(vclock: VirtualClock) -> None:
    """Review L1: pacing waits ignored the deadline, so a reserved start past the budget was slept
    to and sent anyway. It now stops the pacer and refuses, without sleeping."""
    pacer = cgp.GlobalOriginPacer(rps=0.5, max_new_hosts_per_minute=_NO_CAP, deadline=3.0)

    async def run() -> None:
        await pacer.acquire("a.example")  # t=0
        await pacer.acquire("a.example")  # t=2
        await pacer.acquire("a.example")  # its slot (4) is past the deadline (3)

    with pytest.raises(cgp.GlobalPaceStopped):
        asyncio.run(run())
    assert pacer.grants == [0.0, 2.0] and vclock.t == 2.0, "it did not sleep towards the slot"
    assert pacer.stop_reason == "budget ends before the next slot"


def test_nothing_is_paced_without_an_installed_pacer(vclock: VirtualClock) -> None:
    assert asyncio.run(cgp.acquire("a.example")) == 0.0
    pacer = cgp.GlobalOriginPacer(rps=0.5, max_new_hosts_per_minute=_NO_CAP)
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
    pacer = cgp.GlobalOriginPacer(rps=1.0, max_new_hosts_per_minute=6)

    async def run() -> None:
        await asyncio.gather(*(pacer.acquire(f"h{i}.example") for i in range(20)))

    asyncio.run(run())
    # The cap counts ACTUAL first-request starts (the rate spaces them 1 s apart), not the instants
    # hosts were admitted: six starts, then the seventh 60 s after the FIRST of them, and so on.
    firsts = sorted(pacer._contacted.values())
    assert firsts == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 60.0, 61.0, 62.0, 63.0, 64.0, 65.0,
                      120.0, 121.0, 122.0, 123.0, 124.0, 125.0, 180.0, 181.0], firsts
    assert firsts == sorted(pacer.grants)
    for start in firsts:
        assert sum(1 for t in firsts if start <= t < start + 60) <= 6
    assert pacer.new_hosts == 20


def test_a_host_already_contacted_costs_no_new_admission(vclock: VirtualClock) -> None:
    pacer = cgp.GlobalOriginPacer(rps=1.0, max_new_hosts_per_minute=1)

    async def run() -> None:
        for _ in range(5):
            await pacer.acquire("same.example")
        await pacer.acquire("other.example")

    asyncio.run(run())
    assert pacer.new_hosts == 2
    assert vclock.t == 60.0, "only the second host waited for the window"


def test_new_host_wait_reports_the_room_under_the_cap(vclock: VirtualClock) -> None:
    pacer = cgp.GlobalOriginPacer(rps=1.0, max_new_hosts_per_minute=2)
    assert pacer.new_host_wait() == 0
    pacer.admit("a.example")
    assert pacer.new_host_wait() == 0
    pacer.admit("b.example")
    assert pacer.new_host_wait() == 60.0
    vclock.t = 29.0
    assert pacer.new_host_wait() == 31.0
    pacer.release("a.example")
    assert pacer.new_host_wait() == 0, "a released place is room again"


def test_phantom_places_expire_and_never_push_back_an_earlier_host(vclock: VirtualClock) -> None:
    """Controller review (LOW): places the scheduler took and never used starved a real new host to
    t=600 s. (1) An unused place expires PENDING_GRACE_SECONDS after its own instant; (2) a host asking
    to start ignores places reserved for later instants than its own."""
    pacer = cgp.GlobalOriginPacer(rps=1.0, max_new_hosts_per_minute=1)
    pacer.admit("a.example")  # its place: 0 s
    pacer.admit("ghost.example")  # the next place: 60 s, never used
    asyncio.run(pacer.acquire("a.example"))
    assert pacer.grants == [0.0], "the ghost's later place did not push a (0 s) back to 120 s"

    pacer = cgp.GlobalOriginPacer(rps=1.0, max_new_hosts_per_minute=1)
    pacer.admit("ghost.example")  # 0 s, never used
    vclock.t = cgp.PENDING_GRACE_SECONDS + 1.0
    assert pacer.new_host_wait() == 0, "an unused place expired after its grace"
    asyncio.run(pacer.acquire("real.example"))
    assert pacer.grants == [cgp.PENDING_GRACE_SECONDS + 1.0]


# ===================================================================== pause, half rate, stop


def test_a_pause_holds_every_start_until_the_cool_down_ends(vclock: VirtualClock) -> None:
    pacer = cgp.GlobalOriginPacer(rps=0.5, max_new_hosts_per_minute=_NO_CAP)
    pacer.pause(900)
    asyncio.run(pacer.acquire("a.example"))
    assert pacer.grants == [900.0]
    assert pacer.cooldowns == 1 and pacer.cooldown_seconds == 900.0


def test_a_request_waiting_for_its_slot_when_a_trip_lands_waits_out_the_cool_down(
    vclock: VirtualClock,
) -> None:
    """Its slot (2 s) was reserved before the trip; it must not go out at 2 s into the cool-down."""
    pacer = cgp.GlobalOriginPacer(rps=0.5, max_new_hosts_per_minute=_NO_CAP)

    async def run() -> None:
        await pacer.acquire("a.example")  # t=0, no wait
        vclock.on_sleep = lambda: pacer.pause(900)  # the trip lands during the next wait
        await pacer.acquire("b.example")

    asyncio.run(run())
    assert pacer.grants[0] == 0.0
    assert pacer.grants[1] >= 900.0, pacer.grants


def test_a_halved_rate_doubles_the_gap(vclock: VirtualClock) -> None:
    pacer = cgp.GlobalOriginPacer(rps=0.5, max_new_hosts_per_minute=_NO_CAP)
    pacer.set_rate(pacer.rps / 2)

    async def run() -> None:
        for _ in range(3):
            await pacer.acquire("a.example")

    asyncio.run(run())
    assert _gaps(pacer.grants) == [4.0, 4.0]
    assert pacer.summary()["global_rps"] == 0.5 and pacer.summary()["final_rate"] == 0.25


def test_a_stopped_pacer_sends_nothing(vclock: VirtualClock) -> None:
    pacer = cgp.GlobalOriginPacer(rps=0.5, max_new_hosts_per_minute=_NO_CAP)
    pacer.stop("ip breaker tripped")
    with pytest.raises(cgp.GlobalPaceStopped):
        asyncio.run(pacer.acquire("a.example"))
    assert pacer.requests == 0


def test_a_cool_down_that_outlasts_the_budget_stops_instead_of_waiting(vclock: VirtualClock) -> None:
    pacer = cgp.GlobalOriginPacer(rps=0.5, max_new_hosts_per_minute=_NO_CAP, deadline=300.0)
    pacer.pause(900)
    with pytest.raises(cgp.GlobalPaceStopped):
        asyncio.run(pacer.acquire("a.example"))
    assert vclock.t == 0.0, "it did not sleep into the cool-down first"


# ===================================================================== the staged breaker


def _breaker(**kwargs: Any) -> Tuple[cit.StagedIpThrottleBreaker, List[Dict[str, Any]]]:
    """The rule's mechanics are tested at a 3-host trip (small fixtures); the shipped default (8) has
    its own test below."""
    kwargs.setdefault("challenge_trip_hosts", 3)
    events: List[Dict[str, Any]] = []
    breaker = cit.StagedIpThrottleBreaker(on_trip=lambda _b, e: events.append(e), **kwargs)
    return breaker, events


def test_the_default_challenge_trip_survives_a_healthy_night_and_trips_a_listing_night(
    vclock: VirtualClock,
) -> None:
    """Healthy nights 10-08..10-15 peaked at 4 challenged hosts in 120 s; the 10-16 listing night at
    360. The default must let the first through and stop the second within a few hosts."""
    assert cit.CHALLENGE_TRIP_HOSTS_DEFAULT == 8
    events: List[Dict[str, Any]] = []
    breaker = cit.StagedIpThrottleBreaker(on_trip=lambda _b, e: events.append(e))
    assert breaker.challenge_trip_hosts == 8
    for n in range(4):  # 10-08's peak: 4 hosts inside 120 s
        breaker.observe(f"h{n}.example", *CHALLENGE)
    assert not events and breaker.trips == 0
    for n in range(4, 7):
        breaker.observe(f"h{n}.example", *CHALLENGE)
    assert not events, "7 hosts is still under the default"
    breaker.observe("h7.example", *CHALLENGE)
    assert len(events) == 1 and breaker.trips == 1


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
        8, 120.0, 900.0,
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
    # The storm fixtures are three challenge hosts per trip; the shipped default (8) is tested on its own.
    batch_kwargs.setdefault("ip_challenge_trip_hosts", 3)
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
        monkeypatch, vclock, rows, concurrency=4, max_new_hosts_per_minute=_NO_CAP,
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
        monkeypatch, vclock, rows, robots=robots, max_new_hosts_per_minute=_NO_CAP,
    )
    assert robots == {"a.example": 1, "b.example": 1}
    assert len(requests) == 5 and pacer.requests == 5
    assert _gaps(pacer.grants) == [2.0] * 4
    assert [t for t, _ in requests] == sorted(t for t, _ in requests)


def test_the_batch_never_contacts_more_than_six_new_hosts_a_minute(
    monkeypatch: pytest.MonkeyPatch, vclock: VirtualClock,
) -> None:
    """Instant answers, the fastest rate allowed (1.0/s), four workers, 20 hosts: at most six FIRST
    REQUESTS start in any 60 s window. Measured on the reserved starts (`_contacted`, the instant
    each host's first request was granted), because `VirtualClock` adds concurrent sleeps up rather
    than waking each at its own instant, so the stub's clock reading overstates with 4 workers.

    This is the test that caught the cap counting ADMISSIONS (take time) instead of first-request
    starts: with the rate on, six hosts admitted at 0 s started at 0-5 s, the next six were admitted
    at 60 s, and the window [5, 65) held seven first requests."""
    rows = [(f"h{i}.example", CHALLENGE_429 if i % 2 else OK) for i in range(20)]
    summary, _stamped, requests, pacer = _drive(
        monkeypatch, vclock, rows, concurrency=4, global_rps=1.0, ip_throttle_enabled=False,
    )
    assert {httpx.URL(url).host for _t, url in requests} == set(pacer._contacted)
    instants = sorted(pacer._contacted.values())
    assert len(instants) == 20
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
        monkeypatch, vclock, rows, global_rps=1.0, max_new_hosts_per_minute=2,
    )
    order = [httpx.URL(url).host.split(".")[0] for _t, url in requests]
    assert order[:6] == ["h0", "h1", "h0", "h1", "h0", "h1"], order
    # h0/h1's first contacts start at 0 and 1 s (1.0/s). h2 may start 60 s after h0's; at 60 s h3
    # still has 1 s to wait (61 s = h1's first + 60), so the scheduler reads h2's next row instead
    # of parking, then h3 twice (its head is earlier in the queue than h2's last), then both.
    assert order[6:] == ["h2", "h2", "h3", "h3", "h2", "h3"], order
    assert [t for t, _ in requests] == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 60.0, 61.0, 62.0, 63.0, 64.0, 65.0]


def test_a_challenge_storm_cools_down_resumes_at_half_rate_and_ends_on_the_second_trip(
    monkeypatch: pytest.MonkeyPatch, vclock: VirtualClock,
) -> None:
    rows = [("c0.example", CHALLENGE), ("c1.example", CHALLENGE), ("c2.example", CHALLENGE)]
    rows += [("ok.example", OK)] * 3
    rows += [("d0.example", CHALLENGE), ("d1.example", CHALLENGE), ("d2.example", CHALLENGE)]
    rows += [("late.example", OK)] * 5
    summary, stamped, requests, pacer = _drive(
        monkeypatch, vclock, rows, max_new_hosts_per_minute=_NO_CAP,
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
        monkeypatch, vclock, rows, concurrency=4, max_new_hosts_per_minute=_NO_CAP,
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
        monkeypatch, vclock, rows, budget_seconds=300, max_new_hosts_per_minute=_NO_CAP,
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
    assert err._refresh_global_rps(0) == 0.5, "0 is NOT an off switch any more (review M3)"
    assert err._refresh_max_new_hosts_per_minute(0) == 6
    assert err._refresh_max_new_hosts_per_minute(0.9) == 1, "rounded up, never truncated to 0"
    for bad in ("abc", "nan", "inf", "-1", "0", "-0"):
        monkeypatch.setenv("EXTERNAL_REFERRAL_REFRESH_GLOBAL_RPS", bad)
        monkeypatch.setenv("EXTERNAL_REFERRAL_REFRESH_MAX_NEW_HOSTS_PER_MINUTE", bad)
        assert err._refresh_global_rps(None) == 0.5, f"{bad!r} must not mean unpaced"
        assert err._refresh_max_new_hosts_per_minute(None) == 6, f"{bad!r} must not mean uncapped"
    monkeypatch.setenv("EXTERNAL_REFERRAL_REFRESH_GLOBAL_RPS", "3")
    assert err._refresh_global_rps(None) == 1.0, "clamped to the 1.0/s ceiling"
    monkeypatch.setenv("EXTERNAL_REFERRAL_REFRESH_MIN_BUDGET_REACH", "0.2")
    assert err._min_budget_reach() == 0.2, "env-tunable after the head-host census (review M4)"
    for bad in ("0", "-1", "nan", "x"):
        monkeypatch.setenv("EXTERNAL_REFERRAL_REFRESH_MIN_BUDGET_REACH", bad)
        assert err._min_budget_reach() == 0.5
    monkeypatch.setenv("EXTERNAL_REFERRAL_REFRESH_MIN_BUDGET_REACH", "7")
    assert err._min_budget_reach() == 1.0


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


# ===================================================================== review 2026-10-17: M1
#
# Rows in flight when the pacer stops (the breaker's final trip, or the budget) used to be STAMPED:
# `GlobalPaceStopped` is a RuntimeError, `resolve_external_offer`'s `except Exception` swallowed it
# and returned the cached snapshot, and `_refresh_external_seed_by_id` wrote it as a success
# (`last_crawl_attempt_at`, `updated_at`, the snapshot); with no cached snapshot it stamped the row
# degraded. Driven here through the REAL `_refresh_external_seed_by_id` -> REAL
# `resolve_external_offer` -> REAL `_fetch_html` -> `crawl_politeness` -> the installed pacer, with
# only the database (captured) and HTTP (MockTransport) stubbed.

def _seed_row(seed_id: str, host: str) -> Dict[str, Any]:
    url = f"https://{host}/products/{seed_id}"
    return {
        "id": seed_id, "external_product_id": f"ext_{seed_id}", "market": "US", "tool": "*",
        "destination_url": url, "canonical_url": url, "domain": host, "title": "Toner",
        "price_amount": 28.0, "price_currency": "USD", "availability": "in_stock",
        "seed_data": {"title": "Toner", "snapshot": {}}, "status": "active",
        "attached_product_key": None, "destination_checked_at": None, "destination_verdict": None,
        "destination_failure_streak": 0, "last_crawled_at": None,
    }


def _cached_snapshot(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": f"eo_{row['id']}", "market": "US", "canonical_url": row["canonical_url"],
        "domain": row["domain"], "title": "cached", "price_amount": 28.0, "price_currency": "USD",
        "availability": "in_stock", "last_checked_at": None, "evidence": {},
    }


def _real_refresh_harness(
    monkeypatch: pytest.MonkeyPatch,
    rows: Dict[str, Dict[str, Any]],
    *,
    cached: bool,
    answer=lambda request: httpx.Response(404, request=request),
) -> Tuple[List[Tuple[str, Dict[str, Any]]], List[Any], List[str]]:
    """Returns (seed writes, snapshot writes, urls the origin saw)."""
    import routes.employee_products as mod

    writes: List[Tuple[str, Dict[str, Any]]] = []
    snapshot_writes: List[Any] = []
    seen: List[str] = []

    async def fetch_one(_query: str, values=None):
        return dict(rows[values["id"]]) if values and values.get("id") in rows else None

    async def execute_seed_data_stmt(query: str, values):
        writes.append((query, dict(values or {})))

    async def get_snapshot_row(_market, url_hash):
        for row in rows.values():
            if svc._url_hash(svc._normalize_url(row["destination_url"])) == url_hash:
                return _cached_snapshot(row) if cached else None
        return None

    async def record_observation(seed_id, observation, **_kw):
        writes.append(("observation", {"id": seed_id, "verdict": observation.verdict}))
        return {"seed_id": seed_id, "verdict": observation.verdict}

    async def snapshot_execute(*args, **kwargs):
        snapshot_writes.append(args)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return answer(request)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(svc.httpx, "AsyncClient", lambda *a, **kw: _REAL_ASYNC_CLIENT(*a, transport=transport, **kw))
    monkeypatch.setattr(mod, "_ensure_external_seeds_table", lambda: asyncio.sleep(0))
    monkeypatch.setattr(mod.database, "fetch_one", fetch_one)
    monkeypatch.setattr(mod, "_execute_seed_data_stmt", execute_seed_data_stmt)
    monkeypatch.setattr(mod.destination_liveness, "record_destination_observation", record_observation)
    monkeypatch.setattr(svc, "_get_snapshot_row", get_snapshot_row)
    monkeypatch.setattr(svc.database, "execute", snapshot_execute)
    return writes, snapshot_writes, seen


def _stamped_ids(writes: List[Tuple[str, Dict[str, Any]]]) -> List[str]:
    return [values.get("id") for query, values in writes if "last_crawl_attempt_at" in query]


@pytest.mark.parametrize("cached", [True, False], ids=["with_a_cached_snapshot", "without_one"])
def test_a_request_the_pacer_refused_is_never_stamped(
    monkeypatch: pytest.MonkeyPatch, vclock: VirtualClock, cached: bool,
) -> None:
    import routes.employee_products as mod

    rows = {"eps_m1": _seed_row("eps_m1", "brand.example")}
    writes, snapshot_writes, seen = _real_refresh_harness(monkeypatch, rows, cached=cached)
    pacer = cgp.GlobalOriginPacer(rps=0.5, max_new_hosts_per_minute=_NO_CAP)
    pacer.stop("ip breaker tripped")
    with cgp.installed(pacer):
        with pytest.raises(cgp.GlobalPaceStopped):
            asyncio.run(mod._refresh_external_seed_by_id("eps_m1", max_wait=0))
    assert seen == [], "nothing was sent"
    assert writes == [], f"a refused row must write nothing, stamp included: {writes}"
    assert snapshot_writes == []


def _batch_over_real_refresh(
    monkeypatch: pytest.MonkeyPatch, rows: Dict[str, Dict[str, Any]], **kwargs: Any,
) -> Dict[str, Any]:
    import routes.employee_products as mod

    ids = list(rows)
    monkeypatch.setattr(err, "get_external_referral_refresh_candidate_seed_ids",
                        lambda *a, **k: asyncio.sleep(0, result=ids))
    monkeypatch.setattr(err, "_fetch_refresh_candidate_hosts",
                        lambda seed_ids: asyncio.sleep(0, result={s: rows[s]["domain"] for s in seed_ids}))
    return asyncio.run(err.run_external_referral_refresh_batch(
        refresh_seed_by_id=lambda seed_id: mod._refresh_external_seed_by_id(seed_id, max_wait=0),
        limit=len(ids), **kwargs,
    ))


def test_a_budget_stop_mid_row_defers_the_row_unstamped(
    monkeypatch: pytest.MonkeyPatch, vclock: VirtualClock,
) -> None:
    """Budget 3 s at 0.5/s: slots at 0 and 2 s; the third row's slot (4 s) is past the deadline, so
    its request is refused (review L1) and the row is counted in `skipped_for_budget`, unstamped."""
    rows = {f"eps_{i}": _seed_row(f"eps_{i}", "brand.example") for i in range(5)}
    writes, _snapshot_writes, seen = _real_refresh_harness(monkeypatch, rows, cached=True)
    summary = _batch_over_real_refresh(
        monkeypatch, rows, budget_seconds=3, max_new_hosts_per_minute=_NO_CAP,
    )
    assert len(seen) == 2
    assert _stamped_ids(writes) == ["eps_0", "eps_1"], writes
    assert summary["stopped_early"] is True and summary["skipped_for_budget"] == 3
    assert summary["rows_started"] == 3, "no row is started once the pacer has stopped on the budget"
    assert summary["failed"] == 0 and summary["refreshed"] == 0
    assert summary["pacer_stopped"] == "budget ends before the next slot"


def test_the_final_trip_defers_rows_in_flight_unstamped(
    monkeypatch: pytest.MonkeyPatch, vclock: VirtualClock,
) -> None:
    """Four workers, the first trip is the last: rows waiting for a slot when it lands are refused,
    never stamped, and counted in `skipped_for_ip_throttle`."""
    monkeypatch.setenv("CRAWL_IP_THROTTLE_COOLDOWN_SECONDS", "0")
    monkeypatch.setenv("CRAWL_IP_CHALLENGE_TRIP_HOSTS", "3")  # a 3-host trip; the default (8) has its own test
    hosts = [f"c{i}.example" for i in range(3)] + [f"ok{i}.example" for i in range(8)]
    rows = {f"eps_{i}": _seed_row(f"eps_{i}", host) for i, host in enumerate(hosts)}

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.host.startswith("c"):
            return httpx.Response(403, headers=dict(CHALLENGE[1]), request=request)
        return httpx.Response(404, request=request)

    writes, _snapshot_writes, seen = _real_refresh_harness(monkeypatch, rows, cached=True, answer=answer)
    summary = _batch_over_real_refresh(
        monkeypatch, rows, host_concurrency=4, max_new_hosts_per_minute=_NO_CAP,
    )
    assert summary["status"] == "ip_throttled"
    assert sorted(httpx.URL(u).host for u in seen) == ["c0.example", "c1.example", "c2.example"]
    assert sorted(_stamped_ids(writes)) == ["eps_0", "eps_1", "eps_2"], writes
    assert summary["skipped_for_ip_throttle"] == len(rows) - 3
    assert sum(summary["ip_throttle_skips"].values()) == summary["skipped_for_ip_throttle"], (
        "a refused in-flight row is attributed to its host like the drained ones"
    )
    assert summary["failed"] == 0


# ===================================================================== review 2026-10-17: M2

def _redirect_chain(chain: Dict[str, str], final_status: int = 200):
    """A MockTransport answering 301 along `chain` (url -> next url) and `final_status` at its end."""
    seen: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        if url in chain:
            return httpx.Response(301, headers={"location": chain[url]}, request=request)
        return httpx.Response(final_status, content=b"<html></html>", request=request)

    return httpx.MockTransport(handler), seen


def test_every_redirect_hop_takes_a_slot(monkeypatch: pytest.MonkeyPatch, vclock: VirtualClock) -> None:
    """A 3-hop chain on one host: the first request plus 3 hops = 4 slots, 2 s apart at 0.5/s."""
    chain = {
        "https://a.example/products/x": "https://a.example/products/x1",
        "https://a.example/products/x1": "https://a.example/products/x2",
        "https://a.example/products/x2": "https://a.example/products/x3",
    }
    transport, seen = _redirect_chain(chain)
    monkeypatch.setattr(svc.httpx, "AsyncClient", lambda *a, **kw: _REAL_ASYNC_CLIENT(*a, transport=transport, **kw))
    pacer = cgp.GlobalOriginPacer(rps=0.5, max_new_hosts_per_minute=_NO_CAP)
    with cgp.installed(pacer):
        asyncio.run(svc._fetch_html("https://a.example/products/x", max_wait=0))
    assert len(seen) == 4
    assert pacer.requests == 4, "the first request and three hops, each a slot"
    assert _gaps(pacer.grants) == [2.0, 2.0, 2.0]


def test_a_cross_host_hop_is_admitted_as_a_new_host(
    monkeypatch: pytest.MonkeyPatch, vclock: VirtualClock,
) -> None:
    """A hop onto another host is that host's first contact: it counts against the cap (here 1 a
    minute, so the hop waits 60 s for the window) and in `new_hosts_contacted`."""
    chain = {"https://a.example/products/x": "https://b.example/products/x"}
    transport, seen = _redirect_chain(chain)
    monkeypatch.setattr(svc.httpx, "AsyncClient", lambda *a, **kw: _REAL_ASYNC_CLIENT(*a, transport=transport, **kw))
    pacer = cgp.GlobalOriginPacer(rps=1.0, max_new_hosts_per_minute=1)
    with cgp.installed(pacer):
        asyncio.run(svc._fetch_html("https://a.example/products/x", max_wait=0))
    assert seen == ["https://a.example/products/x", "https://b.example/products/x"]
    assert pacer.new_hosts == 2 and set(pacer._contacted) == {"a.example", "b.example"}
    assert pacer.grants == [0.0, 60.0]


def test_a_crawl_fetch_follows_at_most_five_hops(monkeypatch: pytest.MonkeyPatch) -> None:
    chain = {f"https://a.example/p{i}": f"https://a.example/p{i + 1}" for i in range(6)}
    transport, seen = _redirect_chain(chain)
    monkeypatch.setattr(svc.httpx, "AsyncClient", lambda *a, **kw: _REAL_ASYNC_CLIENT(*a, transport=transport, **kw))
    with pytest.raises(httpx.TooManyRedirects):
        asyncio.run(svc._fetch_html("https://a.example/p0", max_wait=0))
    assert len(seen) == 6, "the first request and five hops; httpx's default would allow 20"


def test_a_robots_redirect_hop_takes_a_slot_too(monkeypatch: pytest.MonkeyPatch, vclock: VirtualClock) -> None:
    monkeypatch.setenv("CRAWL_ROBOTS_ENABLED", "true")
    chain = {"https://a.example/robots.txt": "https://www.a.example/robots.txt"}
    transport, seen = _redirect_chain(chain, final_status=404)
    token = cp.ROBOTS_TRANSPORT_FACTORY.set(lambda: transport)
    try:
        pacer = cgp.GlobalOriginPacer(rps=0.5, max_new_hosts_per_minute=_NO_CAP)
        with cgp.installed(pacer):
            asyncio.run(cp.robots_allows("https://a.example/products/x", user_agent="PivotaBot"))
    finally:
        cp.ROBOTS_TRANSPORT_FACTORY.reset(token)
    assert len(seen) == 2 and pacer.requests == 2
    assert set(pacer._contacted) == {"a.example", "www.a.example"}


def test_no_pacer_means_the_hook_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    chain = {"https://a.example/products/x": "https://b.example/products/x"}
    transport, seen = _redirect_chain(chain)
    monkeypatch.setattr(svc.httpx, "AsyncClient", lambda *a, **kw: _REAL_ASYNC_CLIENT(*a, transport=transport, **kw))
    asyncio.run(svc._fetch_html("https://a.example/products/x", max_wait=0))
    assert len(seen) == 2


# ===================================================================== review 2026-10-17: L2, M5

def test_a_robots_429_arms_the_hosts_backoff_and_feeds_the_breaker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRAWL_ROBOTS_ENABLED", "true")
    monkeypatch.setenv("CRAWL_BACKOFF_BASE_SECONDS", "30")
    monkeypatch.setenv("CRAWL_MAX_BACKOFF_SECONDS", "300")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"server": "cloudflare", "cf-mitigated": "challenge"}, request=request)

    token = cp.ROBOTS_TRANSPORT_FACTORY.set(lambda: httpx.MockTransport(handler))
    breaker = cit.StagedIpThrottleBreaker(challenge_trip_hosts=3)
    try:
        with cit.installed(breaker):
            asyncio.run(cp.robots_allows("https://r.example/products/x", user_agent="PivotaBot"))
    finally:
        cp.ROBOTS_TRANSPORT_FACTORY.reset(token)
    assert cp.consecutive_blocks("r.example") == 1
    assert cp._STATE["r.example"].backoff_until > time.monotonic() + 20
    assert breaker.challenge_responses == 1 and "r.example" in breaker.challenge_hosts


def test_a_robots_200_does_not_clear_a_page_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRAWL_ROBOTS_ENABLED", "true")
    monkeypatch.setenv("CRAWL_BACKOFF_BASE_SECONDS", "30")
    monkeypatch.setenv("CRAWL_MAX_BACKOFF_SECONDS", "300")
    cp.note_response("https://r.example/products/x", 429)
    token = cp.ROBOTS_TRANSPORT_FACTORY.set(
        lambda: httpx.MockTransport(lambda request: httpx.Response(200, text="User-agent: *\n", request=request))
    )
    try:
        asyncio.run(cp.robots_allows("https://r.example/products/x", user_agent="PivotaBot"))
    finally:
        cp.ROBOTS_TRANSPORT_FACTORY.reset(token)
    assert cp.consecutive_blocks("r.example") == 1
    assert cp._STATE["r.example"].backoff_until > time.monotonic() + 20


def test_a_403_challenge_leaves_a_line_with_its_host(caplog) -> None:
    with caplog.at_level("WARNING"):
        cp.note_response("https://ch.example/products/x", 403, headers={"cf-mitigated": "challenge", "server": "cloudflare"})
        cp.note_response("https://plain.example/products/x", 403, headers={"server": "nginx"})
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("crawl challenge:")]
    assert lines == ["crawl challenge: ch.example returned 403 [server=cloudflare cf-mitigated=challenge]"]


def test_an_excluded_host_never_counts_toward_a_trip(vclock: VirtualClock) -> None:
    breaker, events = _breaker(excluded_hosts=["www.always.example", "never.example"])
    for host in ("always.example", "never.example", "a.example", "b.example"):
        breaker.observe(host, 403, {"cf-mitigated": "challenge"})
    assert events == [], "two counted hosts, two excluded: no trip"
    breaker.observe("c.example", 403, {"cf-mitigated": "challenge"})
    assert len(events) == 1 and events[0]["challenge_hosts"] == 3
    summary = breaker.summary()
    assert summary["ip_throttle_excluded_challenge_responses"] == 2
    assert summary["ip_throttle_challenge_excluded_hosts"] == ["always.example", "never.example"]
    assert summary["ip_throttle_challenge_top_hosts"]["always.example"] == 1


def test_an_excluded_hosts_429_leaves_the_throttle_window_too(vclock: VirtualClock) -> None:
    breaker, events = _breaker(excluded_hosts="x.example", trip_hosts=2, challenge_trip_hosts=0)
    breaker.observe("x.example", 429, {"retry-after": "60"})
    breaker.observe("y.example", 429, {"retry-after": "60"})
    assert events == []
    breaker.observe("z.example", 429, {"retry-after": "60"})
    assert len(events) == 1


def test_the_exclusion_list_reads_its_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert cit.StagedIpThrottleBreaker.from_env().excluded_hosts == set(), "empty by default"
    monkeypatch.setenv("CRAWL_IP_CHALLENGE_EXCLUDED_HOSTS", " WWW.A.example, b.example  c.example,")
    assert cit.StagedIpThrottleBreaker.from_env().excluded_hosts == {"a.example", "b.example", "c.example"}


def test_the_challenge_top_hosts_reach_the_ip_throttle_line(
    monkeypatch: pytest.MonkeyPatch, vclock: VirtualClock,
) -> None:
    rows = [("c0.example", CHALLENGE), ("c0.example", CHALLENGE), ("ok.example", OK)]
    summary, _s, _r, _p = _drive(monkeypatch, vclock, rows, max_new_hosts_per_minute=_NO_CAP)
    line = job.ip_throttle_line(summary)
    assert line["ip_throttle_challenge_top_hosts"] == {"c0.example": 2}
    assert line["global_pacer_disabled"] is False


class OrderedClock:
    """A virtual clock whose sleepers wake IN ORDER, each at its own instant. `VirtualClock` adds
    concurrent sleeps up, which is fine for one sleeper at a time but lets a waiter wake "after" a
    cool-down it should have seen begin; this one resolves the earliest wake-up only once every task
    is blocked."""

    def __init__(self) -> None:
        self.t = 0.0
        self._sleepers: List[Tuple[float, int, "asyncio.Future[None]"]] = []
        self._seq = 0

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        fut = asyncio.get_running_loop().create_future()
        self._seq += 1
        self._sleepers.append((self.t + max(0.0, seconds), self._seq, fut))
        await fut

    async def drive(self, work) -> Any:
        task = asyncio.ensure_future(work)
        while not task.done():
            for _ in range(50):
                await asyncio.sleep(0)
            if task.done() or not self._sleepers:
                continue
            self._sleepers.sort(key=lambda s: (s[0], s[1]))
            wake, _seq, fut = self._sleepers.pop(0)
            self.t = max(self.t, wake)
            fut.set_result(None)
        return task.result()


def test_first_contacts_refused_by_a_cool_down_are_counted_again_after_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """Controller mutant P20: four first-contact requests are waiting for their slots when a trip
    lands. The three that had not gone out must NOT keep their pre-trip first-contact records: they
    re-enter the new-host cap when they really start, after the cool-down, so the cap still holds
    over everything that starts then (4 a minute here)."""
    clock = OrderedClock()
    monkeypatch.setattr(cgp, "_now", clock.now)
    monkeypatch.setattr(cgp, "_sleep", clock.sleep)
    monkeypatch.setattr(cgp, "_random", lambda: 0.5)
    pacer = cgp.GlobalOriginPacer(rps=1.0, max_new_hosts_per_minute=4)
    starts: Dict[str, float] = {}

    async def first(host: str) -> None:
        await pacer.acquire(host)
        starts.setdefault(host, pacer.grants[-1])  # no await between the append and the return

    async def trip_at_half_a_second() -> None:
        await clock.sleep(0.5)
        pacer.pause(900)  # the trip lands while w1..w3 wait for their slots (1, 2, 3 s)

    async def run() -> None:
        await asyncio.gather(trip_at_half_a_second(), *(first(f"w{i}.example") for i in range(4)))
        for i in range(4):
            await first(f"n{i}.example")

    asyncio.run(clock.drive(run()))
    assert starts["w0.example"] == 0.0, "the one that went out before the trip"
    late = sorted(t for h, t in starts.items() if h != "w0.example")
    assert len(late) == 7 and min(late) >= 900.5, late
    for start in late:
        assert sum(1 for t in late if start <= t < start + 60) <= 4, late


# ===================================================================== review 2026-10-17 (2): host challenge streak

@pytest.mark.parametrize("excluded", [False, True], ids=["plain", "excluded_from_the_ip_rule"])
def test_a_host_challenging_every_request_is_dropped_after_three(
    monkeypatch: pytest.MonkeyPatch, vclock: VirtualClock, excluded: bool,
) -> None:
    """The controller's heavy-host repro, rebuilt: one host answers 403 + `cf-mitigated: challenge`
    to every request (ichibanm.com: 239 of 239 in one run, never stopped). After three in a row its
    lane is drained: the other 17 rows are never handed to the refresher (unstamped) and are counted
    per host. Excluding the host from the IP rule does not keep it in the queue."""
    if excluded:
        monkeypatch.setenv("CRAWL_IP_CHALLENGE_EXCLUDED_HOSTS", "heavy.example")
    rows = [("heavy.example", CHALLENGE)] * 20 + [(f"ok{i}.example", OK) for i in range(3)]
    summary, stamped, requests, _pacer = _drive(monkeypatch, vclock, rows, max_new_hosts_per_minute=_NO_CAP)
    heavy = [u for _t, u in requests if "heavy.example" in u]
    assert len(heavy) == 3, heavy
    assert len([s for s in stamped if int(s.split("_")[1]) < 20]) == 3
    assert summary["skipped_for_host_challenge"] == 17
    assert summary["host_challenge_skips"] == {"heavy.example": 17}
    assert summary["host_challenge_trip"] == 3
    assert summary["attempted_count"] == len(rows) - 17, "passed-over rows leave the yield denominator"
    assert summary["status"] != "ip_throttled", "one host is not an IP storm"
    assert all(f"https://ok{i}.example/products/p{20 + i}" in [u for _t, u in requests] for i in range(3))


def test_an_answer_that_is_not_a_challenge_breaks_the_streak(monkeypatch: pytest.MonkeyPatch, vclock: VirtualClock) -> None:
    rows = [("h.example", CHALLENGE), ("h.example", CHALLENGE), ("h.example", OK)] * 3
    summary, stamped, _requests, _pacer = _drive(monkeypatch, vclock, rows, max_new_hosts_per_minute=_NO_CAP)
    assert len(stamped) == len(rows) and summary["skipped_for_host_challenge"] == 0


def test_the_challenge_trip_reads_its_env_and_zero_is_not_off(monkeypatch: pytest.MonkeyPatch) -> None:
    assert err._host_challenge_trip() == 3
    monkeypatch.setenv("EXTERNAL_REFERRAL_REFRESH_HOST_CHALLENGE_TRIP", "5")
    assert err._host_challenge_trip() == 5
    for bad in ("0", "-1", "nan", "x"):
        monkeypatch.setenv("EXTERNAL_REFERRAL_REFRESH_HOST_CHALLENGE_TRIP", bad)
        assert err._host_challenge_trip() == 3, bad
    monkeypatch.setenv("EXTERNAL_REFERRAL_REFRESH_HOST_CHALLENGE_TRIP", "1.2")
    assert err._host_challenge_trip() == 2


def test_the_challenge_streak_counts_any_status_and_robots_never_resets_it() -> None:
    challenge = {"cf-mitigated": "challenge", "server": "cloudflare"}
    cp.note_response("https://s.example/products/a", 403, headers=challenge)
    cp.note_response("https://s.example/products/b", 429, headers=challenge)
    assert cp.consecutive_challenges("s.example") == 2
    cp.note_response("https://s.example/robots.txt", 404, headers={}, clears=False)
    assert cp.consecutive_challenges("s.example") == 2, "a robots answer says nothing about the pages"
    cp.note_response("https://s.example/robots.txt", 403, headers=challenge, clears=False)
    assert cp.consecutive_challenges("s.example") == 3, "a challenged robots.txt is still a challenge"
    cp.note_response("https://s.example/products/c", 200, headers={})
    assert cp.consecutive_challenges("s.example") == 0


# ===================================================================== review 2026-10-17 (2): hops obey robots + host slot

def _hop_site(robots: Dict[str, str], chain: Dict[str, str]):
    seen: List[Tuple[float, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append((time.monotonic(), url))
        if request.url.path == "/robots.txt":
            body = robots.get(request.url.host)
            return httpx.Response(200, text=body, request=request) if body is not None else httpx.Response(404, request=request)
        if url in chain:
            return httpx.Response(301, headers={"location": chain[url]}, request=request)
        return httpx.Response(200, content=b"<html></html>", request=request)

    return httpx.MockTransport(handler), seen


def test_a_cross_host_hop_is_refused_when_its_robots_disallow_it(monkeypatch: pytest.MonkeyPatch, vclock: VirtualClock) -> None:
    """The controller's repro: a.example/p redirects to c.example/p, and c.example serves
    `Disallow: /`. The hop is refused (RobotsDisallowed out of the fetch), c.example/p never asked."""
    monkeypatch.setenv("CRAWL_ROBOTS_ENABLED", "true")
    transport, seen = _hop_site({"c.example": "User-agent: *\nDisallow: /\n"},
                                {"https://a.example/p": "https://c.example/p"})
    monkeypatch.setattr(svc.httpx, "AsyncClient", lambda *a, **kw: _REAL_ASYNC_CLIENT(*a, transport=transport, **kw))
    pacer = cgp.GlobalOriginPacer(rps=1.0, max_new_hosts_per_minute=_NO_CAP)
    with cgp.installed(pacer):
        with pytest.raises(cp.RobotsDisallowed):
            asyncio.run(svc._fetch_html("https://a.example/p", max_wait=0))
    urls = [u for _t, u in seen]
    assert "https://c.example/robots.txt" in urls and "https://c.example/p" not in urls


def test_a_cross_host_hop_its_robots_allow_goes_ahead(monkeypatch: pytest.MonkeyPatch, vclock: VirtualClock) -> None:
    monkeypatch.setenv("CRAWL_ROBOTS_ENABLED", "true")
    transport, seen = _hop_site({"c.example": "User-agent: *\nDisallow: /private\n"},
                                {"https://a.example/p": "https://c.example/p"})
    monkeypatch.setattr(svc.httpx, "AsyncClient", lambda *a, **kw: _REAL_ASYNC_CLIENT(*a, transport=transport, **kw))
    pacer = cgp.GlobalOriginPacer(rps=1.0, max_new_hosts_per_minute=_NO_CAP)
    with cgp.installed(pacer):
        asyncio.run(svc._fetch_html("https://a.example/p", max_wait=0))
    assert [u for _t, u in seen][-1] == "https://c.example/p"


def test_a_hop_waits_for_its_hosts_own_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real clock, scaled down: a same-host hop pays the host's 0.3 s interval, not just a global
    slot (0.02 s here)."""
    monkeypatch.setenv("CRAWL_MIN_INTERVAL_SECONDS", "0.3")
    monkeypatch.setattr(cgp, "RPS_MAX", 100.0)
    transport, seen = _hop_site({}, {"https://a.example/x": "https://a.example/y"})
    monkeypatch.setattr(svc.httpx, "AsyncClient", lambda *a, **kw: _REAL_ASYNC_CLIENT(*a, transport=transport, **kw))
    pacer = cgp.GlobalOriginPacer(rps=50.0, max_new_hosts_per_minute=_NO_CAP, jitter=0.0)
    with cgp.installed(pacer):
        asyncio.run(svc._fetch_html("https://a.example/x", max_wait=0))
    (t0, first), (t1, hop) = seen
    assert (first, hop) == ("https://a.example/x", "https://a.example/y")
    assert t1 - t0 >= 0.27, f"the hop went {t1 - t0:.3f}s after the first request"


def test_a_hop_waits_out_its_hosts_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """b.example answered 429 a moment ago (0.4 s hold): a hop onto it waits the hold out."""
    monkeypatch.setenv("CRAWL_BACKOFF_BASE_SECONDS", "0.4")
    monkeypatch.setenv("CRAWL_MAX_BACKOFF_SECONDS", "5")
    monkeypatch.setattr(cgp, "RPS_MAX", 100.0)
    transport, seen = _hop_site({}, {"https://a.example/x": "https://b.example/x"})
    monkeypatch.setattr(svc.httpx, "AsyncClient", lambda *a, **kw: _REAL_ASYNC_CLIENT(*a, transport=transport, **kw))
    cp.note_response("https://b.example/other", 429)
    pacer = cgp.GlobalOriginPacer(rps=50.0, max_new_hosts_per_minute=_NO_CAP, jitter=0.0)
    with cgp.installed(pacer):
        asyncio.run(svc._fetch_html("https://a.example/x", max_wait=0))
    (t0, _first), (t1, hop) = seen
    assert hop == "https://b.example/x" and t1 - t0 >= 0.3, f"{t1 - t0:.3f}s"


def test_a_row_that_never_sent_a_request_gives_its_new_host_place_back(
    monkeypatch: pytest.MonkeyPatch, vclock: VirtualClock,
) -> None:
    """Controller review (LOW), on the real scheduler: one new host a minute. gone.example's row
    raises SEED_NOT_FOUND before any request, so its host was taken but never contacted. Its place
    is released when the row ends, and next.example starts at once instead of a minute later."""
    from fastapi import HTTPException

    rows = [("gone.example", OK), ("next.example", OK)]
    seed_ids = ["eps_0", "eps_1"]
    hosts = {"eps_0": "gone.example", "eps_1": "next.example"}
    monkeypatch.setattr(err, "get_external_referral_refresh_candidate_seed_ids",
                        lambda *a, **k: asyncio.sleep(0, result=seed_ids))
    monkeypatch.setattr(err, "_fetch_refresh_candidate_hosts", lambda ids: asyncio.sleep(0, result=dict(hosts)))
    pacers: List[cgp.GlobalOriginPacer] = []
    real = cgp.GlobalOriginPacer
    monkeypatch.setattr(err.crawl_global_pacer, "GlobalOriginPacer",
                        lambda **kw: pacers.append(real(**kw)) or pacers[-1])

    async def refresh(seed_id: str) -> Dict[str, Any]:
        if seed_id == "eps_0":
            raise HTTPException(status_code=404, detail="SEED_NOT_FOUND")
        await pacers[0].acquire(hosts[seed_id])
        return {"status": "success", "price_refresh": {"status": "unchanged"}, "projection": {}}

    summary = asyncio.run(err.run_external_referral_refresh_batch(
        refresh_seed_by_id=refresh, limit=len(rows), global_rps=1.0, max_new_hosts_per_minute=1,
    ))
    assert summary["unprocessable"] == 1
    assert pacers[0].grants == [0.0], "next.example was not held back by gone.example's unused place"


def test_without_a_pacer_a_hop_never_waits_on_its_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """The interactive route installs no pacer: a hop there must not take on the batch's unbounded
    per-host wait. b.example is in a 2 s backoff; the hop goes at once."""
    monkeypatch.setenv("CRAWL_BACKOFF_BASE_SECONDS", "2")
    monkeypatch.setenv("CRAWL_MAX_BACKOFF_SECONDS", "5")
    transport, seen = _hop_site({}, {"https://a.example/x": "https://b.example/x"})
    monkeypatch.setattr(svc.httpx, "AsyncClient", lambda *a, **kw: _REAL_ASYNC_CLIENT(*a, transport=transport, **kw))
    cp.note_response("https://b.example/other", 429)
    asyncio.run(svc._fetch_html("https://a.example/x", max_wait=0))
    (t0, _first), (t1, hop) = seen
    assert hop == "https://b.example/x" and t1 - t0 < 1.0, f"{t1 - t0:.3f}s"
