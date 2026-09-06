"""Phase 4.2 — audit_evidence accessors + taxonomy tests.

Pure-logic tests for the validation helpers + taxonomy constants.
DB round-trip is documented skip (same rationale as P2.1: SQLAlchemy
JSONB + ARRAY types + partial indexes don't round-trip cleanly on
SQLite — verified against real Postgres in the P4.3+ dual-write flow).
"""

from __future__ import annotations

import asyncio

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
        EVIDENCE_TYPE_ACCEPTANCE_SIGNAL,
        EVIDENCE_TYPE_COMMERCE_PLATFORM,
        EVIDENCE_TYPE_COMMERCE_CHECKOUT_ROUTE,
        EVIDENCE_TYPE_COMMERCE_CARTABILITY,
        EVIDENCE_TYPE_COMMERCE_INTEGRATION_AUTHORIZATION,
        EVIDENCE_TYPE_COMMERCE_RETURN_POLICY,
        EVIDENCE_TYPE_COMMERCE_AFTER_SALES_REVIEW,
        EVIDENCE_TYPE_CUSTOM,
    )
    assert EVIDENCE_TYPE_GROUNDING_CHUNK in VALID_EVIDENCE_TYPES
    assert EVIDENCE_TYPE_COMPETITOR_MENTION in VALID_EVIDENCE_TYPES
    assert EVIDENCE_TYPE_URL_MATCH in VALID_EVIDENCE_TYPES
    assert EVIDENCE_TYPE_MISSING_SIGNAL in VALID_EVIDENCE_TYPES
    assert EVIDENCE_TYPE_INDUSTRY_STAT in VALID_EVIDENCE_TYPES
    assert EVIDENCE_TYPE_ACCEPTANCE_SIGNAL in VALID_EVIDENCE_TYPES
    assert EVIDENCE_TYPE_COMMERCE_PLATFORM in VALID_EVIDENCE_TYPES
    assert EVIDENCE_TYPE_COMMERCE_CHECKOUT_ROUTE in VALID_EVIDENCE_TYPES
    assert EVIDENCE_TYPE_COMMERCE_CARTABILITY in VALID_EVIDENCE_TYPES
    assert EVIDENCE_TYPE_COMMERCE_INTEGRATION_AUTHORIZATION in VALID_EVIDENCE_TYPES
    assert EVIDENCE_TYPE_COMMERCE_RETURN_POLICY in VALID_EVIDENCE_TYPES
    assert EVIDENCE_TYPE_COMMERCE_AFTER_SALES_REVIEW in VALID_EVIDENCE_TYPES
    assert EVIDENCE_TYPE_CUSTOM in VALID_EVIDENCE_TYPES
    assert len(VALID_EVIDENCE_TYPES) == 13


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


