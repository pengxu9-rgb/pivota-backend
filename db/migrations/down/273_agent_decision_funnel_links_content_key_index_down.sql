-- Nothing depends on the index for correctness; dropping it only makes the reads in 273's header slow.
DROP INDEX CONCURRENTLY IF EXISTS idx_agent_decision_funnel_links_content_key;
