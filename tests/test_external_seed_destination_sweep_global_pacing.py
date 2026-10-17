"""Strict global pacing for the external-seed destination sweep (the refresh's rules, be#2640).

Measured 10-14/10-15: the sweep read 5,000 candidates across 228-354 hosts in ~15 min, ~4-5
requests/s from the one crawl NAT address. On 10-16, after Cloudflare challenge-listed that address,
it took 319 backoffs from its first second and ran into its 3,600 s task timeout.

Driven here with NO network: the REAL sweep (`run_destination_sweep`) and the REAL gate
(`crawl_politeness` -> the installed pacer and breaker), with a canned client and a virtual clock.
"""
from __future__ import annotations

import asyncio
import json
import types
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytest

import jobs.external_seed_destination_sweep as job
import services.crawl_global_pacer as cgp
import services.crawl_ip_throttle as cit
import services.crawl_politeness as cp
import services.external_seed_destination_liveness as liveness

_REAL_ASYNC_CLIENT = httpx.AsyncClient
CHALLENGE = {"server": "cloudflare", "cf-mitigated": "challenge"}
# A new-host cap far above anything a test reaches: since 2026-10-17 a cap of 0 is refused (it used
# to mean "off"), so a test that isolates the rate passes this instead.
_NO_CAP = 10_000

_ENV = (
    "EXTERNAL_SEED_DESTINATION_SWEEP_GLOBAL_RPS",
    "EXTERNAL_SEED_DESTINATION_SWEEP_MAX_NEW_HOSTS_PER_MINUTE",
    "EXTERNAL_SEED_DESTINATION_SWEEP_BUDGET_SECONDS",
    "CRAWL_IP_CHALLENGE_TRIP_HOSTS",
    "CRAWL_IP_CHALLENGE_WINDOW_SECONDS",
    "CRAWL_IP_THROTTLE_COOLDOWN_SECONDS",
    "CRAWL_IP_THROTTLE_TRIP_HOSTS",
    "CRAWL_IP_THROTTLE_WINDOW_SECONDS",
    "CRAWL_IP_THROTTLE_BREAKER_ENABLED",
    "CRAWL_SHOPIFY_EDGE_PACER_ENABLED",
    "CRAWL_GLOBAL_PACER_DISABLED",
    "CRAWL_IP_CHALLENGE_EXCLUDED_HOSTS",
)


class VirtualClock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
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
    monkeypatch.setattr(cgp, "_random", lambda: 0.5)  # gaps of exactly 1/rps
    # The sweep's own catalogue-retry sleeps (3 s, 6 s) pass instantly; nothing else changes.
    fast = types.SimpleNamespace(
        sleep=lambda _s: asyncio.sleep(0), gather=asyncio.gather, Semaphore=asyncio.Semaphore,
    )
    monkeypatch.setattr(liveness, "asyncio", fast)
    return clock


def _gaps(instants: List[float]) -> List[float]:
    ordered = sorted(instants)
    return [round(b - a, 6) for a, b in zip(ordered, ordered[1:])]


class _Client:
    """Per host: page 1 lists handle `a`, page 2 is empty; `/products/b` (delisted) answers 404.
    `special[url]` overrides with a list of (status, headers) answered in order."""

    def __init__(
        self,
        clock: VirtualClock,
        special: Optional[Dict[str, List[Tuple[int, Dict[str, str]]]]] = None,
        slow: Optional[Dict[str, float]] = None,
    ):
        self.clock = clock
        self.special = {k: list(v) for k, v in (special or {}).items()}
        # url -> seconds the answer takes (the clock moves on by that much).
        self.slow = dict(slow or {})
        self.requests: List[Tuple[float, str]] = []

    async def get(self, url, headers=None):  # noqa: ANN001
        self.requests.append((self.clock.t, url))
        self.clock.t += self.slow.get(url, 0.0)
        if self.special.get(url):
            status, hdrs = self.special[url].pop(0)
            resp = httpx.Response(status, json={"products": [{"handle": "a"}]}, headers=hdrs)
        elif "/products.json" in url:
            products = [{"handle": "a"}] if "page=1" in url else []
            resp = httpx.Response(200, json={"products": products})
        else:
            resp = httpx.Response(404)
        resp.request = httpx.Request("GET", url)
        return resp

    async def aclose(self):
        return None


def _seeds(hosts: List[str]) -> List[Dict[str, Any]]:
    seeds = []
    for host in hosts:
        seeds.append({"id": f"{host}:a", "canonical_url": f"https://{host}/products/a"})
        seeds.append({"id": f"{host}:b", "canonical_url": f"https://{host}/products/b"})
    return seeds


