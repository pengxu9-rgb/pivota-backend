-- 190: upstream-probe bookkeeping on merchant_stores.
--
-- WHY THIS EXISTS (issue #1648, root cause).
-- Store lifecycle is modelled entirely from PUSH signals: the Shopify
-- `app/uninstalled` webhook flips merchant_stores.status -> 'disconnected'
-- (routes/webhook_routes.py), the portal flips it on disconnect/delete. Nothing
-- ever asks the upstream platform whether a store we believe is connected is
-- still connected. When the webhook is missed the row stays 'active' forever:
-- 92sfrj-bi.myshopify.com sat status='active' for ~3 weeks after the founder
-- deactivated it upstream, which is how a retired test rig kept serving on
-- public search.
--
-- scripts/sweep_stale_catalog_products.py structurally CANNOT catch this: it
-- compares each row's last_seen_in_sync_at against the merchant's own
-- last_full_sync_at, and a dead store freezes both together, so no row ever
-- goes stale. The only way to learn a store is gone is to PULL — probe it.
--
-- These columns are the probe's memory. They exist so the flip decision is
-- (a) evidence-based and (b) auditable after the fact:
--
--   upstream_probe_at          when we last actually reached out
--   upstream_probe_status      the classified outcome (see the service)
--   upstream_probe_http_status the raw HTTP status, so a misclassification is
--                              diagnosable without re-probing
--   upstream_probe_failures    CONSECUTIVE hard-auth failures. The flip needs
--                              two, which is what keeps a transient 401 (token
--                              refresh race, platform blip) from disconnecting
--                              a live merchant. Reset to 0 on any successful
--                              probe.
--
-- Deliberately NO default on upstream_probe_at / _status: NULL means "never
-- probed", which must stay distinguishable from "probed and healthy" — a
-- default would erase exactly the distinction the reconciliation job reads to
-- decide what is due.

ALTER TABLE merchant_stores
  ADD COLUMN IF NOT EXISTS upstream_probe_at TIMESTAMPTZ;

ALTER TABLE merchant_stores
  ADD COLUMN IF NOT EXISTS upstream_probe_status TEXT;

ALTER TABLE merchant_stores
  ADD COLUMN IF NOT EXISTS upstream_probe_http_status INTEGER;

ALTER TABLE merchant_stores
  ADD COLUMN IF NOT EXISTS upstream_probe_failures INTEGER NOT NULL DEFAULT 0;

COMMENT ON COLUMN merchant_stores.upstream_probe_at IS
  'When services/store_lifecycle_service.py last probed this store upstream. '
  'NULL = never probed (and therefore always due). Written on EVERY probe '
  'outcome, including transient failures, so the job cannot spin on one store.';

COMMENT ON COLUMN merchant_stores.upstream_probe_status IS
  'Classified outcome of the last probe: ok | auth_failed | store_closed | '
  'permission_denied | unreachable | no_credentials | unsupported_platform. '
  'Only auth_failed and store_closed are treated as evidence the store is gone; '
  'everything else is explicitly NOT a deactivation signal.';

COMMENT ON COLUMN merchant_stores.upstream_probe_http_status IS
  'Raw HTTP status from the last probe, or NULL when no response was received '
  '(timeout/DNS/TLS). Kept so a wrong classification is diagnosable from the DB.';

COMMENT ON COLUMN merchant_stores.upstream_probe_failures IS
  'Count of CONSECUTIVE hard failures (auth_failed/store_closed). Reset to 0 by '
  'any ok probe. The job flips status -> disconnected only at >= 2, so a single '
  'transient auth error can never disconnect a live merchant.';
