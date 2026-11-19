-- Reverses 290. DESTROYS THE AFFILIATE PROGRAM REGISTRY and its change history: export both tables first
-- if any row was ever written. Switch AFFILIATE_PROGRAM_REGISTRY_ENABLED off first. The boot heal
-- (db/schema_guard -> db/affiliate_programs) recreates the empty tables at the next start unless the code
-- is reverted too. Events first: they reference the programs.
DROP INDEX IF EXISTS idx_affiliate_program_events_program;
DROP TABLE IF EXISTS affiliate_program_events;
DROP INDEX IF EXISTS uq_affiliate_programs_network_advertiser;
DROP TABLE IF EXISTS affiliate_programs;