def _sweep(monkeypatch, client, seeds, *, concurrency: Optional[int] = None, **kwargs):
    # The storm fixtures trip on three challenge hosts; the shipped default (8) is pinned in
    # test_the_env_and_flag_defaults, which passes nothing.
    if kwargs.pop("shipped_defaults", False) is False:
        kwargs.setdefault("ip_challenge_trip_hosts", 3)
    recorded: List[str] = []

    async def fake_candidates(limit):
        return seeds

    async def fake_record(seed_id, observation, *, now=None, suppress=False):
        recorded.append(seed_id)
        return {"seed_id": seed_id, "verdict": observation.verdict, "retire": False}

    async def fake_retire(seed_id, observation, *, now=None):
        return {"retired": True}

    monkeypatch.setattr(liveness, "get_sweep_candidates", fake_candidates)
    monkeypatch.setattr(liveness, "record_destination_observation", fake_record)
    monkeypatch.setattr(liveness, "retire_seed_for_dead_destination", fake_retire)
    if concurrency is not None:
        monkeypatch.setattr(liveness, "SWEEP_HOST_CONCURRENCY", concurrency)
    pacers: List[cgp.GlobalOriginPacer] = []
    real = cgp.GlobalOriginPacer

    def capture(**kw):
        pacer = real(**kw)
        pacers.append(pacer)
        return pacer

    monkeypatch.setattr(liveness.crawl_global_pacer, "GlobalOriginPacer", capture)
    summary = asyncio.run(liveness.run_destination_sweep(client=client, **kwargs))
    return summary, recorded, pacers[0]


# ===================================================================== the bucket


def test_every_request_of_the_sweep_takes_a_slot_across_four_host_workers(monkeypatch, vclock) -> None:
    client = _Client(vclock)
    hosts = [f"h{i}.example" for i in range(6)]
    summary, recorded, pacer = _sweep(monkeypatch, client, _seeds(hosts), max_new_hosts_per_minute=_NO_CAP)
    # Two catalogue pages and one probe (the delisted `b`) per host.
    assert len(client.requests) == 18
    assert pacer.requests == 18, "every request the origin saw went through the bucket"
    assert _gaps(pacer.grants) == [2.0] * 17
    assert len(recorded) == 12
    assert summary["global_rps"] == 0.5 and summary["paced_requests"] == 18
    assert summary["probed"] == 6 and summary["listed"] == 6


def test_catalogue_retries_and_robots_reads_are_paced_too(monkeypatch, vclock) -> None:
    monkeypatch.setenv("CRAWL_ROBOTS_ENABLED", "true")
    robots: List[str] = []

    def robots_handler(request: httpx.Request) -> httpx.Response:
        robots.append(str(request.url))
        return httpx.Response(404, request=request)

    transport = httpx.MockTransport(robots_handler)
    monkeypatch.setattr(cp.httpx, "AsyncClient", lambda *a, **kw: _REAL_ASYNC_CLIENT(*a, transport=transport, **kw))
    page1 = "https://r.example/products.json?limit=250&page=1"
    client = _Client(vclock, {page1: [(429, {}), (200, {})]})
    summary, _recorded, pacer = _sweep(monkeypatch, client, _seeds(["r.example"]), max_new_hosts_per_minute=_NO_CAP)
    assert robots == ["https://r.example/robots.txt"]
    # robots + page 1 (429) + its retry + page 2 + the probe.
    assert len(client.requests) == 4 and pacer.requests == 5
    assert _gaps(pacer.grants) == [2.0] * 4


def test_the_sweep_never_contacts_more_than_six_new_hosts_a_minute(monkeypatch, vclock) -> None:
    """At the fastest rate allowed (1.0/s; 0 is no longer "unpaced"). Measured on the reserved
    first-request starts (`_contacted`): `VirtualClock` adds concurrent sleeps up, so the canned
    client's clock reading overstates with four host workers."""
    client = _Client(vclock)
    hosts = [f"h{i}.example" for i in range(20)]
    summary, _recorded, pacer = _sweep(monkeypatch, client, _seeds(hosts), global_rps=1.0)
    assert {httpx.URL(u).host for _t, u in client.requests} == set(pacer._contacted)
    instants = sorted(pacer._contacted.values())
    assert len(instants) == 20
    for start in instants:
        assert sum(1 for t in instants if start <= t < start + 60) <= 6, instants
    assert summary["new_hosts_contacted"] == 20 and summary["max_new_hosts_per_minute"] == 6


# ===================================================================== the breaker


