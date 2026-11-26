-- Identity truth set v1 (services/identity_truth_set.py): the US listings that share a barcode
-- across two or more hosts. READ ONLY. One text column for readonly_sql_gated.sh, prefix TRUTHSET:
-- every row is 'TRUTHSET_ROW ' + one JSON object, so the runner's Cloud Logging fallback (which keeps
-- only lines starting with the prefix) keeps the rows too.
-- A barcode is kept when live signed listings on at least two hosts carry it (any spelling: leading
-- zeros trimmed here; the check digit and the GS1 restricted ranges are checked in Python). Each
-- listing gets ONE US offer (in stock first, then the lowest price, then the offer id) for its host,
-- price, currency and variant title. Pairs, labels and vetoes are computed offline in Python.
-- At most 1800 rows (the runner's readback reads 2000 log lines): exactly 1800 means truncated;
-- the offline parser then drops the last barcode group, which may be partial, and the run is narrowed.
-- The plan is reviewed first: ops_identity_truth_set_barcode_rows_explain.sql (the same SELECT).
BEGIN READ ONLY;
SET LOCAL statement_timeout = '20s';
SET LOCAL lock_timeout = '2s';
WITH live AS (
    SELECT p.product_key, p.brand, p.title, p.gtin, p.image_url, p.canonical_url,
           ltrim(btrim(p.gtin), '0') AS g
    FROM catalog_products p
    WHERE p.gtin IS NOT NULL AND btrim(p.gtin) ~ '^[0-9]{8,14}$'
      AND p.pivota_signature_id IS NOT NULL AND p.suppressed_at IS NULL AND p.suppression_reason IS NULL
      AND p.sync_status = 'live'
),
shared AS (
    SELECT g FROM live WHERE g <> '' GROUP BY g HAVING count(*) >= 2
),
cand AS (
    SELECT l.product_key, l.brand, l.title, l.gtin, l.image_url, l.canonical_url, l.g,
           o.host, o.price, o.currency, o.availability, o.variant_title
    FROM live l
    JOIN shared s ON s.g = l.g
    JOIN LATERAL (
        SELECT lower(coalesce(nullif(btrim(o.source_domain), ''),
                              substring(o.offer_payload->>'destination_url' from '^[A-Za-z][A-Za-z0-9+.-]*://([^/:?#]+)'))) AS host,
               coalesce(o.merchant_effective_price, o.list_price) AS price, o.currency, o.availability,
               k.title AS variant_title
        FROM catalog_offers o
        LEFT JOIN catalog_skus k ON k.sku_key = o.sku_key AND k.suppressed_at IS NULL AND k.suppression_reason IS NULL
        WHERE o.product_key = l.product_key AND o.market = 'US'
          AND o.suppressed_at IS NULL AND o.suppression_reason IS NULL
        ORDER BY (lower(coalesce(o.availability, '')) IN ('in_stock', 'instock', 'limited_stock', 'low_stock', 'available')) DESC,
                 coalesce(o.merchant_effective_price, o.list_price) NULLS LAST, o.offer_id
        LIMIT 1
    ) o ON true
),
cross_host AS (
    SELECT g FROM cand WHERE host IS NOT NULL GROUP BY g HAVING count(DISTINCT host) >= 2
)
SELECT 'TRUTHSET_ROW ' || json_build_object(
           'listing_key', c.product_key, 'host', c.host, 'brand', c.brand, 'title', c.title,
           'variant_title', c.variant_title, 'gtin', c.gtin, 'price', c.price, 'currency', c.currency,
           'availability', c.availability, 'image_url', c.image_url, 'url', c.canonical_url)::text AS row
FROM cand c
JOIN cross_host x ON x.g = c.g
ORDER BY c.g, c.product_key
LIMIT 1800;
COMMIT;
