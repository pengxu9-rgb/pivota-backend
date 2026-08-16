"""P4.2 — canonical evidence/finding/action/verification/projection accessors.

Python interface to the 5 tables introduced by db/migrations/
086_canonical_evidence_model.sql. Mirrors the P2.1 / P3.1 pattern:

  - SQLAlchemy Table definitions (so SELECTs can use the .c.column API)
  - Inline DDL backstop (per-statement tolerant) for hermetic test envs
  - Best-effort write accessors — DB failures log + return None
    rather than raising, so dual-write paths in P4.3-P4.5 don't
    take down the legacy JSONB pipeline if the new tables are
    unavailable

This module is intentionally only data plumbing. The synthesis logic
that produces evidence_type / finding_type values lives in
services/audit_evidence_builder.py (a follow-up). The 5-audience
projection logic lives in services/audit_projection_builder.py (P4.5).

Phase 4 dual-write strategy:
  - Legacy: agent_center_bd_report_service.build_structured_report
    continues to populate report_jsonb as it does today
  - New: at the same code points, also call insert_evidence_item /
    insert_finding / insert_action so the canonical tables stay
    in sync
  - Phase 6 retires the legacy JSONB once consumers migrate
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    ARRAY,
    Boolean, Column, DateTime, Index, Integer, Table, Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from db._ddl_guard import apply_ddl_statements
from db.database import database, metadata

logger = logging.getLogger(__name__)


# =====================================================================
# Evidence type taxonomy. Validated at insert time; rows with an
# unknown evidence_type are coerced to 'custom' + the original
# value preserved in payload_jsonb._raw_type so we don't lose data.
# =====================================================================

EVIDENCE_TYPE_GROUNDING_CHUNK = "grounding_chunk"
EVIDENCE_TYPE_COMPETITOR_MENTION = "competitor_mention"
EVIDENCE_TYPE_URL_MATCH = "url_match"
EVIDENCE_TYPE_MISSING_SIGNAL = "missing_signal"
EVIDENCE_TYPE_INDUSTRY_STAT = "industry_stat"
EVIDENCE_TYPE_CUSTOM = "custom"

VALID_EVIDENCE_TYPES = frozenset({
    EVIDENCE_TYPE_GROUNDING_CHUNK,
    EVIDENCE_TYPE_COMPETITOR_MENTION,
    EVIDENCE_TYPE_URL_MATCH,
    EVIDENCE_TYPE_MISSING_SIGNAL,
    EVIDENCE_TYPE_INDUSTRY_STAT,
    EVIDENCE_TYPE_CUSTOM,
})


# Finding type taxonomy. Each value maps to a specific synthesis
# template downstream (PR-8a archetype detection). Adding a new
# finding type means adding it here + handling it in the synthesis
# layer.

FINDING_VISIBLE_VIA_RETAILERS_ONLY = "merchant_visible_via_retailers_only"
FINDING_FORM_FACTOR_UNIQUE = "form_factor_unique_in_cohort"
FINDING_CATEGORY_VISIBILITY_LOW = "category_visibility_low"
FINDING_ATTRIBUTION_GAP_WITH_EDITORIAL = (
    "attribution_gap_with_strong_editorial"
)
FINDING_INTEGRATION_INCOMPLETE = "integration_state_incomplete"
FINDING_FIRST_PARTY_INDEXING_GAP = "first_party_pdp_indexing_gap"
FINDING_NO_KOL_ENDORSEMENTS = "no_kol_endorsements_detected"
FINDING_CORPORATE_OWNERSHIP_KNOWN = "corporate_ownership_known"
FINDING_BASELINE_REFERENCE_AVAILABLE = "baseline_reference_available"

# Free-form finding types are accepted; the taxonomy above documents
# the ones with synthesis-layer handlers. Other types still insert
# but won't drive narrative templates.


# Action lever taxonomy. Mirrors data/playbooks.json lever values.
LEVER_PIVOTA_INTEGRATION = "pivota_integration"
LEVER_CONTENT_CREATION = "content_creation"
LEVER_PDP_OPTIMIZATION = "pdp_optimization"
LEVER_SITEMAP_HYGIENE = "sitemap_hygiene"
LEVER_KOL_OUTREACH = "kol_outreach"
LEVER_EDITORIAL_OUTREACH = "editorial_outreach"


# Severity values used by both findings + actions.
SEVERITY_CRITICAL = "critical"
SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_LOW = "low"
VALID_SEVERITIES = frozenset({
    SEVERITY_CRITICAL, SEVERITY_HIGH, SEVERITY_MEDIUM, SEVERITY_LOW,
})


# Owner values used by actions.
OWNER_PIVOTA_OPS = "pivota_ops"
OWNER_MERCHANT_BRAND = "merchant_brand_team"
OWNER_MERCHANT_GROWTH = "merchant_growth_team"
OWNER_MERCHANT_TECH = "merchant_tech_team"
OWNER_JOINT = "joint"
VALID_OWNERS = frozenset({
    OWNER_PIVOTA_OPS, OWNER_MERCHANT_BRAND, OWNER_MERCHANT_GROWTH,
    OWNER_MERCHANT_TECH, OWNER_JOINT,
})


# Phase values.
PHASE_WEEK_1_TO_4 = "week_1_to_4"
PHASE_WEEK_4_TO_12 = "week_4_to_12"
PHASE_WEEK_12_TO_24 = "week_12_to_24"


# Verifier IDs for the Phase 5 verify loop. Documented here so the
# Phase 5 worker knows what to enqueue.
VERIFIER_PDP_RENDERS = "pdp_renders"
VERIFIER_PDP_IN_SITEMAP = "pdp_in_sitemap"
VERIFIER_GSC_URL_SUBMITTED = "gsc_url_submitted"
VERIFIER_GSC_INDEXING_STATUS = "gsc_indexing_status"
VERIFIER_PIVOTA_INTERNAL_RETRIEVAL = "pivota_internal_retrieval"
VERIFIER_FRONTEND_AGENT_CITE = "frontend_agent_cite"
VERIFIER_PUBLIC_LLM_CITATION = "public_llm_citation_movement"

VALID_VERIFIERS = frozenset({
    VERIFIER_PDP_RENDERS, VERIFIER_PDP_IN_SITEMAP,
    VERIFIER_GSC_URL_SUBMITTED, VERIFIER_GSC_INDEXING_STATUS,
    VERIFIER_PIVOTA_INTERNAL_RETRIEVAL,
    VERIFIER_FRONTEND_AGENT_CITE,
    VERIFIER_PUBLIC_LLM_CITATION,
})


# Verification status state machine (P5.1). Reuses the existing
# `status` column on verification_runs as the canonical state.
#
# Lifecycle:
#   pending → claimed → succeeded
#                     → failed → (re-enqueue back to pending) → ...
#                     → exhausted_retries (terminal)
#                     → blocked (terminal — upstream unavailable;
#                                NOT a soft failure, don't retry)
VERIFICATION_STATUS_PENDING = "pending"
VERIFICATION_STATUS_CLAIMED = "claimed"
VERIFICATION_STATUS_SUCCEEDED = "succeeded"
VERIFICATION_STATUS_FAILED = "failed"
VERIFICATION_STATUS_EXHAUSTED_RETRIES = "exhausted_retries"
VERIFICATION_STATUS_BLOCKED = "blocked"

VERIFICATION_ACTIVE = frozenset({
    VERIFICATION_STATUS_PENDING, VERIFICATION_STATUS_CLAIMED,
})
VERIFICATION_TERMINAL = frozenset({
    VERIFICATION_STATUS_SUCCEEDED, VERIFICATION_STATUS_FAILED,
    VERIFICATION_STATUS_EXHAUSTED_RETRIES,
    VERIFICATION_STATUS_BLOCKED,
})

VALID_VERIFICATION_TRANSITIONS: dict = {
    VERIFICATION_STATUS_PENDING: {VERIFICATION_STATUS_CLAIMED},
    VERIFICATION_STATUS_CLAIMED: {
        VERIFICATION_STATUS_SUCCEEDED, VERIFICATION_STATUS_FAILED,
        VERIFICATION_STATUS_PENDING,
        VERIFICATION_STATUS_EXHAUSTED_RETRIES,
        VERIFICATION_STATUS_BLOCKED,
    },
    VERIFICATION_STATUS_SUCCEEDED: set(),
    VERIFICATION_STATUS_FAILED: set(),
    VERIFICATION_STATUS_EXHAUSTED_RETRIES: set(),
    VERIFICATION_STATUS_BLOCKED: set(),
}


def is_valid_verification_transition(
    from_status: str, to_status: str,
) -> bool:
    return to_status in VALID_VERIFICATION_TRANSITIONS.get(
        from_status, set(),
    )


# Verifier-specific lease durations. Most verifiers are quick HTTP
# fetches (default 120s); the citation-movement verifier needs much
# longer since it re-runs probe queries.
DEFAULT_VERIFICATION_LEASE_SECONDS = 120
LONG_VERIFICATION_LEASE_SECONDS = 600  # for citation_movement

# Grace period before reclaim runs.
VERIFICATION_STALE_LEASE_GRACE_SECONDS = 30


# Audience IDs for the 5-audience projection.
AUDIENCE_EMPLOYEE_BD = "employee_bd"
AUDIENCE_MERCHANT = "merchant"
AUDIENCE_INTERNAL_OPS = "internal_ops"
AUDIENCE_PIVOTA_PDP_FEED = "pivota_pdp_feed"
AUDIENCE_FRONTEND_AGENT_FEED = "frontend_agent_feed"

VALID_AUDIENCES = frozenset({
    AUDIENCE_EMPLOYEE_BD, AUDIENCE_MERCHANT,
    AUDIENCE_INTERNAL_OPS, AUDIENCE_PIVOTA_PDP_FEED,
    AUDIENCE_FRONTEND_AGENT_FEED,
})

# P0-4: role-gated audience access. Merchant JWTs may only read the
# projections they're documented to be the consumer of. The four
# internal projections carry data merchants should not see:
#   - employee_bd: full evidence + cost detail (BD-operator view)
#   - internal_ops: cost + latency dashboard ("not expected to ship
#     to merchants" per the builder docstring)
#   - pivota_pdp_feed: rendering hints for Pivota's own PDP service
#   - frontend_agent_feed: agent-discoverable shape, currently
#     conservatively gated; expand the allow list if product
#     intentionally exposes it to merchant clients
#
# Non-merchant audiences require an employee/admin auth path that
# the public `/api/audits/{run_id}` route does NOT currently expose
# — the route's only auth dependency is get_current_merchant. A
# follow-up PR can introduce role-aware dependencies; until then
# the route returns 403 for any audience outside this allow list
# and the projections stay merchant-invisible.
MERCHANT_ALLOWED_AUDIENCES = frozenset({AUDIENCE_MERCHANT})


# =====================================================================
# SQLAlchemy Tables
# =====================================================================

evidence_items = Table(
    "evidence_items",
    metadata,
    Column("evidence_id", UUID(as_uuid=False), primary_key=True),
    Column("audit_run_id", UUID(as_uuid=False), nullable=False),
    # P5.8.1: tenancy at the column level (was: route-layer only)
    Column("merchant_id", Text, nullable=True),
    Column("product_key", Text, nullable=True),
    # P0.2: canonical entity key — stamped on the deposit so audit exhaust
    # accretes on the cross-merchant product entity, not just the listing.
    Column("content_key", Text, nullable=True),
    Column("probe_run_id", UUID(as_uuid=False), nullable=True),
    Column("evidence_type", Text, nullable=False),
    Column("payload_jsonb", JSONB, nullable=False),
    Column("confidence", Integer, nullable=True),
    # P5.8.1: idempotency key — deterministic per (audit, item-sig);
    # paired with partial unique index for ON CONFLICT DO NOTHING.
    Column("idempotency_key", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Index("idx_evidence_items_audit_run", "audit_run_id", "created_at"),
    Index("idx_evidence_items_type", "evidence_type", "audit_run_id"),
    extend_existing=True,
)


readiness_findings = Table(
    "readiness_findings",
    metadata,
    Column("finding_id", UUID(as_uuid=False), primary_key=True),
    Column("audit_run_id", UUID(as_uuid=False), nullable=False),
    Column("merchant_id", Text, nullable=True),  # P5.8.1
    Column("product_key", Text, nullable=True),
    Column("finding_type", Text, nullable=False),
    Column("severity", Text, nullable=False, server_default="medium"),
    Column("payload_jsonb", JSONB, nullable=False),
    Column("confidence", Integer, nullable=True),
    Column("short_summary", Text, nullable=True),
    Column("idempotency_key", Text, nullable=True),  # P5.8.1
    Column("created_at", DateTime(timezone=True), nullable=False),
    Index("idx_findings_audit_run", "audit_run_id", "created_at"),
    Index("idx_findings_type", "finding_type", "audit_run_id"),
    extend_existing=True,
)


action_plan_items = Table(
    "action_plan_items",
    metadata,
    Column("action_id", UUID(as_uuid=False), primary_key=True),
    Column("audit_run_id", UUID(as_uuid=False), nullable=False),
    Column("merchant_id", Text, nullable=True),  # P5.8.1
    Column("product_key", Text, nullable=True),
    Column("parent_finding_id", UUID(as_uuid=False), nullable=True),
    Column("severity", Text, nullable=False, server_default="medium"),
    Column("lever", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("body", Text, nullable=True),
    Column("owner", Text, nullable=True),
    Column("kpi_to_track", Text, nullable=True),
    Column("expected_outcome", Text, nullable=True),
    Column("phase", Text, nullable=True),
    Column("depends_on", ARRAY(UUID(as_uuid=False)), nullable=True),
    Column("materialized_task_id", UUID(as_uuid=False), nullable=True),
    Column("idempotency_key", Text, nullable=True),  # P5.8.1
    Column("created_at", DateTime(timezone=True), nullable=False),
    Index(
        "idx_actions_audit_run",
        "audit_run_id", "severity", "created_at",
    ),
    extend_existing=True,
)


verification_runs = Table(
    "verification_runs",
    metadata,
    Column("verify_id", UUID(as_uuid=False), primary_key=True),
    Column("audit_run_id", UUID(as_uuid=False), nullable=False),
    Column("merchant_id", Text, nullable=True),  # P5.8.7
    Column("product_key", Text, nullable=True),
    Column("verifier_id", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="pending"),
    Column("evidence_jsonb", JSONB, nullable=True),
    Column("error_message", Text, nullable=True),
    Column("last_checked_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    # P5.1: durable work-queue columns. See db/migrations/
    # 087_verification_runs_durability.sql for column commentary.
    Column("claimed_by_worker", Text, nullable=True),
    Column("claimed_until", DateTime(timezone=True), nullable=True),
    Column("retry_count", Integer, nullable=False, server_default="0"),
    Column("max_retries", Integer, nullable=False, server_default="2"),
    Column("not_before", DateTime(timezone=True), nullable=True),
    Column("idempotency_key", Text, nullable=True),
    Index("idx_verify_audit_run", "audit_run_id", "verifier_id"),
    extend_existing=True,
)


report_projections = Table(
    "report_projections",
    metadata,
    Column("projection_id", UUID(as_uuid=False), primary_key=True),
    Column("audit_run_id", UUID(as_uuid=False), nullable=False),
    Column("merchant_id", Text, nullable=True),  # P5.8.1
    Column("audience", Text, nullable=False),
    Column("payload_jsonb", JSONB, nullable=False),
    Column("builder_version", Text, nullable=False),
    Column("built_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "audit_run_id", "audience",
        name="uq_report_projections_audit_audience",
    ),
    extend_existing=True,
)


# P0.2 — the cross-channel citation matrix (build_authority_map.hosts[]),
# persisted relationally + content_key-keyed instead of dying in report_jsonb.
# One row per (audit_run, content_key, provider, query, cited_host).
citation_observations = Table(
    "citation_observations",
    metadata,
    Column("observation_id", UUID(as_uuid=False), primary_key=True),
    Column("audit_run_id", Text, nullable=False),
    Column("merchant_id", Text, nullable=False),
    Column("content_key", Text, nullable=False),
    Column("product_key", Text, nullable=True),
    Column("provider", Text, nullable=False),
    Column("query", Text, nullable=False),
    Column("axis", Text, nullable=True),
    Column("query_class", Text, nullable=True),
    Column("cited_host", Text, nullable=True),
    Column("host_type", Text, nullable=True),
    Column("citation_role", Text, nullable=True),
    Column("first_party", Boolean, nullable=True),
    Column("is_competitor", Boolean, nullable=True),
    Column("evidence_url", Text, nullable=True),
    Column("content_key_basis", Text, nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("idempotency_key", Text, nullable=False),
    extend_existing=True,
)


# =====================================================================
# DDL backstop — per-statement tolerant (matches P2.1/P3.1 pattern)
# =====================================================================

_DDL_READY = False
_DDL_LOCK = asyncio.Lock()


_DDL_STATEMENTS = [
    # evidence_items
    """
    CREATE TABLE IF NOT EXISTS evidence_items (
        evidence_id    UUID PRIMARY KEY,
        audit_run_id   UUID NOT NULL,
        product_key    TEXT NULL,
        probe_run_id   UUID NULL,
        evidence_type  TEXT NOT NULL,
        payload_jsonb  JSONB NOT NULL,
        confidence     INTEGER NULL,
        created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_evidence_items_audit_run "
    "ON evidence_items (audit_run_id, created_at);",
    "CREATE INDEX IF NOT EXISTS idx_evidence_items_type "
    "ON evidence_items (evidence_type, audit_run_id);",
    # P0.2: canonical entity key (migration 158) — backstop for fresh DBs.
    "ALTER TABLE evidence_items ADD COLUMN IF NOT EXISTS content_key VARCHAR(40);",
    "CREATE INDEX IF NOT EXISTS idx_evidence_items_content_key "
    "ON evidence_items (content_key, evidence_type);",
    # P0.2: citation_observations (migration 159) — backstop for fresh DBs.
    """
    CREATE TABLE IF NOT EXISTS citation_observations (
        observation_id    UUID PRIMARY KEY,
        audit_run_id      TEXT NOT NULL,
        merchant_id       TEXT NOT NULL,
        content_key       VARCHAR(40) NOT NULL,
        product_key       TEXT NULL,
        provider          TEXT NOT NULL,
        query             TEXT NOT NULL,
        axis              TEXT NULL,
        query_class       TEXT NULL,
        cited_host        TEXT NULL,
        host_type         TEXT NULL,
        citation_role     TEXT NULL,
        first_party       BOOLEAN NULL,
        is_competitor     BOOLEAN NULL,
        evidence_url      TEXT NULL,
        content_key_basis TEXT NOT NULL,
        observed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        idempotency_key   TEXT NOT NULL
    );
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_citation_observations_idem "
    "ON citation_observations (idempotency_key);",
    "CREATE INDEX IF NOT EXISTS idx_citation_observations_entity "
    "ON citation_observations (content_key, provider, observed_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_citation_observations_run "
    "ON citation_observations (audit_run_id);",

    # readiness_findings
    """
    CREATE TABLE IF NOT EXISTS readiness_findings (
        finding_id     UUID PRIMARY KEY,
        audit_run_id   UUID NOT NULL,
        product_key    TEXT NULL,
        finding_type   TEXT NOT NULL,
        severity       TEXT NOT NULL DEFAULT 'medium',
        payload_jsonb  JSONB NOT NULL,
        confidence     INTEGER NULL,
        short_summary  TEXT NULL,
        created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_findings_audit_run "
    "ON readiness_findings (audit_run_id, created_at);",
    "CREATE INDEX IF NOT EXISTS idx_findings_type "
    "ON readiness_findings (finding_type, audit_run_id);",

    # action_plan_items
    """
    CREATE TABLE IF NOT EXISTS action_plan_items (
        action_id              UUID PRIMARY KEY,
        audit_run_id           UUID NOT NULL,
        product_key            TEXT NULL,
        parent_finding_id      UUID NULL,
        severity               TEXT NOT NULL DEFAULT 'medium',
        lever                  TEXT NOT NULL,
        title                  TEXT NOT NULL,
        body                   TEXT NULL,
        owner                  TEXT NULL,
        kpi_to_track           TEXT NULL,
        expected_outcome       TEXT NULL,
        phase                  TEXT NULL,
        depends_on             UUID[] NULL,
        materialized_task_id   UUID NULL,
        created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_actions_audit_run "
    "ON action_plan_items (audit_run_id, severity, created_at);",

    # verification_runs
    """
    CREATE TABLE IF NOT EXISTS verification_runs (
        verify_id        UUID PRIMARY KEY,
        audit_run_id     UUID NOT NULL,
        product_key      TEXT NULL,
        verifier_id      TEXT NOT NULL,
        status           TEXT NOT NULL DEFAULT 'pending',
        evidence_jsonb   JSONB NULL,
        error_message    TEXT NULL,
        last_checked_at  TIMESTAMPTZ NULL,
        created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        completed_at     TIMESTAMPTZ NULL
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_verify_audit_run "
    "ON verification_runs (audit_run_id, verifier_id);",
    # P5.1: durable work-queue columns. ALTERs are idempotent.
    "ALTER TABLE verification_runs "
    "ADD COLUMN IF NOT EXISTS claimed_by_worker TEXT;",
    "ALTER TABLE verification_runs "
    "ADD COLUMN IF NOT EXISTS claimed_until TIMESTAMPTZ;",
    "ALTER TABLE verification_runs "
    "ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0;",
    "ALTER TABLE verification_runs "
    "ADD COLUMN IF NOT EXISTS max_retries INTEGER NOT NULL DEFAULT 2;",
    "ALTER TABLE verification_runs "
    "ADD COLUMN IF NOT EXISTS not_before TIMESTAMPTZ;",
    "ALTER TABLE verification_runs "
    "ADD COLUMN IF NOT EXISTS idempotency_key TEXT;",
    "CREATE INDEX IF NOT EXISTS idx_verification_runs_worker_pull "
    "ON verification_runs (status, claimed_until, not_before, created_at) "
    "WHERE status IN ('pending', 'claimed');",
    "CREATE INDEX IF NOT EXISTS idx_verification_runs_idempotency "
    "ON verification_runs (idempotency_key) "
    "WHERE idempotency_key IS NOT NULL "
    "AND status IN ('pending', 'claimed');",

    # report_projections
    """
    CREATE TABLE IF NOT EXISTS report_projections (
        projection_id     UUID PRIMARY KEY,
        audit_run_id      UUID NOT NULL,
        audience          TEXT NOT NULL,
        payload_jsonb     JSONB NOT NULL,
        builder_version   TEXT NOT NULL,
        built_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (audit_run_id, audience)
    );
    """,

    # P5.8.1: merchant_id + idempotency_key on canonical tables
    # (matches migration 088). Two-layer tenancy + idempotent
    # writes via partial unique indexes. ALTERs are idempotent.
    "ALTER TABLE evidence_items "
    "ADD COLUMN IF NOT EXISTS merchant_id TEXT;",
    "ALTER TABLE evidence_items "
    "ADD COLUMN IF NOT EXISTS idempotency_key TEXT;",
    "CREATE INDEX IF NOT EXISTS idx_evidence_items_merchant "
    "ON evidence_items (merchant_id, audit_run_id) "
    "WHERE merchant_id IS NOT NULL;",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_evidence_items_idempotency "
    "ON evidence_items (audit_run_id, idempotency_key) "
    "WHERE idempotency_key IS NOT NULL;",

    "ALTER TABLE readiness_findings "
    "ADD COLUMN IF NOT EXISTS merchant_id TEXT;",
    "ALTER TABLE readiness_findings "
    "ADD COLUMN IF NOT EXISTS idempotency_key TEXT;",
    "CREATE INDEX IF NOT EXISTS idx_findings_merchant "
    "ON readiness_findings (merchant_id, audit_run_id) "
    "WHERE merchant_id IS NOT NULL;",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_findings_idempotency "
    "ON readiness_findings (audit_run_id, idempotency_key) "
    "WHERE idempotency_key IS NOT NULL;",

    "ALTER TABLE action_plan_items "
    "ADD COLUMN IF NOT EXISTS merchant_id TEXT;",
    "ALTER TABLE action_plan_items "
    "ADD COLUMN IF NOT EXISTS idempotency_key TEXT;",
    "CREATE INDEX IF NOT EXISTS idx_actions_merchant "
    "ON action_plan_items (merchant_id, audit_run_id) "
    "WHERE merchant_id IS NOT NULL;",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_actions_idempotency "
    "ON action_plan_items (audit_run_id, idempotency_key) "
    "WHERE idempotency_key IS NOT NULL;",

    "ALTER TABLE report_projections "
    "ADD COLUMN IF NOT EXISTS merchant_id TEXT;",
    "CREATE INDEX IF NOT EXISTS idx_projections_merchant "
    "ON report_projections (merchant_id, audience);",

    "ALTER TABLE verification_runs "
    "ADD COLUMN IF NOT EXISTS merchant_id TEXT;",
    "CREATE INDEX IF NOT EXISTS idx_verification_runs_merchant "
    "ON verification_runs (merchant_id, audit_run_id) "
    "WHERE merchant_id IS NOT NULL;",
]


async def ensure_audit_evidence_tables() -> None:
    """Per-statement-tolerant backstop. Postgres prod runs the .sql
    migration directly; this inline DDL covers hermetic test envs
    where partial-index syntax isn't supported (matches P2.1/P3.1).

    A pass in which any statement failed does NOT memoize, so a
    transient failure (e.g. a CREATE INDEX cut by the statement
    timeout while blocked on a lock) retries on the next call rather
    than leaving a UNIQUE idempotency index silently absent for the
    process lifetime. See db/_ddl_guard.py."""
    global _DDL_READY
    if _DDL_READY:
        return
    async with _DDL_LOCK:
        if _DDL_READY:
            return
        _DDL_READY = await apply_ddl_statements(
            _DDL_STATEMENTS,
            label="ensure_audit_evidence_tables",
            logger=logger,
            execute=database.execute,
        )


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# =====================================================================
# Validation helpers — coerce unknown values rather than reject
# =====================================================================

def _coerce_evidence_type(t: str) -> str:
    if t in VALID_EVIDENCE_TYPES:
        return t
    return EVIDENCE_TYPE_CUSTOM


def _coerce_severity(s: Optional[str]) -> str:
    """P5.8.6: case-insensitive match. The original was strict
    equality, so `"CRITICAL"` or `"High"` (which PR-8b's
    recommendation engine can emit when paraphrasing Gemini output)
    silently fell back to "medium" — wrong severity, wrong sort
    order in the merchant action queue."""
    s_normalized = (s or "").strip().lower()
    if s_normalized in VALID_SEVERITIES:
        return s_normalized
    return SEVERITY_MEDIUM


def _coerce_owner(o: Optional[str]) -> Optional[str]:
    if o is None or o in VALID_OWNERS:
        return o
    # Unknown owners are stored as-is; new owner types added to the
    # taxonomy later will work without backfill. This trades strict
    # validation for evolution-friendliness.
    return o


# =====================================================================
# Write accessors — best-effort. Return new UUID on success, None
# on persistence failure (caller's legacy JSONB write still happens).
# =====================================================================


async def insert_evidence_item(
    *,
    audit_run_id: str,
    evidence_type: str,
    payload: Dict[str, Any],
    merchant_id: Optional[str] = None,
    product_key: Optional[str] = None,
    content_key: Optional[str] = None,
    probe_run_id: Optional[str] = None,
    confidence: Optional[int] = None,
    idempotency_key: Optional[str] = None,
) -> Optional[str]:
    """Best-effort write. Unknown evidence_type is coerced to
    'custom' with the original value preserved in payload_jsonb.

    P5.8.1: merchant_id + idempotency_key plumbed through for
    two-layer tenancy + idempotent re-runs. When idempotency_key
    is set and a row already exists for (audit_run_id,
    idempotency_key), the partial unique index causes an ON
    CONFLICT — we catch + return the existing-row marker rather
    than the new uuid.

    Returns the new evidence_id, or None on persistence failure /
    idempotent skip.
    """
    await ensure_audit_evidence_tables()
    coerced_type = _coerce_evidence_type(evidence_type)
    safe_payload = dict(payload or {})
    if coerced_type == EVIDENCE_TYPE_CUSTOM and evidence_type != EVIDENCE_TYPE_CUSTOM:
        safe_payload["_raw_type"] = evidence_type
    # Coerce UUID / datetime / Decimal at the JSONB write boundary
    # (mirrors upsert_projection's PR #477 fix). Builders sometimes
    # pass-through asyncpg-returned columns; without this, a single
    # UUID anywhere in the payload tree breaks the whole insert.
    safe_payload = _json_safe(safe_payload)
    evidence_id = str(uuid.uuid4())
    try:
        await database.execute(
            evidence_items.insert().values(
                evidence_id=evidence_id,
                audit_run_id=audit_run_id,
                merchant_id=merchant_id,
                product_key=product_key,
                content_key=content_key,
                probe_run_id=probe_run_id,
                evidence_type=coerced_type,
                payload_jsonb=safe_payload,
                confidence=(
                    int(confidence) if confidence is not None else None
                ),
                idempotency_key=idempotency_key,
                created_at=_now_utc(),
            )
        )
        return evidence_id
    except Exception as exc:  # noqa: BLE001
        # ON CONFLICT (unique violation on idempotency_key) is the
        # expected case on re-run. But genuine errors (JSONB
        # encoder failures, FK violations, etc.) also land here —
        # and on Railway prod, root logger filters at WARNING so
        # logger.debug is invisible. Always log at WARNING; the
        # caller distinguishes "deduped" from "failed" via the
        # post-insert SELECT it already does.
        logger.warning(
            "insert_evidence_item idempotent-skip or failed "
            "audit_run=%s type=%s key=%s: %s",
            audit_run_id, evidence_type,
            (idempotency_key or "")[:16], str(exc)[:200],
        )
        return None


async def insert_citation_observation(
    *,
    audit_run_id: str,
    merchant_id: Optional[str],
    content_key: str,
    provider: str,
    query: str,
    content_key_basis: str,
    product_key: Optional[str] = None,
    axis: Optional[str] = None,
    query_class: Optional[str] = None,
    cited_host: Optional[str] = None,
    host_type: Optional[str] = None,
    citation_role: Optional[str] = None,
    first_party: Optional[bool] = None,
    is_competitor: Optional[bool] = None,
    evidence_url: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> Optional[str]:
    """P0.2 best-effort write of one citation observation. Idempotent via the
    unique index on idempotency_key (a re-run of the same audit collapses to
    the same row). Returns observation_id, or None on failure / idempotent
    skip — never raises, so it can't take down the audit lifecycle."""
    await ensure_audit_evidence_tables()
    observation_id = str(uuid.uuid4())
    try:
        await database.execute(
            citation_observations.insert().values(
                observation_id=observation_id,
                audit_run_id=audit_run_id,
                merchant_id=merchant_id,
                content_key=content_key,
                product_key=product_key,
                provider=provider,
                query=query,
                axis=axis,
                query_class=query_class,
                cited_host=cited_host,
                host_type=host_type,
                citation_role=citation_role,
                first_party=first_party,
                is_competitor=is_competitor,
                evidence_url=evidence_url,
                content_key_basis=content_key_basis,
                observed_at=_now_utc(),
                idempotency_key=idempotency_key,
            )
        )
        return observation_id
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "insert_citation_observation idempotent-skip or failed "
            "audit_run=%s ck=%s key=%s: %s",
            audit_run_id, content_key,
            (idempotency_key or "")[:16], str(exc)[:200],
        )
        return None


async def fetch_citation_observations(
    merchant_id: str,
    *,
    content_key: Optional[str] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """Read a merchant's recorded citation observations — who cited their
    products, for which query, mentioned vs recommended — newest first. STRICTLY
    merchant-scoped (only their own audit observations); optional content_key
    filter. Closes the get-cited proof loop (data was write-only, dying in
    report_jsonb). Best-effort: returns [] on any error, never raises."""
    if not merchant_id:
        return []
    capped = max(1, min(int(limit or 200), 1000))
    try:
        await ensure_audit_evidence_tables()
        query = citation_observations.select().where(
            citation_observations.c.merchant_id == merchant_id
        )
        if content_key:
            query = query.where(citation_observations.c.content_key == content_key)
        query = query.order_by(citation_observations.c.observed_at.desc()).limit(capped)
        rows = await database.fetch_all(query)
        return [dict(r) for r in rows or []]
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_citation_observations failed for %s: %s", merchant_id, str(exc)[:200])
        return []


async def insert_finding(
    *,
    audit_run_id: str,
    finding_type: str,
    payload: Dict[str, Any],
    merchant_id: Optional[str] = None,
    product_key: Optional[str] = None,
    severity: Optional[str] = None,
    confidence: Optional[int] = None,
    short_summary: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> Optional[str]:
    """Best-effort write. finding_type is stored as-given (the
    synthesis layer documents the canonical taxonomy but free-form
    types are accepted).

    P5.8.1: merchant_id + idempotency_key for two-layer tenancy +
    idempotent re-runs (matches insert_evidence_item)."""
    await ensure_audit_evidence_tables()
    finding_id = str(uuid.uuid4())
    try:
        await database.execute(
            readiness_findings.insert().values(
                finding_id=finding_id,
                audit_run_id=audit_run_id,
                merchant_id=merchant_id,
                product_key=product_key,
                finding_type=finding_type,
                severity=_coerce_severity(severity),
                # JSONB write boundary — coerce UUID/datetime/Decimal
                # to JSON-safe primitives (see insert_evidence_item).
                payload_jsonb=_json_safe(dict(payload or {})),
                confidence=(
                    int(confidence) if confidence is not None else None
                ),
                short_summary=(
                    short_summary[:2000] if short_summary else None
                ),
                idempotency_key=idempotency_key,
                created_at=_now_utc(),
            )
        )
        return finding_id
    except Exception as exc:  # noqa: BLE001
        # See insert_evidence_item — WARNING for prod visibility.
        logger.warning(
            "insert_finding idempotent-skip or failed "
            "audit_run=%s type=%s: %s",
            audit_run_id, finding_type, str(exc)[:200],
        )
        return None


async def insert_action(
    *,
    audit_run_id: str,
    lever: str,
    title: str,
    body: Optional[str] = None,
    merchant_id: Optional[str] = None,
    product_key: Optional[str] = None,
    parent_finding_id: Optional[str] = None,
    severity: Optional[str] = None,
    owner: Optional[str] = None,
    kpi_to_track: Optional[str] = None,
    expected_outcome: Optional[str] = None,
    phase: Optional[str] = None,
    depends_on: Optional[List[str]] = None,
    idempotency_key: Optional[str] = None,
) -> Optional[str]:
    """Best-effort write. Returns the new action_id or None.

    P5.8.1: merchant_id + idempotency_key for two-layer tenancy +
    idempotent re-runs."""
    await ensure_audit_evidence_tables()
    action_id = str(uuid.uuid4())
    try:
        await database.execute(
            action_plan_items.insert().values(
                action_id=action_id,
                audit_run_id=audit_run_id,
                merchant_id=merchant_id,
                product_key=product_key,
                parent_finding_id=parent_finding_id,
                severity=_coerce_severity(severity),
                lever=lever,
                title=title[:500] if title else "(untitled)",
                body=body[:5000] if body else None,
                owner=_coerce_owner(owner),
                kpi_to_track=(
                    kpi_to_track[:500] if kpi_to_track else None
                ),
                expected_outcome=(
                    expected_outcome[:500] if expected_outcome else None
                ),
                phase=phase,
                depends_on=list(depends_on) if depends_on else None,
                idempotency_key=idempotency_key,
                created_at=_now_utc(),
            )
        )
        return action_id
    except Exception as exc:  # noqa: BLE001
        # See insert_evidence_item — WARNING for prod visibility.
        logger.warning(
            "insert_action idempotent-skip or failed "
            "audit_run=%s lever=%s: %s",
            audit_run_id, lever, str(exc)[:200],
        )
        return None


async def link_action_to_task(
    *, action_id: str, task_id: str,
) -> bool:
    """Update action_plan_items.materialized_task_id after the
    task materializer creates the merchant_tasks row. Lets us
    join from action back to its tracking task."""
    await ensure_audit_evidence_tables()
    try:
        await database.execute(
            action_plan_items.update()
            .where(action_plan_items.c.action_id == action_id)
            .values(materialized_task_id=task_id)
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "link_action_to_task failed action=%s task=%s: %s",
            action_id, task_id, str(exc)[:200],
        )
        return False


async def insert_verification_run(
    *,
    audit_run_id: str,
    verifier_id: str,
    product_key: Optional[str] = None,
    status: str = "pending",
    evidence_jsonb: Optional[Dict[str, Any]] = None,
    error_message: Optional[str] = None,
) -> Optional[str]:
    """Best-effort write. Phase 5 worker calls this when starting
    a verifier; subsequent transitions go through update_verification_run."""
    await ensure_audit_evidence_tables()
    if verifier_id not in VALID_VERIFIERS:
        logger.warning(
            "insert_verification_run: unknown verifier_id %r "
            "for audit_run=%s — inserting anyway",
            verifier_id, audit_run_id,
        )
    verify_id = str(uuid.uuid4())
    try:
        await database.execute(
            verification_runs.insert().values(
                verify_id=verify_id,
                audit_run_id=audit_run_id,
                product_key=product_key,
                verifier_id=verifier_id,
                status=status,
                # JSONB write boundary — coerce UUID/datetime/Decimal
                # to JSON-safe primitives. Matches the defense applied
                # at insert_evidence_item / insert_finding / upsert_
                # projection by PR #477 + #479.
                evidence_jsonb=_json_safe(evidence_jsonb) if evidence_jsonb else evidence_jsonb,
                error_message=(
                    error_message[:2000] if error_message else None
                ),
                created_at=_now_utc(),
            )
        )
        return verify_id
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "insert_verification_run failed: %s", str(exc)[:200],
        )
        return None


# _json_safe lives in db/_jsonb_safe so other db modules (executor_runs,
# merchant_audit_runs) can use it at their JSONB write boundaries too.
from db._jsonb_safe import _json_safe  # noqa: E402, F401 — re-exported


async def upsert_projection(
    *,
    audit_run_id: str,
    audience: str,
    payload: Dict[str, Any],
    builder_version: str,
    merchant_id: Optional[str] = None,
) -> Optional[str]:
    """Insert or replace the projection for (audit_run, audience).
    The UNIQUE constraint forces replace semantics — re-rendering
    overwrites the existing row.

    Implementation: try insert; on unique-constraint violation,
    update. Two round-trips in the conflict case, one in the
    common case. Acceptable since projections are written at
    audit completion (low volume) not on every request.
    """
    await ensure_audit_evidence_tables()
    if audience not in VALID_AUDIENCES:
        logger.warning(
            "upsert_projection: unknown audience %r — accepting",
            audience,
        )
    # Coerce UUID / datetime / Decimal to JSON-safe primitives before
    # asyncpg hands the payload to json.dumps for JSONB storage.
    # employee_bd in particular pass-throughs DB rows that include
    # evidence_id / finding_id / action_id (all UUID columns); without
    # this, that projection fails with "Object of type UUID is not
    # JSON serializable" and never persists. See PR-projection-uuid-fix.
    safe_payload = _json_safe(payload)
    projection_id = str(uuid.uuid4())
    now = _now_utc()
    try:
        await database.execute(
            report_projections.insert().values(
                projection_id=projection_id,
                audit_run_id=audit_run_id,
                merchant_id=merchant_id,
                audience=audience,
                payload_jsonb=safe_payload,
                builder_version=builder_version,
                built_at=now,
            )
        )
        return projection_id
    except Exception:
        # Likely unique-constraint conflict (existing projection).
        # Fall through to update.
        try:
            await database.execute(
                report_projections.update()
                .where(
                    report_projections.c.audit_run_id == audit_run_id,
                    report_projections.c.audience == audience,
                )
                .values(
                    merchant_id=merchant_id,
                    payload_jsonb=safe_payload,
                    builder_version=builder_version,
                    built_at=now,
                )
            )
            return None  # updated — no new projection_id
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "upsert_projection failed audit_run=%s aud=%s: %s",
                audit_run_id, audience, str(exc)[:200],
            )
            return None


# =====================================================================
# Read accessors
# =====================================================================


async def list_evidence_for_run(
    *, audit_run_id: str,
    evidence_type: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """All evidence rows for one audit. Optional type filter."""
    await ensure_audit_evidence_tables()
    try:
        q = evidence_items.select().where(
            evidence_items.c.audit_run_id == audit_run_id,
        )
        if evidence_type is not None:
            q = q.where(evidence_items.c.evidence_type == evidence_type)
        q = q.order_by(evidence_items.c.created_at)
        rows = await database.fetch_all(q)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "list_evidence_for_run failed for %s: %s",
            audit_run_id, str(exc)[:200],
        )
        return []
    return [dict(r) for r in (rows or [])]


async def list_findings_for_run(
    *, audit_run_id: str,
) -> List[Dict[str, Any]]:
    """All findings for one audit, ordered by created_at."""
    await ensure_audit_evidence_tables()
    try:
        rows = await database.fetch_all(
            readiness_findings.select()
            .where(readiness_findings.c.audit_run_id == audit_run_id)
            .order_by(readiness_findings.c.created_at)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "list_findings_for_run failed for %s: %s",
            audit_run_id, str(exc)[:200],
        )
        return []
    return [dict(r) for r in (rows or [])]


async def list_actions_for_run(
    *, audit_run_id: str,
) -> List[Dict[str, Any]]:
    """All actions for one audit, ordered by severity (critical
    first) then created_at."""
    await ensure_audit_evidence_tables()
    try:
        rows = await database.fetch_all(
            action_plan_items.select()
            .where(action_plan_items.c.audit_run_id == audit_run_id)
            .order_by(action_plan_items.c.created_at)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "list_actions_for_run failed for %s: %s",
            audit_run_id, str(exc)[:200],
        )
        return []
    return [dict(r) for r in (rows or [])]


async def fetch_projection(
    *, audit_run_id: str, audience: str,
) -> Optional[Dict[str, Any]]:
    """Return the cached projection for (audit_run, audience), or
    None if not yet built."""
    await ensure_audit_evidence_tables()
    try:
        row = await database.fetch_one(
            report_projections.select().where(
                report_projections.c.audit_run_id == audit_run_id,
                report_projections.c.audience == audience,
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "fetch_projection failed audit_run=%s aud=%s: %s",
            audit_run_id, audience, str(exc)[:200],
        )
        return None
    if row is None:
        return None
    return dict(row)


# =====================================================================
# P5.1 — verification_runs worker-pull accessors
# =====================================================================


def compute_canonical_idempotency_key(
    *,
    audit_run_id: str,
    item_type: str,
    item_signature: str,
) -> str:
    """P5.8.2 helper. Deterministic key per (audit, item-class,
    item-content). Lets persist_canonical_evidence be idempotent
    across worker re-runs: same input → same key → second INSERT
    hits the partial unique index and ON CONFLICT skips.

    item_type: "evidence" | "finding" | "action"
    item_signature: caller-chosen distinguishing substring (e.g.,
      for evidence: f"{evidence_type}|{product_key}|{host}|{excerpt[:50]}";
      for finding: f"{finding_type}|{product_key}";
      for action: f"{lever}|{product_key}|{title[:50]}").
    """
    import hashlib
    components = [
        (audit_run_id or "").strip(),
        (item_type or "").strip(),
        (item_signature or "").strip(),
    ]
    return hashlib.sha256("|".join(components).encode("utf-8")).hexdigest()


def compute_verification_idempotency_key(
    *,
    audit_run_id: str,
    verifier_id: str,
    product_key: Optional[str] = None,
) -> str:
    """Stable sha256 of the dedupe tuple. The enqueue path (P5.7)
    calls this so re-firing the enqueue at audit completion doesn't
    create duplicate verification_runs rows for in-flight verifiers."""
    import hashlib
    components = [
        (audit_run_id or "").strip(),
        (verifier_id or "").strip(),
        (product_key or "").strip(),
    ]
    return hashlib.sha256("|".join(components).encode("utf-8")).hexdigest()


async def find_in_flight_verification_by_idempotency(
    *, idempotency_key: str,
) -> Optional[str]:
    """Pre-enqueue dedupe. Returns the verify_id of any in-flight
    row matching this key, or None."""
    if not idempotency_key:
        return None
    await ensure_audit_evidence_tables()
    try:
        from sqlalchemy.sql import select
        row = await database.fetch_one(
            select(verification_runs.c.verify_id)
            .where(
                verification_runs.c.idempotency_key == idempotency_key,
                verification_runs.c.status.in_(list(VERIFICATION_ACTIVE)),
            )
            .limit(1)
        )
        if row is None:
            return None
        return str(row[0])
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "find_in_flight_verification_by_idempotency failed: %s",
            str(exc)[:200],
        )
        return None


async def enqueue_verification_run(
    *,
    audit_run_id: str,
    verifier_id: str,
    merchant_id: Optional[str] = None,
    product_key: Optional[str] = None,
    not_before: Optional[datetime] = None,
    max_retries: int = 2,
    idempotency_key: Optional[str] = None,
) -> Optional[str]:
    """Insert a verification_runs row in status='pending' for the
    worker to claim. The enqueue path (P5.7 at audit completion)
    typically calls find_in_flight_verification_by_idempotency
    first to dedupe."""
    await ensure_audit_evidence_tables()
    if verifier_id not in VALID_VERIFIERS:
        logger.warning(
            "enqueue_verification_run: unknown verifier_id %r "
            "for audit_run=%s — inserting anyway",
            verifier_id, audit_run_id,
        )
    verify_id = str(uuid.uuid4())
    try:
        await database.execute(
            verification_runs.insert().values(
                verify_id=verify_id,
                audit_run_id=audit_run_id,
                merchant_id=merchant_id,
                product_key=product_key,
                verifier_id=verifier_id,
                status=VERIFICATION_STATUS_PENDING,
                not_before=not_before,
                max_retries=int(max_retries),
                idempotency_key=idempotency_key,
                created_at=_now_utc(),
            )
        )
        return verify_id
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "enqueue_verification_run failed verifier=%s audit=%s: %s",
            verifier_id, audit_run_id, str(exc)[:200],
        )
        return None


async def claim_next_pending_verification(
    *,
    worker_id: str,
    lease_seconds: int = DEFAULT_VERIFICATION_LEASE_SECONDS,
) -> Optional[Dict[str, Any]]:
    """Atomically claim the oldest pending or stale-leased
    verification. Skips rows where not_before > NOW() (the citation
    movement verifier sets this to audit_completed_at + 30 days).
    SKIP LOCKED makes this safe under multiple workers.
    """
    await ensure_audit_evidence_tables()
    now = _now_utc()
    new_until = datetime.fromtimestamp(
        now.timestamp() + lease_seconds, tz=timezone.utc,
    )
    query = """
        UPDATE verification_runs
           SET claimed_by_worker = :worker_id,
               claimed_until     = :new_until,
               status            = 'claimed',
               last_checked_at   = :now
         WHERE verify_id = (
             SELECT verify_id
               FROM verification_runs
              WHERE status IN ('pending', 'claimed')
                AND (
                    claimed_until IS NULL
                 OR claimed_until < :now
                )
                AND (
                    not_before IS NULL
                 OR not_before < :now
                )
              ORDER BY created_at ASC
              FOR UPDATE SKIP LOCKED
              LIMIT 1
         )
        RETURNING verify_id, audit_run_id, product_key, verifier_id,
                  retry_count, max_retries, idempotency_key,
                  created_at
    """
    try:
        row = await database.fetch_one(
            query,
            {"worker_id": worker_id, "new_until": new_until, "now": now},
        )
        if row is None:
            return None
        d = dict(row)
        return {
            "verify_id": str(d.get("verify_id")),
            "audit_run_id": (
                str(d.get("audit_run_id"))
                if d.get("audit_run_id") else None
            ),
            "product_key": d.get("product_key"),
            "verifier_id": d.get("verifier_id"),
            "retry_count": int(d.get("retry_count") or 0),
            "max_retries": int(d.get("max_retries") or 2),
            "idempotency_key": d.get("idempotency_key"),
            "created_at": (
                d["created_at"].isoformat()
                if isinstance(d.get("created_at"), datetime)
                else None
            ),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "claim_next_pending_verification failed for worker=%s: %s",
            worker_id, str(exc)[:200],
        )
        return None


async def mark_verification_succeeded(
    *,
    verify_id: str,
    worker_id: str,
    evidence_jsonb: Optional[Dict[str, Any]] = None,
) -> bool:
    """Terminal succeeded transition. Guarded on worker ownership."""
    await ensure_audit_evidence_tables()
    now = _now_utc()
    values: Dict[str, Any] = {
        "status": VERIFICATION_STATUS_SUCCEEDED,
        "completed_at": now,
        "last_checked_at": now,
    }
    if evidence_jsonb is not None:
        values["evidence_jsonb"] = evidence_jsonb
    try:
        result = await database.execute(
            verification_runs.update()
            .where(
                verification_runs.c.verify_id == verify_id,
                verification_runs.c.status == VERIFICATION_STATUS_CLAIMED,
                verification_runs.c.claimed_by_worker == worker_id,
            )
            .values(**values)
        )
        if isinstance(result, int):
            return result > 0
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "mark_verification_succeeded failed for verify=%s: %s",
            verify_id, str(exc)[:200],
        )
        return False


