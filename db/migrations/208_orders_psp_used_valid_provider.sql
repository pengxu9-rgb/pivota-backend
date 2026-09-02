-- Migration 208: `orders.psp_used` must accept every provider this code WRITES.
--
-- WHY THIS EXISTS
-- ---------------
-- Migration 006 froze the provider vocabulary at five names:
--
--     CHECK (psp_used IS NULL OR psp_used IN
--            ('stripe','adyen','checkout','paypal','braintree'))
--
-- The code moved on; the constraint did not. `orders.psp_used` is written from
-- exactly one place -- routes/order_routes.py `_resolve_active_order_psp` copies
-- `merchant_psps.provider` into it via db/orders.create_order -- and
-- `merchant_psps.provider` can hold values 006 never heard of:
--
--   * 'antom'  -- in services/merchant_psp_config_service.SUPPORTED_CANONICAL_PSPS,
--                 accepted by POST /merchant/integrations/psp/connect.
--   * 'square' -- named in POST /merchant/onboarding/setup-psp's own capabilities
--                 map and in the ConnectPSPRequest comment. That route took
--                 `psp_type: str` with NO allowlist and wrote status='active';
--                 `fetch_active_runtime_merchant_psp` filters on status only, so
--                 the value came straight back out at order creation.
--
-- Both were reproduced against Postgres 15 on 2026-09-01: setup-psp returns 200
-- "connected successfully", the portal shows the PSP, and then EVERY order
-- creation dies with
--
--     new row for relation "orders" violates check constraint
--     "check_psp_used_valid_provider"
--
-- -- the identical failure mode as the psp_id-format defect fixed in 20f4542c: a
-- value the writer accepts and the reader refuses, silent between onboarding and
-- the merchant's first sale.
--
-- TWO ENDS, TWO FIXES
-- -------------------
-- 'square' has no PSP adapter anywhere in this repo (only catalog/storefront
-- sync), so it is NOT a provider `orders` should learn -- it is a value the door
-- should never have taken. routes/employee_store_psp_fixes.py now rejects it with
-- a 400 at onboarding, matching the allowlists the sibling doors have carried all
-- along (/admin/psp/connect, /merchant/integrations/psp/connect).
--
-- 'antom' is real and supported, so the constraint widens to admit it. This file
-- is the reader-side half; the endpoint allowlist is the writer-side half. Both
-- are needed: an allowlist on ONE route does not cover the routes nobody has
-- audited yet, and a constraint alone still tells the merchant at their first
-- sale instead of at onboarding.
--
-- THE FULL LIST, AND WHERE EACH NAME COMES FROM
-- ---------------------------------------------
--   stripe, adyen, checkout   SUPPORTED_CANONICAL_PSPS; live adapters.
--   paypal                    accepted by /admin/psp/connect; in 006 already.
--   braintree                 in 006 already. No adapter, but dropping a name is
--                             a NARROWING that could reject a legacy row, and
--                             this migration must only ever widen.
--   antom                     SUPPORTED_CANONICAL_PSPS. NEW.
--   protocol_deferred         routes/order_routes.CAPABILITY_DEFERRED_PSP_PROVIDER
--                             -- the sentinel a capability-gated deferred order
--                             carries when it has no merchant_psps row. It never
--                             charges. NEW; see the note below.
--
-- 'protocol_deferred' was the SECOND latent instance of this same defect: the
-- deferred lane writes it to psp_used and writes
-- a `<merchant_id>`-prefixed sentinel to psp_id, and BOTH violated their
-- constraints -- so turning AGENT_CHECKOUT_CAPABILITY_GATE on would have 500'd
-- every order it was meant to enable. The psp_id sentinel is fixed in
-- routes/order_routes.py to satisfy check_psp_id_format; this list fixes the
-- psp_used half. NOTE: that lane remains default-OFF and unexercised. Removing
-- its guaranteed CHECK failure is not a statement that the lane works.
--
-- WHY `NOT VALID`, AND WHY THE GUARD
-- ----------------------------------
-- The new list is a strict SUPERSET of 006's, so no row that satisfies the old
-- constraint can fail the new one. But this constraint may not be installed at
-- all on a given database: production fast mode skips db/migrations/ entirely,
-- and 006 predates the startup_sql_migrations ledger. On such a database this is
-- an ADD, not a widen, and a validating ADD would scan rows written across years
-- of a vocabulary nobody constrained -- aborting the migration, and with it the
-- boot. `NOT VALID` skips the scan of existing rows while enforcing every
-- subsequent INSERT and UPDATE, which is the behaviour we want. A migration that
-- cannot apply protects nothing.
--
-- The `pg_get_constraintdef` guard makes the whole file a no-op once the widened
-- constraint is in place. That matters because db/schema_guard.py runs the same
-- logic on EVERY boot: an unconditional DROP+ADD would take an ACCESS EXCLUSIVE
-- lock on `orders` -- the table every checkout writes -- once per instance start.
-- Matching on the two NEW tokens rather than on the exact definition text also
-- means the guard re-fires if something re-narrows the constraint, which
-- routes/admin_run_migration.py's /admin/migrations/run/006-psp-constraints used
-- to do on every invocation (fixed in the same change).
--
-- FIND THE ROWS THIS FILE DOES NOT REPAIR
-- ---------------------------------------
--     python scripts/audit_unaccepted_psp_providers.py --database-url "$DATABASE_URL"
--
-- reports merchant_psps rows whose provider is still outside this list -- each is
-- a merchant who CANNOT create an order. Once it comes back empty:
--
--     ALTER TABLE orders VALIDATE CONSTRAINT check_psp_used_valid_provider;
--
-- (VALIDATE takes only a SHARE UPDATE EXCLUSIVE lock and does not block writes.)

