-- Index `external_product_seeds.attached_product_key`.
--
-- Why now: the quarantine anti-join in `agent_pdp_view_assembler` and
-- `agent_pdp_view_reconciler_cron` read `cp.source_domain` ALONE, while the two
-- other comparators over the same table read the full
-- `coalesce(cp.source_domain, eps.domain, epm.domain, ms.domain)` chain. On a
-- row with a NULL `cp.source_domain` — 4,007 of 14,124 in prod — the assembler
-- therefore kept a quarantined storefront's row in the canonical pick while the
-- trust layer marked it blocked (#1643).
--
-- Widening those two to the full chain needs a lateral lookup by
-- `attached_product_key`, and the only existing indexes mentioning that column
-- are PARTIAL ones on `WHERE attached_product_key IS NULL` — i.e. exactly the
-- opposite of the lookup. Measured on a prod-shaped corpus (14,124 products /
-- 11,381 seeds), assembler query, median of 200:
--
--   narrow chain (today)          0.118 ms
--   wide chain, no index          2.555 ms   <- 21x regression on the APV rebuild path
--   wide chain, with this index   0.172 ms
--
-- So this index is what makes the correctness fix affordable.
--
-- `catalog_row_trust_upserter`'s `minted_seed_one` CTE also groups by this
-- column, so it benefits too.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_external_product_seeds_attached_product_key
  ON external_product_seeds (attached_product_key)
  WHERE attached_product_key IS NOT NULL;
