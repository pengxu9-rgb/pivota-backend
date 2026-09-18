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
--   reap_agentic_purchase_keys   is 24-hour replay state, plus the hash of the request each key
--                                was used for. Dropping it costs a duplicate purchase for a
--                                caller that retries inside that window -- and, until the window
--                                would have expired anyway, it stops `idempotency_conflict`
--                                being detectable at all, so a key reused on a different body
--                                silently opens a second purchase instead of being refused.
--
-- Dropped in the reverse of the order they were created, which costs nothing here (there are no
-- FKs between them, deliberately — see the up migration) but keeps the two files readable as
-- mirrors of each other.
DROP TABLE IF EXISTS reap_agentic_purchase_keys;
DROP TABLE IF EXISTS reap_agentic_buyer_refs;
DROP TABLE IF EXISTS reap_agentic_eligibility;
