-- A3 — the run-level AUDIT BASIS: what a run was measured WITH.
--
-- WHAT IS ALREADY PINNED, AND WHAT IS NOT. services/prompt_basis.py pins the
-- PROMPT SET: a re-audit replays the exact prior questions (prompt_set_id /
-- selected_set_id), regeneration is an explicit versioned event, and tiers are
-- isolated. That closed the loudest source of non-comparability — the 82->50
-- swing on identical URLs — but it is only ONE component of the measurement.
--
-- Everything else still floats free:
--
--   * the MODEL behind each provider (services/coverage_profiles.
--     resolve_provider_models reads a config default that a deploy can change,
--     and a caller may override it per run);
--   * the TIER MIX — how many branded vs discovery-intent questions the
--     selected set actually contained, which moves every share-style number
--     even when the prompt-set id is unchanged;
--   * the OFFICIAL DOMAIN SET (migration 207), which decides first_party on
--     every cited host and therefore the headline "AI sent buyers to your own
--     store" number — a domain added between two runs moves that number with no
--     change in the world;
--   * the PRIMARY DESTINATION RULE (migration 209 /
--     services/primary_destination.PRIMARY_DESTINATION_VERSION).
--
-- A before/after diff that does not check these can report a rule change, a
-- model swap or a domain-set edit as merchant movement. This table records them
-- once per run so the diff can refuse to claim movement across a changed basis
-- (db/audit_basis.bases_are_comparable).
--
-- IMMUTABLE BY CONSTRUCTION. One row per audit_run_id, INSERT-ONLY: a second
-- record_basis for the same run returns the existing row rather than updating
-- it. A basis that could be rewritten after the fact is not a basis — it would
-- let a later deploy retroactively make two runs look comparable. The unique
-- constraint below is what makes that hold under a concurrent retry, not just
-- under the accessor's own check.
--
-- WHY THE JSON COLUMNS ARE TEXT. providers_and_models, tier_mix and
-- official_domains are written once and read back WHOLE — nothing queries
-- inside them, and comparability compares the decoded documents in Python. A
-- TEXT column holding a JSON document is therefore sufficient, and it keeps
-- this DDL byte-identical between Postgres and SQLite, so the hermetic tests
-- exercise the REAL table rather than a hand-written fixture. It also keeps
-- this table out of the json/jsonb model-vs-migration drift class entirely
-- (see tests/test_model_migration_json_drift* and the sql-prepare gate, where a
-- json column declared jsonb by one path and json by the other has repeatedly
-- produced a CannotCoerceError only production could see).
--
-- Idempotent and safe to re-run.
--
-- Every statement is deliberately portable to SQLite as well as Postgres
-- (CURRENT_TIMESTAMP not NOW(); TEXT not JSONB), because the inline DDL
-- backstop in db/audit_basis.py runs the SAME text. Do not "improve" one copy
-- alone — migrations + schema_guard are the schema truth and they must agree.
--
-- schema-guard-exempt: creates a new table only; adds no column to any existing
-- table, and db/audit_basis.py carries the identical CREATE TABLE as its own
-- startup backstop, called by every accessor in that module.

CREATE TABLE IF NOT EXISTS audit_basis (
    -- Surrogate id. audit_run_id is the real key (UNIQUE below); this exists so
    -- the row can be referenced without carrying the run id around.
    basis_id                   TEXT NOT NULL,
    audit_run_id               TEXT NOT NULL,
    merchant_id                TEXT NOT NULL,
    -- ONE version covering the whole methodology. Bumped when ANY component
    -- changes shape or meaning — deliberately coarse, because a diff needs a
    -- single question ("same methodology?") and a per-component version set
    -- invites a caller to check three of four.
    methodology_version        TEXT NOT NULL,
    -- {provider_id: {"model_id": str, "temperature": float|null}} as JSON.
    providers_and_models       TEXT NOT NULL DEFAULT '{}',
    -- Read from the EXISTING prompt basis (services/prompt_basis.py); recorded
    -- here, never re-derived, so the two can never disagree.
    prompt_set_id              TEXT NULL,
    selected_set_id            TEXT NULL,
    -- {intent_axis: count} as JSON, over services/audit_facts.intent_axis_for —
    -- the CURRENT vocabulary, not a parallel one.
    tier_mix                   TEXT NOT NULL DEFAULT '{}',
    -- The official-domain set (migration 207) as it stood WHEN THE RUN RAN.
    -- A JSON array of hosts.
    official_domains           TEXT NOT NULL DEFAULT '[]',
    -- services/primary_destination.PRIMARY_DESTINATION_VERSION at run time.
    primary_destination_version INTEGER NULL,
    market                     TEXT NULL,
    language                   TEXT NULL,
    currency                   TEXT NULL,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (basis_id),
    -- The immutability guarantee. Two concurrent completions of one run race to
    -- INSERT; this makes the loser a no-op instead of a second basis.
    CONSTRAINT uq_audit_basis_run UNIQUE (audit_run_id)
);

CREATE INDEX IF NOT EXISTS idx_audit_basis_merchant
  ON audit_basis (merchant_id, created_at);

COMMENT ON TABLE audit_basis IS
  'A3: what one audit run was measured WITH — methodology version, provider models, pinned prompt/selected set ids, tier mix, official-domain snapshot, primary-destination rule version, market/language/currency. INSERT-ONLY, one row per audit_run_id. Consulted by a before/after diff via db.audit_basis.bases_are_comparable before any movement is claimed.';
COMMENT ON COLUMN audit_basis.methodology_version IS
  'Single version covering the whole methodology; bump when ANY component changes. A mismatch alone makes two runs non-comparable.';
COMMENT ON COLUMN audit_basis.providers_and_models IS
  'JSON object {provider_id: {model_id, temperature}}. temperature is null where the probe path pins none (it currently pins one only in services/llm_providers/deepseek_probe.py) — recorded as null rather than invented, so a future pinning shows up as a basis change.';
COMMENT ON COLUMN audit_basis.tier_mix IS
  'JSON object {intent_axis: count} over services/audit_facts.intent_axis_for. Two runs with the same prompt_set_id but a different tier mix are NOT measuring the same thing.';
COMMENT ON COLUMN audit_basis.official_domains IS
  'JSON array: the merchant_official_domains set (migration 207) at run time. It decides first_party on every cited host, so a domain added between runs moves the headline number with no change in the world.';