def test_a_challenge_storm_cools_down_at_half_rate_and_ends_on_the_second_trip(monkeypatch, vclock) -> None:
    hosts = ["c0.example", "c1.example", "c2.example", "ok.example",
             "d0.example", "d1.example", "d2.example", "late.example"]
    special = {
        f"https://{h}/products.json?limit=250&page=1": [(403, CHALLENGE)]
        for h in hosts if h[0] in "cd"
    }
    client = _Client(vclock, special)
    summary, recorded, pacer = _sweep(
        monkeypatch, client, _seeds(hosts), concurrency=1, max_new_hosts_per_minute=_NO_CAP,
    )
    times = [t for t, _ in client.requests]
    seen_hosts = [httpx.URL(u).host for _t, u in client.requests]
    assert times[:3] == [0.0, 2.0, 4.0]
    assert times[3] >= 904.0, "nothing started during the cool-down"
    assert _gaps(times[3:]) == [4.0] * (len(times) - 4), "half rate after it"
    assert summary["breaker_trips"] == 2 and summary["cooldowns"] == 1
    assert summary["ip_throttled"] is True and summary["final_rate"] == 0.25
    assert "late.example" not in seen_hosts
    assert summary["hosts_not_started"] == 1
    assert summary["catalogue_status"].get(liveness.CATALOGUE_BOT_CHALLENGE) == 6
    assert summary["ip_throttle_challenge_responses"] == 6
    assert set(recorded) == {"ok.example:a", "ok.example:b"}


def test_the_sweeps_answers_reach_the_breaker_with_their_headers(monkeypatch, vclock) -> None:
    """`note_response` gets the response headers, so `cf-mitigated: challenge` on a catalogue read
    counts: three challenged hosts trip the cool-down, and those hosts are held back after it."""
    hosts = ["c0.example", "c1.example", "c2.example", "ok.example"]
    special = {f"https://{h}/products.json?limit=250&page=1": [(403, CHALLENGE)] for h in hosts[:3]}
    client = _Client(vclock, special)
    breaker_ref: List[cit.StagedIpThrottleBreaker] = []
    real_from_env = cit.StagedIpThrottleBreaker.from_env

    def capture(**kw):
        breaker = real_from_env(**kw)
        breaker_ref.append(breaker)
        return breaker

    monkeypatch.setattr(liveness.crawl_ip_throttle.StagedIpThrottleBreaker, "from_env", staticmethod(capture))
    summary, _recorded, _pacer = _sweep(
        monkeypatch, client, _seeds(hosts), concurrency=1, max_new_hosts_per_minute=_NO_CAP,
    )
    breaker = breaker_ref[0]
    assert breaker.trips == 1 and summary["ip_throttled"] is False
    assert all(breaker.blocks(h) for h in hosts[:3]) and not breaker.blocks("ok.example")


def test_held_back_hosts_are_skipped_at_take_time(monkeypatch, vclock) -> None:
    """Two separate seeds groups cannot share a host, so drive the hold-back directly: a host the
    breaker blocks before its turn is skipped and counted, never asked."""
    hosts = ["c0.example", "c1.example", "c2.example", "c0.example.alt"]
    special = {f"https://{h}/products.json?limit=250&page=1": [(403, CHALLENGE)] for h in hosts[:3]}
    client = _Client(vclock, special)
    real_blocks = cit.StagedIpThrottleBreaker.blocks
    monkeypatch.setattr(
        cit.StagedIpThrottleBreaker, "blocks",
        lambda self, host: real_blocks(self, host) or (self.trips > 0 and host.endswith(".alt")),
    )
    summary, _recorded, _pacer = _sweep(
        monkeypatch, client, _seeds(hosts), concurrency=1, max_new_hosts_per_minute=_NO_CAP,
    )
    assert summary["hosts_skipped_ip_throttle"] == 1
    assert not any("c0.example.alt" in u for _t, u in client.requests)


# ===================================================================== the budget


def test_no_request_starts_after_the_budget_and_nothing_is_recorded_for_it(monkeypatch, vclock) -> None:
    """Budget 9 s at 0.5/s: requests at 0, 2, 4, 6, 8; the one due at 10 is never sent. A host cut
    short records nothing for the seed whose probe was refused, and is not 'unverifiable'."""
    hosts = [f"h{i}.example" for i in range(4)]
    client = _Client(vclock)
    summary, recorded, pacer = _sweep(
        monkeypatch, client, _seeds(hosts), concurrency=1, budget_seconds=9, max_new_hosts_per_minute=_NO_CAP,
    )
    assert [t for t, _ in client.requests] == [0.0, 2.0, 4.0, 6.0, 8.0]
    # h0: pages 1, 2, probe b (3 requests, done). h1: pages 1, 2, then its probe is refused.
    assert recorded == ["h0.example:a", "h0.example:b", "h1.example:a"]
    assert summary["hosts_stopped"] == 1 and summary["hosts_not_started"] == 2
    assert summary["hosts_unverifiable"] == 0, "not asking is not failing to read"
    assert summary["stopped"] == "budget spent" and summary["budget_seconds"] == 9.0


