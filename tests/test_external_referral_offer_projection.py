"""The nightly refresh must reach the surfaces a BUYER reads, and must say when it didn't.

Two defects measured on prod 2026-09-06, both of which a green nightly job concealed:

1. `_refresh_external_seed_by_id` wrote ONLY `external_product_seeds`. The search/offers lane
   reads the seed, but the PDP (`agent_pdp_view`) and the index `has_price` gate read
   `catalog_offers`, and nothing re-projected an already-mirrored seed. Result: **1,321 of 5,316
   live products (25%) served a different price on the PDP than in search**, 917 of them with a
   seed read inside 7 days.
2. The batch reported `success if failed == 0`, and `failed` counts only exceptions — so a run
   that stopped on budget (659 rows skipped on 09-05) or served half its rows from cache without
   reaching an origin still exited 0 and showed a green tick in Cloud Run.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import pytest

import routes.employee_products as ep
import services.external_referral_readiness as err


# --------------------------------------------------------------------------- projection

def _arm(monkeypatch, calls, *, sync_status="synced"):
    async def fake_sync(seed_id):
        calls.append(f"offers:{seed_id}")
        return {"status": sync_status}

    async def fake_pdp(*, seed_id, proposal_id, refresh_source):
        calls.append(f"pdp:{seed_id}:{refresh_source}")

    monkeypatch.setattr("services.external_offer_dual_write.sync_offer_for_seed", fake_sync)
    monkeypatch.setattr("services.seed_data_writer.refresh_agent_pdp_view_for_seed", fake_pdp)


def test_a_refreshed_price_is_projected_onto_catalog_offers_and_the_pdp(monkeypatch):
    """The seed write alone is what left the PDP stale; the projection is the fix."""
    calls: List[str] = []
    monkeypatch.setattr("services.external_offer_dual_write.dual_write_enabled", lambda: True)
    _arm(monkeypatch, calls)

    counts = asyncio.run(ep._project_refreshed_seed_to_serving_surfaces("eps_1"))

    assert calls == ["offers:eps_1", "pdp:eps_1:external_referral_refresh"], (
        "both surfaces must be re-projected, and the PDP refresh must name this source"
    )
    assert counts["attempted"] == 1 and counts["projected"] == 1


def test_with_the_flag_off_NEITHER_surface_is_touched(monkeypatch):
    """THE REGRESSION THIS PINS. The first version relied on `sync_offer_for_seed` self-gating
    and left `refresh_agent_pdp_view_for_seed` UNGATED — which has no flag of its own. With the
    flag off that still ran a ~1s/key view rebuild plus a serving-eligibility recompute for
    every refreshed seed (~2,000/night) INSIDE the 3,300s crawl budget, while catalog_offers
    stayed untouched: all of the cost, none of the fix, and `stopped_early` near-certain.
    "Inert until armed" has to be true of the whole helper."""
    calls: List[str] = []
    monkeypatch.setattr("services.external_offer_dual_write.dual_write_enabled", lambda: False)
    _arm(monkeypatch, calls)

    counts = asyncio.run(ep._project_refreshed_seed_to_serving_surfaces("eps_1"))

    assert calls == [], "the PDP rebuild is the expensive half and must not run unarmed"
    assert counts["attempted"] == 0 and counts["projected"] == 0
    assert counts["skipped"] == 0 and counts["errored"] == 0


def test_the_written_status_comes_from_the_writer_not_a_guess(monkeypatch):
    """`sync_offer_for_seed` emits exactly no_seed_id / disabled / seed_missing /
    no_external_product_id / no_mirror_product / synced / error. The first version hardcoded
    {"synced","inserted","updated","ok"} — three it cannot emit — and stubbed "ok" in its own
    positive tests, so the whole success path was calibrated against a value production never
    produces. Deleting "synced" from that set survived every test."""
    from services.external_offer_dual_write import (
        OFFER_SYNC_ERROR_STATUSES, OFFER_SYNC_WRITTEN_STATUSES,
    )
    assert OFFER_SYNC_WRITTEN_STATUSES == {"synced"}
    assert OFFER_SYNC_ERROR_STATUSES == {"error"}

    calls: List[str] = []
    monkeypatch.setattr("services.external_offer_dual_write.dual_write_enabled", lambda: True)
    _arm(monkeypatch, calls, sync_status="ok")
    counts = asyncio.run(ep._project_refreshed_seed_to_serving_surfaces("eps_1"))
    assert counts["projected"] == 0, "'ok' is not a status this writer can return"


def test_a_writer_error_is_counted_as_an_error_not_a_skip(monkeypatch):
    """A skip means "nothing to do"; counting a failed write as one hides it from the summary."""
    calls: List[str] = []
    monkeypatch.setattr("services.external_offer_dual_write.dual_write_enabled", lambda: True)
    _arm(monkeypatch, calls, sync_status="error")
    counts = asyncio.run(ep._project_refreshed_seed_to_serving_surfaces("eps_1"))
    assert counts["errored"] == 1 and counts["skipped"] == 0


def test_a_no_op_projection_is_counted_as_a_skip_not_a_write(monkeypatch):
    """`sync_offer_for_seed` has seven statuses and six mean "did nothing"; `no_mirror_product`
    is expected to be common. Dropping the dict made "healed 2,000" and "healed 0" identical."""
    calls: List[str] = []
    monkeypatch.setattr("services.external_offer_dual_write.dual_write_enabled", lambda: True)
    _arm(monkeypatch, calls, sync_status="no_mirror_product")

    counts = asyncio.run(ep._project_refreshed_seed_to_serving_surfaces("eps_1"))

    assert counts["projected"] == 0 and counts["skipped"] == 1
    assert counts.get("skip_no_mirror_product") == 1, "the reason must survive to the summary"


def test_the_projection_never_breaks_the_refresh_that_produced_a_good_price(monkeypatch):
    """The seed row is already committed and is the source of truth.

    A cache projection that raises must not turn a successful price read into a failed refresh —
    the same isolation `agent_pdp_view_assembler` documents for its own writes.
    """
    async def boom_sync(seed_id):
        raise RuntimeError("offers table on fire")

    async def boom_pdp(*, seed_id, proposal_id, refresh_source):
        raise RuntimeError("pdp view on fire")

    monkeypatch.setattr("services.external_offer_dual_write.dual_write_enabled", lambda: True)
    monkeypatch.setattr("services.external_offer_dual_write.sync_offer_for_seed", boom_sync)
    monkeypatch.setattr("services.seed_data_writer.refresh_agent_pdp_view_for_seed", boom_pdp)

    asyncio.run(ep._project_refreshed_seed_to_serving_surfaces("eps_1"))  # must not raise


def test_an_empty_seed_id_projects_nothing(monkeypatch):
    called = []
    async def fake_sync(seed_id):
        called.append(seed_id)
    monkeypatch.setattr("services.external_offer_dual_write.dual_write_enabled", lambda: True)
    monkeypatch.setattr("services.external_offer_dual_write.sync_offer_for_seed", fake_sync)
    asyncio.run(ep._project_refreshed_seed_to_serving_surfaces(""))
    assert called == []


# --------------------------------------------------------------------------- honest status

def _status(monkeypatch, *, failed=0, stopped_early=False, candidate=10, origin_reads=10,
            min_yield: str = "", price_changes=0, proj_attempted=0, proj_written=0) -> str:
    """Calls the PRODUCTION decision function. An earlier draft of this file re-implemented the
    arithmetic here, which would have passed whether or not the job used it."""
    monkeypatch.setenv("EXTERNAL_REFERRAL_REFRESH_MIN_ORIGIN_YIELD", min_yield)
    return err.batch_run_status(
        failed=failed, stopped_early=stopped_early,
        attempted_count=candidate, origin_reads=origin_reads,
        price_changes=price_changes,
        projections_attempted=proj_attempted, projections_written=proj_written,
    )


def test_a_healthy_run_is_still_success(monkeypatch):
    assert _status(monkeypatch) == "success"


def test_an_exception_run_is_degraded(monkeypatch):
    assert _status(monkeypatch, failed=1) == "degraded"


def test_a_budget_stop_alone_is_reported_not_failed(monkeypatch):
    """CORRECTED after review. With --limit 4000 over 11,769 seeds the queue is DESIGNED to
    drain across ~3 nights, so a budget stop is the steady state. Failing on it would make the
    alarm meaningless from night one; it is reported in the summary instead."""
    assert _status(monkeypatch, stopped_early=True) == "success"


def test_projecting_nothing_while_prices_move_is_degraded(monkeypatch):
    """The offers table is still drifting from the seed — the defect the hook exists to close."""
    assert _status(monkeypatch, price_changes=5, proj_attempted=10, proj_written=0) == "degraded"
    assert _status(monkeypatch, price_changes=5, proj_attempted=10, proj_written=7) == "success"


def test_a_run_served_mostly_from_cache_is_not_a_success(monkeypatch):
    """`refreshed` counts cache-served rows, so it cannot be the health signal on its own:
    origin_reads = refreshed - refreshed_from_cache."""
    assert _status(monkeypatch, origin_reads=2) == "degraded"


def test_the_yield_floor_is_tunable_without_a_deploy(monkeypatch):
    """Set at today's measured level (~0.50) on purpose so it fires; operators can lower it
    while hosts are triaged rather than shipping a code change."""
    assert _status(monkeypatch, origin_reads=2, min_yield="0.1") == "success"
    assert _status(monkeypatch, origin_reads=2, min_yield="0.5") == "degraded"


def test_an_empty_attempted_set_is_not_a_false_alarm(monkeypatch):
    """Nothing attempted means nothing to judge — division must not decide it."""
    assert _status(monkeypatch, candidate=0, origin_reads=0) == "success"


@pytest.mark.parametrize(
    "err_text,bucket",
    [
        ("HTTP 403 Forbidden", "http_403"),
        ("server returned 503", "http_503"),
        ("Read timed out", "timeout"),
        ("SSL: CERTIFICATE_VERIFY_FAILED", "tls"),
        ("blocked by robots.txt", "robots"),
        ("getaddrinfo ENOTFOUND", "dns"),
        ("Just a moment... challenge", "bot_challenge"),
        ("Read timed out (port=443)", "timeout"),
        ("", "unspecified"),
        ("something else entirely", "other"),
    ],
)
def test_degraded_reasons_are_bucketed_not_dropped(err_text, bucket):
    """The reason used to be discarded — `errors[]` is filled only on the `failed` branch, and
    `failed` is structurally 0 for a degraded read. A bare count of 1,353 is unactionable."""
    assert err._degraded_reason_bucket(err_text) == bucket


def test_a_reason_bucket_never_echoes_the_raw_error():
    """Buckets, not raw text: the raw string carries URLs and per-host detail."""
    leaky = "failed to fetch https://brand.example/products/secret-handle?token=abc"
    assert "brand.example" not in err._degraded_reason_bucket(leaky)


# ------------------------------------------------- the CALL SITE, not just the helper

def _drive_refresh(monkeypatch, *, stored_price, fresh_price, from_cache=False):
    """Drive the REAL `_refresh_external_seed_by_id` and report which seeds it projected.

    The helper tests above pin the projection in isolation; nothing pinned that the refresh
    CALLS it, or on which statuses. Review demonstrated the gap: narrowing the gate to
    {applied, filled} — deleting the "self-healing on unchanged" behaviour the PR argues for —
    survived, and so did replacing the condition with `if True:`.
    """
    from unittest.mock import AsyncMock
    from types import SimpleNamespace

    projected: List[str] = []

    async def fake_project(seed_id):
        projected.append(seed_id)
        return {"attempted": 1, "projected": 1, "skipped": 0, "errored": 0}

    async def fake_fetch_one(_q, values=None):
        return {
            "id": "eps_proj_1", "market": "US", "tool": "*",
            "destination_url": "https://brand.com/products/toner",
            "canonical_url": "https://brand.com/products/toner",
            "domain": "brand.com", "seed_data": {"snapshot": {}}, "status": "active",
            "price_amount": stored_price, "price_currency": "USD",
        }

    async def fake_resolve(*, observed=None, **kwargs):
        # A LIVE observation is what makes `read_the_served_product` true; without the status
        # code the refresh records no observation at all and the gate can never open.
        if observed is not None:
            observed["status_code"] = 200
            observed["final_url"] = "https://brand.com/products/toner"
            if from_cache:
                observed["from_cache"] = True
        return SimpleNamespace(
            canonical_url="https://brand.com/products/toner", domain="brand.com",
            title="toner", image_url=None, price_amount=fresh_price,
            price_currency="USD", availability="in_stock", evidence={},
        )

    monkeypatch.setattr(ep, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(ep.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(ep.database, "execute", AsyncMock(return_value=None))
    monkeypatch.setattr(ep, "_execute_seed_data_stmt", AsyncMock(return_value=None))
    monkeypatch.setattr(ep, "_stamp_crawl_attempt", AsyncMock(return_value=None))
    monkeypatch.setattr(ep, "resolve_external_offer", fake_resolve)
    monkeypatch.setattr(ep, "_project_refreshed_seed_to_serving_surfaces", fake_project)

    result = asyncio.run(ep._refresh_external_seed_by_id("eps_proj_1", max_wait=0))
    return result, projected


def test_a_moved_price_projects(monkeypatch):
    result, projected = _drive_refresh(monkeypatch, stored_price=28.0, fresh_price=31.0)
    assert result["price_refresh"]["status"] == "applied"
    assert projected == ["eps_proj_1"]
    assert result["projection"]["projected"] == 1, "the counters must reach the batch"


def test_an_UNCHANGED_price_still_projects(monkeypatch):
    """The self-healing case, and the one a narrowed gate would silently delete. Rows that
    drifted before this fix are repaired by re-projecting a price that did not move — without
    it the 1,321 mismatched products need a separate backfill."""
    result, projected = _drive_refresh(monkeypatch, stored_price=28.0, fresh_price=28.0)
    assert result["price_refresh"]["status"] == "unchanged"
    assert projected == ["eps_proj_1"]


def test_a_price_we_could_not_re_read_does_NOT_project(monkeypatch):
    """`if True:` must not survive. With no fresh amount the stored price is untouched, so
    stamping catalog_offers.updated_at would claim a freshness we did not earn."""
    result, projected = _drive_refresh(monkeypatch, stored_price=28.0, fresh_price=None)
    assert result["price_refresh"]["status"] == "unavailable"
    assert projected == [], "nothing was re-read; nothing may be re-projected"


@pytest.mark.parametrize("mode", ["snapshot_failed", "destination_unavailable", "not_the_served_url"])
def test_every_degraded_return_reports_the_host_it_failed_on(monkeypatch, mode):
    """`top_degraded_hosts` was structurally always {"unknown": N}.

    The summary buckets by `result["domain"]`, but `domain` is read off the SNAPSHOT — which by
    definition does not exist on the paths that degrade. So a night with 1,353 degraded rows
    named no host, and the histogram could not find the one 503-ing site that ate 1,410s of the
    budget. All THREE degraded returns are covered here: the first version tested only
    `snapshot_failed`, and mutants on the other two survived.
    """
    from unittest.mock import AsyncMock
    from services.external_offers_service import ExternalOfferUnavailable

    async def fake_fetch_one(_q, values=None):
        return {
            "id": "eps_bad_1", "market": "US", "tool": "*",
            "destination_url": "https://themedicube.us.com/products/pad",
            "canonical_url": "https://themedicube.us.com/products/pad",
            "domain": "themedicube.us.com", "seed_data": {"snapshot": {}},
            "status": "active", "price_amount": 20.0, "price_currency": "USD",
        }

    async def resolver(*, observed=None, **kwargs):
        if mode == "snapshot_failed":
            raise RuntimeError("connection reset by peer")
        exc = ExternalOfferUnavailable("unavailable")
        exc.status_code = 503
        exc.final_url = (
            "https://themedicube.us.com/products/pad" if mode == "destination_unavailable"
            else "https://elsewhere.example/other"
        )
        raise exc

    monkeypatch.setattr(ep, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(ep.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(ep.database, "execute", AsyncMock(return_value=None))
    monkeypatch.setattr(ep, "_stamp_crawl_attempt", AsyncMock(return_value=None))
    monkeypatch.setattr(ep, "resolve_external_offer", resolver)

    result = asyncio.run(ep._refresh_external_seed_by_id("eps_bad_1", max_wait=0))

    assert result["status"] == "degraded"
    assert result["domain"] == "themedicube.us.com", (
        f"the {mode} path must name its host, or the summary can only say 'unknown'"
    )


def test_a_CACHE_SERVED_row_does_not_project(monkeypatch):
    """A quarter of rows on 09-05 came back from the cached snapshot without reaching the origin.
    They still yield `unchanged`, so a gate on price status ALONE projects them — stamping
    catalog_offers.updated_at = NOW() on a row nobody re-read. The gate needs both conjuncts,
    matching the `extracted_at` stamp the same function already guards this way."""
    result, projected = _drive_refresh(
        monkeypatch, stored_price=28.0, fresh_price=28.0, from_cache=True
    )
    assert result["snapshot_from_cache"] is True
    assert projected == [], "a row we did not re-read must not be re-projected"
