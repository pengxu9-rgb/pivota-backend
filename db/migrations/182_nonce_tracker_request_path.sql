-- Migration 182: nonce_tracker.request_path (corrective)
-- Date: 2026-07-17
-- Purpose: Reconcile the committed nonce_tracker schema with the AP2 code.
--
-- Migration 021 created nonce_tracker WITHOUT a request_path column, but three
-- AP2 code paths INSERT it:
--   * services/consent_service.py         create_consent
--   * middleware/ap2_security.py          verify_ap2_nonce
--   * routes/admin_protocol_sync.py        list_nonces (SELECT)
-- No migration between 021 and 180 adds it, so a fresh env (or any env that
-- applied 021 as committed) would raise "column request_path does not exist"
-- the moment an AP2 nonce path runs. This is latent today because the AP2
-- router is disabled everywhere (ENABLE_AP2_ROUTES=false); resolve it before
-- AP2 is enabled in any environment. Surfaced during review of PR #1441.
--
-- request_path records which endpoint consumed the nonce (audit/forensics for
-- replay-attack investigation). Additive + nullable: legacy rows stay NULL.
--
-- Idempotent (ADD COLUMN IF NOT EXISTS) so it is safe whether or not the column
-- already exists — it corrects drift without assuming the current live state.

-- schema-guard-exempt: nonce_tracker is an opt-in AP2 table. It is NOT created
-- by db/schema_guard.py (prod fast-mode startup) and the AP2 router is disabled
-- in every environment (ENABLE_AP2_ROUTES=false). The whole AP2 schema (021 +
-- this) is applied on demand via the admin migration runner before AP2 is
-- enabled, so this column needs no startup self-heal. If AP2 ever moves to a
-- boot-time table, add nonce_tracker to ensure_required_schema_light and drop
-- this marker.

ALTER TABLE IF EXISTS nonce_tracker
  ADD COLUMN IF NOT EXISTS request_path TEXT;

COMMENT ON COLUMN nonce_tracker.request_path IS
  'Request path that consumed this nonce (AP2 replay-attack audit trail)';

DO $$
BEGIN
    RAISE NOTICE '✅ Migration 182 completed - nonce_tracker.request_path present';
END $$;
