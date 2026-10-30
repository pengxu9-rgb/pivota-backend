-- Dropping the table also makes delete_agent_pdp_view_if_unbuildable a no-op (it fails closed without
-- it). Export unrestored rows first if any reaped key may still need restoring.
DROP TABLE IF EXISTS agent_pdp_view_unbuildable_reap_audit;
