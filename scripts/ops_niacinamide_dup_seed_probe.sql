-- ops_niacinamide_dup_seed_probe.sql
--
-- READ-ONLY diagnostics for the "20% NIACINAMIDE High Potency Dark Spot
-- Serum" (Jumiso USA) near-duplicate test-variant pollution.
--
-- Context
-- -------
-- ~22 near-duplicate external_product_seeds rows exist for this product
-- under the singleton synthetic merchant 'external_seed'. Their titles
-- carry junk suffixes appended during manual / QA cloning:
--   (Copy), (Copy_b)..(Copy_h), (Copy_e), (Copy_T1)..(Copy_T7), (Convert_a)
-- Each distinct suffix produces a distinct content_key on the
-- catalog_products mirror, which is why the serving-side near-dup
-- collapse (PIVOTA-Agent #1738/#1739/#1740) only demotes the ones whose
-- collapse key matches — the exotic-suffix copies leak into the page.
--
-- Root cause = these fake rows should not exist. This probe enumerates
-- them in BOTH mirrors so the canonical row can be confirmed before the
-- cleanup runner (scripts/cleanup_niacinamide_test_variants.py) is run.
--
-- Usage:
--   psql "$DATABASE_URL" -f scripts/ops_niacinamide_dup_seed_probe.sql
--
-- This script does NOT mutate data.

\echo '=== 0) clock ==='
SELECT now() AS captured_at_utc;

-- Junk-suffix predicate (case-insensitive, anchored at title end):
--   matches a trailing "(Copy...)" or "(Convert...)" parenthetical.
-- The clean canonical title has no such suffix and is NOT matched.
\echo '=== 1) external_product_seeds — every row in the product group ==='
SELECT
  eps.id,
  eps.status,
  eps.title,
  COALESCE(
    NULLIF(btrim(eps.seed_data#>>'{snapshot,brand}'), ''),
    NULLIF(btrim(eps.seed_data->>'brand'), ''),
    NULLIF(btrim(eps.seed_data#>>'{snapshot,vendor}'), ''),
    NULLIF(btrim(eps.seed_data->>'vendor'), '')
  ) AS brand,
  eps.external_product_id,
  eps.attached_product_key,
  eps.created_by_employee_id,
  eps.tool,
  eps.created_at,
  (eps.title ~* '\(\s*(copy|convert)') AS is_junk_suffix
FROM external_product_seeds eps
WHERE eps.title ILIKE '%High Potency Dark Spot Serum%'
ORDER BY is_junk_suffix ASC, eps.created_at ASC;

\echo '=== 1.1) seed group counts (active/junk split) ==='
SELECT
  COUNT(*) AS total_in_group,
  COUNT(*) FILTER (WHERE status = 'active') AS active,
  COUNT(*) FILTER (WHERE status = 'active'
                     AND title ~* '\(\s*(copy|convert)') AS active_junk,
  COUNT(*) FILTER (WHERE status = 'active'
                     AND title !~* '\(\s*(copy|convert)') AS active_clean_candidates,
  COUNT(*) FILTER (WHERE attached_product_key IS NOT NULL
                     AND btrim(attached_product_key) <> '') AS attached_rows
FROM external_product_seeds
WHERE title ILIKE '%High Potency Dark Spot Serum%';

\echo '=== 1.2) the clean canonical candidate(s) — expect exactly ONE active ==='
SELECT id, status, title, external_product_id, attached_product_key, created_at
FROM external_product_seeds
WHERE title ILIKE '%High Potency Dark Spot Serum%'
  AND title !~* '\(\s*(copy|convert)'
ORDER BY created_at ASC;

\echo '=== 2) catalog_products mirror rows for this group (merchant external_seed) ==='
-- The mirror keys each row: source_ref = external_product_seeds.id,
-- source_product_id = external_product_id. Joined here on source_ref.
SELECT
  cp.product_key,
  cp.source_ref AS seed_id,
  cp.title,
  cp.brand,
  cp.content_key,
  cp.sync_status,
  cp.suppression_reason,
  (cp.title ~* '\(\s*(copy|convert)') AS is_junk_suffix
FROM catalog_products cp
WHERE cp.merchant_id = 'external_seed'
  AND cp.title ILIKE '%High Potency Dark Spot Serum%'
ORDER BY is_junk_suffix ASC, cp.created_at ASC;

\echo '=== 2.1) catalog_products mirror counts ==='
SELECT
  COUNT(*) AS total_mirror_rows,
  COUNT(*) FILTER (WHERE suppression_reason IS NULL) AS live_unsuppressed,
  COUNT(*) FILTER (WHERE suppression_reason IS NULL
                     AND title ~* '\(\s*(copy|convert)') AS live_junk,
  COUNT(DISTINCT content_key) AS distinct_content_keys
FROM catalog_products
WHERE merchant_id = 'external_seed'
  AND title ILIKE '%High Potency Dark Spot Serum%';
