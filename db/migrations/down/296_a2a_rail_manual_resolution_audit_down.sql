-- Reverses 296. The close-declined command refuses while the table or its trigger is missing
-- (a2a_rail_ledger.manual_resolution_audit_ready), so dropping them stops every close; held rows stay held.
DROP TRIGGER IF EXISTS a2a_rail_manual_resolution_audit_immutable ON a2a_rail_manual_resolution_audit;
DROP TABLE IF EXISTS a2a_rail_manual_resolution_audit;
DROP FUNCTION IF EXISTS prevent_a2a_rail_manual_resolution_audit_mutation();
