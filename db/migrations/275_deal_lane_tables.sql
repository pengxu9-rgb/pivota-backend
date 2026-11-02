-- 275: the deal lane's storage (ADR-027 D3, D6, D7, Q13, Q14). Nothing reads or writes these tables yet;
-- every writer and reader lands behind its own flag, all default off (config/settings.py, "Deal lane").
--
--   observed_store_offers               one row per (seller, market, code or automatic rule, program):
--                                       what the source published. Metadata, never evidence.
--   observed_store_offer_verifications  one row per (deal, variant): what the merchant's checkout said
--                                       about a one-unit reference cart in the deal's market (D3).
--   deal_program_permissions            one row per affiliate program: the recorded permission to test
--   deal_program_permission_hosts       that program's feed codes, and the hosts it covers (Q13).
--   deal_network_links                  one row per (deal, variant, market): the tracking-only and coded
--                                       network links `/r` chooses between (Q14).
--   deal_verify_host_daily_carts        the per-host, per-UTC-day reference-cart counter (D3 cap).
--   deal_lane_stops                     stop rows, one active per market (D7 stop rule).
--   deal_seller_optouts                 sellers that asked never to be tested or shown (D2).
--
-- No table references an existing table, so creating them locks nothing that serves traffic.
-- Also declared in db/deal_lane.py; create_all creates them at startup (prod does not apply numbered
-- migrations). This file is the record, and what the SQL gates plan against.
-- schema-guard-exempt: CREATE TABLE only, no ADD COLUMN; create_all builds the same tables at boot.