def test_the_request_due_exactly_at_the_budget_is_not_sent(monkeypatch, vclock) -> None:
    hosts = [f"h{i}.example" for i in range(3)]
    client = _Client(vclock)
    summary, recorded, _pacer = _sweep(
        monkeypatch, client, _seeds(hosts), concurrency=1, budget_seconds=8, max_new_hosts_per_minute=_NO_CAP,
    )
    # h0: page 1, page 2, probe (0, 2, 4). h1: page 1 at 6; its page 2, due at 8, is refused.
    assert [t for t, _ in client.requests] == [0.0, 2.0, 4.0, 6.0]
    assert summary["hosts_stopped"] == 1 and summary["hosts_not_started"] == 1


def test_a_host_cut_short_mid_catalogue_is_stopped_not_unverifiable(monkeypatch, vclock) -> None:
    client = _Client(vclock)
    summary, recorded, _pacer = _sweep(
        monkeypatch, client, _seeds(["h0.example", "h1.example"]), concurrency=1, budget_seconds=1,
        max_new_hosts_per_minute=_NO_CAP,
    )
    assert len(client.requests) == 1, "page 1 only; page 2 was due after the budget"
    assert summary["hosts_stopped"] == 1 and summary["hosts_unverifiable"] == 0
    assert summary["catalogue_status"] == {}, "no catalogue verdict for a read we stopped"
    assert not recorded


def test_no_host_starts_once_the_budget_has_passed(monkeypatch, vclock) -> None:
    """h0's probe answers slowly (50 s), carrying the clock past a 30 s budget. h1 is not started
    at all: it is not a host cut short, it is one never begun."""
    client = _Client(vclock, slow={"https://h0.example/products/b": 50.0})
    summary, _recorded, _pacer = _sweep(
        monkeypatch, client, _seeds(["h0.example", "h1.example"]), concurrency=1, budget_seconds=30,
        max_new_hosts_per_minute=_NO_CAP,
    )
    assert not any("h1.example" in u for _t, u in client.requests)
    assert summary["hosts_not_started"] == 1 and summary["hosts_stopped"] == 0
    assert summary["stopped"] == "budget spent"


def test_a_challenge_on_a_probe_counts_and_the_final_trip_stops_the_host_mid_way(monkeypatch, vclock) -> None:
    """The third challenged host is challenged on a PDP probe, not its catalogue: the probe's
    headers reach the breaker. That trip is the last (no cool-down), so the host's next probe,
    already queued in the same worker, is never sent."""
    monkeypatch.setenv("CRAWL_IP_THROTTLE_COOLDOWN_SECONDS", "0")
    special = {
        "https://c0.example/products.json?limit=250&page=1": [(403, CHALLENGE)],
        "https://c1.example/products.json?limit=250&page=1": [(403, CHALLENGE)],
        "https://x.example/products/b": [(403, CHALLENGE)],
    }
    seeds = _seeds(["c0.example", "c1.example"]) + [
        {"id": "x:a", "canonical_url": "https://x.example/products/a"},
        {"id": "x:b", "canonical_url": "https://x.example/products/b"},
        {"id": "x:c", "canonical_url": "https://x.example/products/c"},
    ]
    client = _Client(vclock, special)
    summary, recorded, _pacer = _sweep(
        monkeypatch, client, seeds, concurrency=1, max_new_hosts_per_minute=_NO_CAP,
    )
    urls = [u for _t, u in client.requests]
    assert "https://x.example/products/b" in urls
    assert "https://x.example/products/c" not in urls
    assert summary["ip_throttled"] is True and summary["breaker_trips"] == 1
    assert summary["hosts_stopped"] == 1
    assert "x:c" not in recorded


def test_a_cool_down_past_the_budget_stops_the_run(monkeypatch, vclock) -> None:
    hosts = ["c0.example", "c1.example", "c2.example", "ok.example"]
    special = {f"https://{h}/products.json?limit=250&page=1": [(403, CHALLENGE)] for h in hosts[:3]}
    client = _Client(vclock, special)
    summary, recorded, _pacer = _sweep(
        monkeypatch, client, _seeds(hosts), concurrency=1, budget_seconds=600, max_new_hosts_per_minute=_NO_CAP,
    )
    assert len(client.requests) == 3 and not recorded
    assert summary["breaker_trips"] == 1 and summary["stopped"]


def test_every_pacer_deadline_is_hard(vclock) -> None:
    """This branch's opt-in `hard_deadline` became the rule for every run in be#2640 (review L1):
    a slot at or past the deadline stops the pacer instead of being slept to."""
    pacer = cgp.GlobalOriginPacer(rps=0.5, max_new_hosts_per_minute=_NO_CAP, deadline=3.0)

    async def three():
        for _ in range(3):
            await pacer.acquire("a.example")

    with pytest.raises(cgp.GlobalPaceStopped):
        asyncio.run(three())
    assert pacer.grants == [0.0, 2.0] and pacer.stop_reason == "budget ends before the next slot"


# ===================================================================== defaults, flags, summary


