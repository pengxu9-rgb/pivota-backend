#!/usr/bin/env python3
"""
Idempotent staging data seed for the agent-checkout PSP probe.

This script intentionally never prints raw API keys, Stripe keys, webhook
secrets, or DATABASE_URL. It prints only non-secret verification fields.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple


MERCHANT_ID = "merch_efbc46b4619cfbdf"
AGENT_ID = "agent_staging_probe"
AGENT_KEY_ID = "key_staging_probe"
PSP_ID = "psp_stripe_testprobe001"
PRODUCT_ID = "10064562258217"
VARIANT_ID = "50000000000001"
SKU = "PIVOTA-STAGING-PROBE-USD"
QUOTE_ID = "q_staging_probe_10064562258217"
PRICE = "19.99"
CURRENCY = "USD"
EXPECTED_MINOR_UNITS = 1999
REQUEST_FINGERPRINT = "01d4656ea7fe7686afb8e11e06fd38602f457e972d1a46a86f230fb15e5c1ca1"
QUOTE_HASH = "07d3f2c162d82453639242ae055d9c31c3a1622052f80283f0c3b3d35325c358"
STORE_ID = "store_staging_probe_shopify"
ROUTE_ID = "route_staging_probe_stripe"
PRODUCT_KEY = f"{MERCHANT_ID}:shopify:{PRODUCT_ID}"
SKU_KEY = f"{PRODUCT_KEY}:{VARIANT_ID}"
OFFER_ID = f"{SKU_KEY}:default"


class ConfigError(RuntimeError):
    pass


def _load_asyncpg():
    try:
        import asyncpg  # type: ignore
    except ModuleNotFoundError as exc:
        raise ConfigError(
            "asyncpg is not installed. Run from the deployed app image or install requirements first."
        ) from exc
    return asyncpg


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise ConfigError(f"{name} is required")
    return value.strip()


def _validate_secret_prefix(name: str, value: str, prefix: str) -> None:
    if not value.startswith(prefix):
        raise ConfigError(f"{name} must be a real test secret starting with {prefix}")
    lowered = value.lower()
    if "placeholder" in lowered or "paste_" in lowered or "replace_" in lowered:
        raise ConfigError(f"{name} still looks like a placeholder")


def _resolve_agent_key_hash() -> Tuple[str, str]:
    raw_key = (
        os.getenv("STAGING_AGENT_API_KEY")
        or os.getenv("SHOP_GATEWAY_AGENT_API_KEY")
        or os.getenv("AGENT_API_KEY")
        or ""
    ).strip()
    if raw_key:
        if not re.fullmatch(r"ak_(live_)?[0-9a-f]{64}", raw_key):
            raise ConfigError("STAGING_AGENT_API_KEY/SHOP_GATEWAY_AGENT_API_KEY has invalid agent API key format")
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest(), f"{raw_key[:12]}..."

    key_hash = (os.getenv("STAGING_AGENT_API_KEY_SHA256") or os.getenv("AGENT_API_KEY_SHA256") or "").strip()
    key_prefix = (os.getenv("STAGING_AGENT_API_KEY_PREFIX") or os.getenv("AGENT_API_KEY_PREFIX") or "").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", key_hash):
        raise ConfigError("STAGING_AGENT_API_KEY_SHA256 is required when raw STAGING_AGENT_API_KEY is not provided")
    if not key_prefix:
        key_prefix = "ak_live_..."
    return key_hash, key_prefix


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _shipping_address() -> Dict[str, Any]:
    return {
        "name": "Staging Probe Buyer",
        "address_line1": "123 Test Ave",
        "address_line2": "",
        "city": "San Francisco",
        "state": "CA",
        "country": "US",
        "postal_code": "94105",
        "phone": "+14155550100",
    }


def _product_data() -> Dict[str, Any]:
    return {
        "id": PRODUCT_ID,
        "product_id": PRODUCT_ID,
        "platform": "shopify",
        "merchant_id": MERCHANT_ID,
        "title": "Staging Probe Product",
        "description": "Controlled staging product for PSP money-path validation.",
        "vendor": "Pivota Staging",
        "brand": "Pivota Staging",
        "product_type": "Probe",
        "handle": "staging-probe-product",
        "status": "active",
        "price": PRICE,
        "currency": CURRENCY,
        "available": True,
        "inventory_quantity": 99,
        "url": "https://staging-probe.invalid/products/staging-probe-product",
        "images": [],
        "variants": [
            {
                "id": VARIANT_ID,
                "variant_id": VARIANT_ID,
                "product_id": PRODUCT_ID,
                "title": "Default Title",
                "sku": SKU,
                "price": PRICE,
                "currency": CURRENCY,
                "available": True,
                "inventory_quantity": 99,
                "selected_options": {"Title": "Default Title"},
            }
        ],
        "metadata": {
            "seeded_by": "scripts/staging_probe/seed.py",
            "expected_minor_units": EXPECTED_MINOR_UNITS,
        },
    }


def _quote_request_json() -> Dict[str, Any]:
    return {
        "merchant_id": MERCHANT_ID,
        "agent_id": AGENT_ID,
        "items": [
            {
                "product_id": PRODUCT_ID,
                "variant_id": VARIANT_ID,
                "quantity": 1,
            }
        ],
        "discount_codes": [],
        "shipping_address": _shipping_address(),
        "selected_delivery_option": None,
        "payment_context": {
            "surface": "staging_probe",
            "expected_minor_units": EXPECTED_MINOR_UNITS,
        },
    }


def _quote_snapshot_json() -> Dict[str, Any]:
    return {
        "platform": "shopify",
        "engine": "shopify_rest_checkout",
        "engine_ref": "staging_probe_manual_seed",
        "currency": CURRENCY,
        "settlement_currency": CURRENCY,
        "pricing": {
            "subtotal": PRICE,
            "discount_total": "0.00",
            "shipping_fee": "0.00",
            "tax": "0.00",
            "total": PRICE,
        },
        "line_items": [
            {
                "product_id": PRODUCT_ID,
                "variant_id": VARIANT_ID,
                "sku": SKU,
                "title": "Staging Probe Product",
                "quantity": 1,
                "unit_price_original": PRICE,
                "unit_price_effective": PRICE,
                "line_subtotal": PRICE,
                "currency": CURRENCY,
            }
        ],
        "delivery_options": [],
        "promotion_lines": [],
        "discount_evidence": {},
        "store_discount_evidence": {},
        "payment_offer_evidence": {},
        "payment_pricing": {},
        "savings_presentation": {},
        "metadata": {
            "seeded_by": "scripts/staging_probe/seed.py",
            "expected_minor_units": EXPECTED_MINOR_UNITS,
        },
    }


async def _execute_ddl(conn: Any) -> None:
    ddl = [
        """
        CREATE TABLE IF NOT EXISTS agents (
            agent_id VARCHAR(50) PRIMARY KEY,
            agent_name VARCHAR(255),
            agent_type VARCHAR(50),
            name VARCHAR(255),
            email VARCHAR(255),
            company VARCHAR(255),
            description TEXT,
            api_key VARCHAR(255),
            api_key_hash VARCHAR(255),
            status VARCHAR(50) DEFAULT 'active',
            is_active BOOLEAN DEFAULT TRUE,
            allowed_merchants JSONB,
            rate_limit INTEGER DEFAULT 1000,
            daily_quota INTEGER DEFAULT 10000,
            total_requests INTEGER DEFAULT 0,
            total_orders INTEGER DEFAULT 0,
            total_gmv NUMERIC(12,2) DEFAULT 0,
            success_rate NUMERIC(5,2) DEFAULT 0,
            owner_email VARCHAR(255),
            webhook_url VARCHAR(500),
            metadata JSONB,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            last_used_at TIMESTAMP WITH TIME ZONE
        )
        """,
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS agent_name VARCHAR(255)",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS agent_type VARCHAR(50)",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS name VARCHAR(255)",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS email VARCHAR(255)",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS company VARCHAR(255)",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS description TEXT",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS api_key_hash VARCHAR(255)",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS status VARCHAR(50) DEFAULT 'active'",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS allowed_merchants JSONB",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS daily_quota INTEGER DEFAULT 10000",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS owner_email VARCHAR(255)",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS webhook_url VARCHAR(500)",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS metadata JSONB",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMP WITH TIME ZONE",
        """
        CREATE TABLE IF NOT EXISTS api_keys (
            id SERIAL PRIMARY KEY,
            agent_id VARCHAR(255) NOT NULL,
            name VARCHAR(255) NOT NULL,
            key_hash VARCHAR(255) NOT NULL UNIQUE,
            key_prefix VARCHAR(20) NOT NULL,
            status VARCHAR(50) DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_used TIMESTAMP,
            usage_count INTEGER DEFAULT 0
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS agent_api_keys (
            id SERIAL PRIMARY KEY,
            agent_id VARCHAR(50) NOT NULL,
            key_id VARCHAR(50) UNIQUE NOT NULL,
            key_hash VARCHAR(255) NOT NULL,
            key_prefix VARCHAR(20) NOT NULL,
            scopes JSON DEFAULT '["orders:read","products:read"]'::json,
            ip_whitelist JSON DEFAULT '[]'::json,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            expires_at TIMESTAMP WITH TIME ZONE,
            last_used_at TIMESTAMP WITH TIME ZONE,
            last_rotated_at TIMESTAMP WITH TIME ZONE,
            created_by VARCHAR(100)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS merchant_onboarding (
            id SERIAL PRIMARY KEY,
            merchant_id VARCHAR(50) UNIQUE,
            business_name VARCHAR(255) NOT NULL,
            store_url VARCHAR(500) NOT NULL,
            website VARCHAR(500),
            region VARCHAR(50),
            contact_email VARCHAR(255) NOT NULL,
            contact_phone VARCHAR(50),
            auto_approved BOOLEAN DEFAULT FALSE,
            approval_confidence FLOAT DEFAULT 0.0,
            status VARCHAR(50) DEFAULT 'pending_verification',
            psp_connected BOOLEAN DEFAULT FALSE,
            psp_type VARCHAR(50),
            psp_sandbox_key TEXT,
            mcp_connected BOOLEAN DEFAULT FALSE,
            mcp_platform VARCHAR(50),
            mcp_shop_domain VARCHAR(255),
            mcp_access_token TEXT,
            api_key VARCHAR(255),
            api_key_hash VARCHAR(255),
            kyc_documents JSON,
            rejection_reason TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            verified_at TIMESTAMP WITH TIME ZONE,
            psp_connected_at TIMESTAMP WITH TIME ZONE,
            apm_enabled BOOLEAN NOT NULL DEFAULT FALSE
        )
        """,
        "ALTER TABLE merchant_onboarding ADD COLUMN IF NOT EXISTS apm_enabled BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE merchant_onboarding ADD COLUMN IF NOT EXISTS apm_cadence_days INTEGER",
        "ALTER TABLE merchant_onboarding ADD COLUMN IF NOT EXISTS apm_scope_jsonb JSONB",
        "ALTER TABLE merchant_onboarding ADD COLUMN IF NOT EXISTS apm_configured_at TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE merchant_onboarding ADD COLUMN IF NOT EXISTS apm_last_run_at TIMESTAMP WITH TIME ZONE",
        """
        CREATE TABLE IF NOT EXISTS merchant_stores (
            store_id VARCHAR(50) PRIMARY KEY,
            merchant_id VARCHAR(50) NOT NULL,
            platform VARCHAR(50) NOT NULL,
            name VARCHAR(255) NOT NULL,
            domain VARCHAR(255),
            api_key TEXT,
            status VARCHAR(50) DEFAULT 'connected',
            connected_at TIMESTAMP WITH TIME ZONE,
            last_sync TIMESTAMP WITH TIME ZONE,
            product_count INTEGER DEFAULT 0,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
        """,
        "ALTER TABLE merchant_stores ADD COLUMN IF NOT EXISTS is_primary BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE merchant_stores ADD COLUMN IF NOT EXISTS order_writeback_status TEXT NOT NULL DEFAULT 'disabled'",
        """
        CREATE TABLE IF NOT EXISTS merchant_psps (
            psp_id VARCHAR(50) PRIMARY KEY,
            merchant_id VARCHAR(50) NOT NULL,
            provider VARCHAR(50) NOT NULL,
            name VARCHAR(255) NOT NULL,
            api_key TEXT,
            account_id VARCHAR(255),
            secret_key TEXT,
            environment VARCHAR(20) DEFAULT 'unknown',
            provider_config JSONB DEFAULT '{}'::jsonb,
            validation_status VARCHAR(20) DEFAULT 'unknown',
            validation_error TEXT,
            last_validated_at TIMESTAMP WITH TIME ZONE,
            capabilities TEXT,
            status VARCHAR(50) DEFAULT 'active',
            connected_at TIMESTAMP WITH TIME ZONE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS payment_routes (
            id SERIAL PRIMARY KEY,
            route_id VARCHAR(50) UNIQUE NOT NULL,
            agent_id VARCHAR(50),
            merchant_id VARCHAR(50),
            psp_priority JSONB DEFAULT '[]'::jsonb,
            routing_strategy VARCHAR(30) DEFAULT 'priority',
            is_active BOOLEAN DEFAULT TRUE,
            max_retries INTEGER DEFAULT 2,
            timeout_ms INTEGER DEFAULT 30000,
            metadata JSONB DEFAULT '{}'::jsonb,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS products_cache (
            id SERIAL PRIMARY KEY,
            merchant_id VARCHAR(50) NOT NULL,
            platform VARCHAR(50) NOT NULL,
            platform_product_id VARCHAR(100) NOT NULL,
            product_data JSONB NOT NULL,
            cache_status VARCHAR(20) DEFAULT 'fresh',
            cached_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
            ttl_seconds INTEGER DEFAULT 3600,
            access_count INTEGER DEFAULT 0,
            last_accessed_at TIMESTAMP WITH TIME ZONE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS quotes (
            quote_id VARCHAR(64) PRIMARY KEY,
            merchant_id VARCHAR(64) NOT NULL,
            agent_id VARCHAR(64),
            engine VARCHAR(64) NOT NULL,
            engine_ref VARCHAR(256) NOT NULL,
            request_fingerprint VARCHAR(128) NOT NULL,
            request_json JSONB NOT NULL,
            snapshot_json JSONB NOT NULL,
            quote_hash_sha256 CHAR(64),
            status VARCHAR(32) NOT NULL,
            expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
            consumed_at TIMESTAMP WITH TIME ZONE,
            consumed_order_id VARCHAR(64),
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
            debug_id VARCHAR(64),
            notes TEXT
        )
        """,
        """
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
        )
        """,
        """
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
        )
        """,
        """
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
        )
        """,
        """
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
        )
        """,
    ]
    for statement in ddl:
        await conn.execute(statement)


async def _seed(conn: Any, *, agent_key_hash: str, agent_key_prefix: str, stripe_secret: str, stripe_pk: str, stripe_whsec: str) -> None:
    product_data = _product_data()
    quote_request = _quote_request_json()
    quote_snapshot = _quote_snapshot_json()
    provider_config = {
        "mode": "payment_intent",
        "public_key": stripe_pk,
        "webhook_endpoint_secret": stripe_whsec,
        "webhook_url": f"https://web-staging-3f9e.up.railway.app/webhooks/stripe/{PSP_ID}",
        "test_probe": True,
        "expected_minor_units": EXPECTED_MINOR_UNITS,
    }

    async with conn.transaction():
        await _execute_ddl(conn)

        await conn.execute(
            """
            INSERT INTO agents (
                agent_id, agent_name, agent_type, name, email, company, description,
                api_key, api_key_hash, status, is_active, allowed_merchants,
                rate_limit, daily_quota, total_requests, total_orders, total_gmv,
                success_rate, owner_email, metadata, created_at, updated_at
            ) VALUES (
                $1, $2, $3, $2, $4, $5, $6,
                $7, $8, 'active', TRUE, $9,
                1000, 10000, 0, 0, 0,
                0, $4, $10, NOW(), NOW()
            )
            ON CONFLICT (agent_id) DO UPDATE SET
                agent_name = EXCLUDED.agent_name,
                agent_type = EXCLUDED.agent_type,
                name = EXCLUDED.name,
                email = EXCLUDED.email,
                company = EXCLUDED.company,
                description = EXCLUDED.description,
                api_key_hash = EXCLUDED.api_key_hash,
                status = 'active',
                is_active = TRUE,
                allowed_merchants = EXCLUDED.allowed_merchants,
                rate_limit = EXCLUDED.rate_limit,
                daily_quota = EXCLUDED.daily_quota,
                owner_email = EXCLUDED.owner_email,
                metadata = EXCLUDED.metadata,
                updated_at = NOW()
            """,
            AGENT_ID,
            "Staging PSP Probe Agent",
            "test_probe",
            "staging-probe@pivota.invalid",
            "Pivota",
            "Agent used only for the controlled staging PSP probe.",
            "__hash_key_only__",
            agent_key_hash,
            _json([MERCHANT_ID]),
            _json({"seeded_by": "scripts/staging_probe/seed.py", "scope": "staging_psp_probe"}),
        )

        await conn.execute(
            """
            INSERT INTO api_keys (agent_id, name, key_hash, key_prefix, status, created_at, usage_count)
            VALUES ($1, 'staging-psp-probe', $2, $3, 'active', NOW(), 0)
            ON CONFLICT (key_hash) DO UPDATE SET
                agent_id = EXCLUDED.agent_id,
                name = EXCLUDED.name,
                key_prefix = EXCLUDED.key_prefix,
                status = 'active'
            """,
            AGENT_ID,
            agent_key_hash,
            agent_key_prefix,
        )

        await conn.execute(
            """
            INSERT INTO agent_api_keys (
                agent_id, key_id, key_hash, key_prefix, scopes, ip_whitelist,
                is_active, created_at, created_by
            ) VALUES (
                $1, $2, $3, $4, $5, $6, TRUE, NOW(), 'scripts/staging_probe/seed.py'
            )
            ON CONFLICT (key_id) DO UPDATE SET
                key_hash = EXCLUDED.key_hash,
                key_prefix = EXCLUDED.key_prefix,
                scopes = EXCLUDED.scopes,
                ip_whitelist = EXCLUDED.ip_whitelist,
                is_active = TRUE,
                last_rotated_at = NOW(),
                created_by = EXCLUDED.created_by
            """,
            AGENT_ID,
            AGENT_KEY_ID,
            agent_key_hash,
            agent_key_prefix,
            _json(["orders:read", "orders:write", "products:read", "payments:write"]),
            _json([]),
        )

        await conn.execute(
            """
            INSERT INTO merchant_onboarding (
                merchant_id, business_name, store_url, website, region, contact_email,
                contact_phone, auto_approved, approval_confidence, status,
                psp_connected, psp_type, psp_sandbox_key, mcp_connected,
                mcp_platform, mcp_shop_domain, mcp_access_token,
                kyc_documents, created_at, updated_at, verified_at,
                psp_connected_at, apm_enabled
            ) VALUES (
                $1, 'Pivota Staging Probe Merchant', 'https://staging-probe.invalid',
                'https://staging-probe.invalid', 'US', 'staging-probe@pivota.invalid',
                '+14155550100', TRUE, 1.0, 'approved',
                TRUE, 'stripe', 'configured-in-merchant-psps', TRUE,
                'shopify', 'staging-probe.myshopify.com', NULL,
                $2, NOW(), NOW(), NOW(), NOW(), FALSE
            )
            ON CONFLICT (merchant_id) DO UPDATE SET
                business_name = EXCLUDED.business_name,
                store_url = EXCLUDED.store_url,
                website = EXCLUDED.website,
                region = EXCLUDED.region,
                contact_email = EXCLUDED.contact_email,
                status = 'approved',
                psp_connected = TRUE,
                psp_type = 'stripe',
                psp_sandbox_key = 'configured-in-merchant-psps',
                mcp_connected = TRUE,
                mcp_platform = 'shopify',
                mcp_shop_domain = EXCLUDED.mcp_shop_domain,
                updated_at = NOW(),
                verified_at = COALESCE(merchant_onboarding.verified_at, NOW()),
                psp_connected_at = COALESCE(merchant_onboarding.psp_connected_at, NOW()),
                apm_enabled = FALSE
            """,
            MERCHANT_ID,
            _json({"seeded_by": "scripts/staging_probe/seed.py", "test_probe": True}),
        )

        await conn.execute(
            """
            INSERT INTO merchant_stores (
                store_id, merchant_id, platform, name, domain, api_key, status,
                connected_at, last_sync, product_count, created_at,
                is_primary, order_writeback_status
            ) VALUES (
                $1, $2, 'shopify', 'Staging Probe Shopify Store',
                'staging-probe.myshopify.com', NULL, 'connected',
                NOW(), NOW(), 1, NOW(), TRUE, 'disabled'
            )
            ON CONFLICT (store_id) DO UPDATE SET
                merchant_id = EXCLUDED.merchant_id,
                platform = 'shopify',
                name = EXCLUDED.name,
                domain = EXCLUDED.domain,
                status = 'connected',
                connected_at = COALESCE(merchant_stores.connected_at, NOW()),
                last_sync = NOW(),
                product_count = 1,
                is_primary = TRUE,
                order_writeback_status = 'disabled'
            """,
            STORE_ID,
            MERCHANT_ID,
        )

        await conn.execute(
            "UPDATE merchant_stores SET is_primary = FALSE WHERE merchant_id = $1 AND store_id <> $2",
            MERCHANT_ID,
            STORE_ID,
        )

        update_status = await conn.execute(
            """
            UPDATE products_cache
            SET product_data = $4,
                cache_status = 'fresh',
                cached_at = NOW(),
                expires_at = NOW() + INTERVAL '24 hours',
                ttl_seconds = 86400
            WHERE merchant_id = $1 AND platform = $2 AND platform_product_id = $3
            """,
            MERCHANT_ID,
            "shopify",
            PRODUCT_ID,
            _json(product_data),
        )
        if update_status == "UPDATE 0":
            await conn.execute(
                """
                INSERT INTO products_cache (
                    merchant_id, platform, platform_product_id, product_data,
                    cache_status, cached_at, expires_at, ttl_seconds,
                    access_count, last_accessed_at
                ) VALUES (
                    $1, 'shopify', $2, $3, 'fresh', NOW(),
                    NOW() + INTERVAL '24 hours', 86400, 0, NULL
                )
                """,
                MERCHANT_ID,
                PRODUCT_ID,
                _json(product_data),
            )

        await conn.execute(
            """
            INSERT INTO catalog_merchants (
                merchant_id, merchant_name, primary_platform, status, source_system,
                source_ref, metadata_json, created_at, updated_at
            ) VALUES (
                $1, 'Pivota Staging Probe Merchant', 'shopify', 'active',
                'staging_probe', $1, $2, NOW(), NOW()
            )
            ON CONFLICT (merchant_id) DO UPDATE SET
                merchant_name = EXCLUDED.merchant_name,
                primary_platform = 'shopify',
                status = 'active',
                metadata_json = EXCLUDED.metadata_json,
                updated_at = NOW()
            """,
            MERCHANT_ID,
            _json({"seeded_by": "scripts/staging_probe/seed.py"}),
        )

        await conn.execute(
            """
            INSERT INTO catalog_products (
                product_key, merchant_id, platform, source_product_id,
                catalog_track, truth_tier, readiness_tier, source_system,
                source_ref, title, description, brand, product_type,
                category, canonical_url, product_payload, freshness_json,
                created_at, updated_at
            ) VALUES (
                $1, $2, 'shopify', $3, 'internal_merchant', 'primary',
                'commerce_ready', 'staging_probe', $3,
                'Staging Probe Product',
                'Controlled staging product for PSP money-path validation.',
                'Pivota Staging', 'Probe', 'Probe',
                'https://staging-probe.invalid/products/staging-probe-product',
                $4, $5, NOW(), NOW()
            )
            ON CONFLICT (product_key) DO UPDATE SET
                title = EXCLUDED.title,
                description = EXCLUDED.description,
                readiness_tier = 'commerce_ready',
                product_payload = EXCLUDED.product_payload,
                freshness_json = EXCLUDED.freshness_json,
                updated_at = NOW()
            """,
            PRODUCT_KEY,
            MERCHANT_ID,
            PRODUCT_ID,
            _json(product_data),
            _json({"cached_until": "seed_refresh_plus_24h"}),
        )

        await conn.execute(
            """
            INSERT INTO catalog_skus (
                sku_key, product_key, merchant_id, platform, source_product_id,
                source_variant_id, sku, title, currency, visible_attributes,
                visible_option_labels, sku_payload, readiness_tier,
                created_at, updated_at
            ) VALUES (
                $1, $2, $3, 'shopify', $4, $5, $6, 'Default Title', $7,
                $8, $9, $10, 'commerce_ready', NOW(), NOW()
            )
            ON CONFLICT (sku_key) DO UPDATE SET
                sku = EXCLUDED.sku,
                title = EXCLUDED.title,
                currency = EXCLUDED.currency,
                visible_attributes = EXCLUDED.visible_attributes,
                visible_option_labels = EXCLUDED.visible_option_labels,
                sku_payload = EXCLUDED.sku_payload,
                readiness_tier = 'commerce_ready',
                updated_at = NOW()
            """,
            SKU_KEY,
            PRODUCT_KEY,
            MERCHANT_ID,
            PRODUCT_ID,
            VARIANT_ID,
            SKU,
            CURRENCY,
            _json({"Title": "Default Title"}),
            _json(["Default Title"]),
            _json(product_data["variants"][0]),
        )

        await conn.execute(
            """
            INSERT INTO catalog_offers (
                offer_id, sku_key, product_key, merchant_id, catalog_track,
                truth_tier, readiness_tier, offer_mode, channel,
                availability, inventory_quantity, currency, list_price,
                merchant_effective_price, estimated_best_price,
                price_confidence, source_system, source_ref, offer_payload,
                created_at, updated_at
            ) VALUES (
                $1, $2, $3, $4, 'internal_merchant', 'primary',
                'commerce_ready', 'pivota_direct', 'default',
                'in_stock', 99, $5, $6::numeric, $6::numeric, $6::numeric,
                1.00, 'staging_probe', $1, $7, NOW(), NOW()
            )
            ON CONFLICT (offer_id) DO UPDATE SET
                availability = 'in_stock',
                inventory_quantity = 99,
                currency = EXCLUDED.currency,
                list_price = EXCLUDED.list_price,
                merchant_effective_price = EXCLUDED.merchant_effective_price,
                estimated_best_price = EXCLUDED.estimated_best_price,
                price_confidence = 1.00,
                offer_payload = EXCLUDED.offer_payload,
                updated_at = NOW()
            """,
            OFFER_ID,
            SKU_KEY,
            PRODUCT_KEY,
            MERCHANT_ID,
            CURRENCY,
            PRICE,
            _json({"expected_minor_units": EXPECTED_MINOR_UNITS}),
        )

        await conn.execute(
            """
            INSERT INTO merchant_psps (
                psp_id, merchant_id, provider, name, api_key, account_id,
                secret_key, environment, provider_config, validation_status,
                validation_error, last_validated_at, capabilities, status,
                connected_at, created_at
            ) VALUES (
                $1, $2, 'stripe', 'Stripe Test - Staging Probe',
                $3, NULL, $3, 'test', $4, 'valid',
                NULL, NOW(), 'payment_intents,webhooks,card_payments',
                'active', NOW(), NOW()
            )
            ON CONFLICT (psp_id) DO UPDATE SET
                merchant_id = EXCLUDED.merchant_id,
                provider = 'stripe',
                name = EXCLUDED.name,
                api_key = EXCLUDED.api_key,
                secret_key = EXCLUDED.secret_key,
                environment = 'test',
                provider_config = EXCLUDED.provider_config,
                validation_status = 'valid',
                validation_error = NULL,
                last_validated_at = NOW(),
                capabilities = EXCLUDED.capabilities,
                status = 'active',
                connected_at = COALESCE(merchant_psps.connected_at, NOW())
            """,
            PSP_ID,
            MERCHANT_ID,
            stripe_secret,
            _json(provider_config),
        )

        await conn.execute(
            """
            INSERT INTO payment_routes (
                route_id, agent_id, merchant_id, psp_priority,
                routing_strategy, is_active, max_retries, timeout_ms,
                metadata, created_at, updated_at
            ) VALUES (
                $1, $2, $3, $4, 'priority', TRUE, 0, 30000,
                $5, NOW(), NOW()
            )
            ON CONFLICT (route_id) DO UPDATE SET
                agent_id = EXCLUDED.agent_id,
                merchant_id = EXCLUDED.merchant_id,
                psp_priority = EXCLUDED.psp_priority,
                routing_strategy = 'priority',
                is_active = TRUE,
                max_retries = 0,
                timeout_ms = 30000,
                metadata = EXCLUDED.metadata,
                updated_at = NOW()
            """,
            ROUTE_ID,
            AGENT_ID,
            MERCHANT_ID,
            _json([{"psp": "stripe", "priority": 1}]),
            _json({"seeded_by": "scripts/staging_probe/seed.py", "test_probe": True}),
        )

        await conn.execute(
            """
            INSERT INTO quotes (
                quote_id, merchant_id, agent_id, engine, engine_ref,
                request_fingerprint, request_json, snapshot_json,
                quote_hash_sha256, status, expires_at, consumed_at,
                consumed_order_id, created_at, updated_at, debug_id, notes
            ) VALUES (
                $1, $2, $3, 'shopify_rest_checkout', 'staging_probe_manual_seed',
                $4, $5, $6, $7, 'active',
                NOW() + INTERVAL '24 hours', NULL, NULL, NOW(), NOW(),
                'dbg_staging_probe_quote', 'Seeded quote for controlled staging PSP probe'
            )
            ON CONFLICT (quote_id) DO UPDATE SET
                merchant_id = EXCLUDED.merchant_id,
                agent_id = EXCLUDED.agent_id,
                engine = EXCLUDED.engine,
                engine_ref = EXCLUDED.engine_ref,
                request_fingerprint = EXCLUDED.request_fingerprint,
                request_json = EXCLUDED.request_json,
                snapshot_json = EXCLUDED.snapshot_json,
                quote_hash_sha256 = EXCLUDED.quote_hash_sha256,
                status = 'active',
                expires_at = NOW() + INTERVAL '24 hours',
                consumed_at = NULL,
                consumed_order_id = NULL,
                created_at = NOW(),
                updated_at = NOW(),
                debug_id = EXCLUDED.debug_id,
                notes = EXCLUDED.notes
            """,
            QUOTE_ID,
            MERCHANT_ID,
            AGENT_ID,
            REQUEST_FINGERPRINT,
            _json(quote_request),
            _json(quote_snapshot),
            QUOTE_HASH,
        )


async def _verify(conn: Any) -> Dict[str, Any]:
    row = await conn.fetchrow(
        """
        SELECT
          mo.merchant_id,
          mo.status AS merchant_status,
          mo.psp_connected,
          mo.mcp_platform,
          ms.store_id,
          ms.platform AS store_platform,
          ms.is_primary,
          ms.order_writeback_status,
          pc.platform_product_id,
          pc.product_data ->> 'price' AS product_price,
          pc.product_data ->> 'currency' AS product_currency,
          mp.psp_id,
          mp.provider,
          mp.environment,
          mp.status AS psp_status,
          mp.validation_status,
          (COALESCE(mp.secret_key, mp.api_key) IS NOT NULL AND COALESCE(mp.secret_key, mp.api_key) <> '') AS psp_secret_present,
          ((mp.provider_config ->> 'public_key') IS NOT NULL AND (mp.provider_config ->> 'public_key') <> '') AS public_key_present,
          ((mp.provider_config ->> 'webhook_endpoint_secret') IS NOT NULL AND (mp.provider_config ->> 'webhook_endpoint_secret') <> '') AS webhook_secret_present,
          q.quote_id,
          q.status AS quote_status,
          q.request_fingerprint,
          q.expires_at
        FROM merchant_onboarding mo
        LEFT JOIN merchant_stores ms ON ms.merchant_id = mo.merchant_id AND ms.store_id = $2
        LEFT JOIN products_cache pc ON pc.merchant_id = mo.merchant_id AND pc.platform = 'shopify' AND pc.platform_product_id = $3
        LEFT JOIN merchant_psps mp ON mp.merchant_id = mo.merchant_id AND mp.psp_id = $4
        LEFT JOIN quotes q ON q.merchant_id = mo.merchant_id AND q.quote_id = $5
        WHERE mo.merchant_id = $1
        """,
        MERCHANT_ID,
        STORE_ID,
        PRODUCT_ID,
        PSP_ID,
        QUOTE_ID,
    )
    if not row:
        raise RuntimeError("verification row missing")

    api_key_count = await conn.fetchval("SELECT COUNT(*) FROM api_keys WHERE agent_id = $1 AND status = 'active'", AGENT_ID)
    agent_key_count = await conn.fetchval(
        "SELECT COUNT(*) FROM agent_api_keys WHERE agent_id = $1 AND is_active = TRUE",
        AGENT_ID,
    )
    route_count = await conn.fetchval(
        "SELECT COUNT(*) FROM payment_routes WHERE merchant_id = $1 AND is_active = TRUE",
        MERCHANT_ID,
    )
    catalog_offer = await conn.fetchrow(
        """
        SELECT offer_id, currency, merchant_effective_price, availability
        FROM catalog_offers
        WHERE offer_id = $1
        """,
        OFFER_ID,
    )

    return {
        "merchant": {
            "merchant_id": row["merchant_id"],
            "status": row["merchant_status"],
            "psp_connected": bool(row["psp_connected"]),
            "mcp_platform": row["mcp_platform"],
        },
        "store": {
            "store_id": row["store_id"],
            "platform": row["store_platform"],
            "is_primary": bool(row["is_primary"]),
            "order_writeback_status": row["order_writeback_status"],
        },
        "product": {
            "platform_product_id": row["platform_product_id"],
            "variant_id": VARIANT_ID,
            "price": row["product_price"],
            "currency": row["product_currency"],
            "expected_minor_units": EXPECTED_MINOR_UNITS,
        },
        "agent_auth": {
            "agent_id": AGENT_ID,
            "api_keys_active": int(api_key_count or 0),
            "agent_api_keys_active": int(agent_key_count or 0),
        },
        "merchant_psp": {
            "psp_id": row["psp_id"],
            "provider": row["provider"],
            "environment": row["environment"],
            "status": row["psp_status"],
            "validation_status": row["validation_status"],
            "psp_secret_present": bool(row["psp_secret_present"]),
            "public_key_present": bool(row["public_key_present"]),
            "webhook_secret_present": bool(row["webhook_secret_present"]),
        },
        "payment_route": {
            "route_id": ROUTE_ID,
            "active_routes_for_merchant": int(route_count or 0),
        },
        "quote": {
            "quote_id": row["quote_id"],
            "status": row["quote_status"],
            "request_fingerprint": row["request_fingerprint"],
            "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
        },
        "catalog_offer": dict(catalog_offer) if catalog_offer else None,
    }


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Seed or verify the staging PSP probe data.")
    parser.add_argument("--verify-only", action="store_true", help="Run read-only verification and print non-secret fields.")
    args = parser.parse_args()

    try:
        asyncpg = _load_asyncpg()
        database_url = _required_env("DATABASE_URL")

        seed_inputs: Optional[Tuple[str, str, str, str, str]] = None
        if not args.verify_only:
            agent_key_hash, agent_key_prefix = _resolve_agent_key_hash()
            stripe_secret = _required_env("STRIPE_TEST_SECRET_KEY")
            stripe_pk = _required_env("STRIPE_TEST_PUBLISHABLE_KEY")
            stripe_whsec = _required_env("STRIPE_TEST_WEBHOOK_SECRET")
            _validate_secret_prefix("STRIPE_TEST_SECRET_KEY", stripe_secret, "sk_test_")
            _validate_secret_prefix("STRIPE_TEST_PUBLISHABLE_KEY", stripe_pk, "pk_test_")
            _validate_secret_prefix("STRIPE_TEST_WEBHOOK_SECRET", stripe_whsec, "whsec_")
            seed_inputs = (agent_key_hash, agent_key_prefix, stripe_secret, stripe_pk, stripe_whsec)

        conn = await asyncpg.connect(database_url)
        try:
            if seed_inputs:
                await _seed(
                    conn,
                    agent_key_hash=seed_inputs[0],
                    agent_key_prefix=seed_inputs[1],
                    stripe_secret=seed_inputs[2],
                    stripe_pk=seed_inputs[3],
                    stripe_whsec=seed_inputs[4],
                )
            verification = await _verify(conn)
        finally:
            await conn.close()

    except ConfigError as exc:
        print(f"staging_probe_seed_config_error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"staging_probe_seed_error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(verification, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
