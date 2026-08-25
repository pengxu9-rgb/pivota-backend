"""Crawl politeness: robots compliance, per-domain pacing, 429/503 backoff.

These guard the prerequisites `docs/commerce-index-crawl-lane.md` sets before any crawl job runs
on the dedicated `pivota-crawl` subnet. That subnet gives crawling its own reserved egress IP, so
the failure this prevents is not "one worker gets throttled" — it is one unpaced loop earning a
per-IP block that takes the entire crawl lane down.

Time is never slept for real: pacing is asserted by reading the reservation the limiter makes,
and the one place a sleep must happen is driven with a patched `asyncio.sleep` that records the
duration. A test that actually waited a second per assertion would be deleted the first time
someone ran the suite.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List

import httpx
import pytest

from services import crawl_politeness as cp


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch):
    cp.reset_for_tests()
    monkeypatch.delenv("CRAWL_MIN_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("CRAWL_ROBOTS_ENABLED", raising=False)
    monkeypatch.delenv("CRAWL_MAX_BACKOFF_SECONDS", raising=False)
    monkeypatch.delenv("CRAWL_BACKOFF_BASE_SECONDS", raising=False)
    yield
    cp.reset_for_tests()


def _patch_sleep(monkeypatch: pytest.MonkeyPatch) -> List[float]:
    """Record sleep durations without sleeping, and WITHOUT touching the global asyncio module.

    `cp.asyncio` IS the real module object, so `setattr(cp.asyncio, "sleep", ...)` patches
    `asyncio.sleep` process-wide for the duration of the test — and a replacement that then calls
    `asyncio.sleep` calls itself. That RecursionError is how this was found. Swapping the module
    REFERENCE inside the module under test keeps the blast radius to that one name.
    """
    slept: List[float] = []

    async def fake_sleep(d: float) -> None:
        slept.append(d)

    class _AsyncioProxy:
        """Real asyncio for everything except `sleep`.

        A bare SimpleNamespace(sleep=...) is not enough: the module also uses
        `get_running_loop`, `shield` and `Future` for the robots single-flight, and hiding those
        makes the whole module fail for a reason that has nothing to do with the test.
        """

        sleep = staticmethod(fake_sleep)

        def __getattr__(self, name: str) -> Any:
            return getattr(asyncio, name)

    monkeypatch.setattr(cp, "asyncio", _AsyncioProxy())
    return slept


def _serve_robots(monkeypatch: pytest.MonkeyPatch, bodies: Dict[str, Any]) -> List[str]:
    """Serve robots.txt from `bodies` (host -> text, int status, or Exception). Records fetches."""
    fetched: List[str] = []

    class _FakeClient:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def get(self, url: str) -> httpx.Response:
            fetched.append(url)
            # YIELD. An `async def` with no await inside never suspends, so gathered tasks would
            # each run to completion before the next started and the cache would be warm by the
            # second one — concurrency would never actually be exercised. That is exactly why an
            # earlier version of the fan-out test could not tell single-flight from its absence.
            await asyncio.sleep(0)
            host = url.split("://", 1)[1].split("/", 1)[0]
            body = bodies.get(host, 404)
            if isinstance(body, Exception):
                raise body
            if isinstance(body, int):
                return httpx.Response(body, text="", request=httpx.Request("GET", url))
            return httpx.Response(200, text=body, request=httpx.Request("GET", url))

    # Swap the MODULE REFERENCE, not an attribute on the shared httpx module. `cp.httpx` is the
    # real httpx, so `setattr(cp.httpx, "AsyncClient", ...)` would also rebind
    # `external_offers_service.httpx.AsyncClient` — the two tests below patch both, and whichever
    # ran last silently won. Same trap as _patch_sleep.
    monkeypatch.setattr(cp, "httpx", SimpleNamespace(AsyncClient=_FakeClient))
    return fetched


# --- robots ---------------------------------------------------------------------------------

def test_a_disallowed_path_is_refused_even_though_the_root_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE defect in the pre-existing helper, pinned.

    `services/brand_product_discovery._robots_allows` asks `can_fetch(ua, base + "/")` — the site
    ROOT. A `Disallow: /products/` therefore never bites, and /products/ is exactly what this
    crawler fetches. Asking about the root would make this test pass while crawling every
    forbidden page.
    """
    _serve_robots(monkeypatch, {"brand.com": "User-agent: *\nDisallow: /products/\n"})

    assert asyncio.run(cp.robots_allows("https://brand.com/", user_agent="PivotaBot")) is True
    assert asyncio.run(
        cp.robots_allows("https://brand.com/products/serum", user_agent="PivotaBot")
    ) is False


def test_a_disallow_aimed_at_our_agent_blocks_even_when_the_wildcard_allows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve_robots(monkeypatch, {
        "brand.com": "User-agent: *\nAllow: /\n\nUser-agent: PivotaBot\nDisallow: /products/\n",
    })
    assert asyncio.run(
        cp.robots_allows("https://brand.com/products/x", user_agent="PivotaBot")
    ) is False
    # ...and a different crawler is unaffected, proving the block is agent-scoped, not blanket.
    cp.reset_for_tests()
    assert asyncio.run(
        cp.robots_allows("https://brand.com/products/x", user_agent="SomeoneElse")
    ) is True


@pytest.mark.parametrize(
    "body,label",
    [(404, "no robots.txt at all"), (500, "robots endpoint erroring"),
     (httpx.ConnectError("down"), "connection refused")],
)
def test_robots_fails_OPEN_on_an_outage_but_never_on_an_explicit_disallow(
    monkeypatch: pytest.MonkeyPatch, body: Any, label: str
) -> None:
    """A 404 means "no restrictions"; a 5xx or a timeout means "we could not ask". Neither is
    consent, but treating an outage as a refusal lets one flaky endpoint silently stop indexing a
    merchant. An explicit Disallow is a different thing and always blocks — asserted alongside so
    this cannot decay into "always returns True"."""
    _serve_robots(monkeypatch, {"brand.com": body})
    assert asyncio.run(
        cp.robots_allows("https://brand.com/products/x", user_agent="PivotaBot")
    ) is True, label

    cp.reset_for_tests()
    _serve_robots(monkeypatch, {"brand.com": "User-agent: *\nDisallow: /products/\n"})
    assert asyncio.run(
        cp.robots_allows("https://brand.com/products/x", user_agent="PivotaBot")
    ) is False


