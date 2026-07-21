"""Phase 4.2 — audit_evidence accessors + taxonomy tests.

Pure-logic tests for the validation helpers + taxonomy constants.
DB round-trip is documented skip (same rationale as P2.1: SQLAlchemy
JSONB + ARRAY types + partial indexes don't round-trip cleanly on
SQLite — verified against real Postgres in the P4.3+ dual-write flow).
"""

from __future__ import annotations

import pytest


# =====================================================================
# Taxonomy constants — sanity
# =====================================================================


def test_evidence_taxonomy_constants_match_valid_set():
    from db.audit_evidence import (
        VALID_EVIDENCE_TYPES,
        EVIDENCE_TYPE_GROUNDING_CHUNK,
        EVIDENCE_TYPE_COMPETITOR_MENTION,
        EVIDENCE_TYPE_URL_MATCH,
        EVIDENCE_TYPE_MISSING_SIGNAL,
        EVIDENCE_TYPE_INDUSTRY_STAT,
        EVIDENCE_TYPE_CUSTOM,
    )
    assert EVIDENCE_TYPE_GROUNDING_CHUNK in VALID_EVIDENCE_TYPES
    assert EVIDENCE_TYPE_COMPETITOR_MENTION in VALID_EVIDENCE_TYPES
    assert EVIDENCE_TYPE_URL_MATCH in VALID_EVIDENCE_TYPES
    assert EVIDENCE_TYPE_MISSING_SIGNAL in VALID_EVIDENCE_TYPES
    assert EVIDENCE_TYPE_INDUSTRY_STAT in VALID_EVIDENCE_TYPES
    assert EVIDENCE_TYPE_CUSTOM in VALID_EVIDENCE_TYPES
    assert len(VALID_EVIDENCE_TYPES) == 6


def test_severity_constants_canonicalized():
    from db.audit_evidence import (
        VALID_SEVERITIES,
        SEVERITY_CRITICAL, SEVERITY_HIGH,
        SEVERITY_MEDIUM, SEVERITY_LOW,
    )
    assert VALID_SEVERITIES == {
        SEVERITY_CRITICAL, SEVERITY_HIGH,
        SEVERITY_MEDIUM, SEVERITY_LOW,
    }


def test_owner_taxonomy_mirrors_pr_8b_recommendation_engine():
    """PR-8b's recommendation engine v2 defined owner values:
    pivota_ops, merchant_brand_team, merchant_growth_team,
    merchant_tech_team, joint. The action_plan_items owner
    column must accept all 5."""
    from db.audit_evidence import (
        VALID_OWNERS,
        OWNER_PIVOTA_OPS, OWNER_MERCHANT_BRAND,
        OWNER_MERCHANT_GROWTH, OWNER_MERCHANT_TECH, OWNER_JOINT,
    )
    assert VALID_OWNERS == {
        OWNER_PIVOTA_OPS, OWNER_MERCHANT_BRAND,
        OWNER_MERCHANT_GROWTH, OWNER_MERCHANT_TECH, OWNER_JOINT,
    }


def test_verifier_taxonomy_has_all_7_phase5_verifiers():
    """The 7-stage verify loop documented in the audit doc:
    pdp_renders, pdp_in_sitemap, gsc_url_submitted,
    gsc_indexing_status, pivota_internal_retrieval,
    frontend_agent_cite, public_llm_citation_movement."""
    from db.audit_evidence import (
        VALID_VERIFIERS,
        VERIFIER_PDP_RENDERS, VERIFIER_PDP_IN_SITEMAP,
        VERIFIER_GSC_URL_SUBMITTED,
        VERIFIER_GSC_INDEXING_STATUS,
        VERIFIER_PIVOTA_INTERNAL_RETRIEVAL,
        VERIFIER_FRONTEND_AGENT_CITE,
        VERIFIER_PUBLIC_LLM_CITATION,
    )
    expected = {
        VERIFIER_PDP_RENDERS, VERIFIER_PDP_IN_SITEMAP,
        VERIFIER_GSC_URL_SUBMITTED,
        VERIFIER_GSC_INDEXING_STATUS,
        VERIFIER_PIVOTA_INTERNAL_RETRIEVAL,
        VERIFIER_FRONTEND_AGENT_CITE,
        VERIFIER_PUBLIC_LLM_CITATION,
    }
    assert VALID_VERIFIERS == expected
    assert len(VALID_VERIFIERS) == 7


