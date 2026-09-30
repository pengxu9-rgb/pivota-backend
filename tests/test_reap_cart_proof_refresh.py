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
    assert refresh._seed_domain_is("www.e.com", "e.com") and refresh._seed_domain_is("E.com.", "e.com")
    assert refresh._seed_domain_is("shop.e.com", "e.com")
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


def test_a_block_abort_stops_the_whole_pass():
    clock = Clock()
    run_page, calls = pages_fn({
        "a.com": [Page({}, None)],
        "b.com": [Page({"aborted_on_block": True}, None, aborted=True)],
        "c.com": [Page({}, None)],
    })
    results = _drive(["a.com", "b.com", "c.com"], run_page, clock)
    assert [c[0] for c in calls] == ["a.com", "b.com"]
    assert results["b.com"].status == ABORTED and results["c.com"].status == NOT_REACHED
    assert exit_code(results) == refresh.EXIT_ABORTED_ON_BLOCK


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
    run_page, _ = pages_fn({"a.com": [Page({}, None)], "b.com": [Page({}, None, aborted=True)],
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
    ([DONE, NOT_REACHED], 4),
    ([BUDGET_STOPPED, NOT_REACHED], 4),
    ([CRASHED, NOT_REACHED], 3),
    ([CURSOR_STUCK, DONE], 3),
    ([CRASHED, ABORTED, NOT_REACHED], 1),
    ([ABORTED], 1),
])
def test_exit_code_precedence(statuses, code):
    results = {f"d{i}.com": DomainResult(status=s) for i, s in enumerate(statuses)}
    assert exit_code(results) == code


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
def test_the_enrichment_page_calls_the_writer_for_one_plan_with_the_shared_pacer_and_streak(apply):
    domain_report = {"exhausted": False, "next_cursor": "ext:k9", "aborted_on_block": False, "written": 0}
    job = FakeEnrichmentJob([domain_report])
    plans = {"tartecosmetics.com": "PLAN-T", "maccosmetics.com": "PLAN-M"}
    pacer, db, client, streak = object(), object(), object(), {"consecutive": 2}
    page = asyncio.run(refresh.enrichment_page_fn(job, db, client, plans, apply=apply, pacer=pacer,
                                                  block_state=streak)("tartecosmetics.com", "ext:k1"))
    assert job.calls == [(db, client, "PLAN-T", {
        "apply": apply, "source_mode": "auto", "limit": refresh.ENRICHMENT_PAGE_PRODUCTS, "after": "ext:k1",
        "pacer": pacer, "block_limit": 5, "block_state": streak})]
    assert page.next_cursor == "ext:k9" and page.report == domain_report and not page.aborted


def test_the_enrichment_page_is_small_enough_to_commit_and_stop_between_pages():
    import jobs.enrichment_cart_variant_proof as writer

    assert refresh.ENRICHMENT_PAGE_PRODUCTS <= 250 < writer.DEFAULT_LIMIT


def test_an_exhausted_or_aborted_enrichment_domain_has_no_next_page():
    exhausted = FakeEnrichmentJob([{"exhausted": True, "next_cursor": "x"}])
    page = asyncio.run(refresh.enrichment_page_fn(exhausted, None, None, {"a.com": "P"}, apply=False,
                                                  pacer=None, block_state={"consecutive": 0})("a.com", None))
    assert page.next_cursor is None and not page.aborted
    aborted = FakeEnrichmentJob([{"exhausted": False, "next_cursor": "x", "aborted_on_block": True}])
    page = asyncio.run(refresh.enrichment_page_fn(aborted, None, None, {"a.com": "P"}, apply=False,
                                                  pacer=None, block_state={"consecutive": 0})("a.com", None))
    assert page.aborted and page.next_cursor is None


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
                           refresh.enrichment_page_fn(writer, EmptyDb(), None, plans, apply=False, pacer=pacer,
                                                      block_state={"consecutive": 0}),
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