def test_robots_is_cached_so_a_crawl_does_not_refetch_it_per_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched = _serve_robots(monkeypatch, {"brand.com": "User-agent: *\nAllow: /\n"})

    async def probe_many() -> None:
        for i in range(5):
            await cp.robots_allows(f"https://brand.com/products/{i}", user_agent="PivotaBot")

    asyncio.run(probe_many())
    assert len(fetched) == 1, f"robots.txt refetched per page: {fetched}"


def test_a_site_with_no_robots_is_also_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """The absence of robots.txt must be a POSITIVE cache entry. Caching only successes means a
    404 site gets an extra request per page — the opposite of polite."""
    fetched = _serve_robots(monkeypatch, {"brand.com": 404})

    async def probe_many() -> None:
        for i in range(4):
            await cp.robots_allows(f"https://brand.com/products/{i}", user_agent="PivotaBot")

    asyncio.run(probe_many())
    assert len(fetched) == 1, f"a missing robots.txt was re-asked: {fetched}"


def test_the_robots_check_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve_robots(monkeypatch, {"brand.com": "User-agent: *\nDisallow: /\n"})
    monkeypatch.setenv("CRAWL_ROBOTS_ENABLED", "false")
    assert asyncio.run(
        cp.robots_allows("https://brand.com/products/x", user_agent="PivotaBot")
    ) is True


# --- pacing ---------------------------------------------------------------------------------

def test_concurrent_requests_to_one_host_take_distinct_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reservation is what makes pacing work under concurrency.

    Ten coroutines start at the same instant. If the limiter computed a delay from `now` without
    reserving, all ten would compute ~0 and fire together — a burst that reads as "rate limited"
    in a serial test and is anything but. The Nth caller must be scheduled N intervals out.
    """
    _serve_robots(monkeypatch, {"brand.com": 404})
    monkeypatch.setenv("CRAWL_MIN_INTERVAL_SECONDS", "1.0")

    slept = _patch_sleep(monkeypatch)

    async def hammer() -> None:
        await asyncio.gather(*[
            cp.await_slot("https://brand.com/products/x", user_agent="PivotaBot")
            for _ in range(10)
        ])

    asyncio.run(hammer())

    # First caller goes immediately (no sleep, or a zero one); the rest are spread one interval
    # apart. Assert the SPREAD rather than exact floats, which carry monotonic-clock jitter.
    waits = sorted(d for d in slept if d > 0)
    assert len(waits) >= 9, f"expected ~9 delayed callers, got {slept}"
    assert waits[-1] >= 8.0, f"the last caller must be ~9 intervals out, got {waits[-1]}"


def test_pacing_is_per_host_so_one_slow_domain_does_not_stall_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve_robots(monkeypatch, {})
    monkeypatch.setenv("CRAWL_MIN_INTERVAL_SECONDS", "1.0")

    slept: List[float] = []
    slept = _patch_sleep(monkeypatch)

    async def one_each() -> None:
        for host in ("a.com", "b.com", "c.com"):
            await cp.await_slot(f"https://{host}/products/x", user_agent="PivotaBot")

    asyncio.run(one_each())
    assert not [d for d in slept if d > 0], f"first hit on a fresh host must not wait: {slept}"


def test_a_robots_crawl_delay_slows_us_down_but_never_speeds_us_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host asking for 10s must get 10s. Taking min() — or ignoring Crawl-delay entirely —
    would let us hit it every second while still "having a robots check", which is the shape of
    compliance without the substance. The reverse also matters: a host asking for 0.1s must not
    make us FASTER than our own floor."""
    # NOTE: `Crawl-delay: 0.1` would be VACUOUS here — RobotFileParser accepts only an INTEGER
    # (it gates on `.isdigit()`), so a fractional value is discarded by the PARSER and never
    # reaches our max(). The eager case must therefore use an integer below our floor, which
    # means raising our floor above 1 to have anything to be faster than.
    _serve_robots(monkeypatch, {
        "slow.com": "User-agent: *\nCrawl-delay: 10\n",
        "eager.com": "User-agent: *\nCrawl-delay: 1\n",
    })
    monkeypatch.setenv("CRAWL_MIN_INTERVAL_SECONDS", "4.0")
    _patch_sleep(monkeypatch)

    async def two_hits(host: str) -> float:
        await cp.await_slot(f"https://{host}/a", user_agent="PivotaBot")
        before = cp._STATE[host].next_allowed
        await cp.await_slot(f"https://{host}/b", user_agent="PivotaBot")
        return cp._STATE[host].next_allowed - before

    assert asyncio.run(two_hits("slow.com")) == pytest.approx(10.0, abs=0.5), "slower wins"
    assert asyncio.run(two_hits("eager.com")) == pytest.approx(4.0, abs=0.5), (
        "a host asking to be hit FASTER than our floor must not speed us up"
    )


# --- backoff --------------------------------------------------------------------------------

def test_a_429_pushes_the_next_request_out_and_compounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRAWL_BACKOFF_BASE_SECONDS", "2")
    url = "https://brand.com/products/x"

    cp.note_response(url, 429)
    first = cp._STATE["brand.com"].backoff_until
    cp.note_response(url, 429)
    second = cp._STATE["brand.com"].backoff_until

    assert first > 0, "a 429 must arm a backoff"
    assert second > first, "consecutive blocks must compound, not reset the same wait"
    assert cp._STATE["brand.com"].consecutive_blocks == 2


