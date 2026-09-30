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


def test_r7250_a_store_that_answers_only_after_its_run_is_no_health_before_it(monkeypatch):
    """Three stores each open with three timeouts and then answer (a slow start, not a block). Each
    counts, but none had answered BEFORE its run: health that comes after the blocks is not "the blocks
    began after health", and three is not the four a run with nothing answering needs. No trip."""
    domains = ["a.com", "b.com", "c.com", "h.com"]
    handler = _scripted({d: ["timeout"] * 3 for d in ("a.com", "b.com", "c.com")})
    seeds = {d: _mirror_seeds(d, 8) for d in domains}
    results, client = _real_backfill_pass(monkeypatch, seeds, handler, domains, page_size=25, breaker=True)
    assert set(client.breaker.blocks.streaks.counting) == {"a.com", "b.com", "c.com"}
    assert not client.breaker.tripped
    assert [results[d].status for d in domains] == [refresh.DONE] * 4


def test_r7250_a_counting_store_leaves_the_window_after_its_last_block():
    now = [0.0]
    health = refresh.LaneHealth()
    window = refresh.StoreStreakWindow(health, clock=lambda: now[0])
    for store in ("a.com", "b.com", "c.com", "d.com"):
        health.clean(store)
    verdicts = []
    for store, t in (("a.com", 0.0), ("b.com", 500.0), ("c.com", 1000.0), ("d.com", 1100.0)):
        now[0] = t
        for _ in range(refresh.LANE_STORE_STREAK):
            verdict = window.block(store)
        verdicts.append(verdict)
    # c.com at t=1000: a.com's last block was 1000 s ago, outside the 900 s window -> two stores.
    assert verdicts == [None, None, None, "blocks_after_health"]
    assert set(window.counting) == {"b.com", "c.com", "d.com"}


# ── Review of 61fb15f78: health from the cursor table, stores still answering, no abort-only count ─
#
# The reviewer's repro (scratchpad review-2476/repro_r3_2476.py) PRINTS each scenario and asserts
# nothing, so it could not be copied as a regression test. Its scenarios are rebuilt here with the fixed
# behaviour asserted, through the REAL run_lane (MemoryDb keeps the cursor table across nights, so
# defer_blocked and the table's health are the production ones) or `_real_backfill_pass`.


def _night_runner(monkeypatch, domains, seeds, handler, rows=None, lane="mirror"):
    """`night(offset)` runs the REAL run_lane over `domains` against a MemoryDb seeded with `rows`
    (domain -> cursor row fields), and returns (state, hosts requested that night, db)."""
    from datetime import timedelta
    from scripts import backfill_shopify_variant_ids as backfill
    from tests.test_reap_cart_proof_refresh import MemoryDb

    async def fake_select(limit, domain, after=None, seed_ids=None):
        rows_ = [r for r in seeds[domain] if after is None or r["id"] > after]
        return [dict(r) for r in rows_[:limit]]

    requested: List[str] = []

    def counting(request):
        requested.append(request.url.host)
        return handler(request)

    monkeypatch.setattr(backfill, "select_candidates", fake_select)
    monkeypatch.setattr(backfill, "GLOBAL_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(backfill, "PER_DOMAIN_MIN_GAP_S", 0.0)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(refresh.httpx, "AsyncClient",
                        lambda *a, **k: real_client(transport=httpx.MockTransport(counting)))
    db = MemoryDb()
    for domain, row in (rows or {}).items():
        db.rows[(lane, domain)] = {"next_cursor": None, "blocked_until": None, "crash_count": 0,
                                   "updated_at": T0 - timedelta(days=1), "last_completed_at": None, **row}
    plan = refresh.LanePlan(lane=lane, domains=list(domains), writer=backfill, gap_s=0.0,
                            proof_max_age=timedelta(days=7))

    def night(offset=timedelta(0)):
        requested.clear()
        state = refresh.RunState()
        refresh.asyncio.run(refresh.run_lane(plan, apply=True, budget_s=600, emit=lambda line: None, state=state,
                                             db=db, now=lambda: T0 + offset))
        return state, list(requested), db

    return night


def _blocked_until(db, domain, lane="mirror"):
    return db.rows[(lane, domain)]["blocked_until"]


