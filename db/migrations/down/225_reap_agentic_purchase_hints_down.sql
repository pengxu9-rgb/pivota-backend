-- Reverse of 225_reap_agentic_purchase_hints.sql.
--
-- THIS DROPS CATALOG ASSERTIONS A HUMAN MADE, and they are not recoverable from anywhere else:
-- the accepted aliases and the extra domains were supplied per purchase by the caller and are
-- stored nowhere but here. Rows in flight ('resolving' / 'quoting') that depended on an alias
-- will refuse on their next resolve with `options:sole_label_differs` rather than fail loudly,
-- which is the behaviour migration 224 had. Drain the rail, or accept those refusals, before
-- running this.
--
-- `IF EXISTS` on each column so a partial apply — where the schema-guard self-heal added some of
-- them and the migration was never run — reverses cleanly rather than aborting on the first
-- absent column. Separate ALTER statements for the same reason: one ALTER with three DROPs is
-- all-or-nothing per statement, and there is nothing to be gained by coupling them.
ALTER TABLE IF EXISTS reap_agentic_purchases DROP COLUMN IF EXISTS market_country;
ALTER TABLE IF EXISTS reap_agentic_purchases DROP COLUMN IF EXISTS also_accept_domains;
ALTER TABLE IF EXISTS reap_agentic_purchases DROP COLUMN IF EXISTS accept_variant_labels;