def test_a_success_clears_the_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://brand.com/products/x"
    cp.note_response(url, 429)
    assert cp._STATE["brand.com"].backoff_until > 0
    cp.note_response(url, 200)
    assert cp._STATE["brand.com"].backoff_until == 0.0
    assert cp._STATE["brand.com"].consecutive_blocks == 0


def test_a_404_counts_as_the_host_still_talking_to_us() -> None:
    """A dead handle is not a block. Treating every non-200 as throttling would make a cohort
    with 14.5% dead handles back off constantly against hosts that are perfectly healthy."""
    url = "https://brand.com/products/gone"
    cp.note_response(url, 429)
    cp.note_response(url, 404)
    assert cp._STATE["brand.com"].consecutive_blocks == 0
    assert cp._STATE["brand.com"].backoff_until == 0.0


def test_retry_after_only_ever_lengthens_the_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host that says "wait 120" is more authoritative than our curve. But honouring a
    `Retry-After: 0` — or a garbage value parsed as zero — would turn a block into a hot loop
    against a host that just told us to stop."""
    monkeypatch.setenv("CRAWL_BACKOFF_BASE_SECONDS", "2")

    cp.note_response("https://a.com/x", 429, retry_after="120")
    long_wait = cp._STATE["a.com"].backoff_until

    cp.note_response("https://b.com/x", 429, retry_after="0")
    zero = cp._STATE["b.com"].backoff_until
    cp.note_response("https://c.com/x", 429, retry_after="not-a-number")
    junk = cp._STATE["c.com"].backoff_until
    cp.note_response("https://d.com/x", 429)
    plain = cp._STATE["d.com"].backoff_until

    assert long_wait > plain, "an explicit longer Retry-After must win"
    assert zero == pytest.approx(plain, abs=0.5), "Retry-After: 0 must not shorten the backoff"
    assert junk == pytest.approx(plain, abs=0.5), "unparseable Retry-After falls back to the curve"


def test_backoff_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRAWL_BACKOFF_BASE_SECONDS", "2")
    monkeypatch.setenv("CRAWL_MAX_BACKOFF_SECONDS", "30")
    import time as _time

    for _ in range(20):
        cp.note_response("https://a.com/x", 429)
    assert cp._STATE["a.com"].backoff_until - _time.monotonic() <= 30.5
    # A huge Retry-After is clamped too — a host must not be able to stall the lane indefinitely.
    cp.note_response("https://b.com/x", 429, retry_after="99999")
    assert cp._STATE["b.com"].backoff_until - _time.monotonic() <= 30.5


def test_a_backoff_actually_delays_the_next_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Arming `backoff_until` is only half of it — `await_slot` has to consult it. Without this,
    the counter would climb while requests kept going out at full rate."""
    _serve_robots(monkeypatch, {"brand.com": 404})
    monkeypatch.setenv("CRAWL_MIN_INTERVAL_SECONDS", "0.1")
    monkeypatch.setenv("CRAWL_BACKOFF_BASE_SECONDS", "60")

    slept = _patch_sleep(monkeypatch)
    cp.note_response("https://brand.com/x", 429)
    # max_wait=0 (unbounded) because this asserts the BATCH behaviour: a crawl job waits a
    # backoff out. A request-path caller would instead get CrawlPaced — covered separately.
    asyncio.run(
        cp.await_slot("https://brand.com/products/x", user_agent="PivotaBot", max_wait=0)
    )

    assert slept and slept[0] > 30, f"the backoff must gate the next slot, slept {slept}"


# --- the gate as the fetcher uses it ---------------------------------------------------------

def test_before_request_refuses_a_disallowed_url_with_a_distinguishable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`RobotsDisallowed` is a distinct type on purpose. "We were told not to" and "the fetch
    failed" are identical to a bare `except Exception` and mean opposite things — one is
    permanent and should stop us retrying, the other is transient."""
    _serve_robots(monkeypatch, {"brand.com": "User-agent: *\nDisallow: /products/\n"})
    _patch_sleep(monkeypatch)

    with pytest.raises(cp.RobotsDisallowed):
        asyncio.run(cp.before_request("https://brand.com/products/x", user_agent="PivotaBot"))

    # An allowed path on the same host still goes through.
    asyncio.run(cp.before_request("https://brand.com/pages/about", user_agent="PivotaBot"))


def test_a_malformed_url_is_not_a_crawl_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    """No host means nothing to be polite to. It must not throw — the caller's job is to fail on
    a bad URL when it fetches it, not to have the politeness gate raise something unexpected."""
    _patch_sleep(monkeypatch)
    assert asyncio.run(cp.robots_allows("not a url", user_agent="PivotaBot")) is True
    asyncio.run(cp.await_slot("not a url", user_agent="PivotaBot"))
    cp.note_response("not a url", 429)


def test_a_retry_after_SHORTER_than_our_curve_does_not_speed_us_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `max` is the whole guarantee, and only a SHORT Retry-After can prove it.

    An earlier version of this suite asserted `Retry-After: 120` against a 2s curve. Both `max`
    and `min` return 120 there, so the assertion passed under either — it looked like it pinned
    the direction and pinned nothing. A host that has already throttled us five times must not be
    able to say "come back in 5s" and pull us off a 64s backoff.
    """
    import time as _time

    monkeypatch.setenv("CRAWL_BACKOFF_BASE_SECONDS", "2")
    monkeypatch.setenv("CRAWL_MAX_BACKOFF_SECONDS", "300")
    url = "https://brand.com/products/x"

    for _ in range(5):
        cp.note_response(url, 429)
    cp.note_response(url, 429, retry_after="5")

    remaining = cp._STATE["brand.com"].backoff_until - _time.monotonic()
    assert remaining > 60, (
        f"our own curve must win when Retry-After is shorter; held only {remaining:.1f}s"
    )


