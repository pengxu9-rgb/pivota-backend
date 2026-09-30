"""The controller's review of #2476 at 8902ca4d5, as regression tests.

The review's repro (scratchpad review-2476/repro_test_2476.py) asserted the DEFECTS as they stood --
six stores all answering 403 (or all resetting) were all backed off with no pass stop after 48
requests, and a 301 -> www hop sent twice as many requests as were gated. These are the same two
scenarios, built the same way through the REAL backfill and the lane's breakers, asserting the fixed
behaviour instead.
"""
from __future__ import annotations

from typing import List

import httpx
import pytest

import jobs.reap_cart_proof_refresh as refresh
from jobs.reap_cart_proof_refresh import ABORTED, exit_code
from tests.test_reap_cart_proof_refresh import T0, _crawl_state, _mirror_seeds, _real_backfill_pass  # noqa: F401


@pytest.mark.parametrize("kind", ["403", "reset"])
def test_p1_1_an_ip_block_that_is_not_429s_stops_the_pass_and_backs_off_no_store(monkeypatch, kind):
    """The 2026-08-21 shape (403s) and the 2026-09-28 NAT drops (connection resets) are not throttle
    signals, so #2473's breaker never trips on them; the block breaker does. s0 and s1 are aborted
    (8 blocks each), s2's first block is the third store in the window and one not yet aborted: the
    pass stops, and s0/s1's back-offs are forgiven."""
    from scripts import backfill_shopify_variant_ids as backfill

    requested: List[str] = []

    def handler(request):
        requested.append(request.url.host)
        if kind == "reset":
            raise httpx.ConnectError("reset", request=request)
        return httpx.Response(403)

    domains = [f"s{i}.com" for i in range(6)]
    seeds = {d: _mirror_seeds(d, 20) for d in domains}
    results, client = _real_backfill_pass(monkeypatch, seeds, handler, domains, page_size=50, breaker=True)
    limit = backfill.CONSECUTIVE_BLOCK_ABORT
    assert client.breaker.blocks.tripped and not client.breaker.throttle.tripped
    assert len(requested) == 2 * limit + 1, "stopped at the third store's first block, not 48 requests in"
    assert [results[d].status for d in ("s0.com", "s1.com")] == [ABORTED, ABORTED]
    assert results["s2.com"].status == refresh.IP_THROTTLED and results["s2.com"].pass_abort
    assert all(results[d].status == refresh.NOT_REACHED for d in domains[3:])
    backed = [d for d in domains if refresh.cursor_row_for(results[d], True, T0)["blocked_until"]]
    assert backed == [], backed
    assert exit_code(results) == refresh.EXIT_ABORTED_ON_BLOCK


def test_p1_1_one_store_that_genuinely_403s_is_aborted_alone_and_backed_off(monkeypatch):
    def handler(request):
        return httpx.Response(403 if request.url.host == "bad.com" else 404)

    domains = ["bad.com", "a.com", "b.com"]
    seeds = {d: _mirror_seeds(d, 10) for d in domains}
    results, client = _real_backfill_pass(monkeypatch, seeds, handler, domains, page_size=50, breaker=True)
    assert not client.breaker.tripped
    assert results["bad.com"].status == ABORTED and not results["bad.com"].ip_block
    assert refresh.cursor_row_for(results["bad.com"], True, T0)["blocked_until"] is not None
    assert results["a.com"].status == refresh.DONE and results["b.com"].status == refresh.DONE


def test_p1_1_the_09_30_pattern_still_trips_the_throttle_breaker(monkeypatch):
    def handler(request):
        n = int(request.url.path.split("/h")[-1].split(".")[0])
        return httpx.Response(429 if n % 2 == 0 else 404)

    domains = ["a.com", "b.com", "c.com", "d.com"]
    seeds = {d: _mirror_seeds(d, 6) for d in domains}
    results, client = _real_backfill_pass(monkeypatch, seeds, handler, domains, page_size=10, breaker=True)
    assert client.breaker.throttle.tripped
    assert results["c.com"].status == refresh.IP_THROTTLED and results["d.com"].status == refresh.NOT_REACHED
    assert not any(refresh.cursor_row_for(r, True, T0)["blocked_until"] for r in results.values())


def test_p1_2_every_redirect_hop_is_gated_and_nothing_is_sent_ungated(monkeypatch):
    """The review's second repro: apex 301 -> www. Every request the client sends went through
    crawl_politeness first, hop by hop."""
    from services import crawl_politeness

    gated: List[str] = []
    sent: List[str] = []
    real = crawl_politeness.before_request

    async def counting(url, **kw):
        gated.append(url)
        return await real(url, **kw)

    monkeypatch.setattr(crawl_politeness, "before_request", counting)

    def handler(request):
        sent.append(str(request.url))
        if request.url.host == "a.com":
            return httpx.Response(301, headers={"location": str(request.url).replace("://a.com", "://www.a.com")})
        return httpx.Response(200, headers={"content-type": "application/javascript"}, json={"variants": []})

    seeds = {"a.com": _mirror_seeds("a.com", 3)}
    results, client = _real_backfill_pass(monkeypatch, seeds, handler, ["a.com"], page_size=10)
    assert len(sent) == 6 and sent == gated, (gated, sent)
    assert sum("://www.a.com/" in u for u in gated) == 3


