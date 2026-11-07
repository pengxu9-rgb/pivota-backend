-- Drops the hard-404 run clock (migration 279). Turn EXTERNAL_SEED_DEAD_UNCONFIRMED_HIDE off and roll the
-- image back first: the observation writer SELECTs and UPDATEs this column on every observation.
ALTER TABLE IF EXISTS external_product_seeds
  DROP COLUMN IF EXISTS destination_dead_404_since;
