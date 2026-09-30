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
    signals, so #2473's breaker never trips on them; the block breaker does. Every store blocks from the
    run's first request, so no store ever answered: s0, s1 and s2 each count (3 in a row) and are
    aborted (8 each) -- three stores can genuinely refuse us -- and s3's third block, with nothing
    answering since s0 began, trips it (`nothing_answering`). The pass stops and s0-s2's back-offs are
    forgiven. (Review of 7250c3e9d: the old rule tripped on s2's FIRST block; 27 requests, not 17.)"""
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
    assert len(requested) == 3 * limit + refresh.LANE_STORE_STREAK, "the fourth store's third block, not 48 in"
    assert client.breaker.blocks.trip_reason == "nothing_answering"
    assert [results[d].status for d in ("s0.com", "s1.com", "s2.com")] == [ABORTED, ABORTED, ABORTED]
    assert results["s3.com"].status == refresh.IP_THROTTLED and results["s3.com"].pass_abort
    assert all(results[d].status == refresh.NOT_REACHED for d in domains[4:])
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
    """Yield 0.29 on 09-30: 429s in runs between the answers the throttle let through."""
    def handler(request):
        n = int(request.url.path.split("/h")[-1].split(".")[0])
        return httpx.Response(429 if n in (1, 2, 3, 5) else 404)

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
    results, client = _real_backfill_pass(monkeypatch, seeds, handler, ["a.com"], page_size=10)
    assert not any("evil.example" in u or u.startswith("http://") for u in sent), sent
    loops = [u for u in sent if "/products/loop.js" in u]
    assert len(loops) == refresh.MIRROR_MAX_REDIRECTS, "a redirect loop stops at the hop cap"
    # Review of 7250c3e9d, nit: an off-storefront redirect has its OWN outcome (local 421, the writer's
    # `host_redirected`), not the `not_json` the 3xx read as -- and it is not a "blocked" domain. Only
    # the loop (the hop cap, an unknown) stays `not_json`.
    writer = results["a.com"].writer
    assert client.off_storefront_redirects == 2
    assert writer["fetch_outcomes"] == {"http_421": 2, "not_json": 1}, writer["fetch_outcomes"]
    assert writer["most_blocked_domains"] == {"a.com": 1}, writer["most_blocked_domains"]


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


def test_p1_1_a_429_is_the_throttle_breakers_not_the_block_breakers():
    lane = refresh.lane_breaker(["a.com", "b.com", "c.com"])
    for host in ("a.com", "b.com", "c.com"):
        lane.observe_clean(host)
        for _ in range(refresh.LANE_STORE_STREAK):
            lane.observe_block(host, "rate_limited")
    assert not lane.blocks.tripped and lane.blocks.blocks == 0


# ── Review of 7250c3e9d: a store counts only after its OWN run of blocks, and a trip needs health ──
#
# The old rule counted ONE block-shaped answer per store and needed "a store not already aborted" in
# the window -- which the real driver never lacks (the store completing the trip is the one in flight),
# so one ReadTimeout on each of three healthy stores stopped the pass, and three stores that genuinely
# refuse us had their back-offs forgiven. Every test below drives the REAL backfill through
# `_real_backfill_pass` (the lane's breakers installed as run_lane installs them), or the enrichment page
# function through `drive`; no breaker state is pre-marked.


def _scripted(answers):
    """A handler answering each host from its script, in order ("ok" = 404 dead handle, a clean answer;
    "403"/"429"/"502" that status; "timeout"/"reset" a transport error), then "ok" once it runs out."""
    served: dict = {}

    def handler(request):
        host = request.url.host
        i = served.get(host, 0)
        served[host] = i + 1
        script = answers.get(host, [])
        answer = script[i] if i < len(script) else "ok"
        if answer == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        if answer == "reset":
            raise httpx.ConnectError("reset", request=request)
        return httpx.Response(404 if answer == "ok" else int(answer))

    handler.served = served
    return handler


@pytest.mark.parametrize("where", [0, 3])
def test_r7250_a_one_transient_error_on_each_of_three_healthy_stores_does_not_trip(monkeypatch, where):
    """The reviewer's scenario (a): six healthy stores; a.com one ReadTimeout, c.com one 502, e.com one
    ReadTimeout (first request, or mid-walk), everything else answers. No store is blocking us: no trip,
    every store done, exit 0."""
    domains = [f"{c}.com" for c in "abcdef"]
    blips = {"a.com": "timeout", "c.com": "502", "e.com": "timeout"}
    answers = {d: ["ok"] * where + [blips[d]] for d in blips}
    handler = _scripted(answers)
    seeds = {d: _mirror_seeds(d, 10) for d in domains}
    results, client = _real_backfill_pass(monkeypatch, seeds, handler, domains, page_size=25, breaker=True)
    assert not client.breaker.tripped, client.breaker.blocks.summary()
    assert [results[d].status for d in domains] == [refresh.DONE] * 6
    assert client.breaker.blocks.blocks == 3 and client.breaker.blocks.streaks.counting == {}
    assert exit_code(results) == refresh.EXIT_OK


@pytest.mark.parametrize("blip", ["timeout", "429"])
def test_r7250_a_scattered_blocks_on_three_stores_never_add_up_to_a_run(monkeypatch, blip):
    """Three blocks per store, never two in a row: each clean answer ends the store's run. (Kills a
    breaker that forgets to reset on a clean answer.)"""
    domains = ["a.com", "b.com", "c.com", "d.com"]
    script = ["ok", blip, "ok", blip, "ok", blip, "ok"]
    handler = _scripted({d: script for d in domains})
    seeds = {d: _mirror_seeds(d, 8) for d in domains}
    results, client = _real_backfill_pass(monkeypatch, seeds, handler, domains, page_size=25, breaker=True)
    assert not client.breaker.tripped
    assert [results[d].status for d in domains] == [refresh.DONE] * 4


@pytest.mark.parametrize("kind", ["403", "timeout", "429"])
def test_r7250_b_three_stores_blocking_k_in_a_row_after_health_trip(monkeypatch, kind):
    """The address turns mid-run: a.com is walked clean; b.com answers twice, then blocks; c.com and
    d.com block from their first request. b.com, c.com and d.com each reach LANE_STORE_STREAK in a row,
    and b.com had answered before its run: the blocks began after health. The breaker trips at d.com's
    third block, the pass stops, and b/c's back-offs are forgiven."""
    from scripts import backfill_shopify_variant_ids as backfill

    k, limit = refresh.LANE_STORE_STREAK, backfill.CONSECUTIVE_BLOCK_ABORT
    domains = ["a.com", "b.com", "c.com", "d.com", "e.com"]
    handler = _scripted({"b.com": ["ok", "ok"] + [kind] * 20, "c.com": [kind] * 20, "d.com": [kind] * 20,
                         "e.com": [kind] * 20})
    seeds = {d: _mirror_seeds(d, 12) for d in domains}
    results, client = _real_backfill_pass(monkeypatch, seeds, handler, domains, page_size=25, breaker=True)
    breaker = client.breaker.throttle if kind == "429" else client.breaker.blocks
    assert breaker.tripped and breaker.trip_reason == "blocks_after_health"
    assert handler.served == {"a.com": 12, "b.com": 2 + limit, "c.com": limit, "d.com": k}
    assert results["a.com"].status == refresh.DONE
    assert [results[d].status for d in ("b.com", "c.com")] == [ABORTED, ABORTED]
    assert results["b.com"].ip_block and results["c.com"].ip_block
    assert results["d.com"].status == refresh.IP_THROTTLED and results["e.com"].status == refresh.NOT_REACHED
    assert not any(refresh.cursor_row_for(r, True, T0)["blocked_until"] for r in results.values())
    assert exit_code(results) == refresh.EXIT_ABORTED_ON_BLOCK


@pytest.mark.parametrize("kind", ["403", "429"])
def test_r7250_c_three_genuinely_blocking_stores_then_healthy_ones_keep_their_back_offs(monkeypatch, kind):
    """The reviewer's scenario (b): three stores that refuse us from their first request, walked back to
    back, then three healthy ones. Nothing had answered before them and the next store answers: three
    store-level blocks, not the address. No trip; the three keep their back-offs; the healthy stores are
    walked."""
    from scripts import backfill_shopify_variant_ids as backfill

    domains = ["x.com", "y.com", "z.com", "h1.com", "h2.com", "h3.com"]
    handler = _scripted({d: [kind] * 20 for d in ("x.com", "y.com", "z.com")})
    seeds = {d: _mirror_seeds(d, 10) for d in domains}
    results, client = _real_backfill_pass(monkeypatch, seeds, handler, domains, page_size=25, breaker=True)
    assert not client.breaker.tripped
    limit = backfill.CONSECUTIVE_BLOCK_ABORT
    assert [handler.served[d] for d in ("x.com", "y.com", "z.com")] == [limit] * 3
    for d in ("x.com", "y.com", "z.com"):
        assert results[d].status == ABORTED and not results[d].ip_block, d
        assert refresh.cursor_row_for(results[d], True, T0)["blocked_until"] is not None, d
    assert [results[d].status for d in ("h1.com", "h2.com", "h3.com")] == [refresh.DONE] * 3
    assert exit_code(results) == refresh.EXIT_BUDGET


def test_r7250_c_a_healthy_store_between_blocking_stores_keeps_the_pass_going(monkeypatch):
    """Four stores refuse us from their first request, but a healthy store is walked between the first
    and the rest: something answered after the blocks began, so it is not the address. No trip, and
    every refusing store keeps its back-off."""
    domains = ["w.com", "h.com", "x.com", "y.com", "z.com"]
    handler = _scripted({d: ["403"] * 20 for d in ("w.com", "x.com", "y.com", "z.com")})
    seeds = {d: _mirror_seeds(d, 10) for d in domains}
    results, client = _real_backfill_pass(monkeypatch, seeds, handler, domains, page_size=25, breaker=True)
    assert not client.breaker.tripped
    assert [results[d].status for d in domains] == [ABORTED, refresh.DONE, ABORTED, ABORTED, ABORTED]


def test_r7250_d_tiny_stores_under_an_ip_block_count_once_aborted(monkeypatch):
    """Stores with two seeds each can never reach LANE_STORE_STREAK; every answer is a 403 from the
    run's first request. Each is aborted as "never read" -- and an aborted store counts. The fourth,
    with nothing answering since the first, trips the block breaker inside its own page."""
    domains = ["a.com", "b.com", "c.com", "d.com", "e.com"]
    handler = _scripted({d: ["403"] * 5 for d in domains})
    seeds = {d: _mirror_seeds(d, 2) for d in domains}
    results, client = _real_backfill_pass(monkeypatch, seeds, handler, domains, page_size=25, breaker=True)
    assert client.breaker.blocks.tripped and client.breaker.blocks.trip_reason == "nothing_answering"
    assert [results[d].status for d in ("a.com", "b.com", "c.com")] == [ABORTED] * 3
    assert results["d.com"].status == refresh.IP_THROTTLED and results["e.com"].status == refresh.NOT_REACHED
    assert not any(refresh.cursor_row_for(r, True, T0)["blocked_until"] for r in results.values())


def _enrichment_pass(answers):
    from tests.test_reap_cart_proof_refresh import BlockingEnrichmentJob

    breaker = refresh.lane_breaker(list(answers))
    job = BlockingEnrichmentJob(answers, breaker=breaker)
    page_fn = refresh.enrichment_page_fn(job, None, None, {d: d for d in answers}, apply=False, pacer=None,
                                         breaker=breaker)
    results = refresh.asyncio.run(refresh.drive(list(answers), page_fn, refresh.merge_enrichment, budget_s=60,
                                                gap_s=0.0, stop_signal=lambda: breaker.tripped))
    return results, breaker


def test_r7250_c_enrichment_three_genuinely_throttling_stores_then_healthy_ones_do_not_trip():
    results, breaker = _enrichment_pass({"x.com": ["429"] * 9, "y.com": ["429"] * 9, "z.com": ["429"] * 9,
                                         "h.com": ["ok", "ok"]})
    assert not breaker.tripped
    assert [results[d].status for d in ("x.com", "y.com", "z.com")] == [ABORTED] * 3
    assert all(refresh.cursor_row_for(results[d], True, T0)["blocked_until"] for d in ("x.com", "y.com", "z.com"))
    assert results["h.com"].status == refresh.DONE


def test_r7250_d_enrichment_tiny_stores_under_an_ip_block_count_once_aborted():
    """Two blocks each (under both the streak and the writer's limit of 5), never a clean answer: each
    store is aborted "never read", and the fourth abort trips the block breaker in its own page."""
    answers = {d: ["block", "block"] for d in ("a.com", "b.com", "c.com", "d.com", "e.com")}
    results, breaker = _enrichment_pass(answers)
    assert breaker.blocks.tripped and breaker.blocks.trip_reason == "nothing_answering"
    assert [results[d].status for d in ("a.com", "b.com", "c.com")] == [ABORTED] * 3
    assert results["d.com"].status == refresh.IP_THROTTLED and results["e.com"].status == refresh.NOT_REACHED
