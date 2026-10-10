-- Drop the triggers and the functions; keep the columns. Nothing in the backend names them, the
-- gateway's reader falls back to today's expression on NULL (and is flagged), and a drop would only
-- cost a re-backfill on the way back up.
DROP TRIGGER IF EXISTS trg_catalog_products_stamp_name_norm_insert ON catalog_products;
DROP TRIGGER IF EXISTS trg_catalog_products_stamp_name_norm_update ON catalog_products;
DROP TRIGGER IF EXISTS trg_catalog_products_stamp_name_norm ON catalog_products;
DROP FUNCTION IF EXISTS catalog_products_stamp_name_norm();
DROP FUNCTION IF EXISTS catalog_products_identity_fold(TEXT);