def test_p1_2_a_redirect_off_the_storefront_or_off_https_is_never_requested(monkeypatch):
    sent: List[str] = []

    def handler(request):
        sent.append(str(request.url))
        if request.url.host == "a.com" and "h0" in request.url.path:
            return httpx.Response(301, headers={"location": "https://evil.example/products/h0.js"})
        if request.url.host == "a.com" and "h1" in request.url.path:
            return httpx.Response(302, headers={"location": "http://www.a.com/products/h1.js"})
        if request.url.host == "a.com":
            return httpx.Response(301, headers={"location": "/products/loop.js"})
        return httpx.Response(404)

    seeds = {"a.com": _mirror_seeds("a.com", 3)}
    results, _client = _real_backfill_pass(monkeypatch, seeds, handler, ["a.com"], page_size=10)
    assert not any("evil.example" in u or u.startswith("http://") for u in sent), sent
    loops = [u for u in sent if "/products/loop.js" in u]
    assert len(loops) == refresh.MIRROR_MAX_REDIRECTS, "a redirect loop stops at the hop cap"


def test_p1_2_the_patience_is_one_deadline_for_the_whole_request(monkeypatch):
    """A hop that lands on a host under a Retry-After hold is held, and the request counts as held --
    it is not re-waited from zero per hop."""
    from services import crawl_politeness

    crawl_politeness.note_response("https://www.a.com/", 429, retry_after="120")
    sent: List[str] = []

    def handler(request):
        sent.append(str(request.url))
        return httpx.Response(301, headers={"location": str(request.url).replace("://a.com", "://www.a.com")})

    seeds = {"a.com": _mirror_seeds("a.com", 2)}
    results, client = _real_backfill_pass(monkeypatch, seeds, handler, ["a.com"], page_size=10, max_wait=0.5)
    assert all("://a.com/" in u for u in sent) and len(sent) == 2
    assert client.not_sent == 2 and results["a.com"].status == refresh.HELD


def test_nit_a_crawl_delay_over_the_cap_is_permanent_not_a_hold(monkeypatch):
    from services import crawl_politeness

    monkeypatch.setenv("CRAWL_ROBOTS_ENABLED", "true")
    token = crawl_politeness.ROBOTS_TRANSPORT_FACTORY.set(lambda: httpx.MockTransport(
        lambda request: httpx.Response(200, text="User-agent: *\nCrawl-delay: 100000\n")))
    sent: List[str] = []
    try:
        seeds = {"a.com": _mirror_seeds("a.com", 3)}
        results, client = _real_backfill_pass(monkeypatch, seeds, lambda r: sent.append(r.url.host) or
                                              httpx.Response(404), ["a.com"], page_size=10)
    finally:
        crawl_politeness.ROBOTS_TRANSPORT_FACTORY.reset(token)
    assert sent == [] and client.crawl_delay_too_long == 3 and client.not_sent == 0
    assert results["a.com"].status == refresh.DONE, "not held: the store advances past it, no exit 4"
    assert results["a.com"].writer["fetch_outcomes"] == {"http_451": 3}
    assert results["a.com"].writer["most_blocked_domains"] == {}


def test_nit_enrichment_crawl_delay_too_long_is_not_a_hold():
    from tests.test_reap_cart_proof_refresh import FakeEnrichmentJob

    job = FakeEnrichmentJob([{"exhausted": True, "next_cursor": None, "aborted_on_block": False,
                              "fetches": {"crawl_delay_too_long": 2}}])
    page = refresh.asyncio.run(refresh.enrichment_page_fn(job, None, None, {"a.com": "P"}, apply=False,
                                                          pacer=None)("a.com", None))
    assert page.held == 0 and not page.aborted


def test_p1_1_three_stores_we_already_blamed_do_not_trip_the_block_breaker_a_fourth_does():
    """The block breaker's rule is the throttle breaker's: three stores that each blocked us outright
    (all already aborted) are three store-level blocks, backed off on their own evidence; the same
    answer from a store we had not blamed is what makes it the address."""
    aborted = {"a.com", "b.com", "c.com"}
    lane = refresh.lane_breaker(["a.com", "b.com", "c.com", "d.com"], is_aborted=lambda s: s in aborted)
    for host in ("a.com", "www.b.com", "c.com"):
        lane.observe_block(host, "http_403")
    assert not lane.tripped
    lane.observe_block("d.com", "error:ConnectError")
    assert lane.tripped and lane.blocks.trip_store_count == 4
    assert lane.blocks.summary()["by_outcome"] == {"http_403": 3, "error:ConnectError": 1}


def test_p1_1_a_429_is_the_throttle_breakers_not_the_block_breakers():
    lane = refresh.lane_breaker(["a.com", "b.com", "c.com"], is_aborted=lambda s: False)
    for host in ("a.com", "b.com", "c.com"):
        lane.observe_block(host, "rate_limited")
    assert not lane.blocks.tripped and lane.blocks.blocks == 0
