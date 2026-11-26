-- Identity truth set v1 (services/identity_truth_set.py): the US listings that share a barcode.
-- READ ONLY. One text column, one JSON object per listing, for readonly_sql_gated.sh.
-- A barcode is kept when at least two live signed listings carry it (any spelling: leading zeros
-- trimmed here, the check digit and the GS1 restricted ranges are checked in Python). Each listing
-- gets ONE US offer (in stock first, then the lowest price, then the offer id) for its host, price,
-- currency and variant title. Cross-host pairs, labels and vetoes are computed offline in Python.
-- Cost: one pass over catalog_products' barcode-bearing rows, then catalog_offers by product_key
-- for each kept listing (LATERAL ... LIMIT 1). Run it with EXPLAIN first.
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
)
SELECT json_build_object(
           'listing_key', l.product_key, 'host', o.host, 'brand', l.brand, 'title', l.title,
           'variant_title', o.variant_title, 'gtin', l.gtin, 'price', o.price, 'currency', o.currency,
           'availability', o.availability, 'image_url', l.image_url, 'url', l.canonical_url)::text AS row
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
ORDER BY l.g, l.product_key;
COMMIT;