def test_verifier_taxonomy_has_all_registered_verifiers():
    """The legacy 7-stage verify loop plus Store Audit UCP probing:
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
        VERIFIER_UCP_PROBE,
        VERIFIER_COMMERCE_CHECKOUT_PROBE,
    )
    expected = {
        VERIFIER_PDP_RENDERS, VERIFIER_PDP_IN_SITEMAP,
        VERIFIER_GSC_URL_SUBMITTED,
        VERIFIER_GSC_INDEXING_STATUS,
        VERIFIER_PIVOTA_INTERNAL_RETRIEVAL,
        VERIFIER_FRONTEND_AGENT_CITE,
        VERIFIER_PUBLIC_LLM_CITATION,
        VERIFIER_UCP_PROBE,
        VERIFIER_COMMERCE_CHECKOUT_PROBE,
    }
    assert VALID_VERIFIERS == expected
    assert len(VALID_VERIFIERS) == 9


def test_audience_taxonomy_is_exactly_the_seven_projections():
    """The 5-audience layer from the implementation plan, plus C2's two.

    The exact-set assertion is the point: this frozenset is a permission
    boundary, and a new audience appearing in it without a deliberate edit
    here is a new shape someone can read. Note which list each one is on —
    PUBLIC_ALLOWED_AUDIENCES is the unauthenticated surface.
    """
    from db.audit_evidence import (
        VALID_AUDIENCES,
        MERCHANT_ALLOWED_AUDIENCES, PUBLIC_ALLOWED_AUDIENCES,
        AUDIENCE_EMPLOYEE_BD, AUDIENCE_MERCHANT,
        AUDIENCE_INTERNAL_OPS, AUDIENCE_PIVOTA_PDP_FEED,
        AUDIENCE_FRONTEND_AGENT_FEED,
        AUDIENCE_REVENUE_RECOVERY, AUDIENCE_PUBLIC_ANONYMOUS,
    )
    assert VALID_AUDIENCES == {
        AUDIENCE_EMPLOYEE_BD, AUDIENCE_MERCHANT,
        AUDIENCE_INTERNAL_OPS, AUDIENCE_PIVOTA_PDP_FEED,
        AUDIENCE_FRONTEND_AGENT_FEED,
        AUDIENCE_REVENUE_RECOVERY, AUDIENCE_PUBLIC_ANONYMOUS,
    }
    assert len(VALID_AUDIENCES) == 7
    # Who may read what: merchants get two, anonymous readers get exactly one,
    # and the four internal audiences are on neither list.
    assert MERCHANT_ALLOWED_AUDIENCES == {
        AUDIENCE_MERCHANT, AUDIENCE_REVENUE_RECOVERY,
    }
    assert PUBLIC_ALLOWED_AUDIENCES == {AUDIENCE_PUBLIC_ANONYMOUS}
    assert not (MERCHANT_ALLOWED_AUDIENCES | PUBLIC_ALLOWED_AUDIENCES) & {
        AUDIENCE_EMPLOYEE_BD, AUDIENCE_INTERNAL_OPS,
        AUDIENCE_PIVOTA_PDP_FEED, AUDIENCE_FRONTEND_AGENT_FEED,
    }


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


def test_coerce_evidence_type_rejects_unknown():
    """Unknown values must fail before they can silently become custom."""
    from db.audit_evidence import (
        _coerce_evidence_type, UnknownAuditTaxonomyValue,
    )
    with pytest.raises(UnknownAuditTaxonomyValue):
        _coerce_evidence_type("brand_new_type")
    with pytest.raises(UnknownAuditTaxonomyValue):
        _coerce_evidence_type("")


def test_insert_evidence_rejects_unknown_before_any_db_write():
    """The public write accessor cannot turn a typo into `custom`."""
    from db.audit_evidence import (
        UnknownAuditTaxonomyValue,
        insert_evidence_item,
    )

    with pytest.raises(UnknownAuditTaxonomyValue):
        asyncio.run(insert_evidence_item(
            audit_run_id="00000000-0000-0000-0000-000000000001",
            evidence_type="acceptance_singal",
            payload={},
        ))


def test_insert_evidence_returns_existing_row_on_idempotent_replay(monkeypatch):
    import db.audit_evidence as evidence_module

    class DuplicateDatabase:
        async def execute(self, _query):
            raise RuntimeError("duplicate key")

        async def fetch_one(self, _query):
            return {"evidence_id": "existing-evidence-id"}

    async def no_ddl():
        return None

    monkeypatch.setattr(evidence_module, "database", DuplicateDatabase())
    monkeypatch.setattr(evidence_module, "ensure_audit_evidence_tables", no_ddl)
    result = asyncio.run(evidence_module.insert_evidence_item(
        audit_run_id="00000000-0000-0000-0000-000000000001",
        evidence_type="acceptance_signal",
        execution_route_id="00000000-0000-0000-0000-000000000002",
        evidence_level="tested",
        payload={"ok": True},
        idempotency_key="ucp_probe:retry:acceptance",
    ))
    assert result == "existing-evidence-id"


def test_ucp_probe_verifier_is_registered_and_unknown_verifier_rejected():
    from db.audit_evidence import (
        UnknownAuditTaxonomyValue,
        VERIFIER_UCP_PROBE,
        _require_known_verifier,
    )
    assert _require_known_verifier(VERIFIER_UCP_PROBE) == VERIFIER_UCP_PROBE
    with pytest.raises(UnknownAuditTaxonomyValue):
        _require_known_verifier("ucp_proeb")


def test_acceptance_signal_requires_route_and_level():
    from db.audit_evidence import (
        EVIDENCE_LEVEL_TESTED,
        EVIDENCE_TYPE_ACCEPTANCE_SIGNAL,
        _validate_route_evidence,
    )
    _validate_route_evidence(
        evidence_type=EVIDENCE_TYPE_ACCEPTANCE_SIGNAL,
        execution_route_id="00000000-0000-0000-0000-000000000001",
        evidence_level=EVIDENCE_LEVEL_TESTED,
        merchant_id=None,
    )
    with pytest.raises(ValueError, match="execution_route_id"):
        _validate_route_evidence(
            evidence_type=EVIDENCE_TYPE_ACCEPTANCE_SIGNAL,
            execution_route_id=None,
            evidence_level=EVIDENCE_LEVEL_TESTED,
            merchant_id=None,
        )
    with pytest.raises(ValueError, match="evidence_level"):
        _validate_route_evidence(
            evidence_type=EVIDENCE_TYPE_ACCEPTANCE_SIGNAL,
            execution_route_id="00000000-0000-0000-0000-000000000001",
            evidence_level=None,
            merchant_id=None,
        )
    with pytest.raises(ValueError, match="synthetic"):
        _validate_route_evidence(
            evidence_type=EVIDENCE_TYPE_ACCEPTANCE_SIGNAL,
            execution_route_id="00000000-0000-0000-0000-000000000001",
            evidence_level=EVIDENCE_LEVEL_TESTED,
            merchant_id="prospect_ab12cd34ef56",
        )


def test_execution_route_identity_is_domain_keyed_and_canonicalized():
    from db.audit_evidence import normalize_execution_route_identity

    assert normalize_execution_route_identity(
        normalized_domain="Shop.Example.COM.",
        route_kind="UCP",
        endpoint="https://Store.MyShopify.com:443/api/ucp/mcp/",
    ) == (
        "shop.example.com",
        "ucp",
        "https://store.myshopify.com/api/ucp/mcp",
    )


@pytest.mark.parametrize(
    "domain, kind, endpoint",
    [
        ("https://shop.example.com", "ucp", "https://a.example/mcp"),
        ("shop.example.com", "ucp-probe", "https://a.example/mcp"),
        ("shop.example.com", "ucp", "http://a.example/mcp"),
        ("shop.example.com", "ucp", "https://a.example/mcp?token=x"),
    ],
)
def test_execution_route_identity_rejects_noncanonical_inputs(
    domain, kind, endpoint,
):
    from db.audit_evidence import normalize_execution_route_identity

    with pytest.raises(ValueError):
        normalize_execution_route_identity(
            normalized_domain=domain,
            route_kind=kind,
            endpoint=endpoint,
        )


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
