-- Keep merchant_domain_attestations during a runtime rollback. It is the record of who attested
-- which brand store and on what evidence (and of every revocation); older code never names it, so
-- leaving it costs nothing, and dropping it would erase the audit trail behind labels already written.
SELECT 1;
