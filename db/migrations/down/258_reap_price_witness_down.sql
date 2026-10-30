-- Keep the price-witness columns during a runtime rollback. They are evidence of what a buyer
-- was quoted (and, for a corroborated lower price, why a purchase continued at it); older code
-- never names them, so leaving them costs nothing.
SELECT 1;
