-- 081_seed_content_lock_and_proposals.sql
--
-- Long-term fix for the "codex re-audit / re-backfill keeps polluting
-- previously-good PDP content" loop reported 2026-05-09. Adds three
-- pieces of infrastructure so write paths can no longer silently
-- regress vetted content:
--
--   1. external_product_seeds.content_lock — per-field lock metadata.
--      Once a field is vetted (auditor pass + quality gate), it is
--      pinned by hash. Subsequent writes that try to change a locked
--      field must either supply a higher-quality replacement (auto
--      relocks) OR explicitly unlock first. The actual decision logic
--      lives in services/seed_data_writer.upsert_seed_data.
--
--   2. seed_data_proposals — staging table for proposed writes.
--      Codex skills, mirror jobs, and recovery scripts emit proposals
--      here instead of writing directly. The writer service evaluates
--      each proposal against the current state and either merges it
--      or marks it rejected. This is the audit log: every proposed
--      change is recorded with its quality score, who proposed it,
--      and why.
--
--   3. enforce_seed_data_lock trigger — last-line defence. Any UPDATE
--      to external_product_seeds.seed_data that did NOT come through
--      the writer service (which sets pivota.seed_write_token =
--      'authorized' on the session) is logged for review. Initially
--      ships in DRY-RUN mode — logs violations via RAISE NOTICE but
--      allows the write — so we can observe before enforcing.
--      Phase O-9 follow-up flips it to RAISE EXCEPTION once we've
--      confirmed all three callers (employee_products, audit worker,
--      mirror script) consistently set the token.
--
-- See feedback memory "feedback_codex_continuous_pollution" and
-- conversation 2026-05-09 for the chain of incidents that motivated
-- this. The previous fixes (PR #409 preserve review fields, PR #413
-- audit funnel, PR #420 recursive decode) all addressed symptoms;
-- this migration addresses the root cause: no write-time gate that
-- prevents lower-quality content from overwriting higher-quality
-- content.
--
-- Idempotent: ADD COLUMN / CREATE TABLE / CREATE OR REPLACE.

-- ---------------------------------------------------------------------
-- 1. content_lock column
-- ---------------------------------------------------------------------
--
-- Shape:
--   {
--     "<field_name>": {
--       "hash": "sha256-<64hex>",     -- hash of the locked value
--       "locked_at": "2026-05-09T...", -- ISO8601 UTC
--       "locked_by": "auditor_v1",     -- who set the lock
--       "quality_score": 87.3          -- score that earned the lock
--     },
--     ...
--   }
--
-- Empty object {} = nothing locked. Keys correspond to top-level
-- seed_data fields (description, pdp_ingredients_raw, etc.).

ALTER TABLE external_product_seeds
  ADD COLUMN IF NOT EXISTS content_lock JSONB NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN external_product_seeds.content_lock IS
  'Per-field lock metadata. Once a field is vetted (auditor pass + quality gate), the writer service records hash+timestamp+score here. Subsequent writes must either supply higher-quality content (auto-relocks) or explicitly unlock. Empty object {} = nothing locked. See services/seed_data_writer.py.';


-- ---------------------------------------------------------------------
-- 2. seed_data_proposals table
-- ---------------------------------------------------------------------
--
-- Every write attempt by codex skills / mirror / recovery scripts is
-- recorded here. The writer service decides what to do with each
-- proposal:
--
--   pending     — created, awaiting service eval (or operator review
--                 for high-risk proposers)
--   approved    — service decided this should merge; not yet applied
--   rejected    — service rejected (lock violation, lower quality,
--                 audit fail). Kept for forensics.
--   merged      — applied to external_product_seeds. content_lock
--                 reflects the new hash.
--   superseded  — newer proposal arrived for the same fields before
--                 this one merged.
--
-- This table doubles as a recovery audit log: when content regresses,
-- we can see which proposal caused it and which prior version (if
-- any) it overwrote.

CREATE TABLE IF NOT EXISTS seed_data_proposals (
  id BIGSERIAL PRIMARY KEY,
  seed_id TEXT NOT NULL,
  external_product_id TEXT NOT NULL,
  -- Who proposed: 'codex_seed_correction', 'mirror_path_b',
  -- 'employee_products_csv', 'recovery_archive_20260506', etc.
  proposer TEXT NOT NULL,
  -- Where the data came from: 'canonical_url_refetch',
  -- 'archive_restore', 'csv_import', 'manual_edit'
  source TEXT,
  -- Full proposed seed_data dict. NULL fields = no change to that key.
  -- The writer service does field-level diff against current state.
  proposed_seed_data JSONB NOT NULL,
  -- audit_seed_data() output for the proposal
  audit_summary JSONB,
  -- Per-field quality scores: {"description": 87.3, "pdp_ingredients_raw": 92.0}
  quality_score JSONB,
  -- Status workflow above. Includes 'no_change' for forensic records of
  -- proposals where the candidate matched current state on every field
  -- (the writer still inserts the row for audit-trail completeness).
  -- 'no_change' was missing from the 081 ship; added inline 2026-05-12
  -- so fresh installs get the right constraint shape. Prod schema is
  -- aligned by migration 082_seed_data_proposals_allow_no_change.sql.
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'approved', 'rejected', 'merged', 'superseded', 'no_change')),
  -- Why the service made its decision (lock violation, score lower, etc.)
  decision_reason TEXT,
  -- Snapshot of the fields that would change, taken at decision time.
  -- Format: {"<field>": {"old_value": "...", "new_value": "...",
  --           "old_score": N, "new_score": M, "decision": "merge|reject"}}
  -- Lets us reconstruct exactly what each merged proposal did.
  field_decisions JSONB,
  proposed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  reviewed_at TIMESTAMPTZ,
  merged_at TIMESTAMPTZ,
  -- Optional: human reviewer comment on approval/rejection
  notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_seed_proposals_seed_status
  ON seed_data_proposals (seed_id, status);

