-- Migration 042: Buyer review submission support (idempotency + replay prevention)
-- PostgreSQL only.

CREATE TABLE IF NOT EXISTS buyer_review_submission_jtis (
  id          BIGSERIAL PRIMARY KEY,
  merchant_id TEXT NOT NULL,
  jti_hash    TEXT NOT NULL,
  expires_at  TIMESTAMPTZ NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_buyer_review_submission_jtis_jti_hash
  ON buyer_review_submission_jtis (jti_hash);

CREATE INDEX IF NOT EXISTS idx_buyer_review_submission_jtis_expires
  ON buyer_review_submission_jtis (expires_at);


CREATE TABLE IF NOT EXISTS buyer_review_idempotency_keys (
  id                 BIGSERIAL PRIMARY KEY,
  merchant_id         TEXT NOT NULL,
  idempotency_key_hash TEXT NOT NULL,
  request_hash        TEXT NOT NULL,
  review_id           BIGINT NULL REFERENCES product_reviews(id) ON DELETE SET NULL,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_buyer_review_idempotency_keys_merchant_key
  ON buyer_review_idempotency_keys (merchant_id, idempotency_key_hash);


CREATE TABLE IF NOT EXISTS buyer_review_ownership (
  review_id     BIGINT PRIMARY KEY REFERENCES product_reviews(id) ON DELETE CASCADE,
  token_jti_hash TEXT NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_buyer_review_ownership_token
  ON buyer_review_ownership (token_jti_hash);

