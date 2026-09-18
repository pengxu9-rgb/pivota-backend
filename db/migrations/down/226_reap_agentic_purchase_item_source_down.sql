-- Reverse of 226_reap_agentic_purchase_item_source.sql.
--
-- REFUSES WHILE A cart_link PURCHASE IS IN FLIGHT. Dropping the two columns does not delete a
-- cart_link row; it turns it into what every row without an item_source is, a reap_variant
-- row. The next poll would then hand it to the RESOLVER, which searches Reap's catalogue by
-- product name and could quote an object nobody chose. So the first statement raises if any
-- non-terminal cart_link row exists: drain the lane (turn REAP_AGENTIC_CART_LINK_ENABLED off
-- and let those rows finish or expire) before running this. Terminal rows are safe to demote:
-- nothing advances a terminal row.
--
-- cart_url FIRST: its CHECK names item_source. `IF EXISTS` everywhere so a partial apply (the
-- self-heal added one column, the migration never ran) reverses cleanly.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name = 'reap_agentic_purchases'
           AND column_name = 'item_source'
    ) THEN
        IF EXISTS (
            SELECT 1 FROM reap_agentic_purchases
             WHERE item_source = 'cart_link'
               AND state NOT IN ('completed', 'failed', 'refused', 'expired')
        ) THEN
            RAISE EXCEPTION
                'reap_agentic_purchases still has in-flight cart_link rows; drain the lane first';
        END IF;
    END IF;
END
$$;
ALTER TABLE IF EXISTS reap_agentic_purchases DROP COLUMN IF EXISTS cart_url;
ALTER TABLE IF EXISTS reap_agentic_purchases DROP COLUMN IF EXISTS item_source;