CREATE INDEX IF NOT EXISTS idx_seed_proposals_proposer_at
  ON seed_data_proposals (proposer, proposed_at DESC);

-- Partial index for the operator review queue (pending only)
CREATE INDEX IF NOT EXISTS idx_seed_proposals_pending
  ON seed_data_proposals (proposed_at DESC)
  WHERE status = 'pending';

COMMENT ON TABLE seed_data_proposals IS
  'Staging table for proposed writes to external_product_seeds.seed_data. Codex skills, mirror jobs, and recovery scripts MUST write here instead of directly to seed_data. The writer service evaluates each proposal and either merges it (with content_lock relock) or rejects it. This is the audit log of every attempted change. See services/seed_data_writer.py.';


-- ---------------------------------------------------------------------
-- 3. enforce_seed_data_lock trigger (DRY-RUN mode)
-- ---------------------------------------------------------------------
--
-- The writer service sets pivota.seed_write_token = 'authorized' on
-- its connection inside an explicit transaction. Any other UPDATE
-- path won't have set it, so the trigger detects unauthorized writes.
--
-- Initially: dry-run. We RAISE NOTICE the violation, write a row to
-- a violations log table (separate concern, deferred), and ALLOW the
-- write through. This is so we can:
--   (a) observe what paths still bypass the service
--   (b) ship the lock metadata + proposals without breaking anything
--   (c) flip enforcement on in a follow-up PR once telemetry is clean
--
-- Switching to enforce: change the body to RAISE EXCEPTION instead of
-- RAISE NOTICE.

CREATE OR REPLACE FUNCTION enforce_seed_data_lock()
RETURNS TRIGGER AS $$
DECLARE
  write_token TEXT;
  field_key TEXT;
  violation_count INTEGER := 0;
BEGIN
  -- Skip the check entirely if seed_data is unchanged
  IF (OLD.seed_data) IS NOT DISTINCT FROM (NEW.seed_data) THEN
    RETURN NEW;
  END IF;

  -- Authorized writes from services.seed_data_writer set this token
  BEGIN
    write_token := current_setting('pivota.seed_write_token', true);
  EXCEPTION WHEN OTHERS THEN
    write_token := NULL;
  END;

  IF write_token = 'authorized' THEN
    RETURN NEW;
  END IF;

  -- Unauthorized write. In DRY-RUN: log violations per locked field
  -- but allow through. Operator dashboards will show the volume of
  -- bypass attempts; once it drops to ~0 we flip to enforce.
  IF OLD.content_lock IS NOT NULL AND OLD.content_lock != '{}'::jsonb THEN
    FOR field_key IN SELECT jsonb_object_keys(OLD.content_lock) LOOP
      IF (OLD.seed_data->field_key) IS DISTINCT FROM (NEW.seed_data->field_key) THEN
        violation_count := violation_count + 1;
        RAISE NOTICE 'seed_data lock violation (DRY-RUN): seed_id=% field=% locked_by=% — write allowed but bypassed services.seed_data_writer',
          NEW.id, field_key, OLD.content_lock->field_key->>'locked_by';
      END IF;
    END LOOP;
  END IF;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_enforce_seed_data_lock ON external_product_seeds;
CREATE TRIGGER trg_enforce_seed_data_lock
  BEFORE UPDATE OF seed_data ON external_product_seeds
  FOR EACH ROW
  EXECUTE FUNCTION enforce_seed_data_lock();

COMMENT ON FUNCTION enforce_seed_data_lock() IS
  'Gate for direct writes to external_product_seeds.seed_data. Authorized writes from services.seed_data_writer set pivota.seed_write_token=authorized inside their transaction; this trigger respects that. Unauthorized writes that touch a locked field are logged via RAISE NOTICE (DRY-RUN). Flip to RAISE EXCEPTION once bypass volume = 0 (Phase O-9 follow-up).';
