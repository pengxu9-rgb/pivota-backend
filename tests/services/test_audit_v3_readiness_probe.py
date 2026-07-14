"""WS-C: pre-pilot readiness probe — ready only when blocking deps present."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import services.merchant_audit_readiness as mar


def _patch_counts(monkeypatch, counts: Dict[str, int]) -> None:
    async def _fake_count(table, merchant_id, *, platform=None, where_extra=None):
        return counts.get(table, 0)
    monkeypatch.setattr(mar, "_table_count", _fake_count)


async def test_ready_when_catalog_and_quality_present(monkeypatch) -> None:
    _patch_counts(monkeypatch, {
        "catalog_products": 4, "products_cache": 4,
        "product_quality_snapshot": 4, "product_enrichment": 4,
    })
    r = await mar.assess_merchant_audit_readiness("m1")
    assert r["ready"] is True
    assert r["blocking_gaps"] == []
    assert r["enhancement_gaps"] == []


async def test_not_ready_when_quality_missing(monkeypatch) -> None:
    # Catalog present but no quality snapshot — the WS-A.1 backfill hasn't run.
    _patch_counts(monkeypatch, {
        "catalog_products": 4, "products_cache": 4,
        "product_quality_snapshot": 0, "product_enrichment": 4,
    })
    r = await mar.assess_merchant_audit_readiness("m1")
    assert r["ready"] is False
    assert any("product_quality_snapshot" in g for g in r["blocking_gaps"])


async def test_not_ready_when_catalog_empty(monkeypatch) -> None:
    _patch_counts(monkeypatch, {
        "catalog_products": 0, "products_cache": 0,
        "product_quality_snapshot": 0, "product_enrichment": 0,
    })
    r = await mar.assess_merchant_audit_readiness("m1")
    assert r["ready"] is False
    assert any("catalog_products" in g for g in r["blocking_gaps"])


async def test_enrichment_missing_is_enhancement_not_blocking(monkeypatch) -> None:
    # Blocking deps present; only enrichment missing -> ready, but flagged.
    _patch_counts(monkeypatch, {
        "catalog_products": 4, "products_cache": 4,
        "product_quality_snapshot": 4, "product_enrichment": 0,
    })
    r = await mar.assess_merchant_audit_readiness("m1")
    assert r["ready"] is True
    assert any("product_enrichment" in g for g in r["enhancement_gaps"])


async def test_enrichment_rows_without_content_elements_flagged(monkeypatch) -> None:
    # product_enrichment has rows, but they carry only a title_override (no content
    # elements). Row-count alone would say "enrichment present"; the element-aware
    # probe must still flag the gap (else it greenlights a title-only merchant).
    async def _fake_count(table, merchant_id, *, platform=None, where_extra=None):
        if table == "product_enrichment":
            if where_extra and "summary_short" in where_extra:
                return 0  # no row carries any content element
            return 4      # 4 enrichment rows exist (title_override-only)
        return {
            "catalog_products": 4, "products_cache": 4,
            "product_quality_snapshot": 4,
        }.get(table, 0)
    monkeypatch.setattr(mar, "_table_count", _fake_count)

    r = await mar.assess_merchant_audit_readiness("m1")
    # Enrichment is an enhancement, not a blocking dep — still audit-ready.
    assert r["ready"] is True
    assert r["counts"]["product_enrichment"] == 4
    assert r["counts"]["product_enrichment_with_content"] == 0
    gap = next((g for g in r["enhancement_gaps"] if "product_enrichment" in g), None)
    assert gap is not None
    # The title-only message, NOT the "empty" one.
    assert "no content elements" in gap
    assert "4 row(s)" in gap


async def test_enrichment_with_content_raises_no_gap(monkeypatch) -> None:
    # Rows that DO carry content elements -> no enrichment enhancement gap.
    async def _fake_count(table, merchant_id, *, platform=None, where_extra=None):
        if table == "product_enrichment":
            return 4  # both total and with-content return 4
        return {
            "catalog_products": 4, "products_cache": 4,
            "product_quality_snapshot": 4,
        }.get(table, 0)
    monkeypatch.setattr(mar, "_table_count", _fake_count)

    r = await mar.assess_merchant_audit_readiness("m1")
    assert r["ready"] is True
    assert not any("product_enrichment" in g for g in r["enhancement_gaps"])


async def test_recommend_store_sync_when_observed_only(monkeypatch) -> None:
    # Catalog is entirely observed URL-audit/seed rows, no connected store —
    # ready to audit, but recommend a sync for a first-party run. Mirrors the
    # real "merch_924…" (Anuko) shape: connected_store=0, observed>0.
    async def _fake_count(table, merchant_id, *, platform=None, where_extra=None):
        if table == "catalog_products" and where_extra:
            if "NOT IN" in where_extra:
                return 0  # connected_store_products
            if "external_referral" in where_extra:
                return 5  # observed_referral_products
            if "brand_authored" in where_extra:
                return 0  # brand_authored_products
        return {
            "catalog_products": 5, "products_cache": 0,
            "product_quality_snapshot": 5, "product_enrichment": 0,
        }.get(table, 0)
    monkeypatch.setattr(mar, "_table_count", _fake_count)

    r = await mar.assess_merchant_audit_readiness("m1")
    assert r["ready"] is True  # advisory only — never blocks
    assert r["recommend_store_sync"] is True
    assert r["provenance"]["connected_store_products"] == 0
    assert r["provenance"]["observed_referral_products"] == 5


async def test_no_recommend_when_store_connected(monkeypatch) -> None:
    # A merchant with real synced store rows should NOT get the sync nudge.
    async def _fake_count(table, merchant_id, *, platform=None, where_extra=None):
        if table == "catalog_products" and where_extra:
            if "NOT IN" in where_extra:
                return 763  # connected_store_products
            if "external_referral" in where_extra:
                return 0
            if "brand_authored" in where_extra:
                return 0
        return {
            "catalog_products": 763, "products_cache": 763,
            "product_quality_snapshot": 763, "product_enrichment": 763,
        }.get(table, 0)
    monkeypatch.setattr(mar, "_table_count", _fake_count)

    r = await mar.assess_merchant_audit_readiness("m1")
    assert r["recommend_store_sync"] is False
    assert r["provenance"]["connected_store_products"] == 763


async def test_count_refuses_non_whitelisted_table() -> None:
    import pytest
    with pytest.raises(ValueError):
        await mar._table_count("merchant_secrets; DROP TABLE x", "m1")


# --- backfill timing summary (portal "Preparing your catalog" countdown) ---

# 60s after the job started, deterministic clock for eta derivation.
_NOW = datetime(2026, 1, 1, 0, 1, 0, tzinfo=timezone.utc)


def test_derive_backfill_running_with_progress_has_eta() -> None:
    job = {
        "status": "running",
        "requested_at": "2026-01-01T00:00:00Z",
        "started_at": "2026-01-01T00:00:00Z",
        "total_candidates": 10,
        "processed": 2,
        "skipped": 0,
        "failed": 0,
    }
    s = mar._derive_backfill_summary(job, _NOW)
    assert s is not None
    assert s["status"] == "running"
    assert s["total_candidates"] == 10
    assert s["processed"] == 2
    assert s["progress_pct"] == 20
    # rate = 2 items / 60s; remaining 8 -> 8 / (2/60) = 240s
    assert s["eta_seconds"] == 240


def test_derive_backfill_queued_has_no_eta() -> None:
    job = {
        "status": "queued",
        "requested_at": "2026-01-01T00:00:00Z",
        "started_at": None,
        "total_candidates": 10,
        "processed": 0,
    }
    s = mar._derive_backfill_summary(job, _NOW)
    assert s is not None
    assert s["status"] == "queued"
    assert s["eta_seconds"] is None  # can't estimate a rate before it starts
    assert s["progress_pct"] == 0


def test_derive_backfill_eta_capped_at_one_hour() -> None:
    # Barely any progress over a long elapsed -> eta clamps to 3600.
    job = {
        "status": "running",
        "started_at": "2026-01-01T00:00:00Z",
        "total_candidates": 100000,
        "processed": 1,
    }
    s = mar._derive_backfill_summary(job, _NOW)
    assert s["eta_seconds"] == 3600


def test_derive_backfill_none_job_returns_none() -> None:
    assert mar._derive_backfill_summary(None, _NOW) is None


async def test_assess_includes_backfill_when_active_job(monkeypatch) -> None:
    _patch_counts(monkeypatch, {
        "catalog_products": 5, "products_cache": 5,
        "product_quality_snapshot": 0, "product_enrichment": 0,
    })

    async def _fake_job(merchant_id: str):
        return {
            "status": "running",
            "requested_at": "2026-01-01T00:00:00Z",
            "started_at": "2026-01-01T00:00:00Z",
            "total_candidates": 5,
            "processed": 1,
            "skipped": 0,
            "failed": 0,
        }
    monkeypatch.setattr(mar, "_get_active_backfill_job", _fake_job)

    r = await mar.assess_merchant_audit_readiness("m1")
    assert r["ready"] is False  # quality snapshot still missing
    assert r["backfill"] is not None
    assert r["backfill"]["status"] == "running"
    assert r["backfill"]["progress_pct"] == 20


async def test_assess_backfill_none_when_no_active_job(monkeypatch) -> None:
    _patch_counts(monkeypatch, {
        "catalog_products": 5, "products_cache": 5,
        "product_quality_snapshot": 5, "product_enrichment": 5,
    })

    async def _none(merchant_id: str):
        return None
    monkeypatch.setattr(mar, "_get_active_backfill_job", _none)

    r = await mar.assess_merchant_audit_readiness("m1")
    assert r["ready"] is True
    assert r["backfill"] is None


async def test_assess_backfill_lookup_failure_is_soft(monkeypatch) -> None:
    # A readiness verdict must never fail just because the backfill probe did.
    _patch_counts(monkeypatch, {
        "catalog_products": 5, "products_cache": 5,
        "product_quality_snapshot": 0, "product_enrichment": 0,
    })

    async def _boom(merchant_id: str):
        raise RuntimeError("db unavailable")
    monkeypatch.setattr(mar, "_get_active_backfill_job", _boom)

    r = await mar.assess_merchant_audit_readiness("m1")
    assert r["backfill"] is None
    assert r["ready"] is False
