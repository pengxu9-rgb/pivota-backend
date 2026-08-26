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

    async def fake_batch(*, refresh_seed_by_id, limit):
        captured["callback"] = refresh_seed_by_id
        captured["limit"] = limit
        return {"status": "success"}

    monkeypatch.setattr(job, "run_external_referral_refresh_batch", fake_batch)
    asyncio.run(job.run_daily_external_referral_refresh(limit=7))

    assert captured["limit"] == 7
    assert captured["callback"] is job._refresh_unbounded, (
        "the batch must get the unbounded callback, not the raw refresh"
    )
