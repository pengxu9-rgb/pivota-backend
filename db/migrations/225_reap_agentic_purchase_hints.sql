-- RESOLUTION HINTS ON THE PURCHASE ROW — the three values the resolver needs on EVERY
-- re-resolve, and which migration 224 gave it nowhere to keep.
--
-- WHY THIS IS A MIGRATION AND NOT A PARAMETER. `start_purchase` and `advance` run in DIFFERENT
-- PROCESSES: the first is a request handler, the second is a poller step that may run minutes
-- later on another pod, and may run again after that. Anything the resolver needs that is not in
-- a COLUMN is gone by the time the resolve happens. services/reap_agentic_purchase.py (PR #2204)
-- names that gap in `PurchaseRow` and acts on it today by REFUSING the purchase outright with
-- `resolution_hints_not_persistable` — a correct refusal of an incapability, and the incapability
-- is this table. The Flower Beauty Tier-A row is the worked example: it cannot resolve at all
-- without an accepted alias for its sole variant label.
--
-- WHAT EACH COLUMN IS. All three are per-row assertions a HUMAN made about ONE purchase, so they
-- belong on the purchase row rather than in a merchant-wide table:
--
--   accept_variant_labels — "these labels name the same object as my variant_title". Without it
--       a sole-variant merchant whose label spells the shade differently refuses as
--       `options:sole_label_differs`, blaming the label for what is really our storage.
--   also_accept_domains   — "this domain is also this merchant". Storefronts that redirect or
--       that sell the same catalogue under a second hostname.
--   market_country        — the buyer's market, ISO-3166-1 alpha-2. Dropping it degrades RECALL
--       (an out-of-market variant comes back in another currency or at another price, and is
--       refused downstream), so it cannot buy the wrong object — but the refusal is a round trip
--       that this column avoids.
--
-- NOT PII, AND NOT IDENTITY. These are CATALOG assertions about products and hostnames. Nothing
-- here names the buyer, so unlike shipping_address and buyer_email they are NOT nulled when the
-- purchase reaches a terminal state — a completed purchase that kept the alias it resolved
-- through is exactly the record you want when the same alias is questioned later. They are also
-- not in db/reap_agentic_ledger.PUBLIC_PURCHASE_COLUMNS: harmless to show, but the allowlist is
-- kept minimal on principle and an owner-facing read has no use for them.
--
-- jsonb RATHER THAN A text[] ARRAY, for the reason the module header gives at length: this rail
-- is raw SQL with no SQLAlchemy bind or result processor in front of it, so an array type would
-- arrive as a driver-specific shape on one dialect and as nothing at all on the other. jsonb has
-- a SQLite twin (TEXT holding JSON) that `_bind_json`/`_decode_json` already carry, which is what
-- lets one code path serve both engines. NULL means "no hints", NOT `[]` — an empty list and an
-- absent list are the same assertion here, and one spelling of it is enough.
--
-- Production deploys skip db/migrations/, so these three ADD COLUMNs are ALSO in
-- db/schema_guard.ensure_required_schema_light — BYTE-IDENTICAL on the Postgres branch, and with
-- the SQLite twin's usual TEXT/TIMESTAMP substitutions on the other. The catalog-parity test
-- tests/test_reap_agentic_ledger_postgres.py::test_the_self_heal_builds_the_same_schema_as_the_
-- migration compares what the DATABASE built from each, so a divergence here is a failure there
-- rather than a surprise in production.

ALTER TABLE IF EXISTS reap_agentic_purchases
    ADD COLUMN IF NOT EXISTS accept_variant_labels JSONB,
    ADD COLUMN IF NOT EXISTS also_accept_domains JSONB,
    ADD COLUMN IF NOT EXISTS market_country TEXT;