def test_the_env_and_flag_defaults(monkeypatch, vclock) -> None:
    client = _Client(vclock)
    summary, _r, _p = _sweep(monkeypatch, client, _seeds(["a.example"]), shipped_defaults=True)
    assert (summary["global_rps"], summary["max_new_hosts_per_minute"], summary["budget_seconds"]) == (
        0.5, 6, 3300.0,
    )
    assert summary["ip_throttle_challenge_trip_hosts"] == 8
    assert summary["ip_throttle_challenge_window_seconds"] == 120.0
    assert summary["ip_throttle_cooldown_seconds"] == 900.0
    cgp.reset_for_tests()
    monkeypatch.setenv("EXTERNAL_SEED_DESTINATION_SWEEP_GLOBAL_RPS", "0.2")
    monkeypatch.setenv("EXTERNAL_SEED_DESTINATION_SWEEP_MAX_NEW_HOSTS_PER_MINUTE", "3")
    monkeypatch.setenv("EXTERNAL_SEED_DESTINATION_SWEEP_BUDGET_SECONDS", "1200")
    summary, _r, _p = _sweep(monkeypatch, _Client(vclock), _seeds(["a.example"]))
    assert (summary["global_rps"], summary["max_new_hosts_per_minute"], summary["budget_seconds"]) == (
        0.2, 3, 1200.0,
    )
    # 0 IS NOT OFF (review H1): flag or env, it is an ERROR and the default.
    summary, _r, _p = _sweep(
        monkeypatch, _Client(vclock), _seeds(["a.example"]), global_rps=0, budget_seconds=0,
        max_new_hosts_per_minute=0,
    )
    assert (summary["global_rps"], summary["max_new_hosts_per_minute"], summary["budget_seconds"]) == (
        0.5, 6, 3300.0,
    )
    for bad in ("abc", "nan", "-1", "0", "-0"):
        monkeypatch.setenv("EXTERNAL_SEED_DESTINATION_SWEEP_GLOBAL_RPS", bad)
        monkeypatch.setenv("EXTERNAL_SEED_DESTINATION_SWEEP_MAX_NEW_HOSTS_PER_MINUTE", bad)
        monkeypatch.setenv("EXTERNAL_SEED_DESTINATION_SWEEP_BUDGET_SECONDS", bad)
        summary, _r, _p = _sweep(monkeypatch, _Client(vclock), _seeds(["a.example"]))
        assert (summary["global_rps"], summary["max_new_hosts_per_minute"], summary["budget_seconds"]) == (
            0.5, 6, 3300.0,
        ), bad
    monkeypatch.setenv("EXTERNAL_SEED_DESTINATION_SWEEP_GLOBAL_RPS", "4")
    monkeypatch.setenv("EXTERNAL_SEED_DESTINATION_SWEEP_MAX_NEW_HOSTS_PER_MINUTE", "0.9")
    summary, _r, _p = _sweep(monkeypatch, _Client(vclock), _seeds(["a.example"]))
    assert (summary["global_rps"], summary["max_new_hosts_per_minute"]) == (1.0, 1), "clamped; rounded up"


def _main_with(monkeypatch, summary: Dict[str, Any], argv: List[str]):
    seen: Dict[str, Any] = {}

    async def fake_sweep(**kwargs):
        seen.update(kwargs)
        return dict(summary)

    monkeypatch.setattr(job, "run_destination_sweep", fake_sweep)
    monkeypatch.setattr(job.database, "connect", lambda: asyncio.sleep(0))
    monkeypatch.setattr(job.database, "disconnect", lambda: asyncio.sleep(0))
    monkeypatch.setattr("sys.argv", ["external_seed_destination_sweep", "--limit", "1", *argv])
    return job.main(), seen


def test_the_cli_flags_reach_the_sweep_and_the_line_is_printed(monkeypatch, capsys) -> None:
    code, seen = _main_with(monkeypatch, {"hosts": 1, "global_rps": 0.25, "breaker_trips": 0}, [
        "--budget-seconds", "1800", "--global-rps", "0.25", "--max-new-hosts-per-minute", "4",
        "--ip-challenge-trip-hosts", "2", "--ip-challenge-window-seconds", "60",
        "--ip-throttle-cooldown-seconds", "300", "--no-ip-throttle-breaker",
    ])
    assert code == job.EXIT_OK
    assert (seen["budget_seconds"], seen["global_rps"], seen["max_new_hosts_per_minute"]) == (1800.0, 0.25, 4)
    assert (seen["ip_challenge_trip_hosts"], seen["ip_challenge_window_seconds"]) == (2, 60.0)
    assert seen["ip_throttle_cooldown_seconds"] == 300.0 and seen["ip_throttle_enabled"] is False
    line = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("IP_THROTTLE ")]
    assert len(line) == 1 and json.loads(line[0][len("IP_THROTTLE "):])["global_rps"] == 0.25
    _code, seen = _main_with(monkeypatch, {"hosts": 1}, [])
    for key in ("budget_seconds", "global_rps", "max_new_hosts_per_minute", "ip_challenge_trip_hosts",
                "ip_challenge_window_seconds", "ip_throttle_cooldown_seconds", "ip_throttle_enabled"):
        assert seen[key] is None, f"{key}: absent flag leaves the env its say"


