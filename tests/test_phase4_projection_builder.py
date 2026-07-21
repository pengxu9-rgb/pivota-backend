"""Phase 4.5 — audit_projection_builder tests.

Validates the 5-audience projection logic. Each builder is pure
(reads from in-memory lists, returns a dict) so tests are
straightforward.

Also tests the GET /api/audits/{id}?audience=X integration via
FastAPI TestClient.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# =====================================================================
# Test fixtures
# =====================================================================


def _sample_evidence() -> List[Dict[str, Any]]:
    return [
        {
            "evidence_id": "ev-1",
            "evidence_type": "grounding_chunk",
            "product_key": "p-1",
            "payload_jsonb": {
                "host": "forbes.com",
                "excerpt_text": "Best Greens Gummies: Test Brand",
                "query": "best wellness gummies",
            },
            "confidence": 90,
        },
        {
            "evidence_id": "ev-2",
            "evidence_type": "grounding_chunk",
            "payload_jsonb": {
                "host": "trailandkale.com",
                "excerpt_text": "[low-confidence quote]",
            },
            "confidence": 40,  # low; merchant projection should drop
        },
        {
            "evidence_id": "ev-3",
            "evidence_type": "competitor_mention",
            "payload_jsonb": {
                "host": "ag1.com", "times_cited": 5,
            },
            "confidence": 70,
        },
        {
            "evidence_id": "ev-4",
            "evidence_type": "url_match",
            "product_key": "p-1",
            "payload_jsonb": {
                "matched_url": "merchant.com/products/x",
            },
            "confidence": 90,
        },
    ]


def _sample_findings() -> List[Dict[str, Any]]:
    return [
        {
            "finding_id": "f-1",
            "finding_type": "merchant_visible_via_retailers_only",
            "severity": "high",
            "short_summary": "Visible via editorial but weak attribution.",
            "payload_jsonb": {"avg_visibility": 67, "avg_attribution": 18},
            "confidence": 85,
        },
        {
            "finding_id": "f-2",
            "finding_type": "category_visibility_low",
            "severity": "low",  # low-severity finding
            "short_summary": "Category visibility somewhat below average.",
            "payload_jsonb": {"avg_category_visibility": 38},
            "confidence": 80,
        },
    ]


def _sample_actions() -> List[Dict[str, Any]]:
    return [
        {
            "action_id": "a-1",
            "severity": "medium",
            "lever": "content_creation",
            "title": "Draft 3 publisher briefs",
            "body": "Aim for tier-1 editorial coverage",
            "owner": "merchant_brand_team",
            "kpi_to_track": "first-party citation rate",
            "expected_outcome": "1 of 3 queries cites merchant URL",
            "phase": "week_4_to_12",
            "depends_on": None,
        },
        {
            "action_id": "a-2",
            "severity": "critical",
            "lever": "pivota_integration",
            "title": "Complete Pivota integration",
            "phase": "week_1_to_4",
        },
        {
            "action_id": "a-3",
            "severity": "high",
            "lever": "sitemap_hygiene",
            "title": "Submit sitemap to GSC",
            # No phase
        },
    ]


def _sample_audit_run_row() -> Dict[str, Any]:
    return {
        "run_id": "run-1",
        "merchant_id": "merch-1",
        "verdict_labels": ["VISIBLE VIA RETAILERS"],
        "visibility_score_avg": 67,
        "attribution_score_avg": 18,
        "category_visibility_score_avg": 55,
        "cost_summary_jsonb": {
            "providers": [{"provider": "gemini", "cost_usd": 0.045}],
            "estimated_cost_usd": 0.045,
        },
        "requested_at": "2026-05-12T10:00:00+00:00",
        "completed_at": "2026-05-12T10:08:00+00:00",
    }


# =====================================================================
# Per-audience builders
# =====================================================================


def test_employee_bd_projection_includes_everything():
    """BD-employee gets the full report — evidence + findings +
    actions + cost detail."""
    from services.audit_projection_builder import (
        build_employee_bd_projection,
    )
    p = build_employee_bd_projection(
        evidence=_sample_evidence(),
        findings=_sample_findings(),
        actions=_sample_actions(),
        audit_run_row=_sample_audit_run_row(),
    )
    assert p["audience"] == "employee_bd"
    assert len(p["evidence"]) == 4
    assert len(p["findings"]) == 2
    assert len(p["actions"]) == 3
    assert p["scores"]["visibility_avg"] == 67
    # Cost detail visible to BD
    assert p["cost_summary"]["estimated_cost_usd"] == 0.045


def test_merchant_projection_filters_low_confidence_evidence():
    """Merchant-facing evidence quotes drop confidence < 60."""
    from services.audit_projection_builder import (
        build_merchant_projection,
    )
    p = build_merchant_projection(
        evidence=_sample_evidence(),
        findings=_sample_findings(),
        actions=_sample_actions(),
        audit_run_row=_sample_audit_run_row(),
    )
    # ev-1 (90 conf, host=forbes.com) and ev-2 (40 conf) are the
    # grounding_chunks. Only ev-1 makes the cut.
    quote_hosts = [q["host"] for q in p["evidence_quotes"]]
    assert "forbes.com" in quote_hosts
    assert "trailandkale.com" not in quote_hosts


def test_merchant_projection_hides_low_severity_findings():
    """Merchant projection shows critical/high/medium findings
    only — low-severity ('FYI') findings are suppressed."""
    from services.audit_projection_builder import (
        build_merchant_projection,
    )
    p = build_merchant_projection(
        evidence=[], findings=_sample_findings(),
        actions=[], audit_run_row=_sample_audit_run_row(),
    )
    finding_types = {
        f["type"] for f in p["findings_summary"]
    }
    # f-1 (high) included; f-2 (low) dropped
    assert "merchant_visible_via_retailers_only" in finding_types
    assert "category_visibility_low" not in finding_types


def test_merchant_projection_sorts_actions_critical_first():
    """The action_queue must show critical actions before high
    before medium. Within the same severity, earlier phases first."""
    from services.audit_projection_builder import (
        build_merchant_projection,
    )
    p = build_merchant_projection(
        evidence=[], findings=[],
        actions=_sample_actions(),
        audit_run_row=_sample_audit_run_row(),
    )
    order = [a["title"] for a in p["action_queue"]]
    # a-2 (critical) first, then a-3 (high), then a-1 (medium)
    assert order[0] == "Complete Pivota integration"
    assert order[1] == "Submit sitemap to GSC"
    assert order[2] == "Draft 3 publisher briefs"


def test_merchant_projection_hides_cost_detail():
    """Cost detail must not appear in the merchant projection.
    Per privacy: merchants shouldn't see Pivota's per-call cost."""
    from services.audit_projection_builder import (
        build_merchant_projection,
    )
    p = build_merchant_projection(
        evidence=[], findings=[], actions=[],
        audit_run_row=_sample_audit_run_row(),
    )
    # Cost should not appear anywhere in the merchant payload.
    payload_str = str(p)
    assert "cost_summary" not in p
    assert "estimated_cost_usd" not in payload_str


def test_internal_ops_projection_counts_by_category():
    """Internal ops view is mostly counters — evidence_count,
    findings_by_severity, etc."""
    from services.audit_projection_builder import (
        build_internal_ops_projection,
    )
    p = build_internal_ops_projection(
        evidence=_sample_evidence(),
        findings=_sample_findings(),
        actions=_sample_actions(),
        audit_run_row=_sample_audit_run_row(),
    )
    assert p["evidence_count"] == 4
    assert p["findings_count"] == 2
    assert p["actions_count"] == 3
    assert p["evidence_by_type"]["grounding_chunk"] == 2
    assert p["findings_by_severity"]["high"] == 1
    assert p["findings_by_severity"]["low"] == 1
    assert p["actions_by_severity"]["critical"] == 1
    # Cost stays in internal ops (it's the dashboard for it)
    assert p["cost_summary"]["estimated_cost_usd"] == 0.045


def test_pivota_pdp_feed_includes_only_host_bearing_citations():
    """PDP feed's citation list filters to evidence with a host
    (grounding_chunk + competitor_mention). url_match rows lack
    a host."""
    from services.audit_projection_builder import (
        build_pivota_pdp_feed_projection,
    )
    p = build_pivota_pdp_feed_projection(
        evidence=_sample_evidence(),
        findings=[], actions=[],
        audit_run_row=_sample_audit_run_row(),
    )
    hosts = sorted(c["host"] for c in p["citations"])
    # ev-1 (forbes.com), ev-2 (trailandkale.com), ev-3 (ag1.com)
    assert hosts == ["ag1.com", "forbes.com", "trailandkale.com"]
    # ev-4 (url_match, no host) excluded
    assert len(p["citations"]) == 3


def test_frontend_agent_feed_pairs_claims_with_evidence():
    """Frontend agent feed is structured as claim-evidence pairs
    so a downstream LLM can verify Pivota's claims."""
    from services.audit_projection_builder import (
        build_frontend_agent_feed_projection,
    )
    p = build_frontend_agent_feed_projection(
        evidence=_sample_evidence(),
        findings=_sample_findings(),
        actions=[],
        audit_run_row=_sample_audit_run_row(),
    )
    # Each finding becomes a claim
    assert len(p["claims"]) == 2
    assert p["claims"][0]["claim_type"] == "merchant_visible_via_retailers_only"
    # Evidence shows up in the supporting_evidence array (full
    # set — LLM picks what it needs)
    assert len(p["supporting_evidence"]) == 4


