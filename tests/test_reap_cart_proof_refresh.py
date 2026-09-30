"""jobs/reap_cart_proof_refresh.py: domain order, paging, cursors, pacing, the budget, blocks, the report.

No network and no database: the writers are replaced by recorders for the driver and the page
adapters; `run_lane` is driven for real against a stub database and fake writers (the wiring of the
gate, the shared pacer and the no-cookie client); and the end-to-end mirror tests run the REAL
backfill `run()` over a faked selection and a mock transport.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import pytest

import jobs.reap_cart_proof_refresh as refresh
from jobs.reap_cart_proof_refresh import (
    ABORTED,
    BUDGET_STOPPED,
    CRASHED,
    CURSOR_STUCK,
    DONE,
    NOT_REACHED,
    TERMINATED,
    DomainResult,
    Page,
    drive,
    exit_code,
)
from db.reap_cart_proof_refresh_cursors import CursorRow

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _crawl_state(monkeypatch):
    """The mirror client gates every request through services.crawl_politeness: no real robots.txt
    fetch, no per-host sleeping, no backoff sleeping, and no pacer or breaker state carried between
    tests. The shared Shopify-edge pacer is OFF unless a test turns it on."""
    from services import crawl_ip_throttle, crawl_politeness, shopify_edge_pacer

    monkeypatch.setenv("CRAWL_MIN_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("CRAWL_ROBOTS_ENABLED", "false")
    monkeypatch.setenv("CRAWL_BACKOFF_BASE_SECONDS", "0")
    monkeypatch.delenv("CRAWL_SHOPIFY_EDGE_PACER_ENABLED", raising=False)
    crawl_politeness.reset_for_tests()
    shopify_edge_pacer.reset_for_tests()
    crawl_ip_throttle.reset_for_tests()
    yield
    crawl_politeness.reset_for_tests()
    shopify_edge_pacer.reset_for_tests()
    crawl_ip_throttle.reset_for_tests()


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.slept: List[float] = []

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


def pages_fn(script: Dict[str, List[Any]], clock: Optional[Clock] = None, cost: float = 0.0):
    """`script[domain]` is the sequence of Pages (or exceptions) that domain's calls return."""
    calls: List[tuple] = []

    async def run_page(domain: str, after: Optional[str]) -> Page:
        calls.append((domain, after))
        if clock is not None:
            clock.t += cost
        item = script[domain].pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    return run_page, calls


def merge_list(total: Dict[str, Any], page: Dict[str, Any]) -> None:
    total.setdefault("pages", []).append(page)


def _drive(domains, run_page, clock, budget=100.0, gap=3.0, emit=None):
    return asyncio.run(drive(domains, run_page, merge_list, budget_s=budget, gap_s=gap, clock=clock,
                             sleep=clock.sleep, emit=emit))


# ── the gate ────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw, expected", [
    (None, False), ("", False), ("false", False), ("0", False), ("no", False), ("enabled", False),
    ("truee", False), ("true", True), ("TRUE", True), (" true ", True), ("1", True), ("yes", True),
    ("on", True),
])
def test_only_a_recognised_truthy_gate_applies(raw, expected):
    env = {} if raw is None else {refresh.GATE_ENV: raw}
    assert refresh.apply_enabled(env) is expected


# ── domain selection ────────────────────────────────────────────────────────────────────────────


def test_the_mirror_domains_are_the_tier_b_list_once_each():
    from services.tierb_cart_link_merchants import load_merchants

    domains = refresh.mirror_domains()
    assert domains == sorted(set(domains))
    assert set(domains) == {m.domain for m in load_merchants()}


def test_a_domain_listed_under_two_markets_is_walked_once(tmp_path):
    """The Tier B list is keyed by (domain, market); the backfill is keyed by domain alone."""
    path = tmp_path / "merchants.json"
    path.write_text(json.dumps([{"domain": "b.com", "market": "US"}, {"domain": "a.com", "market": "US"},
                                {"domain": "b.com", "market": "SG"}]))
    assert refresh.mirror_domains(str(path)) == ["a.com", "b.com"]


T0 = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def _row(completed=None, cursor=None, status="done"):
    return CursorRow(next_cursor=cursor, last_status=status, last_completed_at=completed, updated_at=None)


def test_the_mirror_order_puts_never_completed_stores_first_then_the_stalest():
    domains = ["a.com", "b.com", "c.com", "d.com"]
    oldest = {"www.b.com": T0 - timedelta(days=5), "c.com": T0 - timedelta(days=1),
              "shop.c.com": T0 - timedelta(days=6), "notd.com": T0 - timedelta(days=9),
              "d.com": T0 - timedelta(hours=3)}
    cursors = {"a.com": _row(completed=T0 - timedelta(days=2)), "b.com": _row(completed=T0 - timedelta(hours=1)),
               "c.com": _row(completed=T0 - timedelta(hours=2))}
    # d.com was never walked to its end -> first, although its one proof is the freshest. Then by
    # min(oldest valid proof, last completed walk): c.com 6 days (shop.c.com), b.com 5 days (www.b.com),
    # a.com 2 days. notd.com is not d.com.
    assert refresh.order_mirror_domains(domains, oldest, cursors) == ["d.com", "c.com", "b.com", "a.com"]


def test_among_never_completed_stores_the_older_proof_goes_first_and_the_suffix_rule_is_the_backfills():
    oldest = {"www.e.com": T0 - timedelta(days=4), "d.com": T0 - timedelta(days=1),
              "note.com": T0 - timedelta(days=6)}
    assert refresh.order_mirror_domains(["d.com", "e.com"], oldest, {}) == ["e.com", "d.com"]
    assert refresh._seed_domain_is("www.e.com", "e.com") and refresh._seed_domain_is("shop.e.com", "e.com")
    # Exactly the backfill's SQL: case-sensitive, no trimming. A seed it will not select must not count.
    assert not refresh._seed_domain_is("E.com", "e.com") and not refresh._seed_domain_is("www.E.COM", "e.com")
    assert not refresh._seed_domain_is("e.com.", "e.com")
    assert refresh._seed_domain_is("WWW.e.com", "e.com"), "the backfill's LIKE '%.e.com' matches it too"
    assert not refresh._seed_domain_is("note.com", "e.com") and not refresh._seed_domain_is("e.com.au", "e.com")


def test_the_mirror_order_breaks_ties_by_name_and_walks_each_store_once():
    assert refresh.order_mirror_domains(["b.com", "a.com", "b.com"], {}, {}) == ["a.com", "b.com"]


def test_a_completed_walk_moves_a_store_behind_the_ones_walked_longer_ago():
    """No starvation: whatever a run completes goes to the back of the next run's order."""
    domains = ["a.com", "b.com", "c.com"]
    cursors = {d: _row(completed=T0 - timedelta(days=i + 1)) for i, d in enumerate(domains)}
    order = refresh.order_mirror_domains(domains, {}, cursors)
    assert order == ["c.com", "b.com", "a.com"]
    cursors["c.com"] = _row(completed=T0)
    assert refresh.order_mirror_domains(domains, {}, cursors) == ["b.com", "a.com", "c.com"]


def test_only_still_valid_proofs_count_and_the_sql_reads_them_with_a_cutoff():
    seen = {}

    class Db:
        async def fetch_all(self, sql, values=None):
            seen["sql"], seen["values"] = sql, values
            return [{"domain": "www.b.com", "oldest_valid_proof": (T0 - timedelta(days=3)).isoformat()},
                    {"domain": "bad.com", "oldest_valid_proof": "not a time"}]

    out = asyncio.run(refresh.select_oldest_valid_mirror_proofs(Db(), now=T0, max_age=timedelta(days=7)))
    assert out == {"www.b.com": T0 - timedelta(days=3)}
    assert seen["values"] == {"cutoff": (T0 - timedelta(days=7)).isoformat()}
    assert ">= :cutoff" in seen["sql"] and "'object'" in seen["sql"] and "status = 'active'" in seen["sql"]


def test_the_enrichment_domains_are_the_census_five_on_the_tier_b_list_mac_last():
    import jobs.enrichment_cart_variant_proof as writer

    assert set(refresh.ENRICHMENT_DOMAINS) == {
        "tartecosmetics.com", "maccosmetics.com", "bluemercury.com", "stilacosmetics.com", "jsmbeauty.sg"}
    plans = writer.plan_domains(list(refresh.ENRICHMENT_DOMAINS))  # raises if one left the Tier B list
    assert [p.domain for p in plans] == list(refresh.ENRICHMENT_DOMAINS), "the writer keeps the order"
    assert refresh.ENRICHMENT_DOMAINS[-1] == "maccosmetics.com"


def test_plan_lane_resolves_both_lanes_without_touching_the_db():
    now = datetime(2026, 9, 30, tzinfo=timezone.utc)
    mirror = refresh.plan_lane("mirror", now)
    assert mirror.domains == refresh.mirror_domains()
    assert mirror.proof_max_age == timedelta(days=7)
    enrichment = refresh.plan_lane("enrichment", now)
    assert enrichment.domains == list(refresh.ENRICHMENT_DOMAINS)
    assert set(enrichment.plans) == set(refresh.ENRICHMENT_DOMAINS)
    from db.enrichment_cart_variant_proofs import ensure_table

    assert enrichment.ensure_proof_table is ensure_table, "run_domain does not ensure the table; the lane must"


def test_the_inter_call_gap_is_the_writers_own_slowest_spacing():
    import jobs.enrichment_cart_variant_proof as writer
    from scripts import backfill_shopify_variant_ids as backfill

    assert refresh.inter_call_gap_s("mirror", backfill=backfill) == max(
        backfill.GLOBAL_MIN_INTERVAL_S, backfill.PER_DOMAIN_MIN_GAP_S)
    assert refresh.inter_call_gap_s("enrichment", job=writer) == writer.request_gap_s()


# ── the driver ──────────────────────────────────────────────────────────────────────────────────


def test_each_domain_is_paged_on_the_writers_cursor_until_exhausted():
    clock = Clock()
    run_page, calls = pages_fn({
        "a.com": [Page({"n": 1}, "c1"), Page({"n": 2}, "c2"), Page({"n": 3}, None)],
        "b.com": [Page({"n": 4}, None)],
    })
    results = _drive(["a.com", "b.com"], run_page, clock)
    assert calls == [("a.com", None), ("a.com", "c1"), ("a.com", "c2"), ("b.com", None)]
    assert results["a.com"].status == DONE and results["a.com"].pages == 3
    assert results["a.com"].writer == {"pages": [{"n": 1}, {"n": 2}, {"n": 3}]}
    assert results["b.com"].status == DONE
    assert exit_code(results) == refresh.EXIT_OK


def test_every_call_but_the_first_waits_the_gap():
    clock = Clock()
    run_page, calls = pages_fn({"a.com": [Page({}, "c1"), Page({}, None)], "b.com": [Page({}, None)]})
    _drive(["a.com", "b.com"], run_page, clock, gap=3.0)
    assert len(calls) == 3 and clock.slept == [3.0, 3.0]


def test_the_budget_stops_new_pages_and_names_what_it_cut():
    clock = Clock()
    run_page, calls = pages_fn({
        "a.com": [Page({}, "c1"), Page({}, "c2"), Page({}, None)],
        "b.com": [Page({}, None)],
        "c.com": [Page({}, None)],
    }, clock=clock, cost=40.0)
    results = _drive(["a.com", "b.com", "c.com"], run_page, clock, budget=100.0, gap=3.0)
    # 40 s, +3+40 = 83 s, +3 -> 86 s < 100 so page 3 starts, +40 = 126 s: b.com never starts.
    assert calls == [("a.com", None), ("a.com", "c1"), ("a.com", "c2")]
    assert results["a.com"].status == DONE
    assert results["b.com"].status == NOT_REACHED and results["c.com"].status == NOT_REACHED
    assert exit_code(results) == refresh.EXIT_BUDGET


def test_a_domain_cut_mid_walk_is_budget_stopped_not_done():
    clock = Clock()
    run_page, calls = pages_fn({"a.com": [Page({}, "c1"), Page({}, "c2")]}, clock=clock, cost=60.0)
    results = _drive(["a.com"], run_page, clock, budget=100.0)
    assert calls == [("a.com", None), ("a.com", "c1")]
    assert results["a.com"].status == BUDGET_STOPPED and results["a.com"].last_cursor == "c2"
    assert exit_code(results) == refresh.EXIT_BUDGET


def test_a_store_that_blocks_is_aborted_and_the_pass_moves_on():
    """N1: one store that blocks us must not cost the others their refresh."""
    clock = Clock()
    run_page, calls = pages_fn({
        "a.com": [Page({}, None)],
        "b.com": [Page({"aborted_on_block": True}, None, aborted=True)],
        "c.com": [Page({}, None)],
    })
    results = _drive(["a.com", "b.com", "c.com"], run_page, clock)
    assert [c[0] for c in calls] == ["a.com", "b.com", "c.com"]
    assert results["b.com"].status == ABORTED and not results["b.com"].pass_abort
    assert results["c.com"].status == DONE
    assert exit_code(results) == refresh.EXIT_BUDGET, "a store left unwalked, not an IP-level block"


