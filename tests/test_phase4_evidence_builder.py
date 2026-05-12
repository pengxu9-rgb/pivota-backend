"""Phase 4.3 — audit_evidence_builder tests.

Validates the pure extraction logic that derives evidence_items +
readiness_findings from the legacy brand_report shape. Persistence
is tested via monkey-patched accessors.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")


# =====================================================================
# Test fixtures — minimal brand_report shapes
# =====================================================================


def _gruns_like_report() -> Dict[str, Any]:
    """A minimal brand_report mimicking the Grüns archetype:
    visible-via-editorial + weak first-party attribution + medium
    category visibility. The hand-written Grüns report led with
    the paradox framing finding this should extract."""
    return {
        "merchant_name": "Test Brand",
        "aggregate": {
            "avg_visibility": 67,
            "avg_attribution": 18,
            "avg_category_visibility": 55,
            "brand_verdict_label": "VISIBLE VIA RETAILERS",
            "products_succeeded": 3,
            "products_failed": 0,
        },
        "per_product": [
            {
                "product_key": "shopify::sp-1",
                "merchant_view": {
                    "headline": {
                        "audited_via_pivota_canonical": False,
                    },
                    "tracking": {
                        "integration_state": {"phase_0_complete": True},
                    },
                },
                "evidence_quotes": [
                    {
                        "query": "best wellness gummies 2026",
                        "source_host": "forbes.com",
                        "source_title": "Best Greens Gummies",
                        "excerpt_text": "Best Green Gummies: Test Brand Greens Gummies.",
                        "attribution_path": "merchant_named",
                    },
                ],
                "raw": {
                    "merchant_store_attribution": {
                        "raw_runs": [
                            {
                                "query": "where to buy test brand",
                                "url_match": {
                                    "matched": True,
                                    "matched_url": "testbrand.com/products/x",
                                    "matched_in": "raw_text",
                                },
                            },
                        ],
                    },
                },
            },
        ],
        "cross_product_competitors": [
            {"host": "forbes.com", "times_cited": 5},
            {"host": "trailandkale.com", "times_cited": 3},
        ],
    }


# =====================================================================
# Evidence extraction
# =====================================================================


def test_extract_evidence_handles_empty_or_invalid_input():
    from services.audit_evidence_builder import extract_evidence_items
    assert extract_evidence_items({}) == []
    assert extract_evidence_items(None) == []
    assert extract_evidence_items("not a dict") == []


def test_extract_evidence_grounding_chunks_from_evidence_quotes():
    """PR-7e evidence_quotes → grounding_chunk evidence rows. One
    per quote; host-bearing quotes get higher confidence."""
    from services.audit_evidence_builder import (
        extract_evidence_items, CONFIDENCE_EVIDENCE_HIGH,
    )
    items = extract_evidence_items(_gruns_like_report())
    grounding = [
        i for i in items if i["evidence_type"] == "grounding_chunk"
    ]
    assert len(grounding) == 1
    g = grounding[0]
    assert g["payload"]["host"] == "forbes.com"
    assert "Test Brand Greens Gummies" in g["payload"]["excerpt_text"]
    assert g["product_key"] == "shopify::sp-1"
    assert g["confidence"] == CONFIDENCE_EVIDENCE_HIGH


def test_extract_evidence_competitor_mentions_from_cross_product():
    """Cross-product competitor list → competitor_mention rows.
    Brand-level evidence (no product_key)."""
    from services.audit_evidence_builder import extract_evidence_items
    items = extract_evidence_items(_gruns_like_report())
    comps = [
        i for i in items if i["evidence_type"] == "competitor_mention"
    ]
    assert len(comps) == 2
    hosts = sorted(c["payload"]["host"] for c in comps)
    assert hosts == ["forbes.com", "trailandkale.com"]
    for c in comps:
        assert c["product_key"] is None  # brand-level


def test_extract_evidence_url_match_from_attribution_runs():
    """Per-product attribution.raw_runs[*].url_match.matched=True
    → url_match evidence."""
    from services.audit_evidence_builder import extract_evidence_items
    items = extract_evidence_items(_gruns_like_report())
    matches = [i for i in items if i["evidence_type"] == "url_match"]
    assert len(matches) == 1
    m = matches[0]
    assert m["payload"]["matched_url"] == "testbrand.com/products/x"
    assert m["product_key"] == "shopify::sp-1"


def test_extract_evidence_skips_quotes_with_empty_excerpt():
    """Quotes with no excerpt_text are not insertable evidence."""
    from services.audit_evidence_builder import extract_evidence_items
    report = {
        "per_product": [{
            "product_key": "p-1",
            "evidence_quotes": [
                {"source_host": "x.com", "excerpt_text": ""},
                {"source_host": "y.com"},  # no excerpt at all
            ],
        }],
    }
    items = extract_evidence_items(report)
    grounding = [
        i for i in items if i["evidence_type"] == "grounding_chunk"
    ]
    assert grounding == []


# =====================================================================
# Finding extraction
# =====================================================================


def test_extract_paradox_finding_when_visible_via_retailers_with_weak_attribution():
    """The Grüns archetype: 'VISIBLE VIA RETAILERS' verdict +
    avg_attribution < 30 → merchant_visible_via_retailers_only
    finding. Drives PR-8a executive summary paradox framing."""
    from services.audit_evidence_builder import extract_findings
    findings = extract_findings(_gruns_like_report())
    paradox = [
        f for f in findings
        if f["finding_type"] == "merchant_visible_via_retailers_only"
    ]
    assert len(paradox) == 1
    assert paradox[0]["severity"] == "high"
    assert paradox[0]["payload"]["avg_visibility"] == 67
    assert paradox[0]["payload"]["avg_attribution"] == 18


def test_paradox_finding_not_emitted_when_attribution_is_strong():
    """If avg_attribution is healthy (>=30), no paradox — the
    brand is BOTH visible AND attributable."""
    from services.audit_evidence_builder import extract_findings
    report = _gruns_like_report()
    report["aggregate"]["avg_attribution"] = 65
    findings = extract_findings(report)
    paradox = [
        f for f in findings
        if f["finding_type"] == "merchant_visible_via_retailers_only"
    ]
    assert paradox == []


def test_extract_category_visibility_low_finding():
    """avg_category_visibility < 40 fires the
    category_visibility_low finding. Severity escalates to high
    when below 20."""
    from services.audit_evidence_builder import extract_findings
    report = _gruns_like_report()
    report["aggregate"]["avg_category_visibility"] = 15
    findings = extract_findings(report)
    cat = [
        f for f in findings if f["finding_type"] == "category_visibility_low"
    ]
    assert len(cat) == 1
    assert cat[0]["severity"] == "high"  # avg < 20

    # Below 40 but >= 20 → medium
    report["aggregate"]["avg_category_visibility"] = 30
    findings = extract_findings(report)
    cat = [
        f for f in findings if f["finding_type"] == "category_visibility_low"
    ]
    assert cat[0]["severity"] == "medium"


def test_extract_first_party_indexing_gap_when_pivota_canonical_used():
    """When any product was audited against the Pivota canonical
    PDP (merchant URL unavailable), fire
    first_party_pdp_indexing_gap finding."""
    from services.audit_evidence_builder import extract_findings
    report = _gruns_like_report()
    # Mark one product as audited via Pivota canonical
    report["per_product"][0]["merchant_view"]["headline"][
        "audited_via_pivota_canonical"
    ] = True
    findings = extract_findings(report)
    gap = [
        f for f in findings
        if f["finding_type"] == "first_party_pdp_indexing_gap"
    ]
    assert len(gap) == 1
    assert gap[0]["payload"]["products_audited_via_pivota_canonical"] == 1


def test_extract_integration_incomplete_finding():
    """When merchant_view.tracking.integration_state.phase_0_complete
    is explicitly False, fire integration_state_incomplete with
    critical severity."""
    from services.audit_evidence_builder import extract_findings
    report = _gruns_like_report()
    report["per_product"][0]["merchant_view"]["tracking"][
        "integration_state"
    ] = {"phase_0_complete": False, "pivota_app_installed": False}
    findings = extract_findings(report)
    integration = [
        f for f in findings
        if f["finding_type"] == "integration_state_incomplete"
    ]
    assert len(integration) == 1
    assert integration[0]["severity"] == "critical"


def test_extract_findings_handles_empty_input():
    from services.audit_evidence_builder import extract_findings
    assert extract_findings({}) == []
    assert extract_findings(None) == []


# =====================================================================
# persist_canonical_evidence — integration via monkey-patched accessors
# =====================================================================


@pytest.mark.asyncio
async def test_persist_canonical_evidence_calls_accessors_per_extracted_item(
    monkeypatch,
):
    """Each extracted evidence + finding should result in one
    accessor call. Counts match between extraction and persistence."""
    from services import audit_evidence_builder as builder
    import db.audit_evidence as ae

    inserted_evidence: List[Dict[str, Any]] = []
    inserted_findings: List[Dict[str, Any]] = []

    async def fake_insert_evidence(**kwargs):
        inserted_evidence.append(kwargs)
        return f"ev-{len(inserted_evidence)}"

    async def fake_insert_finding(**kwargs):
        inserted_findings.append(kwargs)
        return f"f-{len(inserted_findings)}"

    monkeypatch.setattr(ae, "insert_evidence_item", fake_insert_evidence)
    monkeypatch.setattr(ae, "insert_finding", fake_insert_finding)

    summary = await builder.persist_canonical_evidence(
        audit_run_id="audit-1",
        brand_report=_gruns_like_report(),
    )

    # Same count: 1 grounding_chunk + 2 competitor_mentions + 1 url_match = 4
    assert summary["evidence_items_inserted"] == 4
    assert summary["evidence_items_failed"] == 0
    # 1 paradox finding + 0 category_low (55 is above threshold) = 1
    assert summary["findings_inserted"] == 1
    assert summary["findings_failed"] == 0
    # Every accessor call used audit-1
    for call in inserted_evidence + inserted_findings:
        assert call["audit_run_id"] == "audit-1"


@pytest.mark.asyncio
async def test_persist_canonical_evidence_counts_failures(monkeypatch):
    """When the accessor returns None (persistence failure), the
    builder counts it in *_failed rather than crashing."""
    from services import audit_evidence_builder as builder
    import db.audit_evidence as ae

    async def fake_insert_evidence(**kwargs):
        return None  # simulate persistence failure

    async def fake_insert_finding(**kwargs):
        return None

    monkeypatch.setattr(ae, "insert_evidence_item", fake_insert_evidence)
    monkeypatch.setattr(ae, "insert_finding", fake_insert_finding)

    summary = await builder.persist_canonical_evidence(
        audit_run_id="audit-2",
        brand_report=_gruns_like_report(),
    )
    assert summary["evidence_items_inserted"] == 0
    assert summary["evidence_items_failed"] == 4
    assert summary["findings_inserted"] == 0
    assert summary["findings_failed"] == 1


@pytest.mark.asyncio
async def test_persist_swallows_accessor_exceptions(monkeypatch):
    """An exception inside the accessor (vs. just returning None)
    must be caught + counted, not propagated. The audit lifecycle
    can't fail because canonical-evidence persistence had a bug."""
    from services import audit_evidence_builder as builder
    import db.audit_evidence as ae

    async def boom(**kwargs):
        raise RuntimeError("accessor exploded")

    monkeypatch.setattr(ae, "insert_evidence_item", boom)
    monkeypatch.setattr(ae, "insert_finding", boom)

    summary = await builder.persist_canonical_evidence(
        audit_run_id="audit-3",
        brand_report=_gruns_like_report(),
    )
    # All extracted items get counted as failed
    assert summary["evidence_items_inserted"] == 0
    assert summary["evidence_items_failed"] >= 1
