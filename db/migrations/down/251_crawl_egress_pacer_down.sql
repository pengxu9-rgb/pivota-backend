-- Reverses 251_crawl_egress_pacer.sql. The table holds only a pacing schedule; dropping it loses nothing but the
-- current reservation horizon (at most one lease per process, seconds long).
--
-- NOT STICKY ON ITS OWN while CRAWL_SHOPIFY_EDGE_PACER_ENABLED is on anywhere: db/crawl_egress_pacer.ensure_table()
-- recreates it at the next lease. Turn the flag off in the same release if the table must stay gone.
DROP TABLE IF EXISTS crawl_egress_pacer;