DO $$
BEGIN
    IF to_regclass('orders') IS NULL THEN
        RETURN;
    END IF;

    -- Already widened? Then do nothing -- and in particular do not take the
    -- ACCESS EXCLUSIVE lock a DROP+ADD needs.
    IF EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'check_psp_used_valid_provider'
           AND conrelid = to_regclass('orders')
           AND pg_get_constraintdef(oid) LIKE '%antom%'
           AND pg_get_constraintdef(oid) LIKE '%protocol_deferred%'
    ) THEN
        RETURN;
    END IF;

    ALTER TABLE orders DROP CONSTRAINT IF EXISTS check_psp_used_valid_provider;
    ALTER TABLE orders
        ADD CONSTRAINT check_psp_used_valid_provider
        CHECK (
            psp_used IS NULL OR psp_used IN (
                'stripe',
                'adyen',
                'checkout',
                'paypal',
                'braintree',
                'antom',
                'protocol_deferred'
            )
        )
        NOT VALID;

    -- ...then earn the validated status back where it is earnable.
    --
    -- Measured in production 2026-09-02: this constraint is INSTALLED and
    -- convalidated=TRUE, on an `orders` of 593 rows with zero violations. A bare
    -- DROP + ADD NOT VALID would therefore DOWNGRADE a proven invariant to a
    -- merely-enforced one and hand the operator a manual step to get it back --
    -- a regression this file would have shipped silently. The new list is a
    -- strict SUPERSET of the old, so on any database whose old constraint was
    -- validated, no existing row can fail the new one.
    --
    -- VALIDATE takes only SHARE UPDATE EXCLUSIVE: it does not block reads or
    -- writes. Bounded by a local timeout and swallowed, so the two cases this
    -- file was originally written for still behave exactly as before -- a table
    -- too large to scan in time, or one holding genuinely bad rows because 006
    -- never ran, simply keeps the constraint NOT VALID, which still enforces
    -- every new INSERT and UPDATE. The boot is never blocked and never aborted.
    BEGIN
        SET LOCAL statement_timeout = '15s';
        ALTER TABLE orders VALIDATE CONSTRAINT check_psp_used_valid_provider;
    EXCEPTION WHEN OTHERS THEN
        RAISE NOTICE 'check_psp_used_valid_provider left NOT VALID: %', SQLERRM;
    END;

    -- Inside the guard, and via EXECUTE, so the WHOLE file is one statement that
    -- is a no-op on a database with no `orders` table. A bare COMMENT ON would
    -- abort the file there.
    EXECUTE $c$
        COMMENT ON CONSTRAINT check_psp_used_valid_provider ON orders IS
        'Provider vocabulary orders.psp_used may hold. Superset of migration 006''s; must cover every value merchant_psps.provider can carry plus routes/order_routes.CAPABILITY_DEFERRED_PSP_PROVIDER. Widened by migration 208; twinned in db/schema_guard.py because prod fast mode skips db/migrations/.'
    $c$;
END $$;
