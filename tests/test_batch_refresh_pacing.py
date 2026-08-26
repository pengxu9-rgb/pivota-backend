"""The nightly refresh must be able to WAIT; the interactive route must not.

`services/crawl_politeness` refuses a slot further out than the caller allows, and the
default ceiling (`CRAWL_MAX_WAIT_SECONDS`, 10s) is calibrated for the live route where a
human is waiting. `_fetch_html` states the rule in its own comment: a BATCH job must pass 0
(unbounded) "or the backoff curve above ~16s becomes unreachable — `await_slot` would refuse
instead of waiting, and the script's `except Exception` would silently record the row as
fetch_failed. A host that 429s four times in a row would then void the rest of the run in
milliseconds while looking like the host was down."

`tests/test_crawl_politeness.py::test_a_batch_job_can_opt_into_waiting` already proves the
politeness layer honours `max_wait=0`. What was untested — and until now impossible, because
`resolve_external_offer` had no such parameter — is the PLUMBING: that the batch's patience
actually reaches that layer. The sibling destination sweep passes 0 directly to
`crawl_politeness`; the content refresh sits two functions further away, and every one of
them had to forward it.

These rows pin each hop, and the DIRECTION matters as much as the value: a change that made
both callers unbounded would be a regression on the interactive route, not a fix.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest


# --------------------------------------------------------- hop 3: resolve -> _fetch_html

def test_resolve_external_offer_forwards_the_callers_patience(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.external_offers_service as svc

    seen: List[Dict[str, Any]] = []

    async def fake_fetch_html(url, **kwargs):
        seen.append(kwargs)
        raise RuntimeError("stop here")

    async def no_existing(*a, **k):
        return None

    monkeypatch.setattr(svc, "_fetch_html", fake_fetch_html)
    monkeypatch.setattr(svc, "_get_snapshot_row", no_existing)

    for patience in (0, 30.0, None):
        seen.clear()
        with pytest.raises(Exception):
            asyncio.run(
                svc.resolve_external_offer(
                    market="US",
                    url="https://brand.com/products/toner",
                    force_refresh=True,
                    max_wait=patience,
                )
            )
        assert seen, "the fetch was never attempted"
        assert seen[-1].get("max_wait") == patience, (
            f"expected max_wait={patience!r} to reach _fetch_html, got {seen[-1].get('max_wait')!r}"
        )


def test_the_default_stays_none_so_the_live_route_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`None` means "use CRAWL_MAX_WAIT_SECONDS". Defaulting to 0 here would silently make
    every live request willing to block for an arbitrary Crawl-delay."""
    import services.external_offers_service as svc

    seen: List[Dict[str, Any]] = []

    async def fake_fetch_html(url, **kwargs):
        seen.append(kwargs)
        raise RuntimeError("stop here")

    async def no_existing(*a, **k):
        return None

    monkeypatch.setattr(svc, "_fetch_html", fake_fetch_html)
    monkeypatch.setattr(svc, "_get_snapshot_row", no_existing)

    with pytest.raises(Exception):
        asyncio.run(
            svc.resolve_external_offer(
                market="US", url="https://brand.com/products/toner", force_refresh=True
            )
        )
    assert seen[-1].get("max_wait") is None


# ------------------------------------------------- hop 2: _refresh_external_seed_by_id -> resolve

