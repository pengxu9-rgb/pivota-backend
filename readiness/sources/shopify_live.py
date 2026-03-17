from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from adapters.product_adapters import ShopifyProductAdapter
from db.database import database
from db.merchant_onboarding import get_merchant_onboarding
from db.products import get_cached_products
from jobs.catalog_import_worker import _get_shopify_config_for_merchant
from models.standard_product import StandardProduct
from readiness.flags import readiness_alpha_merchant_id
from readiness.models import MerchantSourceDataset
from services.merchant_store_service import get_primary_store

logger = logging.getLogger(__name__)

_POLICY_FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "alpha_merchant_policies.json"


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        raw = value.strip()
        if raw.endswith("+00:00"):
            return raw.replace("+00:00", "Z")
        return raw or None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return str(value)


def _load_policy_map() -> Dict[str, Any]:
    if not _POLICY_FIXTURE_PATH.exists():
        return {}
    try:
        return json.loads(_POLICY_FIXTURE_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed to parse alpha merchant policy fixture")
        return {}


def _map_cache_row_to_standard_product(merchant_id: str, row: Dict[str, Any]) -> Optional[StandardProduct]:
    product_data = row.get("product_data") or {}
    if isinstance(product_data, str):
        try:
            product_data = json.loads(product_data)
        except Exception:
            logger.warning("Skipping unreadable products_cache row for merchant=%s", merchant_id)
            return None

    if all(key in product_data for key in ("id", "merchant_id", "platform", "price")):
        return StandardProduct(**product_data)

    raw = product_data.get("raw")
    if isinstance(raw, dict):
        return ShopifyProductAdapter.convert_to_standard(raw, merchant_id)

    return None


async def _fetch_active_psp_config(merchant_id: str) -> Optional[Dict[str, Any]]:
    try:
        row = await database.fetch_one(
            """
            SELECT provider, psp_id, status, connected_at, api_key, account_id, secret_key
            FROM merchant_psps
            WHERE merchant_id = :merchant_id AND status = 'active'
            ORDER BY connected_at DESC
            LIMIT 1
            """,
            {"merchant_id": merchant_id},
        )
        return dict(row) if row else None
    except Exception:
        logger.warning("PSP capability lookup failed for merchant=%s", merchant_id, exc_info=True)
        return None


async def _fetch_live_products(merchant_id: str, shop_domain: str, access_token: str) -> Tuple[List[StandardProduct], Optional[str]]:
    products, _, error = await ShopifyProductAdapter.fetch_products(
        shop_domain=shop_domain,
        access_token=access_token,
        merchant_id=merchant_id,
        limit=250,
    )
    return products, error


class ShopifyLiveMerchantSource:
    async def load(self, merchant_id: str) -> MerchantSourceDataset:
        alpha_merchant_id = readiness_alpha_merchant_id()
        if merchant_id != alpha_merchant_id:
            raise KeyError(
                f"Unsupported real-merchant alpha id '{merchant_id}'. Expected '{alpha_merchant_id}'."
            )

        merchant = await get_merchant_onboarding(merchant_id) or {}
        merchant_name = str(merchant.get("business_name") or "Readiness Alpha Merchant")
        store = await get_primary_store(merchant_id)
        shopify_cfg = await _get_shopify_config_for_merchant(merchant_id)
        psp_config = await _fetch_active_psp_config(merchant_id)
        policy = (_load_policy_map().get(merchant_id) or {}).copy()

        merchant_blockers: List[str] = []
        merchant_warnings: List[str] = []
        audit_notes: List[str] = []

        shop_domain = str(shopify_cfg.get("shop_domain") or (store or {}).get("domain") or "").strip()
        access_token = str(shopify_cfg.get("access_token") or "").strip()
        shopify_connected = bool(shop_domain and access_token)
        if not shopify_connected:
            merchant_blockers.append("shopify_configuration_missing")
        if not policy:
            merchant_blockers.append("merchant_policy_missing")
            policy = {
                "shipping_supported": False,
                "returns_supported": False,
                "shipping_summary": None,
                "returns_summary": None,
                "policy_source": "readiness.alpha_policy_config.v1",
                "last_reviewed_at": None,
            }

        cached_rows: List[Dict[str, Any]] = []
        try:
            cached_rows = await get_cached_products(merchant_id=merchant_id, platform="shopify", include_expired=True)
        except Exception:
            merchant_warnings.append("products_cache_lookup_failed")
            logger.warning("products_cache lookup failed for merchant=%s", merchant_id, exc_info=True)

        products: List[StandardProduct] = []
        product_diagnostics: Dict[str, Dict[str, Any]] = {}
        variant_diagnostics: Dict[str, Dict[str, Any]] = {}

        if cached_rows:
            for row in cached_rows:
                product = _map_cache_row_to_standard_product(merchant_id, row)
                if not product:
                    continue
                products.append(product)
                cached_at = _iso(row.get("cached_at")) or _iso(product.updated_at)
                expires_at = _iso(row.get("expires_at"))
                source = "shopify_cache.standard_product.v1"
                product_diagnostics[product.id] = {
                    "catalog_last_refreshed_at": cached_at,
                    "media_last_refreshed_at": cached_at,
                    "price_last_refreshed_at": cached_at,
                    "inventory_last_refreshed_at": cached_at,
                    "cache_expires_at": expires_at,
                    "field_sources": {
                        "catalog": {"source": source, "fallback_source": "shopify_admin.products.v2024-07"},
                        "price": {"source": "shopify_cache.variant_offer.v1", "fallback_source": "shopify_admin.products.v2024-07"},
                        "inventory": {"source": "shopify_cache.inventory.v1", "fallback_source": "shopify_admin.inventory.v2024-07"},
                    },
                }
                for variant in product.variants or []:
                    variant_diagnostics[variant.id] = {
                        "catalog_last_refreshed_at": cached_at,
                        "media_last_refreshed_at": cached_at,
                        "price_last_refreshed_at": cached_at,
                        "inventory_last_refreshed_at": cached_at,
                        "shipping_profile": policy.get("shipping_profile") or "alpha_default",
                        "field_sources": {
                            "catalog": {"source": source, "fallback_source": "shopify_admin.products.v2024-07"},
                            "price": {"source": "shopify_cache.variant_offer.v1", "fallback_source": "shopify_admin.products.v2024-07"},
                            "inventory": {"source": "shopify_cache.inventory.v1", "fallback_source": "shopify_admin.inventory.v2024-07"},
                        },
                    }

        if not products and shopify_connected:
            live_products, live_error = await _fetch_live_products(merchant_id, shop_domain, access_token)
            if live_products:
                now_iso = _iso(datetime.now(timezone.utc))
                products = live_products
                for product in products:
                    product_diagnostics[product.id] = {
                        "catalog_last_refreshed_at": now_iso,
                        "media_last_refreshed_at": now_iso,
                        "price_last_refreshed_at": now_iso,
                        "inventory_last_refreshed_at": now_iso,
                        "field_sources": {
                            "catalog": {"source": "shopify_admin.products.v2024-07"},
                            "price": {"source": "shopify_admin.products.v2024-07"},
                            "inventory": {"source": "shopify_admin.inventory.v2024-07", "fallback_source": "shopify_cache.inventory.v1"},
                        },
                    }
                    for variant in product.variants or []:
                        variant_diagnostics[variant.id] = {
                            "catalog_last_refreshed_at": now_iso,
                            "media_last_refreshed_at": now_iso,
                            "price_last_refreshed_at": now_iso,
                            "inventory_last_refreshed_at": now_iso,
                            "shipping_profile": policy.get("shipping_profile") or "alpha_default",
                            "field_sources": {
                                "catalog": {"source": "shopify_admin.products.v2024-07"},
                                "price": {"source": "shopify_admin.products.v2024-07"},
                                "inventory": {"source": "shopify_admin.inventory.v2024-07", "fallback_source": "shopify_cache.inventory.v1"},
                            },
                        }
                audit_notes.append("products_cache was empty; readiness report fell back to live Shopify Admin API.")
            elif live_error:
                merchant_blockers.append("catalog_unavailable")
                merchant_warnings.append("live_shopify_fetch_failed")
                audit_notes.append(f"Live Shopify catalog fetch failed: {live_error}")

        if not products:
            merchant_blockers.append("catalog_missing")

        if not psp_config:
            merchant_warnings.append("active_psp_configuration_missing")

        payment_capabilities = {
            "merchant_native_checkout_supported": bool(shopify_connected and psp_config),
            "merchant_platform_writeback_supported": bool(shopify_connected),
            "ucp_checkout_supported": bool(shopify_connected and psp_config),
            "payment_mode": "merchant_psp" if psp_config else "blocked",
            "psp_provider": (psp_config or {}).get("provider"),
            "psp_id": (psp_config or {}).get("psp_id"),
        }

        capability_status = {
            "merchant_adapter": "ready" if shopify_connected else "blocked",
            "channel_export": "ready" if products else "blocked",
            "checkout": "ready" if payment_capabilities["merchant_native_checkout_supported"] else "blocked",
            "order_sync": "ready" if shopify_connected else "blocked",
        }

        source_of_truth = {
            "catalog": "shopify_cache.standard_product.v1" if cached_rows else "shopify_admin.products.v2024-07",
            "price": "shopify_cache.variant_offer.v1" if cached_rows else "shopify_admin.products.v2024-07",
            "inventory": "shopify_cache.inventory.v1" if cached_rows else "shopify_admin.inventory.v2024-07",
            "fulfillment_policy": str(policy.get("policy_source") or "readiness.alpha_policy_config.v1"),
            "checkout_capability": "readiness.checkout_capability.v1",
            "order_status": "readiness.order_sync.v2",
        }

        evaluation_reference_time = _iso(datetime.now(timezone.utc)) or "2026-03-17T00:00:00Z"
        merchant_connection = {
            "platform": "shopify",
            "store": store or {},
            "shopify": {
                "shop_domain": shop_domain,
                "access_token": access_token,
                "credential_source": str(shopify_cfg.get("source_type") or "unknown"),
            },
            "psp": psp_config or {},
        }

        audit_notes.extend(
            [
                "Real-merchant alpha is restricted to one Shopify merchant.",
                "Normalized review ingestion remains absent and is still scored as blocked.",
            ]
        )

        return MerchantSourceDataset(
            merchant_id=merchant_id,
            merchant_name=merchant_name,
            evaluation_reference_time=evaluation_reference_time,
            merchant_alpha_mode="real_merchant_alpha",
            source_of_truth=source_of_truth,
            capability_status=capability_status,
            merchant_blockers=merchant_blockers,
            merchant_warnings=merchant_warnings,
            stubbed_capabilities=[] if payment_capabilities["merchant_native_checkout_supported"] else ["payment_execution_unimplemented"],
            merchant_policy=policy,
            payment_capabilities=payment_capabilities,
            merchant_connection=merchant_connection,
            products=products,
            product_diagnostics=product_diagnostics,
            variant_diagnostics=variant_diagnostics,
            audit_notes=audit_notes,
        )


async def load_shopify_live_merchant_dataset(merchant_id: str) -> MerchantSourceDataset:
    return await ShopifyLiveMerchantSource().load(merchant_id)
