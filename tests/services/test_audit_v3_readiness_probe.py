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


async def test_count_refuses_non_whitelisted_table() -> None:
    import pytest
    with pytest.raises(ValueError):
        await mar._table_count("merchant_secrets; DROP TABLE x", "m1")
