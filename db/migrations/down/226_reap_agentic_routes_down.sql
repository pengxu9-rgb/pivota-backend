-- Reverse of 226_reap_agentic_routes.sql.
--
-- WHAT THIS DESTROYS, in the order it matters:
--
--   reap_agentic_buyer_refs      is the ONLY record of which opaque owner id Reap knows a buyer
--                                by. Drop it and every enrolled buyer becomes a stranger: the
--                                next purchase mints a NEW ref, finds no active enrollment under
--                                it, and asks a buyer who has already given us a card to enter
--                                it again. Their old enrollment is not deleted at Reap and not
--                                reachable from here — it is stranded. **Do not run this on a
--                                database where the rail has ever been armed.**
--   reap_agentic_eligibility     is operator configuration. Recoverable only by retyping it.
--                                Dropping it fails the rail CLOSED (every purchase refuses
--                                `merchant_not_eligible`), which is the safe direction.
--   reap_agentic_purchase_keys   is 24-hour replay state. Dropping it costs a duplicate purchase
--                                only for a caller that retries inside that window.
--
-- Dropped in the reverse of the order they were created, which costs nothing here (there are no
-- FKs between them, deliberately — see the up migration) but keeps the two files readable as
-- mirrors of each other.
DROP TABLE IF EXISTS reap_agentic_purchase_keys;
DROP TABLE IF EXISTS reap_agentic_buyer_refs;
DROP TABLE IF EXISTS reap_agentic_eligibility;
