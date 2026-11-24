-- 296: A HUMAN'S CLOSE OF A PROVIDER-CONFIRMED DECLINE, recorded append-only (payment orchestration, A2A rail).
--
-- One row per purchase that jobs/a2a_rail_operator.py `close-declined` ended as `failed` /
-- `operator_confirmed_declined` (services/payment_orchestration/a2a_rail_decline_close.py): the purchase was
-- held as `checkout_payment_reached_declined` (or the legacy `checkout_payment_reached_unconfirmed`, with a
-- bound lookup that confirmed the decline), and the provider confirmed in writing that no authorization was
-- approved or captured and no order exists at the merchant. The transition and this row are written in ONE
-- statement (db/a2a_rail_ledger._CLOSE_DECLINED_SQL: UPDATE ... RETURNING feeding the INSERT).
--
-- No PII, no card data, no statement text, no token id: an opaque reference to the statement, the hash of
-- its file, the server-computed evidence digest, the operator handle, the times, the two attestations and
-- whether a lookup ran and agreed. Append-only: the trigger refuses UPDATE, DELETE and TRUNCATE.
--
-- THIS TABLE HAS NO STARTUP HEAL, ON PURPOSE (schema-guard-exempt: no startup heal; a gated one-off builds it).
-- Production does not run db/migrations/ at boot, and this file is applied there once, by hand, as a gated
-- one-off after its PR has merged. Nothing in db/schema_guard or db/a2a_rail_ledger creates the table, its
-- function or its trigger, so a deploy never builds them ahead of that run. Until it has run,
-- db/a2a_rail_ledger.manual_resolution_audit_ready answers False and the close refuses
-- (`manual_resolution_audit_not_ready`). A new, empty table.

CREATE TABLE IF NOT EXISTS a2a_rail_manual_resolution_audit (
    purchase_id VARCHAR(64) PRIMARY KEY,
    from_state VARCHAR(32) NOT NULL CHECK (from_state = 'paying'),
    from_code VARCHAR(64) NOT NULL
        CHECK (from_code IN ('checkout_payment_reached_declined', 'checkout_payment_reached_unconfirmed')),
    from_state_entered_at TIMESTAMPTZ NOT NULL,
    resolved_state VARCHAR(32) NOT NULL CHECK (resolved_state = 'failed'),
    resolution_code VARCHAR(64) NOT NULL CHECK (resolution_code = 'operator_confirmed_declined'),
    provider_status VARCHAR(16) NOT NULL CHECK (provider_status = 'DECLINED'),
    operator_ref VARCHAR(64) NOT NULL,
    evidence_source VARCHAR(64) NOT NULL CHECK (evidence_source = 'provider_written_statement'),
    evidence_reference VARCHAR(128) NOT NULL,
    evidence_file_sha256 VARCHAR(64) NOT NULL,
    evidence_observed_at TIMESTAMPTZ NOT NULL,
    provider_reference_kind VARCHAR(32) NOT NULL
        CHECK (provider_reference_kind IN ('checkout_task_id', 'external_order_reference')),
    provider_reference VARCHAR(128) NOT NULL,
    attests_no_authorization BOOLEAN NOT NULL CHECK (attests_no_authorization = TRUE),
    attests_no_merchant_order BOOLEAN NOT NULL CHECK (attests_no_merchant_order = TRUE),
    lookup_ran BOOLEAN NOT NULL,
    lookup_agreed BOOLEAN NOT NULL CHECK (lookup_agreed = lookup_ran),
    provider_origin VARCHAR(255) NOT NULL,
    evidence_sha256 VARCHAR(64) NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE OR REPLACE FUNCTION prevent_a2a_rail_manual_resolution_audit_mutation() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION 'a2a_rail_manual_resolution_audit is append-only';
END;
$fn$;

DROP TRIGGER IF EXISTS a2a_rail_manual_resolution_audit_immutable ON a2a_rail_manual_resolution_audit;
CREATE TRIGGER a2a_rail_manual_resolution_audit_immutable
    BEFORE UPDATE OR DELETE OR TRUNCATE ON a2a_rail_manual_resolution_audit
    FOR EACH STATEMENT EXECUTE FUNCTION prevent_a2a_rail_manual_resolution_audit_mutation();
