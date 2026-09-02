-- Migration 207: reject a malformed merchant_psps.psp_id AT WRITE TIME.
--
-- WHY THIS EXISTS
-- ---------------
-- `orders` has carried CHECK check_psp_id_format since migration 006:
--
--     CHECK (psp_id IS NULL OR psp_id ~* '^psp_[a-z0-9]+_[a-z0-9]{12}$')
--
-- `merchant_psps` -- the table that MINTS the value -- had no such rule. Order
-- creation copies merchant_psps.psp_id straight into orders.psp_id
-- (routes/order_routes.py `_resolve_active_order_psp` -> db/orders.create_order),
-- so a psp_id the writer accepted became a 500 at the reader, hours or days
-- later. On 2026-08-29 merchant merch_c5e24a8d3738d73b onboarded via
-- POST /merchant/onboarding/setup-psp, which minted an EIGHT-char suffix
-- (`psp_stripe_30cc4106`) instead of twelve. Saving succeeded, validation
-- succeeded, and every checkout then died on the orders CHECK. The generator bug
-- is fixed in routes/employee_store_psp_fixes.py; this constraint is what makes
-- the NEXT one fail at onboarding instead of at the merchant's first sale.
--
-- The regex is byte-identical to migration 006's, deliberately: the whole point
-- is that the writer and the reader agree. `~*` (case-insensitive) matches 006
-- too, so an id that passes here cannot fail there.
--
-- Unlike orders.psp_id, merchant_psps.psp_id is the PRIMARY KEY and is never
-- NULL, so there is no `IS NULL OR` branch.
--
-- WHY `NOT VALID`
-- ---------------
-- `NOT VALID` skips the scan of EXISTING rows; it still enforces the rule on
-- every subsequent INSERT and UPDATE. That is the behaviour we want, for two
-- reasons:
--
--   1. This runner applies files in one transaction each at startup. If any
--      already-onboarded merchant carries a malformed id -- and we know at least
--      one did -- a validating ADD CONSTRAINT would abort the migration and take
--      the boot with it. A migration that cannot apply protects nothing.
--   2. Those rows are ALREADY broken: their merchant cannot create an order at
--      all. Failing their PSP save too is not a regression we should ship
--      silently, so the repair is an explicit, reviewable ops step rather than a
--      surprise UPDATE on production key material.
--
-- FIND AND REPAIR THE EXISTING ROWS
-- ---------------------------------
--     python scripts/audit_malformed_psp_ids.py            # report only
--     python scripts/audit_malformed_psp_ids.py --repair   # rewrite to canonical
--
-- or, by hand:
--
--     SELECT psp_id, merchant_id, provider, status, connected_at
--       FROM merchant_psps
--      WHERE psp_id !~* '^psp_[a-z0-9]+_[a-z0-9]{12}$'
--      ORDER BY connected_at DESC;
--
-- Rewriting is safe: `orders.psp_id` is the only other column in the schema that
-- holds a merchant_psps.psp_id, and its own CHECK has made it impossible for a
-- malformed id to be referenced there. Once the report is empty, promote the
-- constraint with:
--
--     ALTER TABLE merchant_psps VALIDATE CONSTRAINT check_merchant_psps_psp_id_format;
--
-- (VALIDATE takes only a SHARE UPDATE EXCLUSIVE lock and does not block writes.)

DO $$
BEGIN
    IF to_regclass('merchant_psps') IS NULL THEN
        RETURN;
    END IF;

    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'check_merchant_psps_psp_id_format'
           AND conrelid = to_regclass('merchant_psps')
    ) THEN
        ALTER TABLE merchant_psps
            ADD CONSTRAINT check_merchant_psps_psp_id_format
            CHECK (psp_id ~* '^psp_[a-z0-9]+_[a-z0-9]{12}$')
            NOT VALID;
    END IF;

    -- Inside the guard, and via EXECUTE, so the WHOLE file is one statement that
    -- is a no-op when merchant_psps does not exist. A bare COMMENT ON would
    -- abort the file on a database that has not built the table yet.
    EXECUTE $c$
        COMMENT ON COLUMN merchant_psps.psp_id IS
        'Canonical PSP configuration id, format psp_{provider}_{12 lowercase alphanumerics}. Minted by services.merchant_psp_config_service._generate_psp_id; must satisfy the same regex as orders.psp_id (migration 006).'
    $c$;
END $$;
