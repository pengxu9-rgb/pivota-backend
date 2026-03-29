-- 058_catalog_core.sql
-- Canonical catalog core, beauty vertical tables, incentive graph, and quote snapshots.
-- Production truth must come from this SQL migration; SQLAlchemy metadata.create_all remains
-- a dev/self-heal safety net only.

CREATE TABLE IF NOT EXISTS catalog_merchants (
    merchant_id VARCHAR(64) PRIMARY KEY,
    merchant_name VARCHAR(255),
    primary_platform VARCHAR(64),
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    source_system VARCHAR(64),
    source_ref VARCHAR(255),
    metadata_json JSONB,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS catalog_products (
    product_key VARCHAR(255) PRIMARY KEY,
    merchant_id VARCHAR(64) NOT NULL,
    platform VARCHAR(64) NOT NULL,
    source_product_id VARCHAR(128) NOT NULL,
    catalog_track VARCHAR(32) NOT NULL DEFAULT 'internal_merchant',
    truth_tier VARCHAR(32) NOT NULL DEFAULT 'primary',
    readiness_tier VARCHAR(32) NOT NULL DEFAULT 'commerce_ready',
    source_system VARCHAR(64),
    source_ref VARCHAR(255),
    title TEXT NOT NULL,
    description TEXT,
    brand VARCHAR(255),
    product_type VARCHAR(255),
    category VARCHAR(255),
    canonical_url TEXT,
    image_url TEXT,
    product_payload JSONB,
    freshness_json JSONB,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS catalog_skus (
    sku_key VARCHAR(255) PRIMARY KEY,
    product_key VARCHAR(255) NOT NULL,
    merchant_id VARCHAR(64) NOT NULL,
    platform VARCHAR(64) NOT NULL,
    source_product_id VARCHAR(128) NOT NULL,
    source_variant_id VARCHAR(128) NOT NULL,
    sku VARCHAR(128),
    barcode VARCHAR(128),
    title TEXT NOT NULL,
    currency VARCHAR(16),
    image_url TEXT,
    visible_attributes JSONB,
    visible_option_labels JSONB,
    ingredient_ids JSONB,
    sku_payload JSONB,
    readiness_tier VARCHAR(32) NOT NULL DEFAULT 'commerce_ready',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS catalog_offers (
    offer_id VARCHAR(255) PRIMARY KEY,
    sku_key VARCHAR(255) NOT NULL,
    product_key VARCHAR(255) NOT NULL,
    merchant_id VARCHAR(64) NOT NULL,
    catalog_track VARCHAR(32) NOT NULL DEFAULT 'internal_merchant',
    truth_tier VARCHAR(32) NOT NULL DEFAULT 'primary',
    readiness_tier VARCHAR(32) NOT NULL DEFAULT 'commerce_ready',
    offer_mode VARCHAR(32) NOT NULL DEFAULT 'merchant_checkout',
    channel VARCHAR(64) NOT NULL DEFAULT 'default',
    availability VARCHAR(32) NOT NULL DEFAULT 'unknown',
    inventory_quantity INTEGER,
    currency VARCHAR(16),
    list_price NUMERIC(12, 2),
    merchant_effective_price NUMERIC(12, 2),
    estimated_best_price NUMERIC(12, 2),
    price_confidence NUMERIC(5, 2),
    source_system VARCHAR(64),
    source_ref VARCHAR(255),
    offer_payload JSONB,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS catalog_inventory_snapshots (
    id INTEGER PRIMARY KEY,
    offer_id VARCHAR(255) NOT NULL,
    sku_key VARCHAR(255) NOT NULL,
    merchant_id VARCHAR(64) NOT NULL,
    inventory_quantity INTEGER,
    availability VARCHAR(32) NOT NULL DEFAULT 'unknown',
    source_system VARCHAR(64),
    source_ref VARCHAR(255),
    observed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS catalog_price_snapshots (
    id INTEGER PRIMARY KEY,
    offer_id VARCHAR(255) NOT NULL,
    sku_key VARCHAR(255) NOT NULL,
    merchant_id VARCHAR(64) NOT NULL,
    currency VARCHAR(16),
    list_price NUMERIC(12, 2),
    merchant_effective_price NUMERIC(12, 2),
    estimated_best_price NUMERIC(12, 2),
    source_system VARCHAR(64),
    source_ref VARCHAR(255),
    observed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS catalog_field_facts (
    fact_id VARCHAR(255) PRIMARY KEY,
    entity_type VARCHAR(32) NOT NULL,
    entity_id VARCHAR(255) NOT NULL,
    field_family VARCHAR(64) NOT NULL,
    field_key VARCHAR(128) NOT NULL,
    source_system VARCHAR(64) NOT NULL,
    source_ref VARCHAR(255),
    value_json JSONB,
    observed_at TIMESTAMP NOT NULL,
    fresh_until TIMESTAMP,
    confidence NUMERIC(5, 2),
    review_state VARCHAR(32) NOT NULL DEFAULT 'observed',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS catalog_sync_events (
    event_id VARCHAR(255) PRIMARY KEY,
    merchant_id VARCHAR(64) NOT NULL,
    connector VARCHAR(64) NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    topic VARCHAR(128),
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    payload_json JSONB,
    source_ref VARCHAR(255),
    error_message TEXT,
    occurred_at TIMESTAMP,
    processed_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS catalog_sync_jobs (
    job_id VARCHAR(255) PRIMARY KEY,
    merchant_id VARCHAR(64) NOT NULL,
    connector VARCHAR(64) NOT NULL,
    mode VARCHAR(64) NOT NULL,
    scope_json JSONB,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    requested_by VARCHAR(128),
    stats_json JSONB,
    error_message TEXT,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS catalog_promotions (
    promotion_id VARCHAR(255) PRIMARY KEY,
    merchant_id VARCHAR(64) NOT NULL,
    source_promotion_id VARCHAR(255),
    name VARCHAR(255) NOT NULL,
    promotion_class VARCHAR(32) NOT NULL,
    method VARCHAR(32) NOT NULL DEFAULT 'automatic',
    label VARCHAR(255) NOT NULL,
    code VARCHAR(128),
    start_at TIMESTAMP,
    end_at TIMESTAMP,
    channels_json JSONB,
    scope_json JSONB,
    config_json JSONB,
    truth_tier VARCHAR(32) NOT NULL DEFAULT 'primary',
    source_system VARCHAR(64),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS catalog_payment_incentives (
    incentive_id VARCHAR(255) PRIMARY KEY,
    merchant_id VARCHAR(64) NOT NULL,
    incentive_type VARCHAR(64) NOT NULL,
    funding_source VARCHAR(64),
    payment_method_type VARCHAR(64),
    card_network VARCHAR(64),
    issuer_name VARCHAR(128),
    wallet_type VARCHAR(64),
    installment_provider VARCHAR(64),
    label VARCHAR(255) NOT NULL,
    benefit_kind VARCHAR(64) NOT NULL,
    benefit_value NUMERIC(12, 2),
    benefit_currency VARCHAR(16),
    market VARCHAR(16),
    eligibility_confidence NUMERIC(5, 2),
    source_system VARCHAR(64),
    source_ref VARCHAR(255),
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    starts_at TIMESTAMP,
    ends_at TIMESTAMP,
    metadata_json JSONB,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS catalog_incentive_rules (
    rule_id VARCHAR(255) PRIMARY KEY,
    incentive_id VARCHAR(255) NOT NULL,
    merchant_id VARCHAR(64) NOT NULL,
    rule_type VARCHAR(64) NOT NULL,
    scope_json JSONB,
    conditions_json JSONB,
    schedule_json JSONB,
    human_rule TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS catalog_offer_incentive_links (
    link_id VARCHAR(255) PRIMARY KEY,
    offer_id VARCHAR(255) NOT NULL,
    incentive_id VARCHAR(255) NOT NULL,
    merchant_id VARCHAR(64) NOT NULL,
    relationship_type VARCHAR(64) NOT NULL DEFAULT 'eligible',
    priority INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS catalog_quote_snapshots (
    quote_snapshot_id VARCHAR(255) PRIMARY KEY,
    quote_id VARCHAR(255),
    merchant_id VARCHAR(64) NOT NULL,
    offer_id VARCHAR(255),
    sku_key VARCHAR(255),
    product_key VARCHAR(255),
    currency VARCHAR(16),
    list_price NUMERIC(12, 2),
    merchant_effective_price NUMERIC(12, 2),
    estimated_best_price NUMERIC(12, 2),
    exact_quote_price NUMERIC(12, 2),
    incentives_json JSONB,
    quote_payload_json JSONB,
    expires_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS beauty_product_profiles (
    product_key VARCHAR(255) PRIMARY KEY,
    merchant_id VARCHAR(64) NOT NULL,
    taxonomy_json JSONB,
    concerns_json JSONB,
    claims_json JSONB,
    routine_phase VARCHAR(64),
    benefits_json JSONB,
    profile_payload JSONB,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS beauty_sku_ingredients (
    sku_key VARCHAR(255) PRIMARY KEY,
    product_key VARCHAR(255) NOT NULL,
    merchant_id VARCHAR(64) NOT NULL,
    raw_inci TEXT,
    normalized_ingredients_json JSONB,
    active_ingredients_json JSONB,
    concentration_notes_json JSONB,
    allergen_flags_json JSONB,
    evidence_refs_json JSONB,
    source_system VARCHAR(64),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS beauty_usage_guides (
    guide_id VARCHAR(255) PRIMARY KEY,
    product_key VARCHAR(255) NOT NULL,
    sku_key VARCHAR(255),
    merchant_id VARCHAR(64) NOT NULL,
    how_to_use_text TEXT,
    steps_json JSONB,
    frequency VARCHAR(64),
    time_of_day VARCHAR(32),
    application_order INTEGER,
    warnings_json JSONB,
    evidence_refs_json JSONB,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS beauty_shades (
    shade_id VARCHAR(255) PRIMARY KEY,
    sku_key VARCHAR(255) NOT NULL,
    product_key VARCHAR(255) NOT NULL,
    merchant_id VARCHAR(64) NOT NULL,
    shade_code VARCHAR(128),
    shade_name VARCHAR(255) NOT NULL,
    shade_family VARCHAR(128),
    undertone VARCHAR(128),
    finish VARCHAR(128),
    swatch_refs_json JSONB,
    media_refs_json JSONB,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS beauty_compatibility_rules (
    compatibility_rule_id VARCHAR(255) PRIMARY KEY,
    product_key VARCHAR(255),
    sku_key VARCHAR(255),
    merchant_id VARCHAR(64) NOT NULL,
    rule_type VARCHAR(64) NOT NULL,
    subject_ingredients_json JSONB,
    related_ingredients_json JSONB,
    verdict VARCHAR(64) NOT NULL,
    rationale TEXT,
    evidence_refs_json JSONB,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS beauty_content_assets (
    asset_id VARCHAR(255) PRIMARY KEY,
    product_key VARCHAR(255) NOT NULL,
    sku_key VARCHAR(255),
    merchant_id VARCHAR(64) NOT NULL,
    asset_type VARCHAR(64) NOT NULL,
    title VARCHAR(255),
    url TEXT NOT NULL,
    thumbnail_url TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    metadata_json JSONB,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_catalog_products_merchant_id ON catalog_products (merchant_id);
CREATE INDEX IF NOT EXISTS idx_catalog_products_platform ON catalog_products (platform);
CREATE UNIQUE INDEX IF NOT EXISTS idx_catalog_products_source_identity ON catalog_products (merchant_id, platform, source_product_id);

CREATE INDEX IF NOT EXISTS idx_catalog_skus_product_key ON catalog_skus (product_key);
CREATE INDEX IF NOT EXISTS idx_catalog_skus_merchant_id ON catalog_skus (merchant_id);
CREATE INDEX IF NOT EXISTS idx_catalog_skus_platform ON catalog_skus (platform);
CREATE INDEX IF NOT EXISTS idx_catalog_skus_sku ON catalog_skus (sku);
CREATE UNIQUE INDEX IF NOT EXISTS idx_catalog_skus_source_identity ON catalog_skus (merchant_id, platform, source_variant_id);

CREATE INDEX IF NOT EXISTS idx_catalog_offers_sku_key ON catalog_offers (sku_key);
CREATE INDEX IF NOT EXISTS idx_catalog_offers_product_key ON catalog_offers (product_key);
CREATE INDEX IF NOT EXISTS idx_catalog_offers_merchant_id ON catalog_offers (merchant_id);
CREATE INDEX IF NOT EXISTS idx_catalog_offers_merchant_track ON catalog_offers (merchant_id, catalog_track);

CREATE INDEX IF NOT EXISTS idx_catalog_inventory_snapshots_offer_id ON catalog_inventory_snapshots (offer_id);
CREATE INDEX IF NOT EXISTS idx_catalog_inventory_snapshots_sku_key ON catalog_inventory_snapshots (sku_key);
CREATE INDEX IF NOT EXISTS idx_catalog_inventory_snapshots_merchant_id ON catalog_inventory_snapshots (merchant_id);

CREATE INDEX IF NOT EXISTS idx_catalog_price_snapshots_offer_id ON catalog_price_snapshots (offer_id);
CREATE INDEX IF NOT EXISTS idx_catalog_price_snapshots_sku_key ON catalog_price_snapshots (sku_key);
CREATE INDEX IF NOT EXISTS idx_catalog_price_snapshots_merchant_id ON catalog_price_snapshots (merchant_id);

CREATE INDEX IF NOT EXISTS idx_catalog_field_facts_entity_type ON catalog_field_facts (entity_type);
CREATE INDEX IF NOT EXISTS idx_catalog_field_facts_entity_id ON catalog_field_facts (entity_id);
CREATE INDEX IF NOT EXISTS idx_catalog_field_facts_field ON catalog_field_facts (entity_type, entity_id, field_family, field_key);

CREATE INDEX IF NOT EXISTS idx_catalog_sync_events_merchant_id ON catalog_sync_events (merchant_id);
CREATE INDEX IF NOT EXISTS idx_catalog_sync_events_connector ON catalog_sync_events (connector);
CREATE INDEX IF NOT EXISTS idx_catalog_sync_events_pending ON catalog_sync_events (merchant_id, connector, status);

CREATE INDEX IF NOT EXISTS idx_catalog_sync_jobs_merchant_id ON catalog_sync_jobs (merchant_id);
CREATE INDEX IF NOT EXISTS idx_catalog_sync_jobs_connector ON catalog_sync_jobs (connector);
CREATE INDEX IF NOT EXISTS idx_catalog_sync_jobs_status ON catalog_sync_jobs (merchant_id, connector, status);

CREATE INDEX IF NOT EXISTS idx_catalog_promotions_merchant_id ON catalog_promotions (merchant_id);
CREATE INDEX IF NOT EXISTS idx_catalog_payment_incentives_merchant_id ON catalog_payment_incentives (merchant_id);
CREATE INDEX IF NOT EXISTS idx_catalog_incentive_rules_incentive_id ON catalog_incentive_rules (incentive_id);
CREATE INDEX IF NOT EXISTS idx_catalog_incentive_rules_merchant_id ON catalog_incentive_rules (merchant_id);
CREATE INDEX IF NOT EXISTS idx_catalog_offer_incentive_links_offer_id ON catalog_offer_incentive_links (offer_id);
CREATE INDEX IF NOT EXISTS idx_catalog_offer_incentive_links_incentive_id ON catalog_offer_incentive_links (incentive_id);
CREATE INDEX IF NOT EXISTS idx_catalog_offer_incentive_links_merchant_id ON catalog_offer_incentive_links (merchant_id);

CREATE INDEX IF NOT EXISTS idx_catalog_quote_snapshots_quote_id ON catalog_quote_snapshots (quote_id);
CREATE INDEX IF NOT EXISTS idx_catalog_quote_snapshots_merchant_id ON catalog_quote_snapshots (merchant_id);
CREATE INDEX IF NOT EXISTS idx_catalog_quote_snapshots_offer_id ON catalog_quote_snapshots (offer_id);
CREATE INDEX IF NOT EXISTS idx_catalog_quote_snapshots_sku_key ON catalog_quote_snapshots (sku_key);
CREATE INDEX IF NOT EXISTS idx_catalog_quote_snapshots_product_key ON catalog_quote_snapshots (product_key);

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'catalog_inventory_snapshots'
          AND column_name = 'id'
    ) THEN
        CREATE SEQUENCE IF NOT EXISTS catalog_inventory_snapshots_id_seq;
        ALTER SEQUENCE catalog_inventory_snapshots_id_seq OWNED BY catalog_inventory_snapshots.id;
        ALTER TABLE catalog_inventory_snapshots
            ALTER COLUMN id SET DEFAULT nextval('catalog_inventory_snapshots_id_seq');
        PERFORM setval(
            'catalog_inventory_snapshots_id_seq',
            COALESCE((SELECT MAX(id) FROM catalog_inventory_snapshots), 0) + 1,
            false
        );
    END IF;

    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'catalog_price_snapshots'
          AND column_name = 'id'
    ) THEN
        CREATE SEQUENCE IF NOT EXISTS catalog_price_snapshots_id_seq;
        ALTER SEQUENCE catalog_price_snapshots_id_seq OWNED BY catalog_price_snapshots.id;
        ALTER TABLE catalog_price_snapshots
            ALTER COLUMN id SET DEFAULT nextval('catalog_price_snapshots_id_seq');
        PERFORM setval(
            'catalog_price_snapshots_id_seq',
            COALESCE((SELECT MAX(id) FROM catalog_price_snapshots), 0) + 1,
            false
        );
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_beauty_product_profiles_merchant_id ON beauty_product_profiles (merchant_id);
CREATE INDEX IF NOT EXISTS idx_beauty_sku_ingredients_product_key ON beauty_sku_ingredients (product_key);
CREATE INDEX IF NOT EXISTS idx_beauty_sku_ingredients_merchant_id ON beauty_sku_ingredients (merchant_id);
CREATE INDEX IF NOT EXISTS idx_beauty_usage_guides_product_key ON beauty_usage_guides (product_key);
CREATE INDEX IF NOT EXISTS idx_beauty_usage_guides_sku_key ON beauty_usage_guides (sku_key);
CREATE INDEX IF NOT EXISTS idx_beauty_usage_guides_merchant_id ON beauty_usage_guides (merchant_id);
CREATE INDEX IF NOT EXISTS idx_beauty_shades_sku_key ON beauty_shades (sku_key);
CREATE INDEX IF NOT EXISTS idx_beauty_shades_product_key ON beauty_shades (product_key);
CREATE INDEX IF NOT EXISTS idx_beauty_shades_merchant_id ON beauty_shades (merchant_id);
CREATE INDEX IF NOT EXISTS idx_beauty_compatibility_rules_product_key ON beauty_compatibility_rules (product_key);
CREATE INDEX IF NOT EXISTS idx_beauty_compatibility_rules_sku_key ON beauty_compatibility_rules (sku_key);
CREATE INDEX IF NOT EXISTS idx_beauty_compatibility_rules_merchant_id ON beauty_compatibility_rules (merchant_id);
CREATE INDEX IF NOT EXISTS idx_beauty_content_assets_product_key ON beauty_content_assets (product_key);
CREATE INDEX IF NOT EXISTS idx_beauty_content_assets_sku_key ON beauty_content_assets (sku_key);
CREATE INDEX IF NOT EXISTS idx_beauty_content_assets_merchant_id ON beauty_content_assets (merchant_id);
