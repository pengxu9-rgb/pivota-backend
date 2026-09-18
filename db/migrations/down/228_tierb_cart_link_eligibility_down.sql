-- Reverse of 228_tierb_cart_link_eligibility.sql.
--
-- The UNIQUE (shop_domain, market) constraint and its index go with the table. This drops the
-- recorded eligibility verdicts, which is safe on a dark lane: nothing reads them yet, and the
-- next daily run rebuilds every row from the storefronts. Once the purchase flow's start gate
-- reads is_cart_link_eligible, dropping this table closes Tier B for every merchant until the
-- table is back and a run has completed (a missing row reads as not eligible).
DROP TABLE IF EXISTS tierb_cart_link_eligibility;
