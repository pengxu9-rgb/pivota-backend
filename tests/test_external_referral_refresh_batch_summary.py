"""The BATCH seam, and the job's exit code.

The per-row tests in `test_external_referral_offer_projection.py` stop at the row boundary.
Nothing drove `run_external_referral_refresh_batch` or `main()` for anything this change adds, so
the whole aggregation layer was unpinned: `proj = None`, `top_degraded_hosts` hardcoded to
"unknown", `unprocessable` hardcoded to 0, the denominator reverted to all candidates, and
`main()` returning 0 unconditionally ALL survived a full run of the row-level suite.

These tests inject `refresh_seed_by_id`, which the batch already accepts, so they exercise the
real aggregation without a database.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from fastapi import HTTPException

import services.external_referral_readiness as err
import jobs.external_referral_refresh as job


def _run(monkeypatch, rows: List[Dict[str, Any]], *, limit: int = 10) -> Dict[str, Any]:
    """Drive the real batch over a scripted set of per-seed results."""
    seed_ids = [f"eps_{i}" for i in range(len(rows))]
    monkeypatch.setattr(
        err, "get_external_referral_refresh_candidate_seed_ids",
        lambda *a, **k: asyncio.sleep(0, result=seed_ids),
    )
    # No hosts -> no breaker, and no database: these tests pin aggregation, not the breaker.
    monkeypatch.setattr(err, "_fetch_refresh_candidate_hosts", lambda ids: asyncio.sleep(0, result={}))
    scripted = dict(zip(seed_ids, rows))

    async def fake_refresh(seed_id, **kwargs):
        row = scripted[seed_id]
        if isinstance(row, BaseException):
            raise row
        return row

    return asyncio.run(
        err.run_external_referral_refresh_batch(refresh_seed_by_id=fake_refresh, limit=limit)
    )


def _ok(price="applied", *, projected=1, attempted=1):
    return {
        "status": "success",
        "price_refresh": {"status": price},
        "projection": {
            "attempted": attempted, "projected": projected,
            "skipped": 0, "errored": 0, "seconds": 0.5,
        },
    }


def test_projection_counters_reach_the_summary(monkeypatch):
    """`proj = None` at the aggregation seam restores the exact defect the PR set out to fix:
    a run that healed nothing still reporting success."""
    summary = _run(monkeypatch, [_ok(), _ok(), _ok(projected=0)])
    assert summary["projections_attempted"] == 3
    assert summary["projections_written"] == 2
    assert summary["projection_seconds"] > 0


def test_a_run_that_projects_nothing_while_prices_move_is_degraded(monkeypatch):
    summary = _run(monkeypatch, [_ok(projected=0), _ok(projected=0)])
    assert summary["projections_written"] == 0
    assert summary["status"] == "degraded"


def test_degraded_hosts_are_named_not_unknown(monkeypatch):
    """Hardcoding "unknown" survived every row-level test — the histogram is built here."""
    summary = _run(monkeypatch, [
        {"status": "degraded", "error": "http 503", "domain": "themedicube.us.com"},
        {"status": "degraded", "error": "http 503", "domain": "themedicube.us.com"},
        {"status": "degraded", "error": "Read timed out", "domain": "cocomo.sg"},
    ])
    assert summary["top_degraded_hosts"] == {"themedicube.us.com": 2, "cocomo.sg": 1}
    assert summary["degraded_reason_counts"] == {"http_503": 2, "timeout": 1}


def test_an_unprocessable_seed_is_neither_a_failure_nor_a_denominator_row(monkeypatch):
    """SEED_NOT_FOUND / INVALID_URL are permanent per-seed data conditions (~628 rows). They must
    not redden the night, and must not be charged against the origin-read yield."""
    summary = _run(monkeypatch, [
        _ok(), _ok(),
        HTTPException(status_code=404, detail="SEED_NOT_FOUND"),
        HTTPException(status_code=400, detail="INVALID_URL"),
    ])
    assert summary["unprocessable"] == 2
    assert summary["failed"] == 0
    assert summary["unprocessable_reasons"] == {"SEED_NOT_FOUND": 1, "INVALID_URL": 1}
    assert summary["attempted_count"] == 2, "unprocessable rows leave the denominator"
    assert summary["status"] == "success"


def test_an_unexpected_http_error_is_still_a_failure(monkeypatch):
    """A broad `except HTTPException` would silently reclassify a 5xx from a helper as a
    permanent data condition — invisible in errors[] and unable to move the exit code."""
    summary = _run(monkeypatch, [_ok(), HTTPException(status_code=502, detail="UPSTREAM_DOWN")])
    assert summary["unprocessable"] == 0
    assert summary["failed"] == 1
    assert summary["status"] == "degraded"


def test_the_yield_denominator_is_attempted_rows(monkeypatch):
    """Reverting it to all candidates charges the run for work it deliberately deferred."""
    summary = _run(monkeypatch, [_ok(), _ok()])
    assert summary["attempted_count"] == 2
    assert summary["origin_yield"] == 1.0


# ------------------------------------------------------------------ the job's exit code

def test_main_returns_one_on_a_degraded_summary(monkeypatch):
    """`if False:` at the exit-code branch survived, because the job test only ever fed a
    success summary. This is the only place the whole summary reaches an operator."""
    monkeypatch.setattr(
        job, "run_daily_external_referral_refresh",
        lambda **kwargs: asyncio.sleep(0, result={"status": "degraded", "origin_yield": 0.1}),
    )
    monkeypatch.setattr(job.database, "connect", lambda: asyncio.sleep(0))
    monkeypatch.setattr(job.database, "disconnect", lambda: asyncio.sleep(0))
    monkeypatch.setattr("sys.argv", ["external_referral_refresh", "--limit", "1"])
    assert job.main() == 1


def test_main_returns_zero_on_a_healthy_summary(monkeypatch):
    monkeypatch.setattr(
        job, "run_daily_external_referral_refresh",
        lambda **kwargs: asyncio.sleep(0, result={"status": "success"}),
    )
    monkeypatch.setattr(job.database, "connect", lambda: asyncio.sleep(0))
    monkeypatch.setattr(job.database, "disconnect", lambda: asyncio.sleep(0))
    monkeypatch.setattr("sys.argv", ["external_referral_refresh", "--limit", "1"])
    assert job.main() == 0


def test_cache_served_rows_are_not_origin_reads(monkeypatch):
    """`origin_reads = refreshed - refreshed_from_cache` is the run's ONLY live alarm while the
    projection flag is off (production today): `failed` is structurally 0 for a degraded read
    and `stopped_early` is report-only. Every earlier test handed the batch a precomputed
    `origin_reads`; `origin_reads = refreshed` survived. Cache-served rows must yield zero."""
    rows = [dict(_ok(price="unchanged", projected=0, attempted=0), snapshot_from_cache=True)
            for _ in range(5)]
    summary = _run(monkeypatch, rows)
    assert summary["refreshed"] == 5
    assert summary["refreshed_from_cache"] == 5
    assert summary["origin_reads"] == 0
    assert summary["origin_yield"] == 0.0
    assert summary["status"] == "degraded"


def test_a_mix_of_origin_and_cache_reads_yields_the_origin_share(monkeypatch):
    rows = [_ok() for _ in range(3)] + [dict(_ok(), snapshot_from_cache=True) for _ in range(2)]
    summary = _run(monkeypatch, rows)
    assert summary["origin_reads"] == 3 and summary["attempted_count"] == 5
    assert summary["origin_yield"] == 0.6
    assert summary["status"] == "success"


def _errored(*, pdp_only=False, price="unchanged"):
    # `price="unchanged"` on purpose: with prices moving, the OLDER rule (prices moved and
    # nothing written) already degrades the run, and this helper exists to reach the rule
    # that fires when every projection RAISED while no price moved.
    row = _ok(price=price, projected=0 if not pdp_only else 1)
    row["projection"] = {
        "attempted": 1, "projected": 1 if pdp_only else 0, "skipped": 0, "errored": 1,
        "pdp_errored": 1, "seconds": 0.5,
    }
    return row


def test_projection_errors_reach_the_summary_and_an_all_errored_run_is_degraded(monkeypatch):
    """`errored` was written in the helper and read nowhere; a projection that raised on every
    row summarised as healthy. And the PDP half reports separately: the offer landing is not
    the PDP being rebuilt."""
    summary = _run(monkeypatch, [_errored(), _errored(), _errored()])
    assert summary["projections_errored"] == 3
    assert summary["pdp_errored"] == 3
    assert summary["status"] == "degraded"
    # One healthy projection among the errors is not an all-errored run.
    summary = _run(monkeypatch, [_errored(), _ok()])
    assert summary["projections_errored"] == 1 and summary["status"] == "success"


def test_pdp_outcomes_are_counted_separately_from_the_offer_write(monkeypatch):
    rows = []
    r = _ok(); r["projection"].update({"pdp_refreshed": 1}); rows.append(r)
    r = _ok(); r["projection"].update({"pdp_skipped": 1, "pdp_skip_skipped_no_attached_key": 1}); rows.append(r)
    r = _ok(); r["projection"].update({"pdp_skipped": 1, "pdp_skip_skipped_no_attached_key": 1}); rows.append(r)
    r = _ok(projected=0); r["projection"].update({"skip_no_mirror_product": 1, "skipped": 1,
                                                  "pdp_skipped": 1, "pdp_skip_skipped_no_content_key": 1}); rows.append(r)
    summary = _run(monkeypatch, rows)
    assert summary["projections_written"] == 3
    assert summary["pdp_refreshed"] == 1
    assert summary["pdp_skips"] == {"skipped_no_attached_key": 2, "skipped_no_content_key": 1}
    # The offer-side skip histogram is pinned at the seam too: `{}` used to survive.
    assert summary["projection_skips"] == {"no_mirror_product": 1}


# ------------------------------------------------------ one host must not eat the budget

import services.crawl_politeness as cp


def _run_hosts(monkeypatch, hosts: List[str], answers: Dict[str, List[int]], *, trip: str = ""):
    """Drive the real batch over rows on `hosts`, feeding each fetch's status into the REAL
    pacing counter, exactly where `_fetch_html` does. Returns (summary, seed ids refreshed)."""
    cp.reset_for_tests()
    monkeypatch.setenv("EXTERNAL_REFERRAL_REFRESH_HOST_BLOCK_TRIP", trip)
    seed_ids = [f"eps_{i}" for i in range(len(hosts))]
    monkeypatch.setattr(
        err, "get_external_referral_refresh_candidate_seed_ids",
        lambda *a, **k: asyncio.sleep(0, result=seed_ids),
    )
    monkeypatch.setattr(
        err, "_fetch_refresh_candidate_hosts",
        lambda ids: asyncio.sleep(0, result=dict(zip(seed_ids, hosts))),
    )
    queues = {h: list(v) for h, v in answers.items()}
    called: List[str] = []

    async def fake_refresh(seed_id, **kwargs):
        called.append(seed_id)
        host = hosts[seed_ids.index(seed_id)]
        code = queues[host].pop(0) if queues[host] else 200
        cp.note_response(f"https://{host}/products/x", code)
        if code == 200:
            return _ok(price="unchanged", projected=0, attempted=0)
        return {"status": "degraded", "error": f"destination_unavailable: http {code}", "domain": host}

    summary = asyncio.run(
        err.run_external_referral_refresh_batch(refresh_seed_by_id=fake_refresh, limit=len(hosts))
    )
    cp.reset_for_tests()
    return summary, called


def test_a_host_on_a_429_streak_is_skipped_for_the_rest_of_the_run(monkeypatch):
    """The 09-20/21/22 shape: fentybeauty.com 429s on every request while other hosts answer.
    Before, every fenty row waited out a hold that doubled to 300s and the night refreshed ~83
    rows. Now the fifth fenty row onward is never requested, and the other hosts still are."""
    hosts = ["fentybeauty.com", "a.com"] * 10
    summary, called = _run_hosts(monkeypatch, hosts, {"fentybeauty.com": [429] * 20, "a.com": []})
    fenty_called = [s for s in called if hosts[int(s.split("_")[1])] == "fentybeauty.com"]
    assert len(fenty_called) == 4, "trips on the 4th consecutive 429, never asks a 5th time"
    assert summary["skipped_for_host_backoff"] == 6
    assert summary["host_backoff_skips"] == {"fentybeauty.com": 6}
    # Skipped rows are NOT attempts: never handed to the refresher (so never stamped) and out
    # of the yield denominator, like budget skips.
    assert len(called) == 14
    assert summary["attempted_count"] == 14
    assert summary["refreshed"] == 10, "every a.com row was still read"
    assert summary["host_breaker_armed"] is True


def test_a_host_whose_streak_breaks_is_never_tripped(monkeypatch):
    """The breaker counts CONSECUTIVE blocks, as the pacing layer does: a host that 429s three
    times, answers, and 429s three more times is throttling, not refusing."""
    hosts = ["b.com"] * 8
    summary, called = _run_hosts(monkeypatch, hosts, {"b.com": [429, 429, 429, 200, 429, 429, 429, 200]})
    assert len(called) == 8
    assert summary["skipped_for_host_backoff"] == 0
    assert summary["host_backoff_skips"] == {}


def test_a_503_streak_trips_the_breaker_too(monkeypatch):
    """`note_response` backs off on 503 exactly as on 429 (26 of 958 measured holds were 503s),
    so the hold it earns costs the budget the same way."""
    summary, called = _run_hosts(monkeypatch, ["c.com"] * 6, {"c.com": [503] * 6})
    assert len(called) == 4 and summary["host_backoff_skips"] == {"c.com": 2}


def test_the_breaker_can_be_disabled_without_a_deploy(monkeypatch):
    summary, called = _run_hosts(monkeypatch, ["d.com"] * 6, {"d.com": [429] * 6}, trip="0")
    assert len(called) == 6
    assert summary["skipped_for_host_backoff"] == 0
    assert summary["host_breaker_armed"] is False


def test_the_trip_streak_is_tunable(monkeypatch):
    summary, called = _run_hosts(monkeypatch, ["e.com"] * 6, {"e.com": [429] * 6}, trip="2")
    assert len(called) == 2 and summary["host_block_trip"] == 2


def test_the_host_lookup_fails_open_to_no_breaker(monkeypatch):
    """A lookup error must not stop the refresh; it runs exactly as before the breaker."""
    async def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(err.database, "fetch_all", boom)
    assert asyncio.run(err._fetch_refresh_candidate_hosts(["eps_1"])) == {}


def test_the_host_lookup_keys_on_the_url_host_not_the_domain_column(monkeypatch):
    """`crawl_politeness` paces on `host_of(url)`; a breaker keyed on `domain` (which drops the
    `www.`) would never see the streak on `www.cosrx.com`."""
    seen: List[Dict[str, Any]] = []

    async def fake_fetch_all(query, params):
        seen.append(params)
        return [
            {"id": "eps_1", "destination_url": "https://WWW.Cosrx.com/products/snail?utm_source=x"},
            {"id": "eps_2", "destination_url": ""},
        ]
    monkeypatch.setattr(err.database, "fetch_all", fake_fetch_all)
    hosts = asyncio.run(err._fetch_refresh_candidate_hosts(["eps_1", "eps_2"]))
    assert hosts == {"eps_1": "www.cosrx.com"}
    assert seen == [{"id0": "eps_1", "id1": "eps_2"}]


def test_a_starved_budget_stop_is_degraded_end_to_end(monkeypatch):
    """09-23 at the batch seam: the budget runs out after a handful of rows. Every row it DID
    reach read fine, so the yield rule is satisfied; only reach can call this night what it was."""
    clock = {"t": 0.0}
    # The module's `time`, not the global one: asyncio's own loop clock must keep running.
    monkeypatch.setattr(err, "time", SimpleNamespace(monotonic=lambda: clock["t"]))
    seed_ids = [f"eps_{i}" for i in range(10)]
    monkeypatch.setattr(
        err, "get_external_referral_refresh_candidate_seed_ids",
        lambda *a, **k: asyncio.sleep(0, result=seed_ids),
    )
    monkeypatch.setattr(err, "_fetch_refresh_candidate_hosts", lambda ids: asyncio.sleep(0, result={}))

    async def slow_refresh(seed_id, **kwargs):
        clock["t"] += 100.0
        return _ok(price="unchanged", projected=0, attempted=0)

    summary = asyncio.run(err.run_external_referral_refresh_batch(
        refresh_seed_by_id=slow_refresh, limit=10, budget_seconds=250,
    ))
    assert summary["stopped_early"] is True
    assert summary["skipped_for_budget"] == 7
    assert summary["budget_reach"] == 0.3
    assert summary["origin_yield"] == 1.0
    assert summary["status"] == "degraded"

    clock["t"] = 0.0
    summary = asyncio.run(err.run_external_referral_refresh_batch(
        refresh_seed_by_id=slow_refresh, limit=10, budget_seconds=850,
    ))
    assert summary["stopped_early"] is True and summary["budget_reach"] == 0.9
    assert summary["status"] == "success", "a steady-state budget stop stays green"
