-- Phase 7c backfill — add synthetic variant + availability to the 18
-- agent-authored lipstick seeds inserted before Phase 7c shipped.
--
-- Without this, services.external_referral_readiness.evaluate_external_referral_seed
-- audits each seed, flags zero_variants (severity=blocker), and
-- routes/agent_api.py:_build_external_seed_product silently drops the
-- seed at recall time. Probe v9 returned 0/9 lipstick queries because
-- of this. The Python ingestion path is fixed in this same PR; this
-- SQL just patches the rows that already landed.
--
-- Idempotent: only touches rows where seed_data->'variants' is missing
-- or empty. Safe to re-run.
--
-- Run against the production database. Production is Cloud Run
-- (pivota-prod/us-west1) on Cloud SQL and its DATABASE_URL resolves to a PRIVATE
-- IP, so there is no dashboard SQL console and no laptop route to it. Pipe this
-- file in from inside the VPC instead, e.g. via a throwaway job on the
-- production image (see docs/runbooks/operating_on_gcp_production.md).
-- (Kept as a .sql file because it works even when external
-- proxy throttles ad-hoc psql connections). Or via psql once external
-- proxy recovers.

UPDATE external_product_seeds
SET
  availability = COALESCE(
    NULLIF(availability, ''),
    CASE
      WHEN COALESCE((seed_data->>'in_stock')::boolean, true) THEN 'in_stock'
      ELSE 'out_of_stock'
    END
  ),
  seed_data = seed_data || jsonb_build_object(
    -- Synthetic single variant — the audit only requires non-empty
    -- variants[] with currency matching the row's price_currency.
    'variants', jsonb_build_array(
      jsonb_build_object(
        'variant_id', external_product_id || '::canonical',
        'id',         external_product_id || '::canonical',
        'sku',        external_product_id,
        'title',      title,
        'currency',   COALESCE(price_currency, 'USD'),
        'price_amount', price_amount,
        'price',        price_amount,
        'availability', CASE
          WHEN COALESCE((seed_data->>'in_stock')::boolean, true) THEN 'in_stock'
          ELSE 'out_of_stock'
        END,
        'in_stock', COALESCE((seed_data->>'in_stock')::boolean, true)
      )
    ),
    -- Mirror title + availability inside seed_data for any consumer
    -- that reads from the JSON rather than the row column.
    'title', COALESCE(seed_data->>'title', title),
    'availability', CASE
      WHEN COALESCE((seed_data->>'in_stock')::boolean, true) THEN 'in_stock'
      ELSE 'out_of_stock'
    END,
    -- Promote image_url into a single-element image_urls array so the
    -- audit's collect_seed_image_urls picks it up.
    'image_urls', CASE
      WHEN image_url IS NOT NULL AND TRIM(image_url) <> '' THEN jsonb_build_array(image_url)
      ELSE COALESCE(seed_data->'image_urls', '[]'::jsonb)
    END
  ),
  updated_at = NOW()
WHERE tool = 'catalog_enrichment_agent_v1'
  AND status = 'active'
  AND (
    seed_data->'variants' IS NULL
    OR jsonb_array_length(COALESCE(seed_data->'variants', '[]'::jsonb)) = 0
  );

-- Verification — expected: 18 rows updated; all 18 should now show
-- variant_count >= 1, availability = 'in_stock', and have title/image_urls
-- mirrored into seed_data.
SELECT
  COUNT(*) AS total_agent_seeds,
  COUNT(*) FILTER (WHERE availability = 'in_stock') AS in_stock,
  COUNT(*) FILTER (WHERE jsonb_array_length(COALESCE(seed_data->'variants', '[]'::jsonb)) >= 1) AS has_variant,
  COUNT(*) FILTER (WHERE seed_data ? 'title') AS has_seed_title,
  COUNT(*) FILTER (WHERE jsonb_array_length(COALESCE(seed_data->'image_urls', '[]'::jsonb)) >= 1) AS has_image_urls
FROM external_product_seeds
WHERE tool = 'catalog_enrichment_agent_v1' AND status = 'active';