@pytest.mark.parametrize("last_night", ["forgiven", "back_off_expired"])
def test_r61fb_a_four_genuine_blockers_walked_last_do_not_trip_and_keep_their_back_offs(monkeypatch, last_night):
    """The reviewer's loop. a, c, e, g genuinely refuse us; last time they were aborted (their back-off
    either forgiven by a trip, or just expired), so defer_blocked walks them LAST, back to back, after
    the healthy b, d, f, h. The table blames them: no evidence the address went bad, and they are not
    first contact. No trip, exit 4, and each serves its back-off."""
    from datetime import timedelta
    from scripts import backfill_shopify_variant_ids as backfill

    domains = [f"{c}.com" for c in "abcdefgh"]
    bad = ("a.com", "c.com", "e.com", "g.com")
    seeds = {d: _mirror_seeds(d, 12) for d in domains}
    until = None if last_night == "forgiven" else T0 - timedelta(minutes=1)
    rows = {d: {"last_status": refresh.DONE, "last_completed_at": T0 - timedelta(days=1)} for d in domains}
    rows.update({d: {"last_status": ABORTED, "blocked_until": until,
                     "last_completed_at": T0 - timedelta(days=5)} for d in bad})
    night = _night_runner(monkeypatch, domains, seeds, lambda r: httpx.Response(403 if r.url.host in bad else 404),
                          rows=rows)
    state, requested, db = night()
    assert state.info["domains_order"] == ["b.com", "d.com", "f.com", "h.com", *bad]
    assert state.info["block_breaker"]["block_breaker_tripped"] is False
    assert [state.results[d].status for d in bad] == [ABORTED] * 4
    assert all(_blocked_until(db, d) == T0 + refresh.MIRROR_BLOCK_BACKOFF for d in bad)
    assert [requested.count(d) for d in bad] == [backfill.CONSECUTIVE_BLOCK_ABORT] * 4
    assert exit_code(state.results) == refresh.EXIT_BUDGET
    # The next nights: backed off until day 3, then walked last again -- still no trip.
    state, requested, _db = night(timedelta(days=1))
    assert [state.results[d].status for d in bad] == [refresh.BACKED_OFF] * 4 and not set(bad) & set(requested)
    state, _requested, db = night(timedelta(days=3, minutes=1))
    assert state.info["domains_order"][-4:] == list(bad)
    assert not state.info["block_breaker"]["block_breaker_tripped"]
    assert all(_blocked_until(db, d) is not None for d in bad)


def test_r61fb_a_blockers_interleaved_with_answering_stores_do_not_trip_even_with_past_health(monkeypatch):
    """The night they START refusing us: a, c, e, g were healthy last time (evidence), but b, d, f answer
    between them. A store outside the window still answering says the address is fine."""
    from datetime import timedelta

    domains = [f"{c}.com" for c in "abcdefgh"]
    bad = ("a.com", "c.com", "e.com", "g.com")
    rows = {d: {"last_status": refresh.DONE, "last_completed_at": T0 - timedelta(days=1)} for d in domains}
    night = _night_runner(monkeypatch, domains, {d: _mirror_seeds(d, 12) for d in domains},
                          lambda r: httpx.Response(403 if r.url.host in bad else 404), rows=rows)
    state, _requested, db = night()
    assert state.info["domains_order"] == domains
    assert not state.info["block_breaker"]["block_breaker_tripped"]
    assert all(_blocked_until(db, d) is not None for d in bad)
    assert [state.results[d].status for d in domains if d not in bad] == [refresh.DONE] * 4


def test_r61fb_b_four_tiny_stores_with_one_transient_error_each_do_not_trip(monkeypatch):
    """The reviewer's slow-start case: four one-seed stores walked first, each out once (a ReadTimeout),
    then healthy stores. Each is aborted by the never-read rule -- but an abort no longer makes a store
    count, and one error is a run of one. No trip."""
    tiny = [f"t{i}.com" for i in range(4)]
    domains = tiny + ["h1.com", "h2.com"]

    def handler(request):
        if request.url.host in tiny:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(404)

    seeds = {d: _mirror_seeds(d, 1) for d in tiny}
    seeds.update({d: _mirror_seeds(d, 10) for d in ("h1.com", "h2.com")})
    results, client = _real_backfill_pass(monkeypatch, seeds, handler, domains, page_size=25, breaker=True)
    assert not client.breaker.tripped and client.breaker.blocks.streaks.counting == {}
    assert results["h1.com"].status == refresh.DONE and results["h2.com"].status == refresh.DONE
    assert exit_code(results) != refresh.EXIT_ABORTED_ON_BLOCK