async def mark_verification_blocked(
    *,
    verify_id: str,
    worker_id: str,
    error_message: str,
    evidence_jsonb: Optional[Dict[str, Any]] = None,
) -> bool:
    """Terminal blocked transition — upstream system unavailable
    (e.g., GSC consent revoked, agent.pivota.cc 503). NOT a retry
    candidate; if the upstream comes back, a new audit re-enqueues
    a fresh verification_runs row."""
    await ensure_audit_evidence_tables()
    now = _now_utc()
    values: Dict[str, Any] = {
        "status": VERIFICATION_STATUS_BLOCKED,
        "completed_at": now,
        "last_checked_at": now,
        "error_message": (
            error_message[:2000] if error_message else None
        ),
    }
    if evidence_jsonb is not None:
        values["evidence_jsonb"] = evidence_jsonb
    try:
        result = await database.execute(
            verification_runs.update()
            .where(
                verification_runs.c.verify_id == verify_id,
                verification_runs.c.status == VERIFICATION_STATUS_CLAIMED,
                verification_runs.c.claimed_by_worker == worker_id,
            )
            .values(**values)
        )
        if isinstance(result, int):
            return result > 0
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "mark_verification_blocked failed for verify=%s: %s",
            verify_id, str(exc)[:200],
        )
        return False


