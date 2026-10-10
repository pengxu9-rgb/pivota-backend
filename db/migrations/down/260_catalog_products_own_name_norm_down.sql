-- Drop the trigger and the function; keep the columns. Older code never names them, a drop would
-- rewrite nothing but would cost a re-backfill on the way back up, and NULLs are exactly what the
-- gateway's fallback (today's expression) handles.
DROP TRIGGER IF EXISTS trg_catalog_products_stamp_name_norm ON catalog_products;
DROP FUNCTION IF EXISTS catalog_products_stamp_name_norm();
DROP FUNCTION IF EXISTS catalog_products_identity_fold(TEXT);