# --- the fetcher actually consults the gate ---------------------------------------------------

def test_fetch_html_paces_and_feeds_the_429_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gate nothing calls is decoration.

    This drives the REAL `_fetch_html`, so it fails if the politeness call is ever dropped from
    it — which no test of `crawl_politeness` alone could catch. It also pins the ORDER: the 429
    must be recorded BEFORE `raise_for_status`, or the backoff never sees the one signal it
    exists to consume and the next request goes out at the rate that just got us throttled.
    """
    import httpx as _httpx

    from services import external_offers_service as eos

    calls: List[Any] = []

    # An async spy — a sync one makes `await` raise TypeError, which would fail this test for a
    # reason that has nothing to do with the fetcher. It delegates to the REAL gate so the
    # pacing/robots path is genuinely exercised rather than replaced.
    real_gate = cp.before_request

    async def spy(url: str, *, user_agent: str, max_wait=None) -> None:
        # Accepts `max_wait` because the fetcher now threads the caller's patience through — a
        # spy with a stale signature fails on a TypeError that says nothing about the fetcher.
        calls.append(("gate", url, max_wait))
        await real_gate(url, user_agent=user_agent, max_wait=max_wait)

    monkeypatch.setattr(cp, "before_request", spy)
    _serve_robots(monkeypatch, {"brand.com": 404})
    _patch_sleep(monkeypatch)

    class _Resp:
        status_code = 429
        headers = {"content-type": "text/html", "retry-after": "77"}
        encoding = "utf-8"
        content = b"<html></html>"

        def raise_for_status(self) -> None:
            raise _httpx.HTTPStatusError("429", request=None, response=None)  # type: ignore[arg-type]

    class _Client:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def get(self, url: str) -> Any:
            return _Resp()

    monkeypatch.setattr(eos.httpx, "AsyncClient", _Client)

    async def go() -> None:
        with pytest.raises(_httpx.HTTPStatusError):
            await eos._fetch_html("https://brand.com/products/x")

    asyncio.run(go())

    assert calls and calls[0][0] == "gate" and calls[0][1] == "https://brand.com/products/x", (
        f"the fetcher must go through the gate, saw {calls}"
    )
    state = cp._STATE.get("brand.com")
    assert state is not None and state.consecutive_blocks == 1, (
        "the 429 must be recorded even though raise_for_status raised — recording after it would "
        "mean the backoff never sees a throttle"
    )
    assert state.backoff_until > 0


def test_fetch_html_refuses_a_robots_disallowed_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """And the refusal must happen BEFORE any outbound request is made."""
    from services import external_offers_service as eos

    got: List[str] = []

    class _Client:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def get(self, url: str) -> Any:
            got.append(url)
            raise AssertionError("a disallowed url must never be fetched")

    monkeypatch.setattr(eos.httpx, "AsyncClient", _Client)
    _serve_robots(monkeypatch, {"brand.com": "User-agent: *\nDisallow: /products/\n"})
    _patch_sleep(monkeypatch)

    async def go() -> None:
        with pytest.raises(cp.RobotsDisallowed):
            await eos._fetch_html("https://brand.com/products/x")

    asyncio.run(go())
    assert got == [], "no outbound request may be made for a disallowed path"


# --- the wait is bounded on a live request path -----------------------------------------------

def test_pacing_refuses_rather_than_stalling_a_request_indefinitely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_fetch_html` sits behind `POST /api/offers/external/resolve`, which has NO auth
    dependency — a live request path. A host advertising `Crawl-delay: 600`, or a queue of
    earlier callers, must not hold that request open. Refusing degrades to the cached snapshot;
    stalling degrades to a timeout, and the host is spared either way.
    """
    _serve_robots(monkeypatch, {"slow.com": "User-agent: *\nCrawl-delay: 600\n"})
    _patch_sleep(monkeypatch)
    monkeypatch.setenv("CRAWL_MAX_WAIT_SECONDS", "10")

    async def go() -> None:
        # First caller is free — nothing is queued yet.
        await cp.await_slot("https://slow.com/a", user_agent="PivotaBot")
        # The second is 600s out, far past what a request may wait for.
        with pytest.raises(cp.CrawlPaced):
            await cp.await_slot("https://slow.com/b", user_agent="PivotaBot")

    asyncio.run(go())