@pytest.mark.parametrize("kind", ["403", "429"])
@pytest.mark.parametrize("history", ["done", "completed_then_in_progress"])
def test_r61fb_c_a_run_that_starts_blocked_trips_on_stores_that_were_healthy_before(monkeypatch, kind, history):
    """No store answers from the run's first request, but the cursor table says each was walked cleanly
    on a previous run (last_status done, or a last_completed_at kept under a later in-progress walk):
    the address went bad. The third store's third block trips it; nothing is backed off; exit 1."""
    from datetime import timedelta
    from scripts import backfill_shopify_variant_ids as backfill

    domains = [f"s{i}.com" for i in range(6)]
    status = refresh.DONE if history == "done" else refresh.IN_PROGRESS
    rows = {d: {"last_status": status, "last_completed_at": T0 - timedelta(days=2)} for d in domains}
    night = _night_runner(monkeypatch, domains, {d: _mirror_seeds(d, 20) for d in domains},
                          lambda r: httpx.Response(int(kind)), rows=rows)
    state, requested, db = night()
    info = state.info["ip_throttle"] if kind == "429" else state.info["block_breaker"]
    reason = info["ip_throttle_trip_reason"] if kind == "429" else info["trip_reason"]
    assert reason == "blocks_after_health"
    limit, k = backfill.CONSECUTIVE_BLOCK_ABORT, refresh.LANE_STORE_STREAK
    assert len(requested) == 2 * limit + k
    order = state.info["domains_order"]
    assert state.results[order[2]].status == refresh.IP_THROTTLED
    assert all(r["blocked_until"] is None for r in db.rows.values())
    assert exit_code(state.results) == refresh.EXIT_ABORTED_ON_BLOCK


def test_r61fb_d_a_run_that_starts_blocked_with_no_store_ever_read_cleanly_walks_every_store(monkeypatch):
    """(d), stated plainly: the address is blocked from the run's first request, and the table has NO
    health for any store -- each was blamed last time, or walked but never to its end -- and none is
    first contact. There is no evidence the address (rather than each store) is the problem, so there is
    no trip: every store is walked to its own threshold and backed off (mirror 3 days, under the 7-day
    proof life). Cost: T requests per store."""
    from datetime import timedelta
    from scripts import backfill_shopify_variant_ids as backfill

    domains = [f"s{i}.com" for i in range(5)]
    rows = {d: {"last_status": ABORTED, "last_completed_at": None} for d in domains[:3]}
    rows.update({d: {"last_status": refresh.IP_THROTTLED, "last_completed_at": None} for d in domains[3:]})
    night = _night_runner(monkeypatch, domains, {d: _mirror_seeds(d, 20) for d in domains},
                          lambda r: httpx.Response(403), rows=rows)
    state, requested, db = night(timedelta(days=0))
    assert not state.info["block_breaker"]["block_breaker_tripped"]
    assert len(requested) == 5 * backfill.CONSECUTIVE_BLOCK_ABORT
    assert all(state.results[d].status == ABORTED for d in domains)
    assert all(_blocked_until(db, d) is not None for d in domains)
    assert exit_code(state.results) == refresh.EXIT_BUDGET


def test_r61fb_d_first_contact_stores_blocked_from_the_first_request_still_trip(monkeypatch):
    """The other half of (d): stores the table has never seen (first contact) -- a new lane, or new
    stores -- blocked from the first request. The fourth trips `nothing_answering`, through run_lane."""
    domains = [f"s{i}.com" for i in range(6)]
    night = _night_runner(monkeypatch, domains, {d: _mirror_seeds(d, 20) for d in domains},
                          lambda r: httpx.Response(403))
    state, requested, db = night()
    assert state.info["block_breaker"]["trip_reason"] == "nothing_answering"
    assert state.results[state.info["domains_order"][3]].status == refresh.IP_THROTTLED
    assert all(r["blocked_until"] is None for r in db.rows.values())


def test_r61fb_nit_an_off_storefront_redirect_is_neutral_for_breaker_health(monkeypatch):
    """A 421 (redirect off the storefront, never followed) is neither a block nor a clean answer for the
    breakers or the store count; the backfill's report still names it `http_421`."""
    def handler(request):
        return httpx.Response(301, headers={"location": "https://elsewhere.example/p.js"})

    results, client = _real_backfill_pass(monkeypatch, {"a.com": _mirror_seeds("a.com", 3)}, handler, ["a.com"],
                                          page_size=10, breaker=True)
    assert client.off_storefront_redirects == 3 and client.store_clean == 0
    assert client.breaker.health.first_clean == {} and client.breaker.health.last_clean == 0
    assert results["a.com"].writer["fetch_outcomes"] == {"http_421": 3}
    assert results["a.com"].status == refresh.DONE
