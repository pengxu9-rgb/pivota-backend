-- P5.8.1: close two P0 gaps on the Phase 4 canonical tables.
--
-- P0-1: tenancy is single-layer (route check only). The canonical
--       tables had no merchant_id column, so any internal worker /
--       renderer / route that reads by audit_run_id alone bypasses
--       the route-layer guard. Add merchant_id NOT NULL on all 4
--       tables.
--
-- P0-2: persist_canonical_evidence is not idempotent — worker
--       crash + reclaim re-runs persist and doubles all rows.
--       Add idempotency_key TEXT + partial unique index. Builder
--       computes a deterministic key per (audit_run_id, item-signature).
--
-- Both additive + idempotent. Existing data backfilled from the
-- corresponding merchant_audit_runs row.

-- =======================================================================
-- evidence_items
-- =======================================================================

ALTER TABLE evidence_items
  ADD COLUMN IF NOT EXISTS merchant_id TEXT;
ALTER TABLE evidence_items
  ADD COLUMN IF NOT EXISTS idempotency_key TEXT;

-- Backfill merchant_id from the audit row. Best-effort — rows
-- whose audit_run was already pruned stay NULL; the post-backfill
-- NOT NULL constraint is added AFTER (so the backfill doesn't fail
-- on stragglers). Phase 6 can clean up legacy nulls.
UPDATE evidence_items e
   SET merchant_id = m.merchant_id
  FROM merchant_audit_runs m
 WHERE e.audit_run_id = m.run_id
   AND e.merchant_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_evidence_items_merchant
  ON evidence_items (merchant_id, audit_run_id)
  WHERE merchant_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_evidence_items_idempotency
  ON evidence_items (audit_run_id, idempotency_key)
  WHERE idempotency_key IS NOT NULL;

-- =======================================================================
-- readiness_findings
-- =======================================================================

ALTER TABLE readiness_findings
  ADD COLUMN IF NOT EXISTS merchant_id TEXT;
ALTER TABLE readiness_findings
  ADD COLUMN IF NOT EXISTS idempotency_key TEXT;

UPDATE readiness_findings f
   SET merchant_id = m.merchant_id
  FROM merchant_audit_runs m
 WHERE f.audit_run_id = m.run_id
   AND f.merchant_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_findings_merchant
  ON readiness_findings (merchant_id, audit_run_id)
  WHERE merchant_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_findings_idempotency
  ON readiness_findings (audit_run_id, idempotency_key)
  WHERE idempotency_key IS NOT NULL;

-- =======================================================================
-- action_plan_items
-- =======================================================================

ALTER TABLE action_plan_items
  ADD COLUMN IF NOT EXISTS merchant_id TEXT;
ALTER TABLE action_plan_items
  ADD COLUMN IF NOT EXISTS idempotency_key TEXT;

UPDATE action_plan_items a
   SET merchant_id = m.merchant_id
  FROM merchant_audit_runs m
 WHERE a.audit_run_id = m.run_id
   AND a.merchant_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_actions_merchant
  ON action_plan_items (merchant_id, audit_run_id)
  WHERE merchant_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_actions_idempotency
  ON action_plan_items (audit_run_id, idempotency_key)
  WHERE idempotency_key IS NOT NULL;

-- =======================================================================
-- report_projections
-- =======================================================================

ALTER TABLE report_projections
  ADD COLUMN IF NOT EXISTS merchant_id TEXT;
-- Note: no idempotency_key needed; (audit_run_id, audience) UNIQUE
-- already prevents duplicates.

UPDATE report_projections p
   SET merchant_id = m.merchant_id
  FROM merchant_audit_runs m
 WHERE p.audit_run_id = m.run_id
   AND p.merchant_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_projections_merchant
  ON report_projections (merchant_id, audience);
