"""WS-C: pre-pilot readiness probe — ready only when blocking deps present."""
from __future__ import annotations

import sys
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


async def test_count_refuses_non_whitelisted_table() -> None:
    import pytest
    with pytest.raises(ValueError):
        await mar._table_count("merchant_secrets; DROP TABLE x", "m1")
