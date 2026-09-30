"""jobs/reap_cart_proof_refresh.py: domain selection, paging, pacing, the budget, the report.

No network and no database: the writers' `run()` is replaced by a recorder for the driver and the
page adapters, and the one end-to-end mirror test runs the REAL backfill `run()` over a faked
selection and a transport that answers 404 (nothing to write, even in a hypothetical apply).
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
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
    DomainResult,
    Page,
    drive,
    exit_code,
)

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


def test_the_mirror_order_rotates_by_utc_day_and_covers_every_domain():
    domains = ["a.com", "b.com", "c.com"]
    assert refresh.rotate(domains, 0) == ["a.com", "b.com", "c.com"]
    assert refresh.rotate(domains, 1) == ["b.com", "c.com", "a.com"]
    assert refresh.rotate(domains, 5) == ["c.com", "a.com", "b.com"]
    assert refresh.rotate([], 3) == []
    day = datetime(2026, 9, 30, 23, 59, tzinfo=timezone.utc)
    assert refresh.utc_day_index(day) + 1 == refresh.utc_day_index(datetime(2026, 10, 1, 0, 1, tzinfo=timezone.utc))
    firsts = {refresh.rotate(domains, refresh.utc_day_index(day) + i)[0] for i in range(3)}
    assert firsts == set(domains), "over len(domains) days every domain goes first once"


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
    assert mirror.domains == refresh.rotate(refresh.mirror_domains(), refresh.utc_day_index(now))
    enrichment = refresh.plan_lane("enrichment", now)
    assert enrichment.domains == list(refresh.ENRICHMENT_DOMAINS)
    assert set(enrichment.plans) == set(refresh.ENRICHMENT_DOMAINS)


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
    def __init__(self, summaries: List[Dict[str, Any]]) -> None:
        self.summaries = summaries
        self.calls: List[tuple] = []

    async def run(self, db: Any, client: Any, plans: List[Any], **kwargs: Any) -> Dict[str, Any]:
        self.calls.append((db, client, plans, kwargs))
        return self.summaries.pop(0)


@pytest.mark.parametrize("apply", [False, True])
def test_the_enrichment_page_calls_the_writer_for_one_plan_with_the_shared_pacer(apply):
    domain_report = {"exhausted": False, "next_cursor": "ext:k9", "aborted_on_block": False, "written": 0}
    job = FakeEnrichmentJob([{"domains": {"tartecosmetics.com": domain_report}, "aborted_on_block": False}])
    plans = {"tartecosmetics.com": "PLAN-T", "maccosmetics.com": "PLAN-M"}
    pacer, db, client = object(), object(), object()
    page = asyncio.run(refresh.enrichment_page_fn(job, db, client, plans, apply=apply, pacer=pacer)(
        "tartecosmetics.com", "ext:k1"))
    assert job.calls == [(db, client, ["PLAN-T"], {"apply": apply, "after": "ext:k1", "pacer": pacer})]
    assert page.next_cursor == "ext:k9" and page.report == domain_report and not page.aborted


def test_an_exhausted_or_aborted_enrichment_domain_has_no_next_page():
    exhausted = FakeEnrichmentJob([{"domains": {"a.com": {"exhausted": True, "next_cursor": "x"}},
                                    "aborted_on_block": False}])
    page = asyncio.run(refresh.enrichment_page_fn(exhausted, None, None, {"a.com": "P"}, apply=False,
                                                  pacer=None)("a.com", None))
    assert page.next_cursor is None and not page.aborted
    aborted = FakeEnrichmentJob([{"domains": {"a.com": {"exhausted": False, "next_cursor": "x"}},
                                  "aborted_on_block": True}])
    page = asyncio.run(refresh.enrichment_page_fn(aborted, None, None, {"a.com": "P"}, apply=False,
                                                  pacer=None)("a.com", None))
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

    async def run_lane(plan, *, apply, budget_s, emit):
        seen.update(apply=apply, budget_s=budget_s)
        return {"domains_order": plan.domains, "inter_call_gap_s": plan.gap_s,
                "results": {d: DomainResult(status=s) for d, s in statuses.items()}}

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
    assert report["status_counts"] == {"done": 2}
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
