-- 277: storage for a buyer's connected Shop account and the checkouts Pivota runs through Shop MCP
-- (local Shop MCP sign-in and checkout design, 2026-11-06: D2, D4, D5, D7). Nothing reads or writes
-- these tables yet; the connect flow, refresher and checkout tools land behind their own flags.
--
--   shop_account_claims  one row per connected Shop account: the ONE Pivota account it belongs to.
--   shop_account_links   one row per (Pivota account, agent client) connection; the refresh token is
--                        stored only encrypted (utils/token_cipher.py), the access token never.
--   shop_link_attempts   one row per Connect: hashed ticket and state, encrypted PKCE verifier.
--   shop_checkouts       one row per Shop MCP checkout, keyed by a HASH of Shop's checkout id
--                        (the raw id and continue_url carry an access key and are never stored).
--
-- No table references an existing table, so creating them locks nothing that serves traffic.
-- Also declared in db/shop_account_links.py, which main.py does NOT import yet: prod (which does not
-- apply numbered migrations) gets these tables from create_all only once the first writer PR
-- registers the module. Until then they exist where migrations run. This file is the record, and
-- what the SQL gates plan against.
-- schema-guard-exempt: CREATE TABLE only, no ADD COLUMN; create_all will build the same tables.

CREATE TABLE IF NOT EXISTS shop_account_claims (
  shop_sub_hash      VARCHAR(64) PRIMARY KEY,
  pivota_account_id  VARCHAR(128) NOT NULL,
  claimed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT ck_shop_account_claims_shop_sub_hash_len CHECK (length(shop_sub_hash) = 64),
  CONSTRAINT uq_shop_account_claims_sub_account UNIQUE (shop_sub_hash, pivota_account_id)
);
CREATE INDEX IF NOT EXISTS ix_shop_account_claims_account ON shop_account_claims (pivota_account_id);

