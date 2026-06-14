"""Supplier evidence intake composes the verify→grade→serve pipeline.

The intake is the merchant-facing writer for the canonical record: INCI →
ingest_canonical_inci (precedence) → enrich_and_persist_product (substantiated,
drug-screened claims → evidence_profile) → refresh_agent_pdp_view (serve by
content_key). These tests pin the orchestration + the honest status mapping
without touching the DB.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from services import supplier_evidence_intake as sei


def _patch(monkeypatch, *, inci=None, enrich=None, refresh_ret=True, refresh_raises=False):
    calls: Dict[str, Any] = {}

    async def fake_inci(product_key, raw_inci, source, *, db=None, dry_run=False):
        calls["inci"] = {"product_key": product_key, "raw_inci": raw_inci, "source": source}
        return inci or {"status": "ok", "content_key": "ck-1"}

    async def fake_enrich(product_key, *, db=None, dry_run=False):
        calls["enrich"] = {"product_key": product_key}
        return enrich or {
            "status": "ok", "content_key": "ck-1", "category_kind": "skincare",
            "written": {"evidence_claims": True},
            "derived": {"substantiated_claims": ["Helps brighten and even the look of skin tone"]},
        }

    async def fake_refresh(content_key, *, refresh_source, db=None):
        calls["refresh"] = {"content_key": content_key, "refresh_source": refresh_source}
        if refresh_raises:
            raise RuntimeError("boom")
        return refresh_ret

    monkeypatch.setattr(sei, "ingest_canonical_inci", fake_inci)
    monkeypatch.setattr(sei, "enrich_and_persist_product", fake_enrich)
    monkeypatch.setattr(sei, "refresh_agent_pdp_view_for_content_key", fake_refresh)
    return calls


@pytest.mark.asyncio
async def test_no_inci_is_no_evidence(monkeypatch):
    calls = _patch(monkeypatch)
    out = await sei.ingest_supplier_evidence("m1|shopify|p1", raw_inci=None)
    assert out["status"] == "no_evidence"
    assert calls == {}  # nothing called


@pytest.mark.asyncio
async def test_happy_path_grades_and_serves(monkeypatch):
    calls = _patch(monkeypatch)
    out = await sei.ingest_supplier_evidence("m1|shopify|p1", raw_inci="Water, Niacinamide, Glycerin")
    assert out["status"] == "ok"
    assert out["content_key"] == "ck-1"
    assert out["wrote_evidence"] is True
    assert out["served"] is True
    assert "Helps brighten and even the look of skin tone" in out["substantiated_claims"]
    # INCI written under supplier_input precedence
    assert calls["inci"]["source"] == sei.SUPPLIER_SOURCE
    assert calls["enrich"]["product_key"] == "m1|shopify|p1"
    assert calls["refresh"]["content_key"] == "ck-1"


@pytest.mark.asyncio
async def test_not_found_short_circuits(monkeypatch):
    calls = _patch(monkeypatch, inci={"status": "not_found", "content_key": None})
    out = await sei.ingest_supplier_evidence("m1|shopify|missing", raw_inci="Water, Niacinamide")
    assert out["status"] == "not_found"
    assert "enrich" not in calls  # never enriched a missing product


@pytest.mark.asyncio
async def test_non_inci_rejected(monkeypatch):
    calls = _patch(monkeypatch, inci={"status": "rejected_not_inci", "content_key": None})
    out = await sei.ingest_supplier_evidence("m1|shopify|p1", raw_inci="just some marketing text")
    assert out["status"] == "rejected_not_inci"
    assert "enrich" not in calls


@pytest.mark.asyncio
async def test_serve_refresh_failure_is_best_effort(monkeypatch):
    _patch(monkeypatch, refresh_raises=True)
    out = await sei.ingest_supplier_evidence("m1|shopify|p1", raw_inci="Water, Niacinamide, Glycerin")
    # evidence still persisted; serving just didn't refresh
    assert out["status"] == "ok"
    assert out["wrote_evidence"] is True
    assert out["served"] is False