@pytest.mark.parametrize("status, final, stored", [
    (DONE, True, {"next_cursor": None, "last_status": DONE, "completed_at": T0}),
    (CURSOR_STUCK, True, {"next_cursor": None, "last_status": CURSOR_STUCK, "completed_at": None}),
    (BUDGET_STOPPED, True, {"next_cursor": "c7", "last_status": BUDGET_STOPPED, "completed_at": None}),
    (ABORTED, True, {"next_cursor": "c7", "last_status": ABORTED, "completed_at": None}),
    (CRASHED, True, {"next_cursor": "c7", "last_status": CRASHED, "completed_at": None}),
    (NOT_REACHED, False, {"next_cursor": "c7", "last_status": "in_progress", "completed_at": None}),
])
def test_what_a_checkpoint_stores(status, final, stored):
    result = DomainResult(status=status, start_cursor="c3", last_cursor="c7")
    assert refresh.cursor_row_for(result, final, T0) == stored


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


def _real_backfill_pass(monkeypatch, seeds_by_domain, handler, domains, page_size=3):
    from scripts import backfill_shopify_variant_ids as backfill

    async def fake_select(limit, domain, after=None, seed_ids=None):
        rows = [s for s in seeds_by_domain[domain] if after is None or s["id"] > after]
        return [dict(r) for r in rows[:limit]]

    monkeypatch.setattr(backfill, "select_candidates", fake_select)
    monkeypatch.setattr(backfill, "GLOBAL_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(backfill, "PER_DOMAIN_MIN_GAP_S", 0.0)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as raw:
            client = refresh.BlockStreakClient(raw, backfill)
            results = await drive(domains, refresh.mirror_page_fn(backfill, client, apply=False, page_size=page_size),
                                  refresh.merge_mirror, budget_s=60, gap_s=0.0)
            return results, client

    return asyncio.run(go())


def test_eight_consecutive_blocks_across_two_small_domains_abort_the_pass(monkeypatch):
    """Each store has 5 seeds, under the backfill's threshold of 8 on its own. Without the carried
    streak neither call aborts and the pass walks a third store into the same block."""
    from scripts import backfill_shopify_variant_ids as backfill

    assert backfill.CONSECUTIVE_BLOCK_ABORT == 8
    requested: List[str] = []

    def handler(request):
        requested.append(request.url.host)
        return httpx.Response(429)

    seeds = {d: _mirror_seeds(d, 5) for d in ("a.com", "b.com", "c.com")}
    results, client = _real_backfill_pass(monkeypatch, seeds, handler, ["a.com", "b.com", "c.com"], page_size=10)
    assert results["a.com"].status == DONE
    assert results["b.com"].status == ABORTED and results["c.com"].status == NOT_REACHED
    assert len(requested) == 8, "no request leaves once the carried streak reaches the threshold"
    assert client.short_circuited == 2, "b.com's last two seeds were answered locally"
    assert exit_code(results) == refresh.EXIT_ABORTED_ON_BLOCK


def test_a_clean_answer_resets_the_carried_streak_and_not_json_neither_adds_nor_resets(monkeypatch):
    answers = iter([httpx.Response(429)] * 5 + [httpx.Response(404)] + [httpx.Response(429)] * 4
                   + [httpx.Response(200, text="<html>", headers={"content-type": "text/html"})]
                   + [httpx.Response(429)] * 3)

    def handler(request):
        return next(answers)

    seeds = {"a.com": _mirror_seeds("a.com", 6), "b.com": _mirror_seeds("b.com", 8)}
    results, client = _real_backfill_pass(monkeypatch, seeds, handler, ["a.com", "b.com"])
    # 5 blocks, a 404 resets; then 4 + (not_json) + 3 = 7 blocks: under 8, so no abort.
    assert results["a.com"].status == DONE and results["b.com"].status == DONE
    assert client.state["consecutive"] == 7 and client.short_circuited == 0


def test_the_streak_client_passes_a_transport_error_through_after_counting_it():
    from scripts import backfill_shopify_variant_ids as backfill

    class Raising:
        async def get(self, url, **kwargs):
            raise httpx.ConnectError("reset")

    client = refresh.BlockStreakClient(Raising(), backfill)
    with pytest.raises(httpx.ConnectError):
        asyncio.run(client.get("https://a.com/products/x.js"))
    assert client.state["consecutive"] == 1


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
    assert len({id(c[3]["block_state"]) for c in writer.calls}) == 1, "one block streak for the pass"
    assert all(c[3]["limit"] == refresh.ENRICHMENT_PAGE_PRODUCTS for c in writer.calls)
    assert ensured == ([True] if apply else [])
    assert {r.status for r in state.results.values()} == {DONE}
    assert state.info["requests"] == 0
