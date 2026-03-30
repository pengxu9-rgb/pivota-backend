-- Merchant Commerce Readiness Query Pack
-- Use these queries for Stage B / Stage C operator dashboards and cohort signoff reviews.

-- 1) Fleet rollup by platform and four-domain readiness state.
SELECT
  COALESCE(primary_platform, 'unknown') AS primary_platform,
  foundation_status,
  discover_status,
  signals_status,
  execute_status,
  COUNT(*) AS merchant_count
FROM merchant_commerce_readiness_state
GROUP BY 1, 2, 3, 4, 5
ORDER BY merchant_count DESC, primary_platform;

-- 2) Stage B target mix: Shopify + Wix cohort capacity.
SELECT
  COALESCE(primary_platform, 'unknown') AS primary_platform,
  COUNT(*) AS merchant_count,
  SUM(CASE WHEN foundation_status = 'ready' THEN 1 ELSE 0 END) AS foundation_ready_count,
  SUM(CASE WHEN discover_status = 'ready' THEN 1 ELSE 0 END) AS discover_ready_count,
  SUM(CASE WHEN signals_status = 'ready' THEN 1 ELSE 0 END) AS signals_ready_count,
  SUM(CASE WHEN execute_status = 'ready' THEN 1 ELSE 0 END) AS execute_ready_count
FROM merchant_commerce_readiness_state
GROUP BY 1
ORDER BY merchant_count DESC, primary_platform;

-- 3) Merchant blockers by domain.
WITH blocker_rows AS (
  SELECT
    merchant_id,
    primary_platform,
    'foundation' AS domain,
    jsonb_array_elements_text(COALESCE(foundation_blockers, '[]'::jsonb)) AS blocker
  FROM merchant_commerce_readiness_state
  UNION ALL
  SELECT
    merchant_id,
    primary_platform,
    'discover' AS domain,
    jsonb_array_elements_text(COALESCE(discover_blockers, '[]'::jsonb)) AS blocker
  FROM merchant_commerce_readiness_state
  UNION ALL
  SELECT
    merchant_id,
    primary_platform,
    'signals' AS domain,
    jsonb_array_elements_text(COALESCE(signals_blockers, '[]'::jsonb)) AS blocker
  FROM merchant_commerce_readiness_state
  UNION ALL
  SELECT
    merchant_id,
    primary_platform,
    'execute' AS domain,
    jsonb_array_elements_text(COALESCE(execute_blockers, '[]'::jsonb)) AS blocker
  FROM merchant_commerce_readiness_state
)
SELECT
  domain,
  COALESCE(primary_platform, 'unknown') AS primary_platform,
  blocker,
  COUNT(*) AS merchant_count
FROM blocker_rows
GROUP BY 1, 2, 3
ORDER BY domain, merchant_count DESC, blocker;

-- 4) Merchants closest to default-on but still blocked.
SELECT
  merchant_id,
  primary_platform,
  foundation_status,
  discover_status,
  signals_status,
  execute_status,
  foundation_blockers,
  discover_blockers,
  signals_blockers,
  execute_blockers,
  metadata ->> 'indexed_exposure' AS indexed_exposure,
  metadata ->> 'surfaced_exposure' AS surfaced_exposure,
  metadata ->> 'clicked_exposure' AS clicked_exposure,
  metadata ->> 'ordered_conversion' AS ordered_conversion,
  observed_at
FROM merchant_commerce_readiness_state
WHERE foundation_status != 'ready'
   OR discover_status != 'ready'
   OR signals_status != 'ready'
   OR execute_status != 'ready'
ORDER BY observed_at DESC, merchant_id
LIMIT 50;

-- 5) Signals issue leaderboard from canonical ledger + attribution artifacts.
WITH issue_rollup AS (
  SELECT
    merchant_id,
    'LISTING_ERROR' AS issue_code,
    COUNT(*) AS issue_count
  FROM surface_listing_errors
  GROUP BY merchant_id
  UNION ALL
  SELECT
    merchant_id,
    'MISSING_INFO' AS issue_code,
    COUNT(*) AS issue_count
  FROM surface_click_events
  WHERE canonical_variant_id IS NULL OR TRIM(canonical_variant_id) = ''
  GROUP BY merchant_id
  UNION ALL
  SELECT
    merchant_id,
    'UNATTRIBUTED_ORDER' AS issue_code,
    COUNT(*) AS issue_count
  FROM commerce_attribution_edges
  WHERE click_id IS NULL OR TRIM(click_id) = ''
  GROUP BY merchant_id
  UNION ALL
  SELECT
    merchant_id,
    'TRACE_BROKEN' AS issue_code,
    COUNT(*) AS issue_count
  FROM commerce_attribution_edges cae
  WHERE click_id IS NOT NULL
    AND NOT EXISTS (
      SELECT 1
      FROM surface_click_events sce
      WHERE sce.click_id = cae.click_id
    )
  GROUP BY merchant_id
)
SELECT
  merchant_id,
  issue_code,
  SUM(issue_count) AS issue_count
FROM issue_rollup
GROUP BY 1, 2
ORDER BY issue_count DESC, merchant_id, issue_code;

-- 6) Ledger convergence over time.
SELECT
  DATE_TRUNC('day', occurred_at) AS event_day,
  event_type,
  COUNT(*) AS event_count,
  COUNT(DISTINCT interaction_id) AS interaction_count
FROM commerce_interaction_events
GROUP BY 1, 2
ORDER BY event_day DESC, event_type;