CREATE TABLE IF NOT EXISTS shop_account_links (
  link_id               VARCHAR(64) PRIMARY KEY,
  pivota_account_id     VARCHAR(128) NOT NULL,
  agent_client_id       VARCHAR(128) NOT NULL,
  shop_sub_hash         VARCHAR(64) NOT NULL,
  claimed_sub_hash      VARCHAR(64),
  scope                 VARCHAR(512) NOT NULL,
  refresh_token_ct      TEXT,
  link_generation       INTEGER NOT NULL DEFAULT 1,
  refresh_lease_until   TIMESTAMPTZ,
  status                VARCHAR(16) NOT NULL,
  status_reason         VARCHAR(64),
  connected_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_refreshed_at     TIMESTAMPTZ,
  revoked_at            TIMESTAMPTZ,
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT ck_shop_account_links_status
    CHECK (status IN ('active', 'needs_reconnect', 'revoked')),
  CONSTRAINT ck_shop_account_links_active_has_token
    CHECK ((status <> 'active') OR (refresh_token_ct IS NOT NULL)),
  CONSTRAINT ck_shop_account_links_revoked_iff_revoked_at
    CHECK ((status = 'revoked') = (revoked_at IS NOT NULL)),
  CONSTRAINT ck_shop_account_links_revoked_has_no_token
    CHECK ((status <> 'revoked') OR (refresh_token_ct IS NULL)),
  CONSTRAINT ck_shop_account_links_live_is_claimed
    CHECK ((status = 'revoked' AND claimed_sub_hash IS NULL)
      OR (status <> 'revoked' AND claimed_sub_hash IS NOT NULL AND claimed_sub_hash = shop_sub_hash)),
  CONSTRAINT ck_shop_account_links_generation_positive CHECK (link_generation >= 1),
  CONSTRAINT ck_shop_account_links_shop_sub_hash_len CHECK (length(shop_sub_hash) = 64),
  CONSTRAINT fk_shop_account_links_claim FOREIGN KEY (claimed_sub_hash, pivota_account_id)
    REFERENCES shop_account_claims (shop_sub_hash, pivota_account_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_shop_account_links_active_account_client
  ON shop_account_links (pivota_account_id, agent_client_id) WHERE status <> 'revoked';
CREATE INDEX IF NOT EXISTS ix_shop_account_links_live_sub
  ON shop_account_links (shop_sub_hash) WHERE status <> 'revoked';

CREATE TABLE IF NOT EXISTS shop_link_attempts (
  attempt_id          VARCHAR(64) PRIMARY KEY,
  ticket_hash         VARCHAR(64) NOT NULL,
  state_hash          VARCHAR(64),
  pivota_account_id   VARCHAR(128) NOT NULL,
  agent_client_id     VARCHAR(128) NOT NULL,
  code_verifier_ct    TEXT,
  nonce_hash          VARCHAR(64),
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at          TIMESTAMPTZ NOT NULL,
  connect_clicked_at  TIMESTAMPTZ,
  consumed_at         TIMESTAMPTZ,
  outcome             VARCHAR(32),
  CONSTRAINT ck_shop_link_attempts_connect_fields_together
    CHECK (((state_hash IS NULL) = (code_verifier_ct IS NULL)) AND ((state_hash IS NULL) = (nonce_hash IS NULL))),
  CONSTRAINT ck_shop_link_attempts_state_iff_clicked
    CHECK ((state_hash IS NULL) = (connect_clicked_at IS NULL)),
  CONSTRAINT ck_shop_link_attempts_expiry_after_creation CHECK (expires_at > created_at),
  CONSTRAINT ck_shop_link_attempts_ticket_hash_len CHECK (length(ticket_hash) = 64),
  CONSTRAINT ck_shop_link_attempts_state_hash_len CHECK (length(state_hash) = 64),
  CONSTRAINT ck_shop_link_attempts_nonce_hash_len CHECK (length(nonce_hash) = 64)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_shop_link_attempts_ticket_hash ON shop_link_attempts (ticket_hash);
CREATE UNIQUE INDEX IF NOT EXISTS uq_shop_link_attempts_state_hash
  ON shop_link_attempts (state_hash) WHERE state_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_shop_link_attempts_expires_at ON shop_link_attempts (expires_at);

CREATE TABLE IF NOT EXISTS shop_checkouts (
  checkout_id_hash         VARCHAR(64) PRIMARY KEY,
  pivota_account_id        VARCHAR(128) NOT NULL,
  agent_client_id          VARCHAR(128) NOT NULL,
  link_id                  VARCHAR(64),
  shop_domain              VARCHAR(255) NOT NULL,
  market                   VARCHAR(8) NOT NULL,
  click_id                 VARCHAR(64),
  shown_totals             JSONB,
  contact_consent_at       TIMESTAMPTZ,
  mandate_hash             VARCHAR(64),
  completion_attempted_at  TIMESTAMPTZ,
  outcome                  VARCHAR(24) NOT NULL DEFAULT 'open',
  order_ref_hash           VARCHAR(64),
  created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT ck_shop_checkouts_outcome
    CHECK (outcome IN ('open', 'handed_off', 'completed', 'canceled', 'declined', 'unknown_outcome')),
  CONSTRAINT ck_shop_checkouts_checkout_id_hash_len CHECK (length(checkout_id_hash) = 64),
  CONSTRAINT ck_shop_checkouts_mandate_hash_len CHECK (length(mandate_hash) = 64),
  CONSTRAINT ck_shop_checkouts_order_ref_hash_len CHECK (length(order_ref_hash) = 64),
  CONSTRAINT ck_shop_checkouts_completion_needs_mandate
    CHECK ((completion_attempted_at IS NULL) OR (mandate_hash IS NOT NULL)),
  CONSTRAINT ck_shop_checkouts_attempt_outcome_needs_attempt
    CHECK ((outcome NOT IN ('declined', 'unknown_outcome')) OR (completion_attempted_at IS NOT NULL)),
  CONSTRAINT ck_shop_checkouts_completed_names_order
    CHECK ((outcome <> 'completed') OR (order_ref_hash IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS ix_shop_checkouts_account_created ON shop_checkouts (pivota_account_id, created_at);