def test_an_ip_breaker_trip_stops_the_whole_pass_and_forgives_this_runs_back_offs():
    clock = Clock()
    run_page, calls = pages_fn({
        "a.com": [Page({}, None, aborted=True)],
        "b.com": [Page({}, None, abort_pass=True)],
        "c.com": [Page({}, None)],
    })
    marks: List[tuple] = []

    async def checkpoint(domain, result, final):
        if final:
            marks.append((domain, refresh.cursor_row_for(result, True, T0)["blocked_until"]))

    results = asyncio.run(drive(["a.com", "b.com", "c.com"], run_page, merge_list, budget_s=100, gap_s=0.0,
                                clock=clock, sleep=clock.sleep, checkpoint=checkpoint))
    assert [c[0] for c in calls] == ["a.com", "b.com"]
    assert results["b.com"].status == refresh.IP_THROTTLED and results["b.com"].pass_abort
    assert results["c.com"].status == NOT_REACHED
    assert results["a.com"].status == ABORTED and results["a.com"].ip_block
    assert marks == [("a.com", T0 + refresh.MIRROR_BLOCK_BACKOFF), ("a.com", None), ("b.com", None)], \
        "a.com is first recorded with a back-off, then re-recorded without it when the breaker trips"
    assert exit_code(results) == refresh.EXIT_ABORTED_ON_BLOCK


def test_a_trip_seen_between_pages_stops_before_the_next_writer_call():
    clock = Clock()
    tripped = {"v": False}
    run_page, calls = pages_fn({"a.com": [Page({}, "c1"), Page({}, None)], "b.com": [Page({}, None)]})

    async def page(domain, after):
        out = await run_page(domain, after)
        tripped["v"] = True
        return out

    results = asyncio.run(drive(["a.com", "b.com"], page, merge_list, budget_s=100, gap_s=0.0, clock=clock,
                                sleep=clock.sleep, stop_signal=lambda: tripped["v"]))
    assert calls == [("a.com", None)]
    assert results["a.com"].status == refresh.IP_THROTTLED and results["a.com"].last_cursor == "c1"
    assert results["b.com"].status == NOT_REACHED
    row = refresh.cursor_row_for(results["a.com"], True, T0)
    assert row["next_cursor"] == "c1" and row["blocked_until"] is None and row["completed_at"] is None


def test_an_abort_mid_domain_does_not_follow_its_cursor():
    clock = Clock()
    run_page, calls = pages_fn({"a.com": [Page({}, "c1", aborted=True), Page({}, None)]})
    results = _drive(["a.com"], run_page, clock)
    assert calls == [("a.com", None)] and results["a.com"].status == ABORTED


def test_a_crash_is_recorded_and_the_next_domain_still_runs():
    clock = Clock()
    run_page, calls = pages_fn({
        "a.com": [RuntimeError("boom")],
        "b.com": [Page({}, None)],
    })
    results = _drive(["a.com", "b.com"], run_page, clock)
    assert [c[0] for c in calls] == ["a.com", "b.com"]
    assert results["a.com"].status == CRASHED and "RuntimeError: boom" in results["a.com"].error
    assert results["b.com"].status == DONE
    assert exit_code(results) == refresh.EXIT_CRASHED


def test_a_cursor_that_does_not_move_stops_the_domain_instead_of_spinning():
    clock = Clock()
    run_page, calls = pages_fn({"a.com": [Page({}, "c1"), Page({}, "c1"), Page({}, None)],
                                "b.com": [Page({}, None)]})
    results = _drive(["a.com", "b.com"], run_page, clock)
    assert calls == [("a.com", None), ("a.com", "c1"), ("b.com", None)]
    assert results["a.com"].status == CURSOR_STUCK and results["b.com"].status == DONE
    assert exit_code(results) == refresh.EXIT_CRASHED


def test_the_driver_refuses_a_non_positive_budget():
    run_page, _ = pages_fn({})
    with pytest.raises(ValueError):
        asyncio.run(drive(["a.com"], run_page, merge_list, budget_s=0, gap_s=1.0))


def test_a_progress_line_per_finished_domain_none_for_the_unreached():
    clock = Clock()
    lines: List[str] = []
    run_page, _ = pages_fn({"a.com": [Page({}, None)], "b.com": [Page({}, None, aborted=True, abort_pass=True)],
                            "c.com": [Page({}, None)]})
    _drive(["a.com", "b.com", "c.com"], run_page, clock, emit=lines.append)
    assert [json.loads(line[len(refresh.PROGRESS_PREFIX):])["domain"] for line in lines] == ["a.com", "b.com"]
    assert all(line.startswith(refresh.PROGRESS_PREFIX) for line in lines)


def test_no_progress_line_for_a_domain_the_budget_never_started():
    clock = Clock()
    lines: List[str] = []
    run_page, calls = pages_fn({"a.com": [Page({}, None)], "b.com": [Page({}, None)]}, clock=clock, cost=200.0)
    results = _drive(["a.com", "b.com"], run_page, clock, budget=100.0, emit=lines.append)
    assert calls == [("a.com", None)] and results["b.com"].status == NOT_REACHED
    assert [json.loads(line[len(refresh.PROGRESS_PREFIX):])["domain"] for line in lines] == ["a.com"]


@pytest.mark.parametrize("statuses, code", [
    ([DONE, DONE], 0),
    ([DONE, refresh.BACKED_OFF], 0),
    ([DONE, NOT_REACHED], 4),
    ([BUDGET_STOPPED, NOT_REACHED], 4),
    ([DONE, ABORTED], 4),
    ([CRASHED, NOT_REACHED], 3),
    ([CRASHED, ABORTED], 3),
    ([CURSOR_STUCK, DONE], 3),
    ([CRASHED, "pass", NOT_REACHED], 1),
    (["pass"], 1),
])
def test_exit_code_precedence(statuses, code):
    results = {f"d{i}.com": (DomainResult(status=ABORTED, pass_abort=True) if s == "pass" else DomainResult(status=s))
               for i, s in enumerate(statuses)}
    assert exit_code(results) == code


def test_a_sigterm_is_never_exit_0_even_with_nothing_walked():
    assert exit_code({}, terminated=True) == refresh.EXIT_BUDGET
    assert exit_code({"a.com": DomainResult(status=DONE)}, terminated=True) == refresh.EXIT_BUDGET
    assert exit_code({"a.com": DomainResult(status=CRASHED)}, terminated=True) == refresh.EXIT_CRASHED


# ── the page adapters: what each writer is called with, and how its report is read ─────────────