def test_build_projection_dispatcher_routes_correctly():
    """build_projection() dispatches to the right builder per
    audience string. Unknown audience → None."""
    from services.audit_projection_builder import build_projection
    p = build_projection(
        audience="merchant",
        evidence=[], findings=[], actions=[],
        audit_run_row=_sample_audit_run_row(),
    )
    assert p is not None
    assert p["audience"] == "merchant"

    # Unknown
    p2 = build_projection(
        audience="nonexistent",
        evidence=[], findings=[], actions=[],
    )
    assert p2 is None


# =====================================================================
# GET /api/audits/{id}?audience=X integration
# =====================================================================


class _ProjStub:
    def __init__(self):
        self.fetch_returns: Optional[Dict[str, Any]] = None
        self.audit_row: Optional[Dict[str, Any]] = None

    async def fetch_projection(self, *, audit_run_id, audience):
        return self.fetch_returns

    async def fetch_audit(self, *, run_id):
        return self.audit_row


@pytest.fixture
def proj_stub():
    return _ProjStub()


@pytest.fixture
def proj_client(proj_stub, monkeypatch):
    from routes import audit_runs_routes
    from db import audit_evidence as ae
    from utils import auth as auth_module

    monkeypatch.setattr(
        audit_runs_routes, "fetch_audit_run_by_id",
        proj_stub.fetch_audit,
    )
    monkeypatch.setattr(
        ae, "fetch_projection", proj_stub.fetch_projection,
    )

    app = FastAPI()
    app.include_router(audit_runs_routes.router)
    app.dependency_overrides[auth_module.get_current_merchant] = (
        lambda: "merch-A"
    )
    return TestClient(app)


