-- 284: order_quote_claims, the database guard that one quote backs at most one order per agent.
--
-- Written only by db/orders.py `create_order`, before the order row, for every order priced from a
-- quote: one INSERT ... ON CONFLICT DO NOTHING on the primary key (agent_id, quote_id). The row
-- names the one order id that quote may use, and orders.order_id is a primary key too, so two
-- creates for one quote can never both write an order, whatever a read before the insert said.
-- agent_id is '' for an order created with a quote and no agent. Rows are never deleted: a
-- cancelled or soft-deleted order still used its quote.
--
-- A new, empty table that references no other table. No row written before it existed can violate
-- it, so creating it cannot fail on old data, and it locks nothing that exists.
--
-- Also declared in db/orders.py; create_all creates it at startup (prod does not apply numbered
-- migrations). This file is the record, and what the SQL gates plan against. Until the table exists
-- `create_order` writes orders without the guard and logs a WARNING for each one.
-- schema-guard-exempt: CREATE TABLE only, no ADD COLUMN; create_all builds the same table at boot.

CREATE TABLE IF NOT EXISTS order_quote_claims (
  agent_id    VARCHAR(255) NOT NULL,
  quote_id    VARCHAR(255) NOT NULL,
  order_id    VARCHAR(50)  NOT NULL,
  created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
  PRIMARY KEY (agent_id, quote_id)
);