def _capture_resolve(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    import routes.employee_products as mod

    seen: List[Dict[str, Any]] = []

    async def fake_resolve(**kwargs):
        seen.append(kwargs)
        raise RuntimeError("stop here")

    async def fake_fetch_one(_q, values=None):
        return {
            "id": "eps_pace_1",
            "market": "US",
            "tool": "*",
            "destination_url": "https://brand.com/products/toner",
            "canonical_url": "https://brand.com/products/toner",
            "domain": "brand.com",
            "seed_data": {"snapshot": {}},
            "status": "active",
            "price_amount": 28.0,
            "price_currency": "USD",
        }

    monkeypatch.setattr(mod, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(mod.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(mod, "resolve_external_offer", fake_resolve)
    monkeypatch.setattr(mod, "_execute_seed_data_stmt", AsyncMock(return_value=None))
    monkeypatch.setattr(mod, "_stamp_crawl_attempt", AsyncMock(return_value=None))
    return seen


def test_the_refresh_forwards_an_explicit_patience(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.employee_products as mod

    seen = _capture_resolve(monkeypatch)
    asyncio.run(mod._refresh_external_seed_by_id("eps_pace_1", max_wait=0))
    assert seen[-1].get("max_wait") == 0


def test_the_refresh_defaults_to_the_bounded_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """The employee route calls this with no `max_wait`. A human is waiting on that request,
    so a throttled host must fail fast rather than block the response."""
    import routes.employee_products as mod

    seen = _capture_resolve(monkeypatch)
    asyncio.run(mod._refresh_external_seed_by_id("eps_pace_1"))
    assert seen[-1].get("max_wait") is None


# ------------------------------------------------------------ hop 1: the job -> the refresh

def test_the_nightly_job_asks_for_unbounded_patience(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE ROW THAT MATTERS. Everything above is plumbing; this is the caller that has to use
    it, and it is the one that was wrong."""
    import jobs.external_referral_refresh as job

    seen: List[Dict[str, Any]] = []

    async def fake_refresh(seed_id, **kwargs):
        seen.append({"seed_id": seed_id, **kwargs})
        return {"status": "success"}

    monkeypatch.setattr(job, "_refresh_external_seed_by_id", fake_refresh)
    asyncio.run(job._refresh_unbounded("eps_pace_1"))

    assert seen == [{"seed_id": "eps_pace_1", "max_wait": 0}]


def test_the_job_hands_the_batch_its_unbounded_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pins the WIRING, not just the helper: a helper that exists but is never passed to
    `run_external_referral_refresh_batch` leaves the ceiling in place for every row."""
    import jobs.external_referral_refresh as job

    captured: Dict[str, Any] = {}

    async def fake_batch(*, refresh_seed_by_id, limit, **kwargs):
        captured["callback"] = refresh_seed_by_id
        captured["limit"] = limit
        captured.update(kwargs)
        return {"status": "success"}

    monkeypatch.setattr(job, "run_external_referral_refresh_batch", fake_batch)
    asyncio.run(job.run_daily_external_referral_refresh(limit=7, budget_seconds=123.0))

    assert captured["limit"] == 7
    assert captured["callback"] is job._refresh_unbounded, (
        "the batch must get the unbounded callback, not the raw refresh"
    )
    # The wall-clock budget is the OTHER half of what makes unbounded patience safe to schedule;
    # a job that accepts `--budget-seconds` and then drops it on the floor is worse than one
    # that never offered the flag.
    assert captured.get("budget_seconds") == 123.0, (
        f"the run's wall-clock budget must reach the batch, got {captured.get('budget_seconds')!r}"
    )


def test_the_patience_reaches_crawl_politeness_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the LAST hop, which is the one that actually delivers the fix.

    The test above stops at `_fetch_html`, so it proves the patience crosses
    resolve_external_offer -> _fetch_html and nothing further. But `_fetch_html`
    is not the pacer; `crawl_politeness.before_request` is, and it is the only
    line whose `max_wait` decides whether `await_slot` waits or raises
    `CrawlPaced`. Dropping `max_wait=max_wait` from
    services/external_offers_service.py's `before_request` call survived the
    ENTIRE 12,557-test suite: every hop this change threads would become dead
    plumbing, the nightly batch would silently revert to the 10s ceiling, and
    all of the other tests here would stay green.

    So this asserts against the real module-global `crawl_politeness`, one hop
    past where the coverage previously stopped.
    """
    import services.external_offers_service as svc
    from services import crawl_politeness

    seen: List[Dict[str, Any]] = []

    async def fake_before_request(url, **kwargs):
        seen.append(kwargs)
        raise RuntimeError("stop at the pacer")

    async def no_existing(*a, **k):
        return None

    monkeypatch.setattr(crawl_politeness, "before_request", fake_before_request)
    monkeypatch.setattr(svc, "_get_snapshot_row", no_existing)

    # 0 is the batch's "unbounded"; None is the interactive default ceiling. Both must arrive
    # verbatim — collapsing None to 0 would make the UNAUTHENTICATED live route unbounded too.
    for patience in (0, 30.0, None):
        seen.clear()
        with pytest.raises(RuntimeError, match="stop at the pacer"):
            asyncio.run(
                svc.resolve_external_offer(
                    market="US",
                    url="https://brand.com/products/toner",
                    force_refresh=True,
                    max_wait=patience,
                )
            )
        assert seen, "before_request was never reached"
        assert seen[-1].get("max_wait") == patience, (
            f"expected max_wait={patience!r} to reach crawl_politeness.before_request, "
            f"got {seen[-1].get('max_wait')!r}"
        )


# ------------------------------------------------------- the OTHER half: bounding the RUN
#
# `max_wait=0` makes every row willing to wait; nothing made the RUN willing to stop. With
# `CRAWL_MAX_BACKOFF_SECONDS` at 300s, `limit=500` rows against a persistently-429ing host is a
# worst case near 41 hours — no hostile robots.txt required. `crawl_politeness`'s
# `CRAWL_MAX_ROBOTS_DELAY_SECONDS` bounds what one ROW can cost; only this bounds the run.


class _FakeClock:
    """A monotonic clock the test advances, so the budget is exercised without sleeping."""

    def __init__(self) -> None:
        self.t = 1000.0

    def monotonic(self) -> float:
        return self.t


def _run_batch_with_clock(
    monkeypatch: pytest.MonkeyPatch,
    *,
    candidates: List[str],
    seconds_per_row: float,
    budget_seconds: Optional[float],
) -> Dict[str, Any]:
    from services import external_referral_readiness as module

    clock = _FakeClock()
    refreshed: List[str] = []

    async def fake_candidates(*, limit: int) -> List[str]:
        return list(candidates)

    async def fake_refresh(seed_id: str) -> Dict[str, Any]:
        refreshed.append(seed_id)
        clock.t += seconds_per_row
        return {"status": "success", "seed_id": seed_id}

    monkeypatch.setattr(
        module, "get_external_referral_refresh_candidate_seed_ids", fake_candidates
    )
    monkeypatch.setattr(module.time, "monotonic", clock.monotonic)

    summary = asyncio.run(
        module.run_external_referral_refresh_batch(
            refresh_seed_by_id=fake_refresh,
            limit=len(candidates),
            budget_seconds=budget_seconds,
        )
    )
    summary["_refreshed"] = refreshed
    return summary


def test_the_batch_stops_starting_rows_once_its_budget_is_spent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary = _run_batch_with_clock(
        monkeypatch,
        candidates=[f"eps_{i}" for i in range(10)],
        seconds_per_row=100.0,
        budget_seconds=250.0,
    )

    # Rows 1 and 2 start inside the budget (elapsed 0 and 100); row 3 starts at 200, still
    # inside; row 4 would start at 300 and does not.
    assert summary["_refreshed"] == ["eps_0", "eps_1", "eps_2"], summary["_refreshed"]
    assert summary["stopped_early"] is True
    assert summary["skipped_for_budget"] == 7


def test_a_run_that_fits_its_budget_is_not_reported_as_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The negative case. A `stopped_early` that is always True would pass the row above and
    make the flag meaningless."""
    summary = _run_batch_with_clock(
        monkeypatch,
        candidates=["eps_0", "eps_1"],
        seconds_per_row=1.0,
        budget_seconds=3600.0,
    )
    assert summary["_refreshed"] == ["eps_0", "eps_1"]
    assert summary["stopped_early"] is False
    assert summary["skipped_for_budget"] == 0


def test_a_budget_stop_is_visible_in_the_summary_not_just_a_shorter_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A silent truncation reads EXACTLY like a completed sweep: `candidate_count` 10,
    `refreshed` 3, and nothing to say the other 7 were never attempted. Whoever arms the
    scheduler reads this summary, so the shortfall has to be in it."""
    summary = _run_batch_with_clock(
        monkeypatch,
        candidates=[f"eps_{i}" for i in range(10)],
        seconds_per_row=100.0,
        budget_seconds=250.0,
    )
    assert summary["candidate_count"] == 10
    assert summary["refreshed"] == 3
    assert summary["budget_seconds"] == 250.0
    assert summary["refreshed"] + summary["skipped_for_budget"] == summary["candidate_count"]


def test_the_budget_can_be_disabled_and_falls_back_to_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`0` disables it outright; an unset `budget_seconds` reads the env, so an operator can
    retune a scheduled run without a deploy."""
    summary = _run_batch_with_clock(
        monkeypatch,
        candidates=[f"eps_{i}" for i in range(5)],
        seconds_per_row=10_000.0,
        budget_seconds=0,
    )
    assert summary["_refreshed"] == [f"eps_{i}" for i in range(5)]
    assert summary["stopped_early"] is False

    monkeypatch.setenv("EXTERNAL_REFERRAL_REFRESH_BUDGET_SECONDS", "150")
    summary = _run_batch_with_clock(
        monkeypatch,
        candidates=[f"eps_{i}" for i in range(5)],
        seconds_per_row=100.0,
        budget_seconds=None,
    )
    assert summary["budget_seconds"] == 150.0
    assert summary["_refreshed"] == ["eps_0", "eps_1"], summary["_refreshed"]
