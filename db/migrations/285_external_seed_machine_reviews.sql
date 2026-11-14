-- Durable decisions for the external seed machine-review pilot.
CREATE TABLE IF NOT EXISTS external_seed_machine_reviews (
    request_id VARCHAR(128) PRIMARY KEY CHECK (btrim(request_id) <> ''),
    seed_id VARCHAR(128) NOT NULL CHECK (btrim(seed_id) <> ''),
    review_version TEXT NOT NULL CHECK (review_version ~ '^[0-9a-f]{64}$'),
    policy_version VARCHAR(128) NOT NULL CHECK (btrim(policy_version) <> ''),
    evidence_digest TEXT NOT NULL CHECK (evidence_digest ~ '^[0-9a-f]{64}$'),
    reviewer_identity VARCHAR(255) NOT NULL CHECK (btrim(reviewer_identity) <> ''),
    outcome TEXT NOT NULL CHECK (outcome IN ('hold', 'activated')),
    decision JSONB NOT NULL CHECK (jsonb_typeof(decision) = 'object'),
    active_review_version TEXT CHECK (active_review_version ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK ((outcome = 'activated') = (active_review_version IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS idx_external_seed_machine_reviews_seed_created
    ON external_seed_machine_reviews (seed_id, created_at DESC);

CREATE OR REPLACE FUNCTION prevent_external_seed_machine_review_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'external seed machine reviews are append-only';
END;
$$;

CREATE OR REPLACE TRIGGER external_seed_machine_reviews_immutable
    BEFORE UPDATE OR DELETE OR TRUNCATE ON external_seed_machine_reviews
    FOR EACH STATEMENT EXECUTE FUNCTION prevent_external_seed_machine_review_mutation();
