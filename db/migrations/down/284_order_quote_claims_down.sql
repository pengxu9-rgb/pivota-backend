-- Drops order_quote_claims (migration 284). Safe at any time: db/orders.py `create_order` then
-- writes orders without the one-order-per-quote guard and logs a WARNING for each one. Dropping
-- forgets which quotes already have an order, so re-creating the table starts empty.
DROP TABLE IF EXISTS order_quote_claims;