CREATE TABLE IF NOT EXISTS observed_store_offers (
  deal_id            VARCHAR(64) PRIMARY KEY,
  deal_key           VARCHAR(64) NOT NULL,
  seller_id          VARCHAR(128) NOT NULL,
  market             VARCHAR(8),
  code               VARCHAR(255),
  platform_rule_ref  VARCHAR(255),
  program_id         VARCHAR(128),
  kind               VARCHAR(16) NOT NULL,
  value              NUMERIC(18, 4),
  currency           VARCHAR(3),
  min_subtotal       NUMERIC(18, 4),
  new_customer_only  BOOLEAN NOT NULL DEFAULT FALSE,
  once_per_customer  BOOLEAN NOT NULL DEFAULT FALSE,
  segment_limited    BOOLEAN NOT NULL DEFAULT FALSE,
  usage_limited      BOOLEAN NOT NULL DEFAULT FALSE,
  eligibility_known  BOOLEAN NOT NULL DEFAULT FALSE,
  scope_kind         VARCHAR(16),
  scope_refs         JSONB,
  starts_at          TIMESTAMPTZ,
  ends_at            TIMESTAMPTZ,
  source             VARCHAR(32) NOT NULL,
  source_url         TEXT,
  observed_at        TIMESTAMPTZ NOT NULL,
  terms_text         TEXT,
  orders_seen        INTEGER NOT NULL DEFAULT 0,
  orders_applied     INTEGER NOT NULL DEFAULT 0,
  last_order_at      TIMESTAMPTZ,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT ck_observed_store_offers_kind
    CHECK (kind IN ('percent', 'amount', 'free_shipping', 'gift', 'bxgy')),
  CONSTRAINT ck_observed_store_offers_source
    CHECK (source IN ('checkout_automatic', 'merchant_published', 'authorized_feed', 'channel_exclusive')),
  CONSTRAINT ck_observed_store_offers_code_iff_coded_source
    CHECK ((source = 'checkout_automatic') = (code IS NULL)),
  CONSTRAINT ck_observed_store_offers_rule_identified
    CHECK (code IS NOT NULL OR platform_rule_ref IS NOT NULL),
  CONSTRAINT ck_observed_store_offers_program_iff_feed
    CHECK ((source = 'authorized_feed') = (program_id IS NOT NULL)),
  CONSTRAINT ck_observed_store_offers_scope_kind
    CHECK (scope_kind IS NULL OR scope_kind IN ('sitewide', 'collections', 'products')),
  CONSTRAINT ck_observed_store_offers_currency
    CHECK (currency IS NULL OR length(currency) = 3)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_observed_store_offers_deal_key
  ON observed_store_offers (deal_key);
CREATE INDEX IF NOT EXISTS idx_observed_store_offers_seller_market
  ON observed_store_offers (seller_id, market);

CREATE TABLE IF NOT EXISTS observed_store_offer_verifications (
  deal_id                  VARCHAR(64) NOT NULL
                             REFERENCES observed_store_offers (deal_id) ON DELETE CASCADE,
  variant_id               VARCHAR(64) NOT NULL,
  canonical_variant_id     VARCHAR(64),
  state                    VARCHAR(24) NOT NULL DEFAULT 'unverified',
  verified_at              TIMESTAMPTZ,
  expires_at               TIMESTAMPTZ,
  verified_via             VARCHAR(40),
  authorized_by            VARCHAR(160),
  ref_quantity             INTEGER NOT NULL DEFAULT 1,
  ref_unit_price           NUMERIC(18, 4),
  ref_compare_at_price     NUMERIC(18, 4),
  ref_subtotal             NUMERIC(18, 4),
  ref_discount_amount      NUMERIC(18, 4),
  ref_currency             VARCHAR(3),
  ref_automatic_discounts  JSONB,
  failure_reason           TEXT,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (deal_id, variant_id),
  CONSTRAINT ck_observed_store_offer_verifications_state
    CHECK (state IN ('unverified', 'checkout_verified', 'failed', 'expired')),
  CONSTRAINT ck_observed_store_offer_verifications_via
    CHECK (verified_via IS NULL
           OR verified_via IN ('shopify_storefront_connected', 'shopify_storefront_tokenless', 'ucp_checkout')),
  CONSTRAINT ck_observed_store_offer_verifications_authorized_by
    CHECK (authorized_by IS NULL
           OR authorized_by IN ('connected', 'q2_approval')
           OR authorized_by LIKE 'program:_%'),
  CONSTRAINT ck_observed_store_offer_verifications_quantity_one
    CHECK (ref_quantity = 1),
  CONSTRAINT ck_observed_store_offer_verifications_verified_is_complete
    CHECK (state <> 'checkout_verified' OR (
      verified_at IS NOT NULL AND expires_at IS NOT NULL AND expires_at > verified_at
      AND verified_via IS NOT NULL AND authorized_by IS NOT NULL
      AND ref_unit_price IS NOT NULL AND ref_subtotal IS NOT NULL AND ref_currency IS NOT NULL
      AND ref_discount_amount IS NOT NULL AND ref_discount_amount > 0
    ))
);

CREATE INDEX IF NOT EXISTS idx_observed_store_offer_verifications_variant
  ON observed_store_offer_verifications (variant_id, state);

CREATE TABLE IF NOT EXISTS deal_program_permissions (
  program_id         VARCHAR(128) PRIMARY KEY,
  network            VARCHAR(64) NOT NULL,
  membership_status  VARCHAR(16) NOT NULL,
  permission_source  VARCHAR(24) NOT NULL,
  permission_ref     TEXT NOT NULL,
  permission_dated   TIMESTAMPTZ NOT NULL,
  confirmed_at       TIMESTAMPTZ NOT NULL,
  confirmed_by       VARCHAR(128) NOT NULL,
  reconfirm_by       TIMESTAMPTZ NOT NULL,
  terms_changed_at   TIMESTAMPTZ,
  note               TEXT,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT ck_deal_program_permissions_membership
    CHECK (membership_status IN ('active', 'pending', 'suspended', 'ended')),
  CONSTRAINT ck_deal_program_permissions_source
    CHECK (permission_source IN ('program_terms', 'merchant_written')),
  CONSTRAINT ck_deal_program_permissions_reconfirm_after_confirm
    CHECK (reconfirm_by > confirmed_at)
);

CREATE TABLE IF NOT EXISTS deal_program_permission_hosts (
  program_id  VARCHAR(128) NOT NULL
                REFERENCES deal_program_permissions (program_id) ON DELETE CASCADE,
  host        VARCHAR(256) NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (program_id, host)
);

CREATE INDEX IF NOT EXISTS idx_deal_program_permission_hosts_host
  ON deal_program_permission_hosts (host);

CREATE TABLE IF NOT EXISTS deal_network_links (
  link_row_id           VARCHAR(64) PRIMARY KEY,
  deal_id               VARCHAR(64) NOT NULL
                          REFERENCES observed_store_offers (deal_id) ON DELETE CASCADE,
  variant_id            VARCHAR(64) NOT NULL,
  market                VARCHAR(8) NOT NULL,
  program_id            VARCHAR(128) NOT NULL,
  tracking_link         TEXT NOT NULL,
  tracking_generated_at TIMESTAMPTZ NOT NULL,
  tracking_expires_at   TIMESTAMPTZ,
  coded_link            TEXT NOT NULL,
  coded_generated_at    TIMESTAMPTZ NOT NULL,
  coded_expires_at      TIMESTAMPTZ,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT ck_deal_network_links_distinct
    CHECK (coded_link <> tracking_link)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_deal_network_links_deal_variant_market
  ON deal_network_links (deal_id, variant_id, market);

CREATE TABLE IF NOT EXISTS deal_verify_host_daily_carts (
  host        VARCHAR(256) NOT NULL,
  utc_day     DATE NOT NULL,
  carts       INTEGER NOT NULL DEFAULT 0,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (host, utc_day),
  CONSTRAINT ck_deal_verify_host_daily_carts_nonnegative
    CHECK (carts >= 0)
);

CREATE TABLE IF NOT EXISTS deal_lane_stops (
  stop_id     VARCHAR(64) PRIMARY KEY,
  market      VARCHAR(8) NOT NULL,
  stop_trigger VARCHAR(16) NOT NULL,
  detail      JSONB,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  cleared_at  TIMESTAMPTZ,
  cleared_by  VARCHAR(128),
  clear_note  TEXT,
  CONSTRAINT ck_deal_lane_stops_trigger
    CHECK (stop_trigger IN ('rate', 'absolute', 'manual')),
  CONSTRAINT ck_deal_lane_stops_cleared_by_person
    CHECK (cleared_at IS NULL OR cleared_by IS NOT NULL)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_deal_lane_stops_active_market
  ON deal_lane_stops (market)
  WHERE cleared_at IS NULL;

CREATE TABLE IF NOT EXISTS deal_seller_optouts (
  seller_id     VARCHAR(128) PRIMARY KEY,
  reason        TEXT,
  requested_at  TIMESTAMPTZ NOT NULL,
  recorded_by   VARCHAR(128) NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