def test_an_ip_throttled_sweep_exits_three(monkeypatch, capsys) -> None:
    code, _ = _main_with(monkeypatch, {"hosts": 3, "ip_throttled": True, "breaker_trips": 2}, [])
    assert code == job.EXIT_IP_THROTTLED == 3


_KEYS_BEFORE = {
    "candidates", "dead_first_candidates", "hosts", "hosts_unverifiable", "listed", "probed",
    "dead_links_found", "seeds_retired", "pending_suppressed", "pending_lifted", "verdicts",
    "catalogue_status",
}


def test_the_summary_keeps_its_keys_and_adds_the_pacing_ones(monkeypatch, vclock) -> None:
    summary, _r, _p = _sweep(monkeypatch, _Client(vclock), _seeds(["a.example", "b.example"]))
    assert _KEYS_BEFORE <= set(summary)
    assert {"global_rps", "final_rate", "paces_waited_seconds", "breaker_trips", "cooldowns",
            "hosts_stopped", "hosts_not_started", "hosts_skipped_ip_throttle", "budget_seconds",
            "ip_throttled", "stopped"} <= set(summary)
    assert summary["breaker_trips"] == 0 and summary["stopped"] is None
    assert liveness.coverage_alarm(summary) is None
    json.dumps(summary)


# ===================================================================== review 2026-10-17 (be#2641)

def _real_client(monkeypatch, handler) -> List[str]:
    """The sweep's OWN client (no `client=` passed), so the global pacer's hop hook and hop cap are
    installed exactly as in prod; only the transport is a MockTransport."""
    seen: List[str] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return handler(request)

    transport = httpx.MockTransport(recording)
    monkeypatch.setattr(liveness.httpx, "AsyncClient", lambda *a, **kw: _REAL_ASYNC_CLIENT(*a, transport=transport, **kw))
    return seen


def _www_store(request: httpx.Request) -> httpx.Response:
    """A store whose primary host is www.: the apex 301s every URL to it. Three catalogue pages
    (handle a on page 1, b on 2, c on 3), then an empty page; PDPs answer 200 on their own handle."""
    url = request.url
    if url.host == "brand.example":
        return httpx.Response(301, headers={"location": str(url.copy_with(host="www.brand.example"))}, request=request)
    if url.path == "/products.json":
        page = int(url.params.get("page"))
        products = [{"handle": h} for h in {1: ["a"], 2: ["b"], 3: ["c"]}.get(page, [])]
        return httpx.Response(200, json={"products": products}, request=request)
    return httpx.Response(200, content=b"<html></html>", request=request)


def test_the_stored_www_spelling_is_requested_and_every_request_is_paced(monkeypatch, vclock) -> None:
    """Review M1's repro: a 3-page catalogue read off the folded apex cost 8 origin requests (4
    pages, each 301'd to www.) with only 4 paced. Read off the stored spelling: 4 requests, all paced."""
    seen = _real_client(monkeypatch, _www_store)
    seeds = [{"id": f"s{h}", "canonical_url": f"https://www.brand.example/products/{h}"} for h in "abc"]
    summary, recorded, pacer = _sweep(monkeypatch, None, seeds, max_new_hosts_per_minute=_NO_CAP)
    assert all(httpx.URL(u).host == "www.brand.example" for u in seen), seen
    assert len(seen) == 4 and pacer.requests == len(seen), "every origin request took a slot"
    assert summary["listed"] == 3 and sorted(recorded) == ["sa", "sb", "sc"]


def test_a_redirecting_apex_pays_for_every_hop_and_admits_www(monkeypatch, vclock) -> None:
    """Stored as the apex, the store 301s each page to www.: 8 origin requests, now 8 slots, and
    www.brand.example is admitted as a new host (it was never counted before)."""
    seen = _real_client(monkeypatch, _www_store)
    seeds = [{"id": f"s{h}", "canonical_url": f"https://brand.example/products/{h}"} for h in "abc"]
    summary, _recorded, pacer = _sweep(monkeypatch, None, seeds, max_new_hosts_per_minute=_NO_CAP)
    assert len(seen) == 8 and pacer.requests == 8
    assert set(pacer._contacted) == {"brand.example", "www.brand.example"}
    assert _gaps(pacer.grants) == [2.0] * 7


