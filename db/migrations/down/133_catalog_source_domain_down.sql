ALTER TABLE IF EXISTS catalog_offers
  DROP COLUMN IF EXISTS source_domain;

ALTER TABLE IF EXISTS catalog_skus
  DROP COLUMN IF EXISTS source_domain;

ALTER TABLE IF EXISTS catalog_products
  DROP COLUMN IF EXISTS source_domain;
