"""WS-A increment 1: catalog sync auto-enqueues a quality backfill (readiness).

A freshly-onboarded merchant's first v3 audit came back blocked because nothing
populated product_quality_snapshot (content_richness 25 + the serving-eligibility
gate). The fix: run_catalog_sync_job enqueues a quality-backfill job once the
catalog is populated, and a scheduler tick drains it (deterministic, no LLM).
This test asserts the enqueue fires after a successful sync.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import services.catalog_sync_service as css
import db.product_quality_backfill_jobs as qbf


async def test_catalog_sync_enqueues_quality_backfill(monkeypatch) -> None:
    job = {"job_id": "csj_1", "merchant_id": "merch_x", "connector": "shopify",
           "mode": "reconcile", "scope_json": {"platform": "shopify"}}

    async def _get_job(_job_id):
        return dict(job)

    async def _claim(_job_id):
        return dict(job, status="running")

    async def _upsert(*_a, **_k):
        return None

    async def _sync(**_k):
        return {"products": 4}

    enqueued: List[Dict[str, Any]] = []

    async def _create_quality_job(*, merchant_id, platform, requested_by,
                                   force_refresh=False, missing_only=True):
        enqueued.append({"merchant_id": merchant_id, "platform": platform,
                         "requested_by": requested_by})
        return {"job_id": "qbf_test"}

    monkeypatch.setattr(css, "get_catalog_sync_job", _get_job)
    monkeypatch.setattr(css, "claim_catalog_sync_job", _claim)
    monkeypatch.setattr(css, "_upsert_by_pk", _upsert)
    monkeypatch.setattr(css, "sync_products_cache_to_catalog", _sync)
    monkeypatch.setattr(qbf, "create_quality_backfill_job", _create_quality_job)

    await css.run_catalog_sync_job("csj_1")

    assert len(enqueued) == 1, "catalog sync did not enqueue a quality backfill"
    assert enqueued[0]["merchant_id"] == "merch_x"
    assert enqueued[0]["platform"] == "shopify"
    assert enqueued[0]["requested_by"] == "catalog_sync_autodrain"


async def test_quality_enqueue_failure_does_not_break_sync(monkeypatch) -> None:
    job = {"job_id": "csj_2", "merchant_id": "merch_y", "connector": "shopify",
           "mode": "reconcile", "scope_json": {}}

    async def _get_job(_job_id):
        return dict(job)

    async def _claim(_job_id):
        return dict(job, status="running")

    async def _upsert(*_a, **_k):
        return None

    async def _sync(**_k):
        return {"products": 1}

    async def _boom(**_k):
        raise RuntimeError("quality queue down")

    monkeypatch.setattr(css, "get_catalog_sync_job", _get_job)
    monkeypatch.setattr(css, "claim_catalog_sync_job", _claim)
    monkeypatch.setattr(css, "_upsert_by_pk", _upsert)
    monkeypatch.setattr(css, "sync_products_cache_to_catalog", _sync)
    monkeypatch.setattr(qbf, "create_quality_backfill_job", _boom)

    # Best-effort hook: a failing enqueue must NOT fail the catalog sync.
    result = await css.run_catalog_sync_job("csj_2")
    assert result is not None


def test_quality_drain_function_importable() -> None:
    # Guards the scheduler tick target (audit_scheduler imports this).
    from services.product_quality_backfill_service import process_next_quality_backfill_job
    assert callable(process_next_quality_backfill_job)


def test_catalog_sync_drain_function_importable() -> None:
    # The step BEFORE the quality drain: guards the other scheduler tick target,
    # which is now the ONLY thing that runs a catalog ingest in production.
    from services.catalog_sync_drain import run_catalog_sync_drain_tick
    assert callable(run_catalog_sync_drain_tick)
