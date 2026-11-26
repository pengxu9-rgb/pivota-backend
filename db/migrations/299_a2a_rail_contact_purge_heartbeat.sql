-- 299: THE A2A CONTACT PURGE'S HEARTBEAT. One row, stamped by every enabled run of
-- jobs/a2a_rail_contact_purge that finished its purge. The create route (on web) reads it beside the
-- worker's heartbeat (278) and opens no A2A purchase unless a purge run finished within the last 50
-- minutes: the buyer terms promise the buyer's email and address are deleted within 72 hours, and a
-- purchase opened while nothing purges would break that promise. Its own table, because 278 is
-- CHECK (id = 1).
--
-- The same DDL is in db/a2a_rail_ledger (`ensure_a2a_rail_schema`), run by
-- db/schema_guard.ensure_required_schema_light on both dialects; tests/test_a2a_rail_ledger_postgres.py
-- proves the two build the same schema. A new, empty table: it locks nothing that exists.
--
-- schema-guard-exempt: this migration adds no column to an existing table.

CREATE TABLE IF NOT EXISTS a2a_rail_contact_purge_heartbeat (
    id SMALLINT PRIMARY KEY CONSTRAINT ck_a2a_rail_contact_purge_heartbeat_one CHECK (id = 1),
    ran_at TIMESTAMPTZ NOT NULL
);