async def mark_verification_failed_with_retry(
    *,
    verify_id: str,
    worker_id: str,
    error_message: str,
    evidence_jsonb: Optional[Dict[str, Any]] = None,
) -> str:
    """Failed-attempt handling. If retry_count + 1 < max_retries:
    transition back to PENDING + bump retry_count. Otherwise
    transition to EXHAUSTED_RETRIES (terminal). Returns the new
    status string for caller logging.

    P5.8.6: atomic — single UPDATE with CASE on status +
    `retry_count + 1`. Same fix pattern as
    mark_executor_run_failed_with_retry (db/executor_runs.py).
    Eliminates the SELECT-then-UPDATE race where a reaper between
    the two queries could null claimed_by_worker, leaving the
    UPDATE to silently no-op + retry_count never incrementing.
    """
    await ensure_audit_evidence_tables()
    now = _now_utc()
    query = """
        UPDATE verification_runs
           SET retry_count = retry_count + 1,
               status = CASE
                   WHEN retry_count + 1 < max_retries THEN 'pending'
                   ELSE 'exhausted_retries'
               END,
               last_checked_at = :now,
               completed_at = CASE
                   WHEN retry_count + 1 < max_retries THEN completed_at
                   ELSE :now
               END,
               claimed_by_worker = CASE
                   WHEN retry_count + 1 < max_retries THEN NULL
                   ELSE claimed_by_worker
               END,
               claimed_until = CASE
                   WHEN retry_count + 1 < max_retries THEN NULL
                   ELSE claimed_until
               END,
               error_message = :error_message,
               evidence_jsonb = COALESCE(:evidence_jsonb, evidence_jsonb)
         WHERE verify_id = :verify_id
           AND status = 'claimed'
           AND claimed_by_worker = :worker_id
         RETURNING status
    """
    import json as _json
    try:
        row = await database.fetch_one(
            query,
            {
                "verify_id": verify_id,
                "worker_id": worker_id,
                "now": now,
                "error_message": error_message[:2000],
                "evidence_jsonb": (
                    _json.dumps(evidence_jsonb)
                    if evidence_jsonb is not None
                    else None
                ),
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "mark_verification_failed_with_retry atomic UPDATE "
            "failed for verify=%s: %s", verify_id, str(exc)[:200],
        )
        return VERIFICATION_STATUS_FAILED
    if row is None:
        return VERIFICATION_STATUS_FAILED
    return dict(row).get("status") or VERIFICATION_STATUS_FAILED


async def extend_verification_lease(
    *,
    verify_id: str,
    worker_id: str,
    lease_seconds: int = LONG_VERIFICATION_LEASE_SECONDS,
) -> bool:
    """P5.8.6: extend a verification's lease. The worker calls this
    right after claim for verifiers known to run long (e.g.,
    public_llm_citation_movement which does an LLM probe that takes
    minutes, not seconds). Guarded on worker ownership — returns
    False when the lease has been stolen.
    """
    await ensure_audit_evidence_tables()
    new_until = datetime.fromtimestamp(
        _now_utc().timestamp() + lease_seconds, tz=timezone.utc,
    )
    try:
        result = await database.execute(
            verification_runs.update()
            .where(
                verification_runs.c.verify_id == verify_id,
                verification_runs.c.claimed_by_worker == worker_id,
            )
            .values(claimed_until=new_until)
        )
        if isinstance(result, int):
            return result > 0
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "extend_verification_lease failed for verify=%s: %s",
            verify_id, str(exc)[:200],
        )
        return False


