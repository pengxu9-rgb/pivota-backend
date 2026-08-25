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

    monkeypatch.setattr(cp, "asyncio", SimpleNamespace(sleep=fake_sleep))
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
    _serve_robots(monkeypatch, {
        "slow.com": "User-agent: *\nCrawl-delay: 10\n",
        "eager.com": "User-agent: *\nCrawl-delay: 0.1\n",
    })
    monkeypatch.setenv("CRAWL_MIN_INTERVAL_SECONDS", "1.0")
    _patch_sleep(monkeypatch)

    async def two_hits(host: str) -> float:
        await cp.await_slot(f"https://{host}/a", user_agent="PivotaBot")
        before = cp._STATE[host].next_allowed
        await cp.await_slot(f"https://{host}/b", user_agent="PivotaBot")
        return cp._STATE[host].next_allowed - before

    assert asyncio.run(two_hits("slow.com")) == pytest.approx(10.0, abs=0.5)
    assert asyncio.run(two_hits("eager.com")) == pytest.approx(1.0, abs=0.5)


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

    async def spy(url: str, *, user_agent: str) -> None:
        calls.append(("gate", url))
        await real_gate(url, user_agent=user_agent)

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

    assert ("gate", "https://brand.com/products/x") in calls, "the fetcher must go through the gate"
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