def test_audience_taxonomy_has_all_5_projections():
    """The 5-audience projection layer documented in the
    implementation plan."""
    from db.audit_evidence import (
        VALID_AUDIENCES,
        AUDIENCE_EMPLOYEE_BD, AUDIENCE_MERCHANT,
        AUDIENCE_INTERNAL_OPS, AUDIENCE_PIVOTA_PDP_FEED,
        AUDIENCE_FRONTEND_AGENT_FEED,
    )
    assert VALID_AUDIENCES == {
        AUDIENCE_EMPLOYEE_BD, AUDIENCE_MERCHANT,
        AUDIENCE_INTERNAL_OPS, AUDIENCE_PIVOTA_PDP_FEED,
        AUDIENCE_FRONTEND_AGENT_FEED,
    }
    assert len(VALID_AUDIENCES) == 5


# =====================================================================
# Validation helpers — pure logic
# =====================================================================


def test_coerce_evidence_type_passes_through_valid():
    from db.audit_evidence import (
        _coerce_evidence_type, EVIDENCE_TYPE_GROUNDING_CHUNK,
    )
    assert _coerce_evidence_type(EVIDENCE_TYPE_GROUNDING_CHUNK) == (
        EVIDENCE_TYPE_GROUNDING_CHUNK
    )


def test_coerce_evidence_type_falls_back_to_custom_for_unknown():
    """Forward compat: agents that emit a new evidence_type before
    the taxonomy gets updated still produce a row; the original
    value is preserved in payload_jsonb._raw_type (tested in the
    write accessor when DB is available)."""
    from db.audit_evidence import (
        _coerce_evidence_type, EVIDENCE_TYPE_CUSTOM,
    )
    assert _coerce_evidence_type("brand_new_type") == EVIDENCE_TYPE_CUSTOM
    assert _coerce_evidence_type("") == EVIDENCE_TYPE_CUSTOM


def test_coerce_severity_defaults_to_medium():
    """Unknown / None / empty severity falls back to medium. This
    matches the DB column's server_default; ensures consistency
    across write paths."""
    from db.audit_evidence import _coerce_severity, SEVERITY_MEDIUM
    assert _coerce_severity(None) == SEVERITY_MEDIUM
    assert _coerce_severity("garbage") == SEVERITY_MEDIUM
    assert _coerce_severity("") == SEVERITY_MEDIUM
    assert _coerce_severity("high") == "high"


def test_coerce_owner_accepts_unknown_for_evolution_friendly():
    """Unknown owner values are stored as-given (not coerced).
    Rationale: the taxonomy is documented but evolving — new owner
    types added to the recommendation engine should NOT require a
    backfill on existing data. Strict validation would lose new
    types in dual-write scenarios."""
    from db.audit_evidence import (
        _coerce_owner, OWNER_PIVOTA_OPS,
    )
    assert _coerce_owner(None) is None
    assert _coerce_owner(OWNER_PIVOTA_OPS) == OWNER_PIVOTA_OPS
    # Unknown stored as-is
    assert _coerce_owner("future_team") == "future_team"


# =====================================================================
# DB round-trip — skipped (same rationale as P2.1/P3.1)
# =====================================================================


@pytest.mark.skip(
    reason="Round-trip integration via in-memory SQLite is fragile "
    "for the SQLAlchemy ARRAY/JSONB-typed Tables + partial-index "
    "surface this module uses (matches the existing skip rationale "
    "in test_phase2_audit_runs_lifecycle.py). The accessors are "
    "exercised against real Postgres in the P4.3+ dual-write flow "
    "that lands once the BD report service is wired."
)
async def test_round_trip_postgres_only():
    pass