async def release_stale_verification_leases(
    *,
    grace_seconds: int = VERIFICATION_STALE_LEASE_GRACE_SECONDS,
) -> int:
    """Reaper backstop. claim_next_pending_verification already
    tolerates stale leases inline; this guarantees progress when a
    worker hangs in a way that prevents it from re-claiming.
    Returns the number of rows reset to pending."""
    await ensure_audit_evidence_tables()
    cutoff = datetime.fromtimestamp(
        _now_utc().timestamp() - grace_seconds, tz=timezone.utc,
    )
    query = """
        UPDATE verification_runs
           SET status            = 'pending',
               claimed_by_worker = NULL,
               claimed_until     = NULL
         WHERE claimed_until IS NOT NULL
           AND claimed_until < :cutoff
           AND status = 'claimed'
    """
    try:
        result = await database.execute(query, {"cutoff": cutoff})
        if isinstance(result, int):
            return result
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "release_stale_verification_leases failed: %s",
            str(exc)[:200],
        )
        return 0


async def list_verifications_for_run(
    *, audit_run_id: str,
) -> List[Dict[str, Any]]:
    """All verification rows for one audit. Used by the merchant /
    employee projections to show verifier status alongside the
    finding/evidence/action set."""
    await ensure_audit_evidence_tables()
    try:
        rows = await database.fetch_all(
            verification_runs.select()
            .where(verification_runs.c.audit_run_id == audit_run_id)
            .order_by(verification_runs.c.created_at)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "list_verifications_for_run failed for %s: %s",
            audit_run_id, str(exc)[:200],
        )
        return []
    return [dict(r) for r in (rows or [])]