class FakeBackfill:
    def __init__(self, reports: List[Dict[str, Any]]) -> None:
        self.reports = reports
        self.calls: List[Dict[str, Any]] = []

    async def run(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(kwargs)
        return self.reports.pop(0)


@pytest.mark.parametrize("apply", [False, True])
def test_the_mirror_page_calls_the_backfill_for_one_domain_with_the_gate(apply):
    backfill = FakeBackfill([{"candidates": 50, "next_cursor": "epsv_9", "aborted_on_block": False}])
    client = object()
    page = asyncio.run(refresh.mirror_page_fn(backfill, client, apply=apply)("anua.us", "epsv_1"))
    assert backfill.calls == [{"limit": refresh.MIRROR_PAGE_SIZE, "domain": "anua.us", "apply": apply,
                               "client": client, "after": "epsv_1"}]
    assert page.next_cursor == "epsv_9" and not page.aborted


def test_a_short_mirror_page_ends_the_domain_and_an_aborted_one_stops():
    short = FakeBackfill([{"candidates": 49, "next_cursor": "epsv_9", "aborted_on_block": False}])
    page = asyncio.run(refresh.mirror_page_fn(short, None, apply=False)("anua.us", None))
    assert page.next_cursor is None and not page.aborted
    blocked = FakeBackfill([{"candidates": 50, "next_cursor": "epsv_9", "aborted_on_block": True}])
    page = asyncio.run(refresh.mirror_page_fn(blocked, None, apply=False)("anua.us", None))
    assert page.aborted and page.next_cursor is None


class FakeEnrichmentJob:
    def __init__(self, reports: List[Dict[str, Any]]) -> None:
        self.reports = reports
        self.calls: List[tuple] = []

    def abort_after_blocks(self) -> int:
        return 5

    async def run_domain(self, db: Any, client: Any, plan: Any, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append((db, client, plan, kwargs))
        return self.reports.pop(0)


@pytest.mark.parametrize("apply", [False, True])
def test_the_enrichment_page_calls_the_writer_for_one_plan_with_the_shared_pacer_and_the_breaker(apply):
    domain_report = {"exhausted": False, "next_cursor": "ext:k9", "aborted_on_block": False, "written": 0}
    job = FakeEnrichmentJob([domain_report])
    plans = {"tartecosmetics.com": "PLAN-T", "maccosmetics.com": "PLAN-M"}
    pacer, db, client = object(), object(), object()

    class Breaker:
        tripped = False

    breaker = Breaker()
    page = asyncio.run(refresh.enrichment_page_fn(job, db, client, plans, apply=apply, pacer=pacer,
                                                  breaker=breaker)("tartecosmetics.com", "ext:k1"))
    (call,) = job.calls
    streak = call[3].pop("block_state")
    should_stop = call[3].pop("should_stop")
    assert call == (db, client, "PLAN-T", {
        "apply": apply, "source_mode": "auto", "limit": refresh.ENRICHMENT_PAGE_PRODUCTS, "after": "ext:k1",
        "pacer": pacer, "block_limit": 5})
    assert isinstance(streak, refresh.ObservedStreak) and streak["consecutive"] == 0
    assert should_stop() is False
    breaker.tripped = True
    assert should_stop() is True, "the writer's should_stop IS the breaker"
    assert page.next_cursor == "ext:k9" and page.report == domain_report and not page.aborted


def test_the_enrichment_page_is_small_enough_to_commit_and_stop_between_pages():
    import jobs.enrichment_cart_variant_proof as writer

    assert refresh.ENRICHMENT_PAGE_PRODUCTS <= 250 < writer.DEFAULT_LIMIT


def test_an_exhausted_or_aborted_enrichment_domain_has_no_next_page():
    exhausted = FakeEnrichmentJob([{"exhausted": True, "next_cursor": "x"}])
    page = asyncio.run(refresh.enrichment_page_fn(exhausted, None, None, {"a.com": "P"}, apply=False,
                                                  pacer=None)("a.com", None))
    assert page.next_cursor is None and not page.aborted
    aborted = FakeEnrichmentJob([{"exhausted": False, "next_cursor": "x", "aborted_on_block": True}])
    page = asyncio.run(refresh.enrichment_page_fn(aborted, None, None, {"a.com": "P"}, apply=False,
                                                  pacer=None)("a.com", None))
    assert page.aborted and page.next_cursor is None and not page.abort_pass


class BlockingEnrichmentJob:
    """Moves the writer's block_state exactly as `run_domain`'s on_fetch does: +1 per block (aborting the
    store at the limit), 0 on a clean answer; asks `should_stop` after every answer, as on_fetch does.
    A "429" answer is also reported to the breaker, as the writer's note_response(headers=...) does."""

    def __init__(self, answers: Dict[str, List[str]], breaker: Any = None) -> None:
        self.answers = answers
        self.breaker = breaker

    def abort_after_blocks(self) -> int:
        return 5

    async def run_domain(self, db, client, plan, *, block_state, block_limit, should_stop=None, **kwargs):
        for answer in self.answers[plan]:
            if self.breaker is not None and answer in ("block", "429"):
                self.breaker.observe(plan, 429, {})
            if should_stop is not None and should_stop():
                return {"aborted_on_block": True, "exhausted": False, "next_cursor": None}
            if answer in ("block", "429"):
                block_state["consecutive"] += 1
                if block_state["consecutive"] >= block_limit:
                    return {"aborted_on_block": True, "exhausted": False, "next_cursor": None}
            else:
                block_state["consecutive"] = 0
        return {"aborted_on_block": False, "exhausted": True, "next_cursor": None}


def _enrichment_pass(answers, breaker=None):
    job = BlockingEnrichmentJob(answers, breaker=breaker)
    page_fn = refresh.enrichment_page_fn(job, None, None, {d: d for d in answers}, apply=False, pacer=None,
                                         breaker=breaker)
    return asyncio.run(drive(list(answers), page_fn, refresh.merge_enrichment, budget_s=60, gap_s=0.0,
                             stop_signal=(lambda: breaker.tripped) if breaker is not None else None))


def test_enrichment_one_blocking_store_is_aborted_and_the_next_store_is_walked():
    results = _enrichment_pass({"a.com": ["block"] * 9, "b.com": ["ok", "ok"], "c.com": ["ok"]})
    assert results["a.com"].status == ABORTED and not results["a.com"].pass_abort
    assert results["b.com"].status == DONE and results["c.com"].status == DONE


def test_enrichment_stores_throttling_one_after_another_trip_the_breaker_and_stop_the_pass():
    """Under the 09-30 pattern (a rate throttle that lets 200s through) no store reaches its own
    threshold, yet three distinct stores 429ing within the window trip the breaker: the pass stops and
    nothing is backed off."""
    breaker = refresh.store_breaker(["a.com", "b.com", "c.com", "d.com"], is_aborted=lambda s: False)
    mixed = ["429", "ok", "429", "ok", "ok"]
    results = _enrichment_pass({"a.com": mixed, "b.com": mixed, "c.com": mixed, "d.com": ["ok"]}, breaker)
    assert results["a.com"].status == DONE and results["b.com"].status == DONE
    assert results["c.com"].status == refresh.IP_THROTTLED and results["c.com"].pass_abort
    assert results["d.com"].status == NOT_REACHED
    assert not any(refresh.cursor_row_for(r, True, T0)["blocked_until"] for r in results.values())


class PagedEnrichmentJob(BlockingEnrichmentJob):
    """`answers[store]` is a list of PAGES, each a list of answers."""

    async def run_domain(self, db, client, plan, *, block_state, block_limit, after=None, **kwargs):
        page = self.answers[plan].pop(0)
        for answer in page:
            if answer == "block":
                block_state["consecutive"] += 1
                if block_state["consecutive"] >= block_limit:
                    return {"aborted_on_block": True, "exhausted": False, "next_cursor": None}
            else:
                block_state["consecutive"] = 0
        return {"aborted_on_block": False, "exhausted": not self.answers[plan],
                "next_cursor": None if not self.answers[plan] else f"k{len(self.answers[plan])}"}


def test_enrichment_the_store_streak_is_carried_across_that_stores_pages():
    """Three blocks at the end of page 1 and two at the start of page 2 are five in a row: the store
    is aborted, as one multi-domain writer call would have done."""
    job = PagedEnrichmentJob({"a.com": [["ok", "block", "block", "block"], ["block", "block", "ok"]], "b.com": [["ok"]]})
    page_fn = refresh.enrichment_page_fn(job, None, None, {"a.com": "a.com", "b.com": "b.com"}, apply=False,
                                         pacer=None)
    results = asyncio.run(drive(["a.com", "b.com"], page_fn, refresh.merge_enrichment, budget_s=60, gap_s=0.0))
    assert results["a.com"].status == ABORTED and results["a.com"].pages == 2
    assert results["b.com"].status == DONE


def test_the_observed_streak_counts_blocks_and_clean_answers():
    streak = refresh.ObservedStreak()
    streak["consecutive"] += 1
    streak["consecutive"] += 1
    assert streak["consecutive"] == 2 and streak.blocks == 2 and streak.clean == 0
    streak["consecutive"] = 0
    streak["consecutive"] = 0
    assert streak.clean == 2 and streak.blocks == 2


def test_mirror_pages_are_summed_per_domain():
    total: Dict[str, Any] = {}
    refresh.merge_mirror(total, {"candidates": 50, "rows_with_new_ids": 3, "variant_ids_stamped": 4,
                                 "write_conflicts": 1, "fetch_outcomes": {"ok": 40, "dead_handle": 10},
                                 "cart_proofs": {"sole_variant": 2}, "match_reasons": {"x": 1},
                                 "most_blocked_domains": {}})
    refresh.merge_mirror(total, {"candidates": 7, "rows_with_new_ids": 1, "variant_ids_stamped": 0,
                                 "write_conflicts": 0, "fetch_outcomes": {"ok": 7},
                                 "cart_proofs": {"named_variant": 1, "sole_variant": 1}})
    assert total == {"candidates": 57, "rows_with_new_ids": 4, "variant_ids_stamped": 4, "write_conflicts": 1,
                     "fetch_outcomes": {"ok": 47, "dead_handle": 10},
                     "cart_proofs": {"sole_variant": 3, "named_variant": 1}, "match_reasons": {"x": 1},
                     "most_blocked_domains": {}}


# ── the real backfill, paged by the driver (no DB, no network) ──────────────────────────────────


def test_the_real_backfill_is_walked_to_the_end_of_a_domain_through_its_cursor(monkeypatch):
    """The adapter reads the backfill's OWN report keys (`candidates`, `next_cursor`,
    `aborted_on_block`); this runs the real `run()` so a renamed key cannot pass unnoticed."""
    from scripts import backfill_shopify_variant_ids as backfill

    seeds = [{"id": f"epsv_{i:03d}", "domain": "anua.us", "updated_at": None,
              "canonical_url": f"https://anua.us/products/h{i}", "destination_url": None,
              "seed_data": {"snapshot": {"variants": [{"sku": f"s{i}"}]}}} for i in range(7)]
    selects: List[tuple] = []

    async def fake_select(limit, domain, after=None, seed_ids=None):
        selects.append((limit, domain, after))
        rows = [s for s in seeds if after is None or s["id"] > after]
        return [dict(r) for r in rows[:limit]]

    monkeypatch.setattr(backfill, "select_candidates", fake_select)
    monkeypatch.setattr(backfill, "GLOBAL_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(backfill, "PER_DOMAIN_MIN_GAP_S", 0.0)
    requested: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(404)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await drive(["anua.us"], refresh.mirror_page_fn(backfill, client, apply=False, page_size=3),
                               refresh.merge_mirror, budget_s=60, gap_s=0.0)

    results = asyncio.run(go())
    assert selects == [(3, "anua.us", None), (3, "anua.us", "epsv_002"), (3, "anua.us", "epsv_005")]
    assert len(requested) == 7 and all(u.endswith(".js") for u in requested)
    assert results["anua.us"].status == DONE and results["anua.us"].pages == 3
    assert results["anua.us"].writer["candidates"] == 7
    assert results["anua.us"].writer["fetch_outcomes"] == {"dead_handle": 7}


def test_the_real_enrichment_writer_reports_an_empty_domain_as_done():
    """Staging may hold no enrichment rows: the writer reports products 0, exhausted, and the
    driver ends the domain after one page."""
    import jobs.enrichment_cart_variant_proof as writer

    class EmptyDb:
        async def fetch_all(self, sql, values=None):
            return []

    plans = {p.domain: p for p in writer.plan_domains(["tartecosmetics.com"])}
    pacer = writer.Pacer(0.0)

    async def go():
        return await drive(["tartecosmetics.com"],
                           refresh.enrichment_page_fn(writer, EmptyDb(), None, plans, apply=False, pacer=pacer),
                           refresh.merge_enrichment, budget_s=60, gap_s=0.0)

    results = asyncio.run(go())
    result = results["tartecosmetics.com"]
    assert result.status == DONE and result.pages == 1
    assert result.writer["pages"][0]["products"] == 0 and pacer.requests == 0


# ── main: arguments, the gate, the one report line ──────────────────────────────────────────────


def test_main_refuses_to_run_without_the_crawl_egress_flag(monkeypatch, capsys):
    def must_not_plan(*a, **k):
        raise AssertionError("planned without --on-crawl-egress")

    monkeypatch.setattr(refresh, "plan_lane", must_not_plan)
    for lane in refresh.LANES:
        assert refresh.main([lane, "--budget-seconds", "60"]) == refresh.EXIT_BAD_ARGS
    assert "--on-crawl-egress" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [
    [], ["sweep", "--on-crawl-egress", "--budget-seconds", "60"], ["mirror", "--on-crawl-egress"],
    ["mirror", "--on-crawl-egress", "--budget-seconds", "0"],
    ["mirror", "--on-crawl-egress", "--budget-seconds", "nan"],
    ["mirror", "--on-crawl-egress", "--budget-seconds", "-5"],
])
def test_main_bad_arguments_exit_2(argv, monkeypatch):
    monkeypatch.setattr(refresh, "plan_lane", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran")))
    assert refresh.main(argv) == refresh.EXIT_BAD_ARGS


def test_a_refused_domain_list_is_exit_2_with_nothing_attempted(monkeypatch):
    from services.tierb_cart_link_merchants import MerchantListError

    def refuse(lane, now):
        raise MerchantListError("not on the merchant list: ['x.com']")

    async def must_not_run(*a, **k):
        raise AssertionError("ran on a refused list")

    monkeypatch.setattr(refresh, "plan_lane", refuse)
    monkeypatch.setattr(refresh, "run_lane", must_not_run)
    assert refresh.main(["enrichment", "--on-crawl-egress", "--budget-seconds", "60"]) == refresh.EXIT_BAD_ARGS


def _fake_lane(monkeypatch, statuses: Dict[str, str]):
    seen: Dict[str, Any] = {}

    def plan(lane, now):
        return refresh.LanePlan(lane=lane, domains=list(statuses), writer=None, gap_s=3.0)

    async def run_lane(plan, *, apply, budget_s, emit, state):
        seen.update(apply=apply, budget_s=budget_s)
        state.info.update(domains_order=plan.domains)
        state.results.update({d: DomainResult(status=s) for d, s in statuses.items()})
        return state.info

    monkeypatch.setattr(refresh, "plan_lane", plan)
    monkeypatch.setattr(refresh, "run_lane", run_lane)
    return seen


@pytest.mark.parametrize("env, mode", [({}, "dry_run"), ({"REAP_CART_PROOF_APPLY": "false"}, "dry_run"),
                                       ({"REAP_CART_PROOF_APPLY": "true"}, "apply")])
def test_main_prints_one_report_line_and_applies_only_through_the_gate(monkeypatch, env, mode):
    seen = _fake_lane(monkeypatch, {"a.com": DONE, "b.com": DONE})
    lines: List[str] = []
    code = refresh.main(["mirror", "--on-crawl-egress", "--budget-seconds", "120"], environ=env,
                        emit=lines.append)
    assert code == refresh.EXIT_OK
    assert seen == {"apply": mode == "apply", "budget_s": 120.0}
    assert len(lines) == 1 and lines[0].startswith(refresh.REPORT_PREFIX) and "\n" not in lines[0]
    report = json.loads(lines[0][len(refresh.REPORT_PREFIX):])
    assert report["mode"] == mode and report["lane"] == "mirror" and report["exit_code"] == 0
    assert report["status_counts"] == {"done": 2} and report["terminated"] is None
    assert set(report["domains"]) == {"a.com", "b.com"}


def test_main_exit_code_is_the_drivers(monkeypatch):
    _fake_lane(monkeypatch, {"a.com": DONE, "b.com": NOT_REACHED})
    lines: List[str] = []
    assert refresh.main(["enrichment", "--on-crawl-egress", "--budget-seconds", "60"], environ={},
                        emit=lines.append) == refresh.EXIT_BUDGET
    assert json.loads(lines[0][len(refresh.REPORT_PREFIX):])["exit_code"] == refresh.EXIT_BUDGET


def test_a_crash_of_the_whole_pass_is_exit_3_with_no_report(monkeypatch, capsys):
    def plan(lane, now):
        return refresh.LanePlan(lane=lane, domains=["a.com"], writer=None, gap_s=3.0)

    async def run_lane(*a, **k):
        raise ConnectionError("db unreachable")

    monkeypatch.setattr(refresh, "plan_lane", plan)
    monkeypatch.setattr(refresh, "run_lane", run_lane)
    lines: List[str] = []
    assert refresh.main(["mirror", "--on-crawl-egress", "--budget-seconds", "60"], environ={},
                        emit=lines.append) == refresh.EXIT_CRASHED
    assert lines == [] and "REAP_CART_PROOF_CRASH ConnectionError" in capsys.readouterr().err


def test_an_unexpected_planning_error_is_a_crash_not_a_block(monkeypatch, capsys):
    """F7: before run_lane, an uncaught exception would leave Python's exit 1, which reads as a block."""
    def boom(lane, now):
        raise ImportError("no module named x")

    monkeypatch.setattr(refresh, "plan_lane", boom)
    assert refresh.main(["mirror", "--on-crawl-egress", "--budget-seconds", "60"], environ={}) == refresh.EXIT_CRASHED
    assert "REAP_CART_PROOF_CRASH ImportError" in capsys.readouterr().err


def test_a_missing_merchant_list_is_a_bad_argument(monkeypatch, capsys):
    def missing(lane, now):
        raise FileNotFoundError("config/tierb_cart_link_merchants.json")

    async def must_not_run(*a, **k):
        raise AssertionError("ran without a merchant list")

    monkeypatch.setattr(refresh, "plan_lane", missing)
    monkeypatch.setattr(refresh, "run_lane", must_not_run)
    assert refresh.main(["mirror", "--on-crawl-egress", "--budget-seconds", "60"], environ={}) == refresh.EXIT_BAD_ARGS
    assert "FileNotFoundError" in capsys.readouterr().err


def test_sigterm_prints_the_partial_report_and_exits_4(monkeypatch):
    """F3: Cloud Run's task timeout sends SIGTERM. The domains already finished are reported, the one in
    flight is `terminated`, and the exit is 4 (the budget or the timeout ended the pass)."""
    def plan(lane, now):
        return refresh.LanePlan(lane=lane, domains=["a.com", "b.com", "c.com"], writer=None, gap_s=0.0)

    async def run_lane(plan, *, apply, budget_s, emit, state):
        async def page(domain, after):
            if domain == "b.com":
                os.kill(os.getpid(), signal.SIGTERM)
                await asyncio.sleep(30)
            return Page({"n": 1}, None)

        await drive(plan.domains, page, merge_list, budget_s=budget_s, gap_s=0.0, emit=emit, results=state.results)
        return state.info

    monkeypatch.setattr(refresh, "plan_lane", plan)
    monkeypatch.setattr(refresh, "run_lane", run_lane)
    lines: List[str] = []
    code = refresh.main(["mirror", "--on-crawl-egress", "--budget-seconds", "60"], environ={}, emit=lines.append)
    assert code == refresh.EXIT_BUDGET
    reports = [json.loads(line[len(refresh.REPORT_PREFIX):]) for line in lines if line.startswith(refresh.REPORT_PREFIX)]
    assert len(reports) == 1 and reports[0]["terminated"] == "SIGTERM"
    assert {d: r["status"] for d, r in reports[0]["domains"].items()} == {
        "a.com": DONE, "b.com": TERMINATED, "c.com": NOT_REACHED}


# ── operator-run only ───────────────────────────────────────────────────────────────────────────


def test_nothing_that_runs_on_merge_or_on_a_schedule_names_these_jobs():
    """The backend deploys on merge. These jobs are provisioned ONLY by an operator running
    infra/gcp/setup_reap_cart_proof_jobs.sh: no workflow, no deploy script, no Cloud Build config,
    no other setup script and not the worker's scheduler may name the script, the wrapper or the
    job names."""
    names = ("setup_reap_cart_proof_jobs", "reap_cart_proof_refresh", "reap-cart-proof")
    paths = [*sorted((REPO / ".github" / "workflows").glob("*.y*ml")),
             *sorted((REPO / "infra").rglob("*")), REPO / "services" / "audit_scheduler.py",
             *sorted(REPO.glob("*.y*ml")), *sorted(REPO.glob("Procfile*")), *sorted(REPO.glob("*.toml"))]
    own = REPO / "infra" / "gcp" / "setup_reap_cart_proof_jobs.sh"
    checked = 0
    for path in paths:
        if path == own or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        checked += 1
        hits = [n for n in names if n in text]
        assert not hits, f"{path.relative_to(REPO)} names {hits}"
    assert checked > 20, "the scan found almost nothing to scan"
    for deploy in ("deploy_backend.sh", "deploy_worker.sh", "cloudbuild.backend.yaml"):
        assert (REPO / "infra" / "gcp" / deploy).is_file(), f"precondition: {deploy} was scanned"


def _code_references(path: Path) -> str:
    """What a file can EXECUTE with: a shell script's whole text; a Python file's code only -- string
    constants that are not docstrings, and imported module names -- so a docstring that documents the
    wrapper as its caller (scripts/backfill_shopify_variant_ids.py does) is not an invocation."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix != ".py":
        return text
    import ast

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return text
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                docstrings.add(id(body[0].value))
    parts: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            parts.append(node.value)
        elif isinstance(node, ast.Import):
            parts.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            parts.append(node.module or "")
            parts.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return "\n".join(parts)


def test_no_script_under_scripts_invokes_these_jobs():
    """The same pin, over scripts/ (ops helpers, one-off runners, CI helpers): nothing there may run the
    setup script, import or execute the wrapper, or name the Cloud Run jobs."""
    names = ("setup_reap_cart_proof_jobs", "reap_cart_proof_refresh", "reap-cart-proof")
    checked = 0
    for path in sorted((REPO / "scripts").rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix in (".pyc", ".csv", ".md"):
            continue
        checked += 1
        hits = [n for n in names if n in _code_references(path)]
        assert not hits, f"{path.relative_to(REPO)} invokes {hits}"
    assert checked > 50, "the scan found almost nothing under scripts/"


def test_the_scripts_scan_sees_an_invocation_and_ignores_a_docstring(tmp_path):
    shell = tmp_path / "x.sh"
    shell.write_text("infra/gcp/setup_reap_cart_proof_jobs.sh prod abc --enable\n")
    assert "setup_reap_cart_proof_jobs" in _code_references(shell)
    code = tmp_path / "x.py"
    code.write_text('"""Called by jobs/reap_cart_proof_refresh.py."""\nimport subprocess\n'
                    'subprocess.run(["python", "-m", "jobs.reap_cart_proof_refresh", "mirror"])\n')
    assert "reap_cart_proof_refresh" in _code_references(code)
    doc_only = tmp_path / "y.py"
    doc_only.write_text('"""Its scheduled caller is jobs/reap_cart_proof_refresh.py."""\nX = 1\n')
    assert "reap_cart_proof_refresh" not in _code_references(doc_only)
    imported = tmp_path / "z.py"
    imported.write_text("from jobs import reap_cart_proof_refresh\n")
    assert "reap_cart_proof_refresh" in _code_references(imported)
    plain_import = tmp_path / "w.py"
    plain_import.write_text("import jobs.reap_cart_proof_refresh as refresh\n")
    assert "reap_cart_proof_refresh" in _code_references(plain_import)


def test_the_wrapper_holds_no_literal_sentinel_merchant():
    """The ADR-009 rule, applied to this new file: no `external_seed` merchant literal and no
    hardcoded observed-seller prefix."""
    source = (REPO / "jobs" / "reap_cart_proof_refresh.py").read_text()
    assert "'external_seed'" not in source and '"external_seed"' not in source
    assert "merch_obs_" not in source


# ── cursors: resume where a store was cut, forget it when the walk completes ────────────────────


def test_a_domain_resumes_from_its_stored_cursor_and_every_page_is_checkpointed():
    clock = Clock()
    run_page, calls = pages_fn({"a.com": [Page({}, "c5"), Page({}, None)], "b.com": [Page({}, None)]})
    marks: List[tuple] = []

    async def checkpoint(domain, result, final):
        marks.append((domain, final, result.status, result.resume_cursor()))

    results = asyncio.run(drive(["a.com", "b.com"], run_page, merge_list, budget_s=100, gap_s=0.0, clock=clock,
                                sleep=clock.sleep, start_cursors={"a.com": "c4"}, checkpoint=checkpoint))
    assert calls == [("a.com", "c4"), ("a.com", "c5"), ("b.com", None)]
    assert results["a.com"].start_cursor == "c4"
    assert marks == [("a.com", False, NOT_REACHED, "c5"), ("a.com", True, DONE, "c5"), ("b.com", True, DONE, None)]


def test_no_checkpoint_for_a_domain_the_pass_never_reached():
    clock = Clock()
    run_page, _ = pages_fn({"a.com": [Page({}, None)], "b.com": [Page({}, None)]}, clock=clock, cost=200.0)
    marks: List[str] = []

    async def checkpoint(domain, result, final):
        marks.append(domain)

    asyncio.run(drive(["a.com", "b.com"], run_page, merge_list, budget_s=100, gap_s=0.0, clock=clock,
                      sleep=clock.sleep, checkpoint=checkpoint))
    assert marks == ["a.com"]


def test_a_failed_checkpoint_is_recorded_and_the_pass_goes_on():
    clock = Clock()
    run_page, calls = pages_fn({"a.com": [Page({}, "c1"), Page({}, None)], "b.com": [Page({}, None)]})

    async def checkpoint(domain, result, final):
        raise ConnectionError("db down")

    results = asyncio.run(drive(["a.com", "b.com"], run_page, merge_list, budget_s=100, gap_s=0.0, clock=clock,
                                sleep=clock.sleep, checkpoint=checkpoint))
    assert [c[0] for c in calls] == ["a.com", "a.com", "b.com"]
    assert results["a.com"].status == DONE and "ConnectionError" in results["a.com"].checkpoint_error


_BASE = {"completed_at": None, "blocked_until": None, "crash_count": 0}


@pytest.mark.parametrize("status, final, stored", [
    (DONE, True, {**_BASE, "next_cursor": None, "last_status": DONE, "completed_at": T0}),
    (CURSOR_STUCK, True, {**_BASE, "next_cursor": None, "last_status": CURSOR_STUCK}),
    (BUDGET_STOPPED, True, {**_BASE, "next_cursor": "c7", "last_status": BUDGET_STOPPED}),
    (ABORTED, True, {**_BASE, "next_cursor": "c7", "last_status": ABORTED, "blocked_until": T0 + timedelta(days=3)}),
    (CRASHED, True, {**_BASE, "next_cursor": "c7", "last_status": CRASHED, "crash_count": 1}),
    (NOT_REACHED, False, {**_BASE, "next_cursor": "c7", "last_status": "in_progress"}),
])
def test_what_a_checkpoint_stores(status, final, stored):
    result = DomainResult(status=status, start_cursor="c3", last_cursor="c7")
    assert refresh.cursor_row_for(result, final, T0) == stored


def test_the_store_whose_abort_stopped_the_pass_is_not_backed_off():
    result = DomainResult(status=ABORTED, start_cursor="c3", pass_abort=True)
    assert refresh.cursor_row_for(result, True, T0)["blocked_until"] is None


def _crashed(cursor, count=1):
    return CursorRow(next_cursor=cursor, last_status=CRASHED, last_completed_at=None, updated_at=None,
                     crash_count=count)


def test_a_second_crash_at_the_same_cursor_resets_it_so_the_rows_before_are_walked(caplog):
    """N2: a poison page must not pin the cursor forever."""
    result = DomainResult(status=CRASHED, start_cursor="c7")
    row = refresh.cursor_row_for(result, True, T0, _crashed("c7", 1))
    assert row["next_cursor"] is None and row["crash_count"] == 0 and result.crash_cursor_reset
    assert "resetting the cursor" in caplog.text


@pytest.mark.parametrize("prior", [
    None,
    _crashed("c5", 1),                                                    # crashed somewhere else
    CursorRow(next_cursor="c7", last_status=BUDGET_STOPPED, last_completed_at=None, updated_at=None),
])
def test_a_first_crash_at_a_cursor_keeps_it_and_counts_one(prior):
    result = DomainResult(status=CRASHED, start_cursor="c7")
    row = refresh.cursor_row_for(result, True, T0, prior)
    assert row["next_cursor"] == "c7" and row["crash_count"] == 1 and not result.crash_cursor_reset


def test_a_domain_cut_on_its_first_page_keeps_the_cursor_it_resumed_from():
    result = DomainResult(status=ABORTED, start_cursor="c3")
    assert refresh.cursor_row_for(result, True, T0)["next_cursor"] == "c3"


def test_a_merge_failure_is_recorded_against_its_domain_and_the_pass_goes_on():
    clock = Clock()
    run_page, calls = pages_fn({"a.com": [Page({"bad": 1}, None)], "b.com": [Page({}, None)]})

    def merge(total, page):
        if "bad" in page:
            raise TypeError("unsummable")
        total.setdefault("pages", []).append(page)

    results = asyncio.run(drive(["a.com", "b.com"], run_page, merge, budget_s=100, gap_s=0.0, clock=clock,
                                sleep=clock.sleep))
    assert results["a.com"].status == CRASHED and "TypeError" in results["a.com"].error
    assert results["b.com"].status == DONE and [c[0] for c in calls] == ["a.com", "b.com"]


def test_a_terminated_domain_does_not_count_as_walked():
    assert exit_code({"a.com": DomainResult(status=DONE), "b.com": DomainResult(status=TERMINATED)}) == 4


# ── F4: the mirror block streak survives the end of a backfill call ─────────────────────────────


def _mirror_seeds(domain: str, n: int, start: int = 0) -> List[Dict[str, Any]]:
    return [{"id": f"epsv_{domain[0]}{i:03d}", "domain": domain, "updated_at": None,
             "canonical_url": f"https://{domain}/products/h{i}", "destination_url": None,
             "seed_data": {"snapshot": {"variants": [{"sku": f"s{i}"}]}}} for i in range(start, start + n)]


def _real_backfill_pass(monkeypatch, seeds_by_domain, handler, domains, page_size=3, breaker=False,
                        max_wait=refresh.MIRROR_MAX_POLITE_WAIT_S, forgive_window_s=refresh.LANE_IP_TRIP_WINDOW_S):
    """The REAL backfill `run()` over a faked selection and a mock transport, through the mirror client
    (crawl_politeness gate; with `breaker=True`, the lane's store-keyed IP breaker installed exactly as
    run_lane installs it). Bounded at 20 s, so a wait that should have been refused fails the test
    instead of hanging it."""
    from scripts import backfill_shopify_variant_ids as backfill
    from services import crawl_ip_throttle

    async def fake_select(limit, domain, after=None, seed_ids=None):
        rows = [s for s in seeds_by_domain[domain] if after is None or s["id"] > after]
        return [dict(r) for r in rows[:limit]]

    monkeypatch.setattr(backfill, "select_candidates", fake_select)
    monkeypatch.setattr(backfill, "GLOBAL_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(backfill, "PER_DOMAIN_MIN_GAP_S", 0.0)
    results: Dict[str, DomainResult] = {}
    ip = (refresh.store_breaker(domains, is_aborted=lambda s: s in results and results[s].status == ABORTED)
          if breaker else None)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as raw:
            client = refresh.BlockStreakClient(raw, backfill, breaker=ip, max_wait=max_wait)
            await drive(domains, refresh.mirror_page_fn(backfill, client, apply=False, page_size=page_size),
                        refresh.merge_mirror, budget_s=60, gap_s=0.0, on_domain_start=client.start_store,
                        stop_signal=(lambda: ip.tripped) if ip is not None else None,
                        forgive_window_s=forgive_window_s, results=results)
            return results, client

    async def bounded():
        return await asyncio.wait_for(go(), timeout=20)

    if ip is None:
        return asyncio.run(bounded())
    with crawl_ip_throttle.installed(ip):
        return asyncio.run(bounded())


def test_small_stores_under_an_ip_level_block_trip_the_breaker_and_nothing_is_backed_off(monkeypatch):
    """Each store has 5 seeds, under the backfill's threshold of 8, and every answer is a 429. The
    breaker (3 distinct throttling stores) trips at c.com's first answer; c.com asks nothing more, d and
    e are not reached, and a and b -- aborted as "no clean answer" -- have their back-off forgiven."""
    requested: List[str] = []

    def handler(request):
        requested.append(request.url.host)
        return httpx.Response(429)

    domains = ["a.com", "b.com", "c.com", "d.com", "e.com"]
    seeds = {d: _mirror_seeds(d, 5) for d in domains}
    results, client = _real_backfill_pass(monkeypatch, seeds, handler, domains, page_size=10, breaker=True)
    assert client.breaker.tripped and len(requested) == 11
    assert [results[d].status for d in ("a.com", "b.com")] == [ABORTED, ABORTED]
    assert results["a.com"].ip_block and results["b.com"].ip_block
    assert results["c.com"].status == refresh.IP_THROTTLED and results["c.com"].pass_abort
    assert [results[d].status for d in ("d.com", "e.com")] == [NOT_REACHED, NOT_REACHED]
    assert not any(refresh.cursor_row_for(r, True, T0)["blocked_until"] for r in results.values())
    assert exit_code(results) == refresh.EXIT_ABORTED_ON_BLOCK


def test_r2_a_healthy_store_that_inherits_trailing_blocks_does_not_stop_the_pass(monkeypatch):
    """The reviewer's case: X answers 15 clean then 5 blocks; Y blocks throughout; Z sees 3 transient
    429s then answers. No breaker here (one store throttling is not an address throttle), and Z's own
    streak is 3: Z is walked, Y alone is aborted."""
    from scripts import backfill_shopify_variant_ids as backfill

    answers = {"x.com": ["ok"] * 15 + ["block"] * 5, "y.com": ["block"] * 20, "z.com": ["block"] * 3 + ["ok"] * 7}
    served: Dict[str, int] = {}

    def handler(request):
        host = request.url.host
        i = served.get(host, 0)
        served[host] = i + 1
        return httpx.Response(429 if answers[host][i] == "block" else 404)

    seeds = {d: _mirror_seeds(d, len(a)) for d, a in answers.items()}
    # Page size 3: Z's FIRST page is its three 429s; its second page answers.
    results, client = _real_backfill_pass(monkeypatch, seeds, handler, list(answers), page_size=3)
    assert results["x.com"].status == DONE
    assert results["y.com"].status == ABORTED and not results["y.com"].pass_abort
    assert results["z.com"].status == DONE and served["z.com"] == 10
    assert served["y.com"] == backfill.CONSECUTIVE_BLOCK_ABORT
    assert exit_code(results) == refresh.EXIT_BUDGET, "one store blocked us; not an IP-level block"


@pytest.mark.parametrize("page_size", [10, 5])
def test_one_store_that_blocks_is_aborted_alone_and_the_next_store_is_walked(monkeypatch, page_size):
    """page_size 5: the store streak reaches 8 across two backfill calls, while each call's own counter
    stays under 8 -- only the carried store streak can abort it."""
    from scripts import backfill_shopify_variant_ids as backfill

    requested: List[str] = []

    def handler(request):
        requested.append(request.url.host)
        return httpx.Response(403 if request.url.host == "a.com" else 404)

    seeds = {"a.com": _mirror_seeds("a.com", 20), "b.com": _mirror_seeds("b.com", 4)}
    results, client = _real_backfill_pass(monkeypatch, seeds, handler, ["a.com", "b.com"], page_size=page_size)
    assert results["a.com"].status == ABORTED and not results["a.com"].pass_abort
    assert requested.count("a.com") == backfill.CONSECUTIVE_BLOCK_ABORT
    assert results["b.com"].status == DONE and requested.count("b.com") == 4
    assert client.state == {"store": 0}


def test_a_clean_answer_resets_the_carried_streak_and_not_json_neither_adds_nor_resets(monkeypatch):
    answers = iter([httpx.Response(429)] * 5 + [httpx.Response(404)] + [httpx.Response(429)] * 4
                   + [httpx.Response(200, text="<html>", headers={"content-type": "text/html"})]
                   + [httpx.Response(429)] * 3)

    def handler(request):
        return next(answers)

    seeds = {"a.com": _mirror_seeds("a.com", 6), "b.com": _mirror_seeds("b.com", 8)}
    results, client = _real_backfill_pass(monkeypatch, seeds, handler, ["a.com", "b.com"])
    # 5 blocks, a 404 resets; then 4 + (not_json) + 3 = 7 blocks: under 8, so no STORE-streak abort --
    # but b.com never answered cleanly once, so it is not `done` (the "no clean answer" rule).
    assert results["a.com"].status == DONE
    assert results["b.com"].status == ABORTED and not results["b.com"].pass_abort
    assert client.state == {"store": 7} and client.short_circuited == 0


def test_the_streak_client_passes_a_transport_error_through_after_counting_it():
    from scripts import backfill_shopify_variant_ids as backfill

    class Raising:
        async def get(self, url, **kwargs):
            raise httpx.ConnectError("reset")

    client = refresh.BlockStreakClient(Raising(), backfill)
    with pytest.raises(httpx.ConnectError):
        asyncio.run(client.get("https://a.com/products/x.js"))
    assert client.state == {"store": 1}


# ── F1: the REAL run_lane, a stub database, fake writers ────────────────────────────────────────


class StubDb:
    """Answers every read with nothing and records every write; connect/disconnect are counted."""

    def __init__(self, cursor_rows: Optional[List[Dict[str, Any]]] = None) -> None:
        self.cursor_rows = cursor_rows or []
        self.executed: List[tuple] = []
        self.reads: List[str] = []
        self.connected = 0

    async def connect(self):
        self.connected += 1

    async def disconnect(self):
        self.connected -= 1

    async def fetch_all(self, sql, values=None):
        self.reads.append(sql)
        if "reap_cart_proof_refresh_cursors" in sql:
            return list(self.cursor_rows)
        return []

    async def execute(self, sql, values=None):
        self.executed.append((sql, values))


class FakeMirrorWriter:
    GLOBAL_MIN_INTERVAL_S = 0.0
    PER_DOMAIN_MIN_GAP_S = 0.0
    CONSECUTIVE_BLOCK_ABORT = 8

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        return {"candidates": 1, "next_cursor": "x", "aborted_on_block": False}

    async def fetch_product_js(self, client, url):
        return None, "ok"

    @staticmethod
    def _is_block(outcome):
        return False


class FakeNoCookieClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeEnrichmentWriter:
    def __init__(self) -> None:
        self.calls: List[tuple] = []
        self.clients: List[Any] = []
        self.pacers: List[Any] = []

    def request_gap_s(self):
        return 0.0

    def abort_after_blocks(self):
        return 5

    class Pacer:
        def __init__(self, gap):
            self.gap = gap
            self.requests = 0

    def no_cookie_client(self):
        client = FakeNoCookieClient()
        self.clients.append(client)
        return client

    async def run_domain(self, db, client, plan, **kwargs):
        self.calls.append((db, client, plan, kwargs))
        return {"exhausted": True, "next_cursor": None, "aborted_on_block": False, "written": 0}


def _run_real_lane(plan, *, apply, db):
    state = refresh.RunState()
    asyncio.run(refresh.run_lane(plan, apply=apply, budget_s=60, emit=lambda line: None, state=state, db=db,
                                 now=lambda: T0))
    return state


@pytest.mark.parametrize("apply", [False, True])
def test_the_real_mirror_lane_hands_the_gate_and_the_streak_client_to_the_backfill(apply):
    writer = FakeMirrorWriter()
    plan = refresh.LanePlan(lane="mirror", domains=["b.com", "a.com"], writer=writer, gap_s=0.0,
                            proof_max_age=timedelta(days=7))
    db = StubDb()
    state = _run_real_lane(plan, apply=apply, db=db)
    assert [c["domain"] for c in writer.calls] == ["a.com", "b.com"], "never-completed stores, by name"
    assert all(c["apply"] is apply for c in writer.calls)
    assert all(isinstance(c["client"], refresh.BlockStreakClient) for c in writer.calls)
    assert len({id(c["client"]) for c in writer.calls}) == 1, "ONE client, so the streak is carried"
    assert {r.status for r in state.results.values()} == {DONE}
    assert db.connected == 0
    upserts = [v for sql, v in db.executed if v and "INSERT INTO reap_cart_proof_refresh_cursors" in sql]
    if apply:
        assert any("CREATE TABLE IF NOT EXISTS reap_cart_proof_refresh_cursors" in sql for sql, _ in db.executed)
        assert {v["domain"] for v in upserts} == {"a.com", "b.com"}
        assert all(v["last_status"] == DONE and v["next_cursor"] is None for v in upserts)
    else:
        assert db.executed == [], "a dry run writes nothing, not even a cursor or a CREATE"


def test_the_real_mirror_lane_resumes_a_stored_cursor():
    writer = FakeMirrorWriter()
    plan = refresh.LanePlan(lane="mirror", domains=["a.com"], writer=writer, gap_s=0.0,
                            proof_max_age=timedelta(days=7))
    db = StubDb(cursor_rows=[{"domain": "a.com", "next_cursor": "epsv_a040", "last_status": "budget_stopped",
                              "last_completed_at": None, "updated_at": None}])
    state = _run_real_lane(plan, apply=False, db=db)
    assert writer.calls[0]["after"] == "epsv_a040"
    assert state.info["resumed"] == {"a.com": "epsv_a040"}


@pytest.mark.parametrize("apply", [False, True])
def test_the_real_enrichment_lane_hands_the_gate_the_shared_pacer_and_the_no_cookie_client(apply):
    writer = FakeEnrichmentWriter()
    ensured: List[bool] = []

    async def ensure():
        ensured.append(True)
        return True

    plan = refresh.LanePlan(lane="enrichment", domains=["a.com", "b.com"], writer=writer, gap_s=0.0,
                            plans={"a.com": "PLAN-A", "b.com": "PLAN-B"}, ensure_proof_table=ensure)
    db = StubDb()
    state = _run_real_lane(plan, apply=apply, db=db)
    assert [c[2] for c in writer.calls] == ["PLAN-A", "PLAN-B"]
    assert all(c[0] is db for c in writer.calls)
    assert all(c[3]["apply"] is apply for c in writer.calls)
    assert len(writer.clients) == 1 and all(c[1] is writer.clients[0] for c in writer.calls), \
        "the writer's no-cookie client, never a plain one"
    pacers = {id(c[3]["pacer"]) for c in writer.calls}
    assert len(pacers) == 1 and isinstance(writer.calls[0][3]["pacer"], FakeEnrichmentWriter.Pacer)
    streaks = [c[3]["block_state"] for c in writer.calls]
    assert len({id(x) for x in streaks}) == 2 and all(isinstance(x, refresh.ObservedStreak) for x in streaks), \
        "a store streak per store"
    stops = [c[3]["should_stop"] for c in writer.calls]
    assert all(stop() is False for stop in stops), "the run's breaker, not tripped"
    assert all(c[3]["limit"] == refresh.ENRICHMENT_PAGE_PRODUCTS for c in writer.calls)
    assert ensured == ([True] if apply else [])
    assert {r.status for r in state.results.values()} == {DONE}
    assert state.info["requests"] == 0


# ── N1: back-off, blocked stores last, and the reviewer's simulation ─────────────────────────────


def _cursor(status="done", blocked_until=None, completed=None):
    return CursorRow(next_cursor=None, last_status=status, last_completed_at=completed, updated_at=None,
                     blocked_until=blocked_until)


def test_a_store_inside_its_back_off_is_skipped_and_one_past_it_goes_last():
    cursors = {"a.com": _cursor(ABORTED, T0 + timedelta(days=1)),       # still backed off
               "b.com": _cursor(ABORTED, T0 - timedelta(hours=1)),      # back-off over: retry, last
               "c.com": _cursor(DONE)}
    order, skipped = refresh.defer_blocked(["a.com", "b.com", "c.com", "d.com"], cursors, T0)
    assert order == ["c.com", "d.com", "b.com"] and skipped == ["a.com"]


class MemoryDb:
    """A stub database that keeps the cursor table in memory across runs (and answers the stalest-first
    read with nothing), so several days can be simulated through the REAL run_lane."""

    def __init__(self) -> None:
        self.rows: Dict[tuple, Dict[str, Any]] = {}

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def fetch_all(self, sql, values=None):
        if "FROM reap_cart_proof_refresh_cursors" in sql:
            return [dict(r, domain=d) for (lane, d), r in self.rows.items() if lane == values["lane"]]
        return []

    async def execute(self, sql, values=None):
        if values and "INSERT INTO reap_cart_proof_refresh_cursors" in sql:
            key = (values["lane"], values["domain"])
            prior = self.rows.get(key, {})
            row = {k: values[k] for k in ("next_cursor", "last_status", "blocked_until", "crash_count", "updated_at")}
            row["last_completed_at"] = values["last_completed_at"] or prior.get("last_completed_at")
            self.rows[key] = row


def test_the_reviewers_simulation_one_always_blocking_store_and_41_healthy_all_walked(monkeypatch):
    """N1. The blocking store sorts first (never completed, first by name) and 403s every .js. Day 1: it
    is aborted after the threshold and every one of the 41 others is walked. Day 2: it is backed off.
    Day 4: it is walked LAST, and the 41 are still all walked."""
    from scripts import backfill_shopify_variant_ids as backfill

    blocker = "aaa-blocker.com"
    healthy = [f"store{i:02d}.com" for i in range(41)]
    seeds = {d: _mirror_seeds(d, 3) for d in healthy}
    seeds[blocker] = _mirror_seeds(blocker, 30)

    async def fake_select(limit, domain, after=None, seed_ids=None):
        rows = [r for r in seeds[domain] if after is None or r["id"] > after]
        return [dict(r) for r in rows[:limit]]

    monkeypatch.setattr(backfill, "select_candidates", fake_select)
    monkeypatch.setattr(backfill, "GLOBAL_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(backfill, "PER_DOMAIN_MIN_GAP_S", 0.0)
    requested: List[str] = []

    def handler(request):
        requested.append(request.url.host)
        return httpx.Response(403 if request.url.host == blocker else 404)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(refresh.httpx, "AsyncClient",
                        lambda *a, **k: real_client(transport=httpx.MockTransport(handler)))
    db = MemoryDb()
    plan = refresh.LanePlan(lane="mirror", domains=sorted([blocker, *healthy]), writer=backfill, gap_s=0.0,
                            proof_max_age=timedelta(days=7))

    def day(offset):
        requested.clear()
        state = refresh.RunState()
        asyncio.run(refresh.run_lane(plan, apply=True, budget_s=600, emit=lambda line: None, state=state, db=db,
                                     now=lambda: T0 + offset))
        return state

    first = day(timedelta(0))
    assert first.info["domains_order"][0] == blocker
    assert first.results[blocker].status == ABORTED and not first.results[blocker].pass_abort
    assert [first.results[d].status for d in healthy] == [DONE] * 41
    assert requested.count(blocker) == backfill.CONSECUTIVE_BLOCK_ABORT
    assert exit_code(first.results) == refresh.EXIT_BUDGET

    second = day(timedelta(days=1))
    assert second.results[blocker].status == refresh.BACKED_OFF and blocker not in requested
    assert [second.results[d].status for d in healthy] == [DONE] * 41
    assert exit_code(second.results) == refresh.EXIT_OK

    fourth = day(timedelta(days=3, hours=1))
    assert fourth.info["domains_order"][-1] == blocker, "a store that blocked us goes last"
    assert [fourth.results[d].status for d in healthy] == [DONE] * 41
    assert fourth.results[blocker].status == ABORTED
    assert db.rows[("mirror", blocker)]["blocked_until"] == T0 + timedelta(days=3, hours=1) + refresh.MIRROR_BLOCK_BACKOFF


def test_the_enrichment_lane_backs_off_and_defers_its_fixed_order_too():
    writer = FakeEnrichmentWriter()
    plan = refresh.LanePlan(lane="enrichment", domains=["a.com", "b.com", "c.com"], writer=writer, gap_s=0.0,
                            plans={"a.com": "PLAN-A", "b.com": "PLAN-B", "c.com": "PLAN-C"})
    db = StubDb(cursor_rows=[
        {"domain": "a.com", "next_cursor": None, "last_status": ABORTED, "last_completed_at": None,
         "blocked_until": T0 - timedelta(hours=1), "crash_count": 0, "updated_at": None},
        {"domain": "b.com", "next_cursor": None, "last_status": ABORTED, "last_completed_at": None,
         "blocked_until": T0 + timedelta(days=2), "crash_count": 0, "updated_at": None}])
    state = _run_real_lane(plan, apply=False, db=db)
    assert [c[2] for c in writer.calls] == ["PLAN-C", "PLAN-A"]
    assert state.results["b.com"].status == refresh.BACKED_OFF and "b.com" in state.info["backed_off"]


# ── N3: the SIGTERM report is printed before the database is let go, and is never exit 0 ────────


def _sigterm_main(monkeypatch, *, when):
    """Run main() with the REAL run_lane on a stub database; SIGTERM is sent at `when` ("connect" or
    "page"). Returns (exit code, report lines, lines printed before disconnect)."""
    lines: List[str] = []
    before_disconnect: List[List[str]] = []

    class Db(StubDb):
        async def connect(self):
            await super().connect()
            if when == "connect":
                os.kill(os.getpid(), signal.SIGTERM)
                await asyncio.sleep(30)

        async def disconnect(self):
            before_disconnect.append(list(lines))
            await super().disconnect()

    class Writer(FakeMirrorWriter):
        async def run(self, **kwargs):
            if kwargs["domain"] == "b.com":
                os.kill(os.getpid(), signal.SIGTERM)
                await asyncio.sleep(30)
            return {"candidates": 0, "next_cursor": None, "aborted_on_block": False}

    plan = refresh.LanePlan(lane="mirror", domains=["a.com", "b.com", "c.com"], writer=Writer(), gap_s=0.0,
                            proof_max_age=timedelta(days=7))
    monkeypatch.setattr(refresh, "plan_lane", lambda lane, now: plan)
    real = refresh.run_lane

    async def run_lane(plan, **kwargs):
        return await real(plan, db=Db(), **kwargs)

    monkeypatch.setattr(refresh, "run_lane", run_lane)
    code = refresh.main(["mirror", "--on-crawl-egress", "--budget-seconds", "60"], environ={}, emit=lines.append)
    return code, [line for line in lines if line.startswith(refresh.REPORT_PREFIX)], before_disconnect


def test_the_partial_report_is_printed_before_the_disconnect(monkeypatch):
    code, reports, before = _sigterm_main(monkeypatch, when="page")
    assert code == refresh.EXIT_BUDGET and len(reports) == 1
    assert before and any(line.startswith(refresh.REPORT_PREFIX) for line in before[0])
    body = json.loads(reports[0][len(refresh.REPORT_PREFIX):])
    assert body["terminated"] == "SIGTERM" and body["domains"]["b.com"]["status"] == TERMINATED


def test_a_sigterm_before_any_store_still_reports_and_exits_4(monkeypatch):
    code, reports, _before = _sigterm_main(monkeypatch, when="connect")
    assert code == refresh.EXIT_BUDGET and len(reports) == 1
    body = json.loads(reports[0][len(refresh.REPORT_PREFIX):])
    assert body["terminated"] == "SIGTERM" and body["exit_code"] == refresh.EXIT_BUDGET


# ── R1: an IP-level block backs off no store; the enrichment back-off is shorter than its proofs ──


def test_r1_an_ip_level_block_leaves_no_store_backed_off(monkeypatch):
    """20-seed stores, every answer a 429, through the REAL run_lane (breaker installed, lane
    thresholds). a.com and b.com are aborted and first recorded with a back-off; c.com's first 429 trips
    the breaker, the pass stops, and both are re-recorded without it. Nothing carries blocked_until."""
    from scripts import backfill_shopify_variant_ids as backfill

    domains = ["a.com", "b.com", "c.com"]
    seeds = {d: _mirror_seeds(d, 20) for d in domains}

    async def fake_select(limit, domain, after=None, seed_ids=None):
        rows = [r for r in seeds[domain] if after is None or r["id"] > after]
        return [dict(r) for r in rows[:limit]]

    monkeypatch.setattr(backfill, "select_candidates", fake_select)
    monkeypatch.setattr(backfill, "GLOBAL_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(backfill, "PER_DOMAIN_MIN_GAP_S", 0.0)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(refresh.httpx, "AsyncClient",
                        lambda *a, **k: real_client(transport=httpx.MockTransport(lambda r: httpx.Response(429))))
    db = MemoryDb()
    plan = refresh.LanePlan(lane="mirror", domains=domains, writer=backfill, gap_s=0.0,
                            proof_max_age=timedelta(days=7))
    state = refresh.RunState()
    asyncio.run(refresh.run_lane(plan, apply=True, budget_s=600, emit=lambda line: None, state=state, db=db,
                                 now=lambda: T0))
    # a.com and b.com each 429 eight times (store aborts, 2 distinct hosts); c.com's first 429 is the
    # third distinct throttling store inside the window: the breaker trips, the pass stops.
    assert state.results["a.com"].status == ABORTED and state.results["a.com"].ip_block
    assert state.results["b.com"].status == ABORTED and state.results["b.com"].ip_block
    assert state.results["c.com"].status == refresh.IP_THROTTLED and state.results["c.com"].pass_abort
    assert state.info["ip_throttle"]["ip_throttled"] is True
    assert {d: r["blocked_until"] for (_, d), r in db.rows.items()} == {"a.com": None, "b.com": None, "c.com": None}
    assert all(r["last_completed_at"] is None for r in db.rows.values())


def test_without_a_trip_a_store_that_blocked_us_keeps_its_back_off():
    clock = Clock()
    run_page, _ = pages_fn({"a.com": [Page({}, None, aborted=True)], "b.com": [Page({}, None)]})
    results = asyncio.run(drive(["a.com", "b.com"], run_page, merge_list, budget_s=100, gap_s=0.0, clock=clock,
                                sleep=clock.sleep, stop_signal=lambda: False))
    assert results["a.com"].status == ABORTED and not results["a.com"].ip_block
    assert refresh.cursor_row_for(results["a.com"], True, T0)["blocked_until"] == T0 + refresh.MIRROR_BLOCK_BACKOFF


def test_r1_each_lanes_back_off_is_shorter_than_its_proofs_life():
    from services.reap_enrichment_cart_proof import MAX_PROOF_AGE
    from services.shopify_variant_identity import CART_PROOF_MAX_AGE

    assert refresh.block_backoff("enrichment") == timedelta(days=1) < MAX_PROOF_AGE
    assert refresh.block_backoff("mirror") == timedelta(days=3) < CART_PROOF_MAX_AGE
    result = DomainResult(status=ABORTED, start_cursor="c3")
    row = refresh.cursor_row_for(result, True, T0, backoff=refresh.block_backoff("enrichment"))
    assert row["blocked_until"] == T0 + timedelta(days=1)


def test_r1_the_enrichment_lane_records_its_one_day_back_off():
    class Writer(FakeEnrichmentWriter):
        async def run_domain(self, db, client, plan, *, block_state, block_limit, **kwargs):
            if plan == "PLAN-A":
                for _ in range(block_limit):
                    block_state["consecutive"] += 1
                return {"exhausted": False, "next_cursor": None, "aborted_on_block": True}
            block_state["consecutive"] = 0
            return {"exhausted": True, "next_cursor": None, "aborted_on_block": False}

    writer = Writer()

    async def ensure():
        return True

    plan = refresh.LanePlan(lane="enrichment", domains=["a.com", "b.com"], writer=writer, gap_s=0.0,
                            plans={"a.com": "PLAN-A", "b.com": "PLAN-B"}, ensure_proof_table=ensure)
    db = MemoryDb()
    state = refresh.RunState()
    asyncio.run(refresh.run_lane(plan, apply=True, budget_s=60, emit=lambda line: None, state=state, db=db,
                                 now=lambda: T0))
    assert db.rows[("enrichment", "a.com")]["blocked_until"] == T0 + timedelta(days=1)
    assert db.rows[("enrichment", "b.com")]["blocked_until"] is None
    assert state.results["b.com"].status == DONE


# ── minor: a store never read is not `done` ─────────────────────────────────────────────────────


def test_a_store_with_blocks_and_no_clean_answer_is_aborted_not_done():
    def run(answers):
        return _enrichment_pass(answers)

    results = run({"a.com": ["block", "block"], "b.com": ["ok"]})
    assert results["a.com"].status == ABORTED and not results["a.com"].pass_abort
    assert results["b.com"].status == DONE
    assert refresh.cursor_row_for(results["a.com"], True, T0)["completed_at"] is None
    results = run({"a.com": ["block", "ok"]})
    assert results["a.com"].status == DONE, "one clean answer is a read"
    results = run({"a.com": []})
    assert results["a.com"].status == DONE, "a store with nothing to fetch is not blocked"


def test_enrichment_a_store_with_a_few_transient_429s_is_walked():
    job = PagedEnrichmentJob({"a.com": [["ok"] + ["block"] * 4], "b.com": [["block", "block"], ["ok", "ok"]]})
    page_fn = refresh.enrichment_page_fn(job, None, None, {d: d for d in ("a.com", "b.com")}, apply=False,
                                         pacer=None)
    results = asyncio.run(drive(["a.com", "b.com"], page_fn, refresh.merge_enrichment, budget_s=60, gap_s=0.0))
    assert results["a.com"].status == DONE and results["b.com"].status == DONE and results["b.com"].pages == 2


def test_the_carried_store_streak_aborts_a_store_that_had_answered_before(monkeypatch):
    """One clean answer, then blocks, in pages of 5: the store streak reaches 8 in the second page while
    the backfill's own per-call counter is at 5 -- only the carried streak can abort it, and the "no
    clean answer" rule cannot (it had one)."""
    requested: List[str] = []
    served = {"n": 0}

    def handler(request):
        requested.append(request.url.host)
        served["n"] += 1
        return httpx.Response(404 if served["n"] == 1 else 403)

    seeds = {"a.com": _mirror_seeds("a.com", 20)}
    results, client = _real_backfill_pass(monkeypatch, seeds, handler, ["a.com"], page_size=5)
    assert results["a.com"].status == ABORTED and results["a.com"].pages == 2
    assert len(requested) == 9


def test_the_09_30_pattern_a_throttle_that_lets_200s_through_backs_off_no_healthy_store(monkeypatch):
    """Every store answers 429, 404, 429, 404, ... (a rate throttle that lets occasional answers
    through). No store reaches 8 in a row and none is "never read", so without the breaker every one
    would be walked into the throttle; with it, the third throttling store trips it, the pass stops,
    and no store carries a back-off."""

    def handler(request):
        n = int(request.url.path.split("/h")[-1].split(".")[0])
        return httpx.Response(429 if n % 2 == 0 else 404)

    domains = ["a.com", "b.com", "c.com", "d.com"]
    seeds = {d: _mirror_seeds(d, 6) for d in domains}
    results, _client = _real_backfill_pass(monkeypatch, seeds, handler, domains, page_size=10, breaker=True)
    assert results["a.com"].status == DONE and results["b.com"].status == DONE
    assert results["c.com"].status == refresh.IP_THROTTLED and results["d.com"].status == NOT_REACHED
    assert not any(refresh.cursor_row_for(r, True, T0)["blocked_until"] for r in results.values())


def test_a_store_that_blocked_us_earlier_is_forgiven_when_the_breaker_trips_later(monkeypatch):
    """a.com blocks us outright (8 x 403) -- a back-off, at first. Later stores throttle (429s) and the
    breaker trips within the window: a.com's back-off is forgiven too."""

    def handler(request):
        host = request.url.host
        return httpx.Response(403 if host == "a.com" else (404 if host == "b.com" else 429))

    domains = ["a.com", "b.com", "c.com", "d.com", "e.com"]
    seeds = {d: _mirror_seeds(d, 10) for d in domains}
    # 403 is not a throttle signal: a.com alone does not count toward the trip.
    results, _client = _real_backfill_pass(monkeypatch, seeds, handler, domains, page_size=25, breaker=True)
    assert results["a.com"].status == ABORTED and results["a.com"].ip_block
    assert results["b.com"].status == DONE
    assert results["e.com"].status == refresh.IP_THROTTLED
    assert refresh.cursor_row_for(results["a.com"], True, T0)["blocked_until"] is None




# ── the mirror lane goes through crawl_politeness and the shared Shopify-edge pacer ─────────────


def test_a_retry_after_holds_the_mirror_lanes_next_request_and_it_is_not_sent(monkeypatch):
    """The backfill's own fetch ignores Retry-After; the mirror client does not. A 429 with
    `Retry-After: 120` arms the host's backoff in crawl_politeness, so the next four requests are not
    sent (their slot is further out than the lane waits); the backfill sees them as neutral non-answers
    and writes nothing for those rows."""
    requested: List[str] = []

    def handler(request):
        requested.append(request.url.host)
        return httpx.Response(429, headers={"retry-after": "120"})

    seeds = {"a.com": _mirror_seeds("a.com", 5)}
    results, client = _real_backfill_pass(monkeypatch, seeds, handler, ["a.com"], page_size=10, max_wait=0.5)
    assert requested == ["a.com"]
    assert client.not_sent == 4 and client.store_not_sent == 4
    assert results["a.com"].writer["fetch_outcomes"] == {"rate_limited": 1, "not_json": 4}
    # B2(a): one real 429 and four HELD requests is not "had a block, no clean answer": the store was
    # not refused, it was not asked. Held: no back-off, no completion, the cursor stays where it was.
    assert results["a.com"].status == refresh.HELD
    row = refresh.cursor_row_for(results["a.com"], True, T0)
    assert row["next_cursor"] is None and row["blocked_until"] is None and row["completed_at"] is None
    assert exit_code(results) == refresh.EXIT_BUDGET


def test_every_mirror_request_is_marked_shopify_and_takes_a_shared_edge_slot(monkeypatch):
    from services import shopify_edge_pacer

    monkeypatch.setenv("CRAWL_SHOPIFY_EDGE_PACER_ENABLED", "true")
    shopify_edge_pacer.reset_for_tests()
    leases: List[int] = []

    async def lease(bucket, *, slots, rate_per_s, horizon_s=None):
        leases.append(slots)
        return 0.0, 0.0

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(shopify_edge_pacer, "_lease_fn", lease)
    monkeypatch.setattr(shopify_edge_pacer, "_sleep", no_sleep)
    seeds = {"a.com": _mirror_seeds("a.com", 3)}
    results, _client = _real_backfill_pass(monkeypatch, seeds, lambda r: httpx.Response(404), ["a.com"],
                                           page_size=10)
    assert results["a.com"].status == DONE
    assert shopify_edge_pacer.is_shopify_host("a.com")
    assert shopify_edge_pacer.stats()["granted"] == 3 and leases


def test_the_mirror_client_reports_every_answer_with_its_headers_to_crawl_politeness(monkeypatch):
    from services import crawl_politeness
    from scripts import backfill_shopify_variant_ids as backfill

    seen: List[tuple] = []
    real = crawl_politeness.note_response

    def spy(url, status, *, retry_after=None, headers=None):
        seen.append((status, retry_after, None if headers is None else headers.get("x-shopid")))
        return real(url, status, retry_after=retry_after, headers=headers)

    monkeypatch.setattr(crawl_politeness, "note_response", spy)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda r: httpx.Response(429, headers={"retry-after": "1", "x-shopid": "42"}))) as raw:
            client = refresh.BlockStreakClient(raw, backfill)
            await client.get("https://a.com/products/x.js")

    asyncio.run(go())
    assert seen == [(429, "1", "42")]


# ── --only: the first dry run is ONE small store ────────────────────────────────────────────────


def test_only_restricts_the_lane_to_named_stores_in_the_lanes_own_order():
    plan = refresh.LanePlan(lane="enrichment", domains=["a.com", "b.com", "c.com"], writer=None, gap_s=0.0,
                            plans={"a.com": 1, "b.com": 2, "c.com": 3})
    out = refresh.restrict(plan, ["c.com", "www.A.com"])
    assert out.domains == ["a.com", "c.com"] and out.plans == {"a.com": 1, "c.com": 3}
    assert refresh.restrict(refresh.LanePlan(lane="mirror", domains=["a.com"], writer=None, gap_s=0.0),
                            None).domains == ["a.com"]


def test_only_refuses_a_store_off_the_lanes_list(monkeypatch, capsys):
    monkeypatch.setattr(refresh, "plan_lane",
                        lambda lane, now: refresh.LanePlan(lane=lane, domains=["a.com"], writer=None, gap_s=0.0))

    async def must_not_run(*a, **k):
        raise AssertionError("ran with an off-list --only")

    monkeypatch.setattr(refresh, "run_lane", must_not_run)
    assert refresh.main(["mirror", "--on-crawl-egress", "--budget-seconds", "60", "--only", "evil.com"],
                        environ={}) == refresh.EXIT_BAD_ARGS
    assert "evil.com" in capsys.readouterr().err
    assert refresh.main(["mirror", "--on-crawl-egress", "--budget-seconds", "60", "--only", "not a host"],
                        environ={}) == refresh.EXIT_BAD_ARGS


def test_only_reaches_the_lane(monkeypatch):
    seen: Dict[str, Any] = {}
    monkeypatch.setattr(refresh, "plan_lane", lambda lane, now: refresh.LanePlan(
        lane=lane, domains=["a.com", "b.com"], writer=None, gap_s=0.0))

    async def run_lane(plan, *, apply, budget_s, emit, state):
        seen["domains"] = list(plan.domains)
        return state.info

    monkeypatch.setattr(refresh, "run_lane", run_lane)
    refresh.main(["mirror", "--on-crawl-egress", "--budget-seconds", "60", "--only", "b.com"], environ={},
                 emit=lambda line: None)
    assert seen["domains"] == ["b.com"]


def test_the_lane_breaker_is_calibrated_for_one_store_at_a_time():
    """#2473's defaults (10 distinct hosts in 60 s) suit a crawl that reaches hundreds of hosts at ~4
    req/s. These lanes walk ONE store at a time at ~1 request / 3 s, so a store adds at most ~one
    distinct host a minute: the lane needs a few hosts over a long window, or it can never trip."""
    from scripts import backfill_shopify_variant_ids as backfill
    from services import crawl_ip_throttle

    assert refresh.LANE_IP_TRIP_HOSTS <= 3 < crawl_ip_throttle.TRIP_HOSTS_DEFAULT
    # Enough time for LANE_IP_TRIP_HOSTS stores to each reach their own abort threshold, twice over.
    per_store = backfill.CONSECUTIVE_BLOCK_ABORT * backfill.PER_DOMAIN_MIN_GAP_S
    assert refresh.LANE_IP_TRIP_WINDOW_S >= 2 * refresh.LANE_IP_TRIP_HOSTS * per_store
    assert refresh.LANE_IP_TRIP_WINDOW_S >= 600


def test_after_a_trip_no_later_store_is_asked_even_if_the_page_that_tripped_it_crashed():
    """The breaker trips inside a.com's page and the page then crashes: the page never reports
    `abort_pass`, so only the run's stop signal keeps b.com from being asked at all."""
    from services import crawl_ip_throttle

    class Writer(FakeEnrichmentWriter):
        async def run_domain(self, db, client, plan, **kwargs):
            self.calls.append((db, client, plan, kwargs))
            if plan == "PLAN-A":
                for host in ("x.com", "y.com", "z.com"):
                    crawl_ip_throttle.observe_response(host, 429, {})
                raise RuntimeError("the page broke after the trip")
            return {"exhausted": True, "next_cursor": None, "aborted_on_block": False}

    writer = Writer()
    plan = refresh.LanePlan(lane="enrichment", domains=["a.com", "b.com"], writer=writer, gap_s=0.0,
                            plans={"a.com": "PLAN-A", "b.com": "PLAN-B"})
    state = _run_real_lane(plan, apply=False, db=StubDb())
    assert [c[2] for c in writer.calls] == ["PLAN-A"]
    assert state.results["a.com"].status == CRASHED
    assert state.results["b.com"].status == refresh.IP_THROTTLED and state.results["b.com"].pass_abort
    assert state.info["ip_throttle"]["ip_throttled"] is True


# ── B1: the breaker is keyed by STORE and needs a store we had not already blamed ───────────────


def test_b1_one_stores_apex_and_www_plus_one_stray_429_is_not_three_stores():
    """The reviewer's split-host case: a.com answers 429 from its apex AND its www twin, and b.com
    throws one stray 429. Keyed by hostname that is three -- a trip, and the genuine blocker's back-off
    forgiven. Keyed by store it is two: no trip."""
    from services.crawl_ip_throttle import IpThrottleBreaker

    by_host = IpThrottleBreaker(trip_hosts=refresh.LANE_IP_TRIP_HOSTS, window_seconds=refresh.LANE_IP_TRIP_WINDOW_S)
    by_store = refresh.store_breaker(["a.com", "b.com", "c.com"], is_aborted=lambda s: False)
    for breaker in (by_host, by_store):
        for host in ("a.com", "www.a.com", "shop.a.com", "b.com"):
            breaker.observe(host, 429, {})
    assert by_host.tripped, "precondition: the hostname-keyed breaker would have tripped"
    assert not by_store.tripped
    by_store.observe("www.c.com", 429, {})
    assert by_store.tripped and by_store.trip_host_count == 3
    assert by_store.store_of("WWW.A.COM") == "a.com" and by_store.store_of("www.other.com") == "other.com"


def test_b1_three_stores_we_already_blamed_do_not_trip_it_a_fourth_answering_store_does():
    aborted = {"a.com", "b.com", "c.com"}
    breaker = refresh.store_breaker(["a.com", "b.com", "c.com", "d.com"], is_aborted=lambda s: s in aborted)
    for host in ("a.com", "b.com", "c.com"):
        breaker.observe(host, 429, {})
    assert not breaker.tripped, "three store-level blocks, each already backed off on its own evidence"
    breaker.observe("d.com", 429, {})
    assert breaker.tripped


def test_b1_a_non_throttle_answer_never_trips_it_and_its_diagnostics_are_2473s():
    breaker = refresh.store_breaker(["a.com", "b.com", "c.com"], is_aborted=lambda s: False)
    for host in ("a.com", "b.com", "c.com"):
        breaker.observe(host, 403, {})
        breaker.observe(host, 503, {})  # a bare 503 is an outage, not a throttle
    assert not breaker.tripped
    summary = breaker.summary()
    assert summary["ip_throttled"] is False and summary["throttle_diagnostics"]["responses"] == 3
    for host in ("a.com", "b.com", "c.com"):
        breaker.observe(host, 503, {"retry-after": "30"})
    assert breaker.tripped and breaker.summary()["ip_throttle_trip_host_count"] == 3


def test_b1_the_09_30_pattern_trips_on_the_third_store_through_run_lane(monkeypatch):
    """Through the REAL run_lane (the breaker it builds and installs): stores answering 429/404
    alternately trip it at the third store, and no store is backed off."""
    from scripts import backfill_shopify_variant_ids as backfill

    domains = ["a.com", "b.com", "c.com", "d.com"]
    seeds = {d: _mirror_seeds(d, 6) for d in domains}

    async def fake_select(limit, domain, after=None, seed_ids=None):
        rows = [r for r in seeds[domain] if after is None or r["id"] > after]
        return [dict(r) for r in rows[:limit]]

    def handler(request):
        n = int(request.url.path.split("/h")[-1].split(".")[0])
        return httpx.Response(429 if n % 2 == 0 else 404)

    monkeypatch.setattr(backfill, "select_candidates", fake_select)
    monkeypatch.setattr(backfill, "GLOBAL_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(backfill, "PER_DOMAIN_MIN_GAP_S", 0.0)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(refresh.httpx, "AsyncClient",
                        lambda *a, **k: real_client(transport=httpx.MockTransport(handler)))
    db = MemoryDb()
    plan = refresh.LanePlan(lane="mirror", domains=domains, writer=backfill, gap_s=0.0,
                            proof_max_age=timedelta(days=7))
    state = refresh.RunState()
    asyncio.run(refresh.run_lane(plan, apply=True, budget_s=600, emit=lambda line: None, state=state, db=db,
                                 now=lambda: T0))
    assert [state.results[d].status for d in domains] == [DONE, DONE, refresh.IP_THROTTLED, NOT_REACHED]
    assert state.info["ip_throttle"]["ip_throttle_trip_host_count"] == 3
    assert all(r["blocked_until"] is None for r in db.rows.values())


# ── forgiveness: only aborts inside the breaker window ──────────────────────────────────────────


def test_only_a_back_off_recorded_inside_the_window_before_the_trip_is_forgiven():
    clock = Clock()
    run_page, _ = pages_fn({
        "a.com": [Page({}, None, aborted=True)],
        "b.com": [Page({}, None)],
        "c.com": [Page({}, None, aborted=True)],
        "d.com": [Page({}, None, abort_pass=True)],
    }, clock=clock, cost=400.0)
    results = asyncio.run(drive(["a.com", "b.com", "c.com", "d.com"], run_page, merge_list, budget_s=10_000,
                                gap_s=0.0, clock=clock, sleep=clock.sleep, forgive_window_s=900.0))
    # a.com aborted at t=400, the trip at t=1600: 1200 s before it, outside the 900 s window.
    assert results["a.com"].status == ABORTED and not results["a.com"].ip_block
    assert results["c.com"].ip_block, "aborted at t=1200, 400 s before the trip"
    assert refresh.cursor_row_for(results["a.com"], True, T0)["blocked_until"] == T0 + refresh.MIRROR_BLOCK_BACKOFF
    assert refresh.cursor_row_for(results["c.com"], True, T0)["blocked_until"] is None


def test_run_lane_forgives_within_the_lanes_breaker_window():
    captured: Dict[str, Any] = {}
    real = refresh.drive

    async def spy(*args, **kwargs):
        captured.update(kwargs)
        return await real(*args, **kwargs)

    writer = FakeMirrorWriter()
    plan = refresh.LanePlan(lane="mirror", domains=["a.com"], writer=writer, gap_s=0.0,
                            proof_max_age=timedelta(days=7))
    import unittest.mock as mock

    with mock.patch.object(refresh, "drive", spy):
        _run_real_lane(plan, apply=False, db=StubDb())
    assert captured["forgive_window_s"] == refresh.LANE_IP_TRIP_WINDOW_S


# ── B2: held requests are counted, never read as a store's verdict, and hold the cursor ─────────


def test_b2_a_store_whose_every_request_was_held_is_not_done(monkeypatch):
    """The host is under a Retry-After hold before the store starts: every request is held. The old
    code read that as `done` (exit 0, cursor reset, nothing read)."""
    from services import crawl_politeness

    crawl_politeness.note_response("https://a.com/", 429, retry_after="120")
    requested: List[str] = []

    def handler(request):
        requested.append(request.url.host)
        return httpx.Response(404)

    seeds = {"a.com": _mirror_seeds("a.com", 4)}
    results, client = _real_backfill_pass(monkeypatch, seeds, handler, ["a.com"], page_size=10, max_wait=0.5)
    assert requested == [] and client.not_sent == 4
    assert results["a.com"].status == refresh.HELD
    row = refresh.cursor_row_for(results["a.com"], True, T0)
    assert row["completed_at"] is None and row["blocked_until"] is None
    assert exit_code(results) == refresh.EXIT_BUDGET


def test_b2_a_held_page_does_not_advance_the_cursor_past_it(monkeypatch):
    """Page 1 reads fine and advances the cursor; page 2 meets a Retry-After hold: the store stops with
    its cursor at the END OF PAGE 1, so the held rows are walked next run."""
    served = {"n": 0}

    def handler(request):
        served["n"] += 1
        if served["n"] == 4:
            return httpx.Response(429, headers={"retry-after": "120"})
        return httpx.Response(404)

    seeds = {"a.com": _mirror_seeds("a.com", 9)}
    results, client = _real_backfill_pass(monkeypatch, seeds, handler, ["a.com"], page_size=3, max_wait=0.5)
    assert results["a.com"].status == refresh.HELD and results["a.com"].pages == 2
    assert results["a.com"].last_cursor == "epsv_a002", "page 1's cursor, not page 2's"
    assert refresh.cursor_row_for(results["a.com"], True, T0)["next_cursor"] == "epsv_a002"
    assert client.not_sent == 2


def test_b2_enrichment_held_requests_stop_the_store_without_a_verdict():
    job = FakeEnrichmentJob([{"exhausted": True, "next_cursor": None, "aborted_on_block": False,
                              "fetches": {"rate_limited": 1, "crawl_paced": 3}}])
    page = asyncio.run(refresh.enrichment_page_fn(job, None, None, {"a.com": "P"}, apply=False,
                                                  pacer=None)("a.com", None))
    assert page.held == 3 and not page.aborted and page.next_cursor is None
    robots = FakeEnrichmentJob([{"exhausted": True, "next_cursor": None, "aborted_on_block": False,
                                 "fetches": {"robots_disallowed": 3}}])
    page = asyncio.run(refresh.enrichment_page_fn(robots, None, None, {"a.com": "P"}, apply=False,
                                                  pacer=None)("a.com", None))
    assert page.held == 0, "a robots refusal is permanent, not a hold"


def test_b2_enrichment_never_read_ignores_held_requests():
    class Held(BlockingEnrichmentJob):
        async def run_domain(self, db, client, plan, *, block_state, block_limit, **kwargs):
            block_state["consecutive"] += 1  # one real block, then the rest held
            return {"aborted_on_block": False, "exhausted": True, "next_cursor": None,
                    "fetches": {"rate_limited": 1, "crawl_paced": 4}}

    page_fn = refresh.enrichment_page_fn(Held({"a.com": []}), None, None, {"a.com": "a.com"}, apply=False,
                                         pacer=None)
    results = asyncio.run(drive(["a.com"], page_fn, refresh.merge_enrichment, budget_s=60, gap_s=0.0))
    assert results["a.com"].status == refresh.HELD


# ── the pacer's "both slots or neither", and failing open ───────────────────────────────────────


def test_a_shared_slot_further_out_than_the_lane_waits_holds_the_request_and_reserves_nothing(monkeypatch):
    from services import crawl_politeness, shopify_edge_pacer

    monkeypatch.setenv("CRAWL_SHOPIFY_EDGE_PACER_ENABLED", "true")
    shopify_edge_pacer.reset_for_tests()

    async def far_lease(bucket, *, slots, rate_per_s, horizon_s=None):
        return 30.0, 0.0  # the shared schedule's next slot is 30 s out, inside the horizon

    monkeypatch.setattr(shopify_edge_pacer, "_lease_fn", far_lease)
    requested: List[str] = []
    seeds = {"a.com": _mirror_seeds("a.com", 3)}
    results, client = _real_backfill_pass(monkeypatch, seeds, lambda r: requested.append(r.url.host) or
                                          httpx.Response(404), ["a.com"], page_size=10, max_wait=0.5)
    assert requested == [] and client.not_sent == 3 and results["a.com"].status == refresh.HELD
    state = crawl_politeness._STATE.get("a.com")
    assert state is None or state.next_allowed <= time.monotonic(), "a refusal reserved the host slot"
    assert shopify_edge_pacer.stats()["refused"] >= 3


def test_when_the_shared_budget_is_unreachable_the_lane_is_paced_locally_and_still_reads(monkeypatch):
    from services import shopify_edge_pacer

    monkeypatch.setenv("CRAWL_SHOPIFY_EDGE_PACER_ENABLED", "true")
    monkeypatch.setenv("CRAWL_SHOPIFY_EDGE_RPS", "20")
    monkeypatch.setenv("CRAWL_SHOPIFY_EDGE_FALLBACK_RPS", "20")
    shopify_edge_pacer.reset_for_tests()

    async def down(bucket, *, slots, rate_per_s, horizon_s=None):
        raise ConnectionError("db down")

    monkeypatch.setattr(shopify_edge_pacer, "_lease_fn", down)
    requested: List[str] = []
    seeds = {"a.com": _mirror_seeds("a.com", 3)}
    results, client = _real_backfill_pass(monkeypatch, seeds, lambda r: requested.append(r.url.host) or
                                          httpx.Response(404), ["a.com"], page_size=10)
    assert len(requested) == 3 and client.not_sent == 0 and results["a.com"].status == DONE
    stats = shopify_edge_pacer.stats()
    assert stats["db_errors"] >= 1 and stats["local_slots"] >= 3


# ── robots: the mirror lane now honours Disallow ────────────────────────────────────────────────


def test_a_robots_disallowed_mirror_request_is_not_sent_and_is_not_a_hold(monkeypatch):
    from services import crawl_politeness

    monkeypatch.setenv("CRAWL_ROBOTS_ENABLED", "true")
    token = crawl_politeness.ROBOTS_TRANSPORT_FACTORY.set(lambda: httpx.MockTransport(
        lambda request: httpx.Response(200, text="User-agent: *\nDisallow: /products/\n")))
    requested: List[str] = []
    try:
        seeds = {"a.com": _mirror_seeds("a.com", 3)}
        results, client = _real_backfill_pass(monkeypatch, seeds, lambda r: requested.append(r.url.host) or
                                              httpx.Response(404), ["a.com"], page_size=10)
    finally:
        crawl_politeness.ROBOTS_TRANSPORT_FACTORY.reset(token)
    assert requested == [] and client.robots_disallowed == 3 and client.not_sent == 0
    assert results["a.com"].status == DONE, "a permanent refusal: walked, nothing to wait for"


def test_the_pacer_learns_the_host_that_answered_after_a_redirect(monkeypatch):
    from services import shopify_edge_pacer

    monkeypatch.setenv("CRAWL_SHOPIFY_EDGE_PACER_ENABLED", "true")
    shopify_edge_pacer.reset_for_tests()

    async def lease(bucket, *, slots, rate_per_s, horizon_s=None):
        return 0.0, 0.0

    monkeypatch.setattr(shopify_edge_pacer, "_lease_fn", lease)

    def handler(request):
        if request.url.host == "a.com":
            return httpx.Response(301, headers={"location": "https://store.shopcdn.example/products/h0.js"})
        return httpx.Response(404, headers={"x-shopid": "7"})

    seeds = {"a.com": _mirror_seeds("a.com", 1)}
    _real_backfill_pass(monkeypatch, seeds, handler, ["a.com"], page_size=10)
    assert shopify_edge_pacer.is_shopify_host("store.shopcdn.example")
