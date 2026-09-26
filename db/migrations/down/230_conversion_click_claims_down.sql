-- Reverse of 230_conversion_click_claims.sql.
--
-- DROPPING THE CLAIMS REOPENS THE DOUBLE EDGE. Once the table is gone, the merchant-side helper
-- fails open (it closes as it did before 230) and the Reap side fails closed (it skips the edge
-- and records `attribution_claim_unavailable`). So a cart-link click that one channel has not
-- yet closed can still get one edge, from the merchant side. But nothing then stops a sale
-- whose Reap edge is already written from ALSO getting a merchant edge. Drain the cart-link lane
-- (REAP_AGENTIC_CART_LINK_ENABLED off, all cart_link rows terminal, merchant closes caught up)
-- before running this.
DROP TABLE IF EXISTS conversion_click_claims;
