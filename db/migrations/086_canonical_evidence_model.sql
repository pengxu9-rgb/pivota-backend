-- P4.1: canonical evidence + finding + action + verification + projection model.
--
-- Today the entire audit result lives in `merchant_audit_runs.report_jsonb`
-- as one large opaque blob. Consequences:
--   1. No way to query "every audit that cited host X" or "every
--      audit where the brand-uniqueness finding fired"
--   2. The 5 audience projections (employee_bd, merchant,
--      internal_ops, pivota_pdp_feed, frontend_agent_feed) all have
--      to re-derive from the same blob, with each renderer making
--      its own decisions about what to surface
--   3. The synthesis layer (PR-8a executive summary, PR-8b
--      recommendation engine v2, PR-8c roadmap) has to re-parse
--      JSONB on every render
--
-- This migration introduces 5 canonical tables:
--   - evidence_items: one row per cited quote / claim / URL
--   - readiness_findings: one row per detected pattern (paradox,
--     form-factor uniqueness, etc.) — the strategic insights
--   - action_plan_items: one row per recommended action
--     (owner / KPI / outcome / phase / depends_on)
--   - verification_runs: one row per (audit, verifier) — scaffold
--     for the 7-stage Phase 5 verify loop
--   - report_projections: one row per (audit, audience) — cached
--     rendered shape for each of the 5 audiences
--
-- Dual-write pattern: Phase 4 PRs will dual-write to these tables
-- alongside the legacy `report_jsonb`. Phase 6 retires the JSONB.
--
-- Idempotent — safe to re-run.

-- =======================================================================
-- evidence_items
-- =======================================================================

