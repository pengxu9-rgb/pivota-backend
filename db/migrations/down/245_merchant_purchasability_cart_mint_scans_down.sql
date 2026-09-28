-- Reverses 245_merchant_purchasability_cart_mint_scans.sql. Without the table the sweep's cart-mint lane
-- cannot tell whether it scanned today, so it does NOT scan (it will not fall back to an hourly full scan);
-- the lane is counted unreadable and the run exits 1 while the other lanes are still swept.
--
-- NOT STICKY ON ITS OWN: db/merchant_purchasability_cart_mint_scans.ensure_table() recreates it on the next
-- sweep. Remove that call in the same release if the table must stay gone.
DROP TABLE IF EXISTS merchant_purchasability_cart_mint_scans;