def test_request_host_prefers_the_stored_spelling() -> None:
    seeds = [{"canonical_url": "https://www.x.example/products/a"},
             {"canonical_url": "https://www.x.example/products/b"},
             {"canonical_url": "https://x.example/products/c"}]
    assert liveness.request_host(seeds, "x.example") == "www.x.example"
    assert liveness.request_host([{"canonical_url": "https://x.example/products/a"}], "x.example") == "x.example"
    assert liveness.request_host([], "x.example") == "x.example"


def test_a_hop_refused_by_the_pacer_records_nothing(monkeypatch, vclock) -> None:
    """The probe's redirect hop is due past the budget: the hop hook raises inside `client.get`, and
    that is a stopped host, not an `unverifiable` observation (nothing was asked)."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/products.json":
            products = [{"handle": "a"}] if request.url.params.get("page") == "1" else []
            return httpx.Response(200, json={"products": products}, request=request)
        if request.url.path == "/products/b":
            return httpx.Response(302, headers={"location": "https://h.example/products/b2"}, request=request)
        return httpx.Response(200, request=request)

    seen = _real_client(monkeypatch, handler)
    seeds = [{"id": "h:a", "canonical_url": "https://h.example/products/a"},
             {"id": "h:b", "canonical_url": "https://h.example/products/b"}]
    # pages at 0, 2; the probe at 4; its hop would be at 6 = the budget.
    summary, recorded, _pacer = _sweep(monkeypatch, None, seeds, budget_seconds=6, max_new_hosts_per_minute=_NO_CAP)
    assert seen[-1] == "https://h.example/products/b" and len(seen) == 3
    assert recorded == ["h:a"], "the probe whose hop was refused records nothing"
    assert summary["hosts_stopped"] == 1 and summary["hosts_unverifiable"] == 0
    assert summary["stopped"] == "budget spent"


@pytest.mark.parametrize("final_url, note", [
    ("https://brand.example/challenge?return=/products/gone", "landed on a challenge path"),
    ("https://brand.example/checkpoint", "landed on a challenge path"),
    ("https://brand.example/cdn-cgi/challenge-platform/h/b", "landed on a challenge path"),
    ("https://www.hcaptcha.com/verify", "landed on a captcha host"),
    ("https://wall.example/verify?next=/products/gone", "landed on another host"),
    ("https://other.example/", "landed on another host"),
    ("https://brand.example/challenge/products/gone", "landed on a challenge path"),
])
def test_a_wall_that_ends_in_a_200_is_unverifiable_never_dead(final_url, note) -> None:
    """Review M2: a 302 to /challenge, /checkpoint, a captcha host or any other host that ends in a
    200 with no cf-mitigated header used to be `redirected_off_product` (CONFIRMED DEAD), and a
    stage-2 probe is corroborated by construction, so it could retire a live seed."""
    obs = liveness.classify_destination(
        requested_url="https://brand.example/products/gone", status_code=200, final_url=final_url,
        listed_in_catalogue=False,
    )
    assert obs.verdict == liveness.VERDICT_UNVERIFIABLE and obs.note == note
    assert not obs.confirmed_dead


def test_same_host_redirects_still_classify_as_before() -> None:
    off = liveness.classify_destination(
        requested_url="https://brand.example/products/gone", status_code=200,
        final_url="https://www.brand.example/collections/all", listed_in_catalogue=False,
    )
    assert off.verdict == liveness.VERDICT_REDIRECTED_OFF_PRODUCT, "www. folded: the same store"
    renamed = liveness.classify_destination(
        requested_url="https://brand.example/products/old", status_code=200,
        final_url="https://brand.example/products/new",
    )
    assert renamed.verdict == liveness.VERDICT_REDIRECTED_TO_PRODUCT
    moved = liveness.classify_destination(
        requested_url="https://brand.example/products/x", status_code=200,
        final_url="https://brand-store.example/products/x",
    )
    assert moved.verdict == liveness.VERDICT_LIVE, "a domain move keeping the handle is not a wall"


def test_a_probe_walled_to_a_challenge_page_retires_nothing(monkeypatch, vclock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/products.json":
            products = [{"handle": "a"}] if request.url.params.get("page") == "1" else []
            return httpx.Response(200, json={"products": products}, request=request)
        if request.url.path == "/products/b":
            return httpx.Response(302, headers={"location": "https://h.example/challenge"}, request=request)
        return httpx.Response(200, content=b"Verify you are human", request=request)

    _real_client(monkeypatch, handler)
    seeds = [{"id": "h:a", "canonical_url": "https://h.example/products/a"},
             {"id": "h:b", "canonical_url": "https://h.example/products/b"}]
    summary, _recorded, _pacer = _sweep(monkeypatch, None, seeds, max_new_hosts_per_minute=_NO_CAP)
    assert summary["dead_links_found"] == 0
    assert summary["verdicts"][liveness.VERDICT_UNVERIFIABLE] == 1
    assert summary["verdicts"][liveness.VERDICT_REDIRECTED_OFF_PRODUCT] == 0


def test_handle_less_seeds_queue_after_every_verifiable_one() -> None:
    """Review M3, on the real SQL (SQLite, whose LIKE/COALESCE/NULLS FIRST match Postgres here)."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE external_product_seeds (id TEXT, market TEXT, domain TEXT, canonical_url TEXT, "
        "destination_url TEXT, destination_checked_at TEXT, destination_verdict TEXT, status TEXT, updated_at TEXT)"
    )
    rows = [
        ("nohandle_dead", "https://a.example/p/123", None, "dead_404", None),
        ("nohandle_null", "https://a.example/shop/x.html", None, None, None),
        ("empty_handle", "https://a.example/products/", None, None, None),
        ("query_only", "https://a.example/products/?x=1", None, None, None),
        ("dest_only", None, None, None, None),
        ("dead", "https://a.example/products/gone", "2026-10-10", "dead_404", None),
        ("old", "https://a.example/PRODUCTS/old", "2026-10-01", "live", None),
        ("never", "https://a.example/products/never", None, "live", None),
    ]
    for seed_id, canonical, checked, verdict, updated in rows:
        destination = "https://a.example/products/from-dest" if seed_id == "dest_only" else canonical
        conn.execute(
            "INSERT INTO external_product_seeds VALUES (?, 'US', 'a.example', ?, ?, ?, ?, 'active', ?)",
            (seed_id, canonical, destination, checked, verdict, updated),
        )
    sql = liveness.SWEEP_CANDIDATES_SQL.replace(":limit", "100")
    order = [r[0] for r in conn.execute(sql)]
    verifiable = [s for s in order if liveness.group_by_host([{"canonical_url": dict(
        (r[0], r[1] or "https://a.example/products/from-dest") for r in rows)[s]}])]
    assert order[:len(verifiable)] == verifiable, order
    assert order[:4] == ["dead", "dest_only", "never", "old"], order
    assert set(order[4:]) == {"nohandle_dead", "nohandle_null", "empty_handle", "query_only"}