def test_get_with_audience_returns_cached_projection(
    proj_client, proj_stub,
):
    """When ?audience=merchant is set + the projection is cached,
    GET returns the projection payload directly."""
    proj_stub.audit_row = {
        "run_id": "r-1", "merchant_id": "merch-A",
        "stage": "completed", "verdict_labels": [],
        "product_keys": [],
        "audited_via_pivota_canonical": [],
        "subject_type": "merchant",
    }
    proj_stub.fetch_returns = {
        "payload_jsonb": {
            "audience": "merchant",
            "headline_score": 67,
            "action_queue": [],
        }
    }
    res = proj_client.get("/api/audits/r-1?audience=merchant")
    assert res.status_code == 200
    body = res.json()
    assert body["audience"] == "merchant"
    assert body["headline_score"] == 67


def test_get_with_audience_409_when_projection_not_built(
    proj_client, proj_stub,
):
    """When the audit isn't at stage=completed yet, the
    projection hasn't been built. Return 409 with fallback hint."""
    proj_stub.audit_row = {
        "run_id": "r-1", "merchant_id": "merch-A",
        "stage": "probing",
        "verdict_labels": [], "product_keys": [],
        "audited_via_pivota_canonical": [],
        "subject_type": "merchant",
    }
    proj_stub.fetch_returns = None  # not yet cached
    res = proj_client.get("/api/audits/r-1?audience=merchant")
    assert res.status_code == 409
    body = res.json()
    assert body["detail"]["current_stage"] == "probing"
    assert "fallback" in body["detail"]


def test_get_with_invalid_audience_returns_422(proj_client, proj_stub):
    proj_stub.audit_row = {
        "run_id": "r-1", "merchant_id": "merch-A",
        "stage": "completed", "verdict_labels": [],
        "product_keys": [],
        "audited_via_pivota_canonical": [],
        "subject_type": "merchant",
    }
    res = proj_client.get("/api/audits/r-1?audience=hackerman")
    assert res.status_code == 422
    assert "Unknown audience" in res.json()["detail"]


def test_get_without_audience_still_returns_canonical_shape(
    proj_client, proj_stub,
):
    """No regression on the P2.3 contract — omitting ?audience
    returns the AuditRunDetail shape as before."""
    proj_stub.audit_row = {
        "run_id": "r-1", "merchant_id": "merch-A",
        "stage": "completed", "verdict_labels": ["X"],
        "product_keys": ["pk-1"],
        "audited_via_pivota_canonical": [],
        "subject_type": "merchant",
        "stage_updated_at": "2026-05-12T10:00:00+00:00",
        "requested_at": "2026-05-12T09:00:00+00:00",
        "completed_at": "2026-05-12T10:00:00+00:00",
        "cancelled_at": None,
        "visibility_score_avg": 50,
        "attribution_score_avg": 25,
        "category_visibility_score_avg": 60,
        "partial_result_jsonb": None,
        "report_jsonb": {"merchant_name": "X"},
        "cost_summary_jsonb": None,
        "error_jsonb": None,
        "error_message": None,
        "idempotency_key": None,
    }
    res = proj_client.get("/api/audits/r-1")
    assert res.status_code == 200
    body = res.json()
    assert body["run_id"] == "r-1"
    assert body["report_jsonb"]["merchant_name"] == "X"
