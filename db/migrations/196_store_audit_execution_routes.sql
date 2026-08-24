-- Store Audit Phase 1: domain-keyed execution routes and canonical
-- acceptance-signal evidence.
--
-- A route belongs to a commerce domain, not to a Pivota merchant record.
-- merchant_id is deliberately nullable so BD cold-start can gather evidence
-- before onboarding. At conversion, claim the route with one UPDATE; do not
-- duplicate the route or backfill its evidence onto a new merchant-keyed row.
--
-- Evidence semantics:
--   * evidence_items.evidence_level is only DETECTED or TESTED.
--   * verification_runs.status retains its existing work-queue state machine.
--     In particular, blocked means the upstream is unavailable and is not
--     retryable by that run.
--   * evidence expiry is represented by expires_at. "expired" is derived at
--     read time, not stored as a competing evidence/run status.
--
-- Idempotent and safe to re-run.

CREATE TABLE IF NOT EXISTS execution_routes (
    execution_route_id UUID PRIMARY KEY,
    -- Lower-cased host only; no scheme, path, port, or trailing dot.
    normalized_domain TEXT NOT NULL,
    route_kind TEXT NOT NULL,
    -- Canonical absolute endpoint used for the route identity.
    endpoint_normalized TEXT NOT NULL,
    -- Association only. NULL is the correct value for a cold-start prospect.
    merchant_id TEXT NULL,
    claimed_at TIMESTAMPTZ NULL,
    profile_fingerprint TEXT NULL,
    -- Canonical audit run to which the latest route observation belongs.
    -- Re-probes retain this evidence lane instead of creating a parallel run.
    last_audit_run_id UUID NULL,
    first_detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_verified_at TIMESTAMPTZ NULL,
    expires_at TIMESTAMPTZ NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_execution_routes_normalized_domain
      CHECK (
        normalized_domain = lower(btrim(normalized_domain))
        AND normalized_domain <> ''
      ),
    CONSTRAINT ck_execution_routes_claimed_at
      CHECK (merchant_id IS NOT NULL OR claimed_at IS NULL),
    CONSTRAINT uq_execution_routes_domain_kind_endpoint
      UNIQUE (normalized_domain, route_kind, endpoint_normalized)
);

ALTER TABLE execution_routes
  ADD COLUMN IF NOT EXISTS last_audit_run_id UUID NULL;

COMMENT ON TABLE execution_routes IS
  'Store Audit Phase 1: reusable domain-keyed merchant-commerce execution routes. merchant_id is an optional claimed association, not part of route identity.';
COMMENT ON COLUMN execution_routes.merchant_id IS
  'Nullable association. Cold-start prospects leave this NULL; onboarding claims the existing domain route with one UPDATE.';
COMMENT ON COLUMN execution_routes.endpoint_normalized IS
  'Canonical absolute endpoint. Uniqueness is domain + route kind + endpoint, never merchant_id.';
COMMENT ON COLUMN execution_routes.last_audit_run_id IS
  'Canonical audit run receiving the most recent route observation; used by the separate domain/TTL re-probe lane.';

CREATE INDEX IF NOT EXISTS idx_execution_routes_merchant
  ON execution_routes (merchant_id, is_active)
  WHERE merchant_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_execution_routes_reprobe
  ON execution_routes (expires_at, normalized_domain)
  WHERE is_active = TRUE AND expires_at IS NOT NULL;

ALTER TABLE evidence_items
  ADD COLUMN IF NOT EXISTS execution_route_id UUID NULL,
  ADD COLUMN IF NOT EXISTS evidence_level TEXT NULL,
  ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'fk_evidence_items_execution_route'
  ) THEN
    ALTER TABLE evidence_items
      ADD CONSTRAINT fk_evidence_items_execution_route
      FOREIGN KEY (execution_route_id)
      REFERENCES execution_routes (execution_route_id)
      ON DELETE RESTRICT;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'ck_evidence_items_evidence_level'
  ) THEN
    ALTER TABLE evidence_items
      ADD CONSTRAINT ck_evidence_items_evidence_level
      CHECK (evidence_level IS NULL OR evidence_level IN ('detected', 'tested'));
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'ck_evidence_items_acceptance_signal_shape'
  ) THEN
    ALTER TABLE evidence_items
      ADD CONSTRAINT ck_evidence_items_acceptance_signal_shape
      CHECK (
        evidence_type <> 'acceptance_signal'
        OR (execution_route_id IS NOT NULL AND evidence_level IS NOT NULL)
      );
  END IF;

  -- Route evidence collected for a cold-start prospect is accessed through
  -- execution_routes after the route is claimed. Persisting the synthetic
  -- prospect id here would strand it behind merchant-scoped reads.
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'ck_evidence_items_route_not_synthetic_merchant'
  ) THEN
    ALTER TABLE evidence_items
      ADD CONSTRAINT ck_evidence_items_route_not_synthetic_merchant
      CHECK (
        execution_route_id IS NULL
        OR merchant_id IS NULL
        OR merchant_id !~ '^prospect_'
      );
  END IF;
END $$;

COMMENT ON COLUMN evidence_items.evidence_level IS
  'Route evidence confidence class: detected or tested. It is not verification_runs.status; blocked and expired are derived from run status and expires_at.';
COMMENT ON COLUMN evidence_items.expires_at IS
  'Freshness bound for evidence. Expired is derived at read time, not persisted as a status.';

CREATE INDEX IF NOT EXISTS idx_evidence_items_execution_route
  ON evidence_items (execution_route_id, created_at)
  WHERE execution_route_id IS NOT NULL;

ALTER TABLE verification_runs
  ADD COLUMN IF NOT EXISTS execution_route_id UUID NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'fk_verification_runs_execution_route'
  ) THEN
    ALTER TABLE verification_runs
      ADD CONSTRAINT fk_verification_runs_execution_route
      FOREIGN KEY (execution_route_id)
      REFERENCES execution_routes (execution_route_id)
      ON DELETE RESTRICT;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'ck_verification_runs_route_not_synthetic_merchant'
  ) THEN
    ALTER TABLE verification_runs
      ADD CONSTRAINT ck_verification_runs_route_not_synthetic_merchant
      CHECK (
        execution_route_id IS NULL
        OR merchant_id IS NULL
        OR merchant_id !~ '^prospect_'
      );
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_verification_runs_execution_route
  ON verification_runs (execution_route_id, status, created_at)
  WHERE execution_route_id IS NOT NULL;

-- Scheduler-side prechecks reduce normal contention; this unique partial
-- index is the durable backstop when two schedulers race the same route.
CREATE UNIQUE INDEX IF NOT EXISTS uq_verification_runs_active_route_verifier
  ON verification_runs (execution_route_id, verifier_id)
  WHERE execution_route_id IS NOT NULL
    AND status IN ('pending', 'claimed');