CREATE TABLE IF NOT EXISTS evidence_items (
    evidence_id        UUID PRIMARY KEY,
    audit_run_id       UUID NOT NULL,
    -- Which product this evidence is about (NULL = brand-level
    -- evidence not tied to a single product).
    product_key        TEXT NULL,
    -- Where the evidence came from. probe_run_id links to
    -- llm_probe_runs (P2.5) — lets us trace evidence → specific
    -- LLM call → cost.
    probe_run_id       UUID NULL,
    -- Evidence type. Drives renderer choice. Enum-ish (validated
    -- in db/audit_evidence.py).
    --   grounding_chunk: Gemini-style cited URL + excerpt
    --   competitor_mention: "Brand X was listed by Gemini"
    --   url_match: "merchant.com appeared in response"
    --   missing_signal: "no editorial host cited" (a NEGATIVE
    --                   finding's evidence — used in the report)
    --   industry_stat: "category grew 14% YoY"
    --   custom: free-form, payload-typed
    evidence_type      TEXT NOT NULL,
    -- The substantive content. Examples:
    --   {"host": "forbes.com", "url": "...", "excerpt_text": "...",
    --    "scan_mode": "category_visibility_test"}
    --   {"competitor": "AG1", "scan_mode": "open_product_..."}
    --   {"matched_url": "gruns.co/products/x", "matched_in": "raw_text"}
    payload_jsonb      JSONB NOT NULL,
    -- 0-100. Calibrated per evidence_type. Low confidence (<60) is
    -- shown to ops dashboards but not surfaced in merchant-facing
    -- prose.
    confidence         INTEGER NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE evidence_items IS
  'P4.1: atomic citable evidence. One row per cited quote/claim/URL. Linked to audit_run + optionally product_key + probe_run_id (for cost attribution).';

CREATE INDEX IF NOT EXISTS idx_evidence_items_audit_run
  ON evidence_items (audit_run_id, created_at);

CREATE INDEX IF NOT EXISTS idx_evidence_items_product
  ON evidence_items (audit_run_id, product_key)
  WHERE product_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_evidence_items_probe_run
  ON evidence_items (probe_run_id)
  WHERE probe_run_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_evidence_items_type
  ON evidence_items (evidence_type, audit_run_id);


-- =======================================================================
-- readiness_findings
-- =======================================================================

CREATE TABLE IF NOT EXISTS readiness_findings (
    finding_id         UUID PRIMARY KEY,
    audit_run_id       UUID NOT NULL,
    product_key        TEXT NULL,
    -- Canonical finding types. Drives synthesis (PR-8a paradox
    -- framing, executive summary builder) + projection logic.
    -- Examples:
    --   merchant_visible_via_retailers_only (the paradox case)
    --   form_factor_unique_in_cohort
    --   category_visibility_low
    --   attribution_gap_with_strong_editorial
    --   integration_state_incomplete
    --   first_party_pdp_indexing_gap
    --   no_kol_endorsements_detected
    --   etc.
    finding_type       TEXT NOT NULL,
    -- Severity for routing to action_plan_items + UI rendering.
    severity           TEXT NOT NULL DEFAULT 'medium',
    -- Per-type payload shape (validated in code, not DB). Example
    -- for merchant_visible_via_retailers_only:
    --   {"editorial_host_count": 6, "first_party_attribution_score": 12,
    --    "verdict_label": "VISIBLE VIA RETAILERS"}
    payload_jsonb      JSONB NOT NULL,
    confidence         INTEGER NULL,
    -- Free-form human-readable summary the renderer can use
    -- directly. The synthesis layer regenerates this from
    -- payload_jsonb when re-rendering with a different audience.
    short_summary      TEXT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE readiness_findings IS
  'P4.1: strategic insights derived from evidence. One row per detected pattern. Drives synthesis (executive summary, recommendations, roadmap).';

CREATE INDEX IF NOT EXISTS idx_findings_audit_run
  ON readiness_findings (audit_run_id, created_at);

CREATE INDEX IF NOT EXISTS idx_findings_type
  ON readiness_findings (finding_type, audit_run_id);

CREATE INDEX IF NOT EXISTS idx_findings_product
  ON readiness_findings (audit_run_id, product_key)
  WHERE product_key IS NOT NULL;


-- =======================================================================
-- action_plan_items
-- =======================================================================

CREATE TABLE IF NOT EXISTS action_plan_items (
    action_id          UUID PRIMARY KEY,
    audit_run_id       UUID NOT NULL,
    product_key        TEXT NULL,
    -- Which finding triggered this action. NULL allowed for
    -- evergreen actions (integration onboarding, baseline GSC
    -- submit) that aren't finding-driven.
    parent_finding_id  UUID NULL,
    severity           TEXT NOT NULL DEFAULT 'medium',
    -- Audit lever taxonomy: pivota_integration, content_creation,
    -- pdp_optimization, sitemap_hygiene, kol_outreach,
    -- editorial_outreach, pricing, etc.
    lever              TEXT NOT NULL,
    title              TEXT NOT NULL,
    body               TEXT NULL,
    -- Per PR-8b recommendation engine v2: every action has these.
    --   owner: pivota_ops | merchant_brand_team | merchant_growth_team
    --          | merchant_tech_team | joint
    --   kpi_to_track: free text (e.g. "first-party citation rate")
    --   expected_outcome: free text
    --   phase: week_1_to_4 | week_4_to_12 | week_12_to_24
    owner              TEXT NULL,
    kpi_to_track       TEXT NULL,
    expected_outcome   TEXT NULL,
    phase              TEXT NULL,
    -- Action IDs this action depends on (must complete first).
    -- Materialized as an array of UUIDs.
    depends_on         UUID[] NULL,
    -- Tracking integration: when this action materializes into a
    -- merchant_tasks row, the task_id lands here so we can join
    -- back from the canonical model to the work queue.
    materialized_task_id UUID NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE action_plan_items IS
  'P4.1: recommended actions with owner/KPI/outcome/phase. Read by task materialization + PR-8c implementation roadmap builder.';

CREATE INDEX IF NOT EXISTS idx_actions_audit_run
  ON action_plan_items (audit_run_id, severity, created_at);

CREATE INDEX IF NOT EXISTS idx_actions_phase
  ON action_plan_items (audit_run_id, phase)
  WHERE phase IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_actions_finding
  ON action_plan_items (parent_finding_id)
  WHERE parent_finding_id IS NOT NULL;


-- =======================================================================
-- verification_runs (Phase 5 scaffold)
-- =======================================================================
-- One row per (audit_run, verifier) tuple. Phase 4 just creates the
-- table; Phase 5 populates it with the 7-stage verify loop:
--   1. pdp_renders                     (4 of 7 exist today)
--   2. pdp_in_sitemap
--   3. gsc_url_submitted               <- existing GSC integration
--   4. gsc_indexing_status              <- existing GSC integration
--   5. pivota_internal_retrieval        (PARTIAL today)
--   6. frontend_agent_cite              (MISSING — codex-blocked)
--   7. public_llm_citation_movement     (MISSING)

CREATE TABLE IF NOT EXISTS verification_runs (
    verify_id          UUID PRIMARY KEY,
    audit_run_id       UUID NOT NULL,
    product_key        TEXT NULL,
    -- Which verifier ran. See list above.
    verifier_id        TEXT NOT NULL,
    -- pending | running | succeeded | failed | blocked
    -- (blocked = upstream system not available; not a soft failure)
    status             TEXT NOT NULL DEFAULT 'pending',
    -- Per-verifier evidence (response payload, status code, etc.).
    evidence_jsonb     JSONB NULL,
    error_message      TEXT NULL,
    last_checked_at    TIMESTAMPTZ NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at       TIMESTAMPTZ NULL
);

COMMENT ON TABLE verification_runs IS
  'P4.1: scaffold for the 7-stage Phase 5 verify loop. One row per (audit, verifier). Phase 4 just creates the table; Phase 5 populates.';

CREATE INDEX IF NOT EXISTS idx_verify_audit_run
  ON verification_runs (audit_run_id, verifier_id);

CREATE INDEX IF NOT EXISTS idx_verify_status_pending
  ON verification_runs (verifier_id, last_checked_at)
  WHERE status IN ('pending', 'running');


-- =======================================================================
-- report_projections — 5-audience output cache
-- =======================================================================

CREATE TABLE IF NOT EXISTS report_projections (
    projection_id      UUID PRIMARY KEY,
    audit_run_id       UUID NOT NULL,
    -- The 5 audiences. Validated in db/audit_evidence.py.
    --   employee_bd: full report for BD operators
    --   merchant: action-queue-focused merchant-portal shape
    --   internal_ops: cost+latency dashboard
    --   pivota_pdp_feed: PDP rendering hints for agent.pivota.cc
    --   frontend_agent_feed: agent-discoverable JSON-LD-ish shape
    audience           TEXT NOT NULL,
    -- The rendered shape. Built from evidence_items +
    -- readiness_findings + action_plan_items + verification_runs.
    payload_jsonb      JSONB NOT NULL,
    -- Bump when the builder logic changes; a follow-up job
    -- re-renders projections at the old version.
    builder_version    TEXT NOT NULL,
    built_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Unique per (audit_run, audience) — re-rendering overwrites
    -- the existing row.
    UNIQUE (audit_run_id, audience)
);

COMMENT ON TABLE report_projections IS
  'P4.1: cached 5-audience rendered shape. GET /api/audits/{id}?audience=X reads from this cache. Re-built when canonical tables change.';

CREATE INDEX IF NOT EXISTS idx_projections_audience
  ON report_projections (audience, built_at);