def test_a_refused_caller_does_not_reserve_the_slot_it_abandoned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The subtle half. If `CrawlPaced` were raised AFTER the reservation, every refusal would
    still push the queue out — so a burst of impatient callers would starve the patient ones for
    requests that were never made.
    """
    _serve_robots(monkeypatch, {"slow.com": "User-agent: *\nCrawl-delay: 600\n"})
    _patch_sleep(monkeypatch)
    monkeypatch.setenv("CRAWL_MAX_WAIT_SECONDS", "10")

    async def go() -> float:
        await cp.await_slot("https://slow.com/a", user_agent="PivotaBot")
        reserved = cp._STATE["slow.com"].next_allowed
        for _ in range(5):
            with pytest.raises(cp.CrawlPaced):
                await cp.await_slot("https://slow.com/b", user_agent="PivotaBot")
        return cp._STATE["slow.com"].next_allowed - reserved

    assert asyncio.run(go()) == 0.0, "five refusals must not have moved the queue at all"


def test_a_batch_job_can_opt_into_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """`max_wait=0` means unbounded. A crawl Job on the pivota-crawl subnet genuinely wants to
    wait out a Crawl-delay rather than skip the row."""
    _serve_robots(monkeypatch, {"slow.com": "User-agent: *\nCrawl-delay: 600\n"})
    slept = _patch_sleep(monkeypatch)
    monkeypatch.setenv("CRAWL_MAX_WAIT_SECONDS", "10")

    async def go() -> None:
        await cp.await_slot("https://slow.com/a", user_agent="PivotaBot", max_wait=0)
        await cp.await_slot("https://slow.com/b", user_agent="PivotaBot", max_wait=0)

    asyncio.run(go())
    assert any(d > 500 for d in slept), f"the batch caller must actually wait: {slept}"


# --- rows for gaps a mutation run found, each pinning a behaviour nothing else did -------------

def test_a_redirected_robots_is_followed_rather_than_silently_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`follow_redirects=True` is load-bearing, and no other row proves it.

    Measured on the live cohort: 7 of 17 domains serve robots.txt via at least one redirect.
    Without following, the 3xx falls past the `status_code == 200` check and caches as "nothing
    to obey" — robots goes DARK for 41% of the cohort while every test stays green.
    """
    fetched: List[str] = []

    class _RedirectingClient:
        def __init__(self, *a: Any, **kw: Any) -> None:
            self.follow = kw.get("follow_redirects", False)

        async def __aenter__(self) -> "_RedirectingClient":
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def get(self, url: str) -> httpx.Response:
            fetched.append(url)
            if not self.follow:
                # What httpx returns when redirects are NOT followed.
                return httpx.Response(301, text="", request=httpx.Request("GET", url))
            return httpx.Response(
                200, text="User-agent: *\nDisallow: /products/\n",
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr(cp, "httpx", SimpleNamespace(AsyncClient=_RedirectingClient))
    assert asyncio.run(
        cp.robots_allows("https://brand.com/products/x", user_agent="PivotaBot")
    ) is False, "a redirected robots.txt must still be obeyed"
    assert fetched == ["https://brand.com/robots.txt"], (
        f"robots must be fetched over https at the well-known path, got {fetched}"
    )


def test_a_503_backs_off_exactly_like_a_429() -> None:
    """The contract says "429/503". Nothing drove a 503 — and 503 is what Shopify and Cloudflare
    return under load, so it is the likelier of the two in practice."""
    cp.note_response("https://brand.com/x", 503)
    state = cp._STATE["brand.com"]
    assert state.consecutive_blocks == 1
    assert state.backoff_until > 0

    cp.note_response("https://brand.com/x", 503)
    assert cp._STATE["brand.com"].consecutive_blocks == 2, "503s must compound too"


def test_a_negative_env_value_cannot_disable_pacing_or_the_wait_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_f` clamps at 0. Without the clamp a negative `CRAWL_MIN_INTERVAL_SECONDS` walks
    `next_allowed` BACKWARDS — pacing off entirely — and a negative `CRAWL_MAX_WAIT_SECONDS`
    makes `ceiling > 0` false, restoring the unbounded stall on the live route. Operator error
    should not silently switch the gate off."""
    monkeypatch.setenv("CRAWL_MIN_INTERVAL_SECONDS", "-1")
    assert cp._min_interval() == 0.0
    monkeypatch.setenv("CRAWL_MAX_BACKOFF_SECONDS", "-5")
    assert cp._f("CRAWL_MAX_BACKOFF_SECONDS", 300.0) == 0.0


def test_a_wait_exactly_at_the_ceiling_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The boundary of the guard the whole design rests on.

    THE CLOCK MUST BE FROZEN. On a real monotonic clock a few microseconds elapse between
    reserving the slot and measuring the wait, so `start - now` is 9.9999… and never exactly the
    ceiling — `>` and `>=` agree, and the assertion looks like it pins the boundary while pinning
    nothing. (That is exactly how this row first failed to kill its mutant.)
    """
    _serve_robots(monkeypatch, {"brand.com": 404})
    slept = _patch_sleep(monkeypatch)
    monkeypatch.setattr(cp, "time", SimpleNamespace(monotonic=lambda: 1000.0))
    monkeypatch.setenv("CRAWL_MIN_INTERVAL_SECONDS", "10")
    monkeypatch.setenv("CRAWL_MAX_WAIT_SECONDS", "10")

    async def go() -> None:
        await cp.await_slot("https://brand.com/a", user_agent="PivotaBot")
        # The clock is frozen, so the second caller is EXACTLY one interval — exactly the
        # ceiling — out. At the boundary we serve; past it we refuse.
        await cp.await_slot("https://brand.com/b", user_agent="PivotaBot")

    asyncio.run(go())
    assert any(d == pytest.approx(10.0) for d in slept), (
        f"a wait of exactly the ceiling must be served, not refused: {slept}"
    )


def test_a_port_does_not_split_a_host_into_two_pacing_buckets() -> None:
    """`hostname` not `netloc`. Otherwise `b.com` and `b.com:443` pace independently and the host
    sees double our intended rate."""
    assert cp.host_of("https://b.com:443/x") == "b.com"
    assert cp.host_of("https://b.com/x") == "b.com"
    assert cp.host_of("https://User:Pass@B.COM:8443/x") == "b.com"


def test_the_host_caches_cannot_grow_without_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    """`POST /api/offers/external/resolve` has no auth dependency, so the hostname keying these
    caches arrives from an unauthenticated body. Unbounded, an attacker grows them one distinct
    hostname at a time — and doubles our outbound amplification while doing it."""
    monkeypatch.setattr(cp, "_MAX_TRACKED_HOSTS", 50)
    for i in range(120):
        cp.note_response(f"https://h{i}.example/x", 429)
    assert len(cp._STATE) <= 51, f"pacing state grew unbounded: {len(cp._STATE)}"


def test_a_parser_that_blows_up_fails_OPEN_like_every_other_robots_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The defensive `except` around `can_fetch` had no coverage, so nothing stopped it being
    flipped to fail-closed — which would contradict the module's stated contract everywhere else
    and silently stop crawling a host whose robots.txt merely confuses the parser."""
    _serve_robots(monkeypatch, {"brand.com": "User-agent: *\nDisallow: /products/\n"})

    class _ExplodingParser:
        def parse(self, _lines: Any) -> None:
            return None

        def crawl_delay(self, _ua: str) -> None:
            return None

        def can_fetch(self, _ua: str, _url: str) -> bool:
            raise ValueError("malformed rule set")

    monkeypatch.setattr(cp, "RobotFileParser", _ExplodingParser)
    assert asyncio.run(
        cp.robots_allows("https://brand.com/products/x", user_agent="PivotaBot")
    ) is True, "a parser failure is an outage, not a refusal"


def test_the_batch_scripts_opt_into_waiting_rather_than_being_refused() -> None:
    """P1 from review. `max_wait=0` exists FOR these callers and was wired into none of them.

    With the default 10s ceiling, the backoff curve (2, 4, 8, 16, 32 … 300) becomes unreachable
    from the 4th consecutive 429 onward: `await_slot` refuses instead of waiting, the scripts'
    `except Exception` records the row as `fetch_failed`, and — because a refusal returns
    instantly — the whole remaining backlog burns down in milliseconds. Measured in review: a host
    that 429s only its first 4 requests turned 40 rows into 5 repaired and 31 silently skipped,
    indistinguishable from the host being down.

    Asserted against the SOURCE because the failure is silent and the fix is a keyword argument
    that is easy to drop in a refactor: nothing at runtime would complain.
    """
    import pathlib

    repo = pathlib.Path(__file__).resolve().parent.parent
    for rel in ("scripts/source_pdp_offer_image_repair.py", "scripts/source_pdp_content_repair.py"):
        text = (repo / rel).read_text()
        calls = [ln.strip() for ln in text.splitlines() if "await _fetch_html(" in ln]
        assert calls, f"{rel}: expected at least one _fetch_html call"
        for call in calls:
            assert "max_wait=0" in call, (
                f"{rel}: a BATCH fetch must opt into waiting out a backoff, got: {call}"
            )


# --- every merchant-crawling lane goes through the gate ---------------------------------------

# Lanes that fetch THIRD-PARTY hosts (merchant storefronts, sitemaps, editorial articles) and so
# share the one reserved crawl egress IP. A ban earned by any of them takes all of them down,
# which is why coverage is asserted as a SET rather than per-lane: a new lane added without the
# gate should fail this, not slip through because nobody wrote it a test.
#
# Deliberately EXCLUDED, with reasons — this list is the interesting half:
#   * services/executor_agents/canonical_pdp_enrichment.py — POSTs to Google's Vertex Gemini API
#     (`vertex_gemini.generate_content_url`), not a merchant. Pacing a first-party API at one
#     request per second per host would be actively wrong. It appeared in an earlier audit table
#     of "unpaced crawl lanes" only because that table was built by grepping for robots/pacing
#     without checking what each lane actually fetches.
#   * every LLM / partner-API client (agent_center_llm_client, connector_service, …) — same
#     reason: first-party or contracted endpoints, not crawling.
# lane -> how many outbound fetch sites it gates. An EXACT count, not a presence check: a lane
# with two fetch sites where one loses its gate still contains the string, so `in text` would
# pass while half the lane crawled unpaced — the same "a guard on one path does not cover the
# path that bypasses it" shape this whole change exists to close. An exact count fails on a
# removed gate AND on a newly added fetch, which is the moment someone should be made to think.
_GATED_CRAWL_LANES = {
    "services/external_offers_service.py": 1,
    "services/brand_product_discovery.py": 1,
    "services/co_occurrence_finder.py": 1,
    "services/curated_brand_feed.py": 2,       # products.json paging + PDP INCI fetch
    "services/bd_cold_start_service.py": 2,      # Shopify .json + the generic PDP-HTML fallback
    "services/executor_agents/sitemap_freshness.py": 2,  # sitemap + child indexes
}


def test_every_third_party_crawl_lane_goes_through_the_politeness_gate() -> None:
    import pathlib

    repo = pathlib.Path(__file__).resolve().parent.parent
    wrong = {}
    for rel, expected in sorted(_GATED_CRAWL_LANES.items()):
        found = (repo / rel).read_text().count("crawl_politeness.before_request")
        if found != expected:
            wrong[rel] = f"expected {expected} gated fetch(es), found {found}"
    assert not wrong, (
        "these lanes crawl third-party hosts from the shared crawl egress IP and their gating "
        f"changed: {wrong}. If you ADDED a fetch, gate it. If you removed one, update the count."
    )


def test_the_vertex_lane_is_NOT_gated_because_it_is_not_a_crawl() -> None:
    """The exclusion, asserted rather than left as a comment.

    `canonical_pdp_enrichment` POSTs to Google's Vertex Gemini API. Someone reading the list
    above may reasonably wonder why one 'pdp' module is absent; this fails loudly if a future
    change either makes it crawl merchants (in which case it belongs in the set) or wraps the
    Gemini call in a per-host crawl limiter (which would throttle a first-party API to 1/s).
    """
    import pathlib

    repo = pathlib.Path(__file__).resolve().parent.parent
    text = (repo / "services/executor_agents/canonical_pdp_enrichment.py").read_text()
    assert "vertex_gemini.generate_content_url" in text, (
        "this lane no longer targets the Vertex API — re-assess whether it now crawls merchants"
    )
    assert "crawl_politeness" not in text, (
        "a first-party API call must not be paced by the per-host crawl gate"
    )


# --- the gate must not itself become the burst ------------------------------------------------

def test_a_fan_out_over_one_host_makes_exactly_ONE_robots_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review P0. The audit routes `asyncio.gather` over ~20 urls on ONE merchant host, so all
    20 miss the robots cache in the same tick. Without single-flight that is 20 simultaneous,
    entirely UNPACED requests to that host — a burst emitted from the shared crawl egress IP by
    the very code whose purpose is preventing per-IP bans.

    Measured before the fix: 20 fetches for one request.
    """
    fetched = _serve_robots(monkeypatch, {"shop.example": "User-agent: *\nAllow: /\n"})
    _patch_sleep(monkeypatch)

    async def fan_out() -> None:
        await asyncio.gather(*[
            cp.robots_allows(f"https://shop.example/products/{i}", user_agent="PivotaBot")
            for i in range(20)
        ])

    asyncio.run(fan_out())
    assert len(fetched) == 1, f"the gate must not amplify a fan-out: {len(fetched)} robots fetches"


def test_followers_get_the_leaders_answer_not_a_blank_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Single-flight is only correct if the waiters end up with the SAME verdict. A follower that
    adopted "no rules" while the leader read a Disallow would crawl a forbidden path."""
    _serve_robots(monkeypatch, {"shop.example": "User-agent: *\nDisallow: /products/\n"})
    _patch_sleep(monkeypatch)

    async def fan_out() -> List[bool]:
        return list(await asyncio.gather(*[
            cp.robots_allows("https://shop.example/products/x", user_agent="PivotaBot")
            for _ in range(10)
        ]))

    verdicts = asyncio.run(fan_out())
    assert verdicts == [False] * 10, f"every waiter must see the Disallow, got {verdicts}"


def test_a_robots_fetch_that_explodes_does_not_strand_its_followers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Awaiting an unresolved future HANGS rather than raising, so an error in the leader would
    wedge every sibling on that host. The resolution lives in a `finally` for exactly this."""
    class _Boom:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> "_Boom":
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def get(self, url: str) -> Any:
            raise RuntimeError("upstream exploded")

    monkeypatch.setattr(cp, "httpx", SimpleNamespace(AsyncClient=_Boom))
    _patch_sleep(monkeypatch)

    async def fan_out() -> List[bool]:
        return list(await asyncio.gather(*[
            cp.robots_allows("https://shop.example/products/x", user_agent="PivotaBot")
            for _ in range(5)
        ]))

    # Would hang forever if the leader failed to resolve; the timeout turns a wedge into a
    # failure you can read.
    verdicts = asyncio.run(asyncio.wait_for(fan_out(), timeout=5))
    assert verdicts == [True] * 5, "a robots outage is permissive for leader and followers alike"


def test_the_inflight_map_is_dropped_when_the_event_loop_changes() -> None:
    """A future binds to the loop that created it, and awaiting a stale one HANGS instead of
    raising — the failure mode recorded against this repo's module-level primitives. Each
    asyncio.run() below is a DIFFERENT loop, which is also what the test suite does per test."""
    async def once() -> bool:
        return await cp.robots_allows("https://shop.example/x", user_agent="PivotaBot")

    assert asyncio.run(once()) is True
    cp._ROBOTS.clear()          # force a cache miss so the second run takes the leader path
    assert asyncio.run(once()) is True, "a second event loop must not inherit a stale future"


# --- per-lane semantics, not just "the string is present" -------------------------------------
#
# Review found FIVE semantic mutants surviving: a lane could ask robots about a different agent
# than it sends, drop a note_response, or flip max_wait, and every test stayed green. The only
# property under test was the literal string `crawl_politeness.before_request`. These assert what
# the gate is actually asked, per lane, by driving the real functions.

def _gate_spy(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    """Record every (url, user_agent, max_wait) the lanes hand the gate."""
    seen: List[Dict[str, Any]] = []

    async def spy(url: str, *, user_agent: str, max_wait: Any = None) -> None:
        seen.append({"url": url, "user_agent": user_agent, "max_wait": max_wait})

    monkeypatch.setattr(cp, "before_request", spy)
    return seen


def _http_stub(monkeypatch: pytest.MonkeyPatch, module: Any, *, status: int = 200,
               text: str = "", headers: Any = None) -> List[str]:
    got: List[str] = []
    hdrs = headers or {"content-type": "text/html"}

    class _Client:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def get(self, url: str, **kw: Any) -> Any:
            got.append(url)
            return SimpleNamespace(
                status_code=status, headers=hdrs, text=text,
                content=text.encode(), json=lambda: {},
            )

    # Several lanes do `import httpx` INSIDE the function, so there is no module-level attribute
    # to patch — the real httpx module is the only handle. Safe here precisely because
    # `_gate_spy` has already replaced `before_request`, so `crawl_politeness` makes no robots
    # fetch of its own and cannot collide with this stub. monkeypatch reverts it either way.
    target = module.httpx if hasattr(module, "httpx") else httpx
    monkeypatch.setattr(target, "AsyncClient", _Client)
    return got


@pytest.mark.parametrize("lane", ["bd_cold_start", "co_occurrence", "brand_discovery"])
def test_a_request_reachable_lane_uses_the_BOUNDED_wait(
    monkeypatch: pytest.MonkeyPatch, lane: str
) -> None:
    """max_wait=0 means UNBOUNDED. On a lane reachable from a live authenticated route that is
    #1854's bounded-wait lesson re-introduced inverted: a 300s backoff, or a `Crawl-delay: 30`,
    would hold a real request open. Measured in review at 19s of added wall-clock for a 20-URL
    audit even with no adverse conditions.
    """
    seen = _gate_spy(monkeypatch)

    if lane == "bd_cold_start":
        from services import bd_cold_start_service as m
        _http_stub(monkeypatch, m, text="{}")
        asyncio.run(m._fetch_shopify_native("https://shop.example/products/x"))
    elif lane == "co_occurrence":
        from services import co_occurrence_finder as m
        _http_stub(monkeypatch, m, text="<html></html>")
        asyncio.run(m._fetch_article_text("https://news.example/a"))
    else:
        from services import brand_product_discovery as m
        _http_stub(monkeypatch, m, text="<html></html>")
        asyncio.run(m._fetch_text("https://brand.example/products/x", 1000))

    assert seen, f"{lane}: the gate was never called"
    for call in seen:
        assert call["max_wait"] is None, (
            f"{lane}: a request-reachable lane must use the BOUNDED default, "
            f"got max_wait={call['max_wait']!r}"
        )


@pytest.mark.parametrize("lane", ["curated_feed", "sitemap"])
def test_a_batch_only_lane_opts_into_waiting(monkeypatch: pytest.MonkeyPatch, lane: str) -> None:
    """The mirror. A batch lane must pass 0, or a backoff past the default ceiling makes the gate
    refuse and the lane's broad `except` records it as an ordinary fetch failure."""
    seen = _gate_spy(monkeypatch)

    if lane == "curated_feed":
        from services import curated_brand_feed as m
        _http_stub(monkeypatch, m, status=404, text="")
        asyncio.run(m.fetch_shopify_products("shop.example", max_products=1))
    else:
        from services.executor_agents import sitemap_freshness as m
        _http_stub(monkeypatch, m, status=404, text="")
        asyncio.run(m._fetch_sitemap_urls_recursive("https://shop.example/sitemap.xml", max_child_sitemaps=2))

    assert seen, f"{lane}: the gate was never called"
    for call in seen:
        assert call["max_wait"] == 0, (
            f"{lane}: a batch lane must wait a backoff out, got max_wait={call['max_wait']!r}"
        )


@pytest.mark.parametrize(
    "lane,expected_ua_attr",
    [("bd_cold_start", "_BD_UA"), ("sitemap", "_SITEMAP_UA"),
     ("co_occurrence", "_USER_AGENT"), ("brand_discovery", "_USER_AGENT")],
)
def test_the_gate_is_asked_about_the_SAME_agent_the_lane_then_sends(
    monkeypatch: pytest.MonkeyPatch, lane: str, expected_ua_attr: str
) -> None:
    """Asking robots about one agent and sending another is worse than not asking: it produces a
    confident "allowed" for a UA the site never ruled on. The `_BD_UA` / `_SITEMAP_UA` constants
    were introduced with a comment claiming exactly this property and nothing tested it — both
    mutants that swapped the queried agent survived.
    """
    seen = _gate_spy(monkeypatch)

    if lane == "bd_cold_start":
        from services import bd_cold_start_service as m
        _http_stub(monkeypatch, m, text="{}")
        asyncio.run(m._fetch_shopify_native("https://shop.example/products/x"))
    elif lane == "sitemap":
        from services.executor_agents import sitemap_freshness as m
        _http_stub(monkeypatch, m, status=404)
        asyncio.run(m._fetch_sitemap_urls_recursive("https://shop.example/sitemap.xml", max_child_sitemaps=2))
    elif lane == "co_occurrence":
        from services import co_occurrence_finder as m
        _http_stub(monkeypatch, m, text="<html></html>")
        asyncio.run(m._fetch_article_text("https://news.example/a"))
    else:
        from services import brand_product_discovery as m
        _http_stub(monkeypatch, m, text="<html></html>")
        asyncio.run(m._fetch_text("https://brand.example/products/x", 1000))

    expected = getattr(m, expected_ua_attr)
    assert seen, f"{lane}: the gate was never called"
    for call in seen:
        assert call["user_agent"] == expected, (
            f"{lane}: robots was asked about {call['user_agent']!r} but the lane sends "
            f"{expected!r} — a verdict for an agent the site never ruled on"
        )


def test_a_child_sitemap_response_is_fed_back_to_the_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The child-index loop is the high-volume half of that lane — a sitemap index can point at
    many children on the same host. Recording only the PARENT's status means a 429 arriving on
    child 2 of 20 never arms the backoff, and we keep hammering the host that just throttled us.
    """
    from services.executor_agents import sitemap_freshness as m

    _gate_spy(monkeypatch)
    index_xml = (
        '<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<sitemap><loc>https://shop.example/sitemap-products-1.xml</loc></sitemap>"
        "</sitemapindex>"
    )
    seen: List[int] = []
    real_note = cp.note_response

    def spy_note(url: str, status_code: int, *, retry_after: Any = None) -> None:
        seen.append(status_code)
        real_note(url, status_code, retry_after=retry_after)

    monkeypatch.setattr(cp, "note_response", spy_note)

    calls = {"n": 0}

    class _Client:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def get(self, url: str, **kw: Any) -> Any:
            calls["n"] += 1
            # Parent 200 (an index), child 429 — the case the backoff exists for.
            if calls["n"] == 1:
                return SimpleNamespace(
                    status_code=200, headers={}, content=index_xml.encode(),
                )
            return SimpleNamespace(status_code=429, headers={"retry-after": "120"}, content=b"")

    monkeypatch.setattr(m.httpx, "AsyncClient", _Client)
    asyncio.run(m._fetch_sitemap_urls_recursive(
        "https://shop.example/sitemap.xml", max_child_sitemaps=2
    ))

    assert 429 in seen, (
        f"a child sitemap's 429 must reach the backoff, statuses recorded: {seen}"
    )


def test_brand_discovery_robots_actually_consults_the_shared_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_robots_allows` is the pre-flight that raises the operator-facing "site disallows our
    crawler, fall back to manual entry" error. Nothing drove it, so `return True` survived — the
    helper could have been gutted entirely and every test stayed green.

    Also pins that it asks about the FULL path: the old body ended in `can_fetch(ua, base + "/")`
    and a bare origin normalizes to "/", so a `Disallow: /products/` never bit.
    """
    from services import brand_product_discovery as bpd

    _serve_robots(monkeypatch, {"brand.example": "User-agent: *\nDisallow: /products/\n"})

    assert asyncio.run(bpd._robots_allows("https://brand.example/products/x")) is False
    assert asyncio.run(bpd._robots_allows("https://brand.example/pages/about")) is True