def test_the_coverage_alarm_divides_by_the_hosts_actually_asked() -> None:
    """Review LOW 1: 40 hosts asked, 30 unreadable, 300 never started: that is a blind run."""
    summary = {"hosts": 340, "hosts_attempted": 40, "hosts_unverifiable": 30}
    alarm = job.coverage_alarm(summary)
    assert alarm and "30 of 40" in alarm
    assert job.coverage_alarm({"hosts": 340, "hosts_attempted": 40, "hosts_unverifiable": 10}) is None
    assert job.coverage_alarm({"hosts": 10, "hosts_unverifiable": 6}), "an older summary still alarms"


def test_hosts_attempted_counts_only_hosts_whose_catalogue_was_asked(monkeypatch, vclock) -> None:
    client = _Client(vclock)
    summary, _r, _p = _sweep(
        monkeypatch, client, _seeds(["h0.example", "h1.example", "h2.example"]), concurrency=1,
        budget_seconds=5, max_new_hosts_per_minute=_NO_CAP,
    )
    # h0: 0, 2, 4 s, done. h1 starts at 4 s (asked), its page 1 is due at 6 s: refused. h2: never.
    assert summary["hosts_attempted"] == 2 and summary["hosts_not_started"] == 1
    assert summary["hosts_stopped"] == 1


def test_the_kill_switch_runs_the_sweep_unpaced_and_says_so(monkeypatch, vclock, caplog) -> None:
    monkeypatch.setenv("CRAWL_GLOBAL_PACER_DISABLED", "true")
    with caplog.at_level("ERROR"):
        summary, _r, pacer = _sweep(monkeypatch, _Client(vclock), _seeds(["a.example", "b.example"]))
    assert pacer.unpaced and summary["global_pacer_disabled"] is True and summary["global_rps"] is None
    assert vclock.t == 0.0, "no pacing waits"
    assert any("DISABLED" in r.getMessage() for r in caplog.records if r.levelname == "ERROR")
    assert job.ip_throttle_line(summary)["global_pacer_disabled"] is True


def test_a_catalogue_hop_refused_by_the_pacer_is_a_stopped_host_not_a_catalogue_verdict(monkeypatch, vclock) -> None:
    """The apex 301s page 1 to www.; the hop is due at 2 s = the budget, so the hook refuses it inside
    `client.get`. That stops the host: no catalogue status, not unverifiable."""
    seen = _real_client(monkeypatch, _www_store)
    seeds = [{"id": "sa", "canonical_url": "https://brand.example/products/a"}]
    summary, recorded, _pacer = _sweep(monkeypatch, None, seeds, budget_seconds=2, max_new_hosts_per_minute=_NO_CAP)
    assert seen == ["https://brand.example/products.json?limit=250&page=1"]
    assert summary["catalogue_status"] == {} and summary["hosts_stopped"] == 1
    assert summary["hosts_unverifiable"] == 0 and not recorded
