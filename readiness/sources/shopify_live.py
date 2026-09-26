from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from adapters.product_adapters import SHOPIFY_NEXT_PAGE_TOKEN_UNPARSEABLE, ShopifyProductAdapter
from db.database import database
from db.merchant_onboarding import get_merchant_onboarding
from db.products import get_cached_products
from jobs.catalog_import_worker import _get_shopify_config_for_merchant
from models.standard_product import StandardProduct, StandardProductVariant
from readiness.flags import readiness_alpha_merchant_id
from readiness.models import MerchantSourceDataset
from readiness.reviews import load_product_review_summaries
from services.canonical_commerce_service import load_canonical_cache_rows
from services.merchant_store_service import get_primary_store

logger = logging.getLogger(__name__)

# Mirrors `merchant_stores`' own active-status vocabulary
# (services/store_lifecycle_service.ACTIVE_STORE_STATUSES). A store outside this
# set is attached in name only and must not keep a catalog alive.
_ATTACHED_STORE_STATUSES = frozenset({"active", "connected"})

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


def _parse_datetime(value: Any) -> Optional[datetime]:
    raw = _iso(value)
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _model_dump(model: Any) -> Dict[str, Any]:
    dump = getattr(model, "model_dump", None)
    if callable(dump):
        return dump()
    return model.dict()


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


async def _load_runtime_cache_rows(merchant_id: str) -> List[Dict[str, Any]]:
    canonical_rows = await load_canonical_cache_rows(
        merchant_id=merchant_id,
        platform="shopify",
        include_expired=True,
    )
    if canonical_rows:
        return canonical_rows
    return await get_cached_products(merchant_id=merchant_id, platform="shopify", include_expired=True)


async def _fetch_live_products(merchant_id: str, shop_domain: str, access_token: str) -> Tuple[List[StandardProduct], Optional[str]]:
    all_products: List[StandardProduct] = []
    page_info: Optional[str] = None
    page_count = 0

    while True:
        products, next_page_token, error = await ShopifyProductAdapter.fetch_products(
            shop_domain=shop_domain,
            access_token=access_token,
            merchant_id=merchant_id,
            limit=250,
            page_info=page_info,
        )
        if error:
            return all_products, error if all_products else error
        all_products.extend(products)
        page_count += 1

        if not next_page_token:
            break
        if next_page_token == SHOPIFY_NEXT_PAGE_TOKEN_UNPARSEABLE:
            return all_products, f"live_shopify_pagination_unparseable_after_{page_count}_pages"
        if next_page_token == page_info:
            return all_products, f"live_shopify_pagination_loop_detected_after_{page_count}_pages"
        page_info = next_page_token

    deduped: Dict[str, StandardProduct] = {}
    for product in all_products:
        deduped[product.id] = product
    return list(deduped.values()), None


def _cached_rows_need_live_overlay(rows: List[Dict[str, Any]], reference_time: datetime) -> bool:
    inventory_max_age_hours = 0.25
    for row in rows:
        observed_at = _parse_datetime(row.get("cached_at"))
        if observed_at is None:
            return True
        age_hours = (reference_time - observed_at.astimezone(timezone.utc)).total_seconds() / 3600
        if age_hours > inventory_max_age_hours:
            return True
    return False


def _merge_variant_with_live_overlay(
    cached_variant: StandardProductVariant,
    live_variant: StandardProductVariant,
) -> StandardProductVariant:
    merged = _model_dump(cached_variant)
    live = _model_dump(live_variant)
    for field in (
        "title",
        "sku",
        "barcode",
        "price",
        "compare_at_price",
        "inventory_quantity",
        "weight",
        "weight_unit",
        "options",
        "image_url",
    ):
        value = live.get(field)
        if value is not None or field in {"price", "inventory_quantity"}:
            merged[field] = value
    return StandardProductVariant(**merged)


def _merge_product_with_live_overlay(
    cached_product: StandardProduct,
    live_product: StandardProduct,
) -> StandardProduct:
    merged = _model_dump(cached_product)
    live = _model_dump(live_product)

    for field in ("price", "compare_at_price", "currency", "inventory_quantity", "sku", "barcode", "updated_at", "orderable", "orderable_validation", "in_stock"):
        value = live.get(field)
        if value is not None:
            merged[field] = value

    if not merged.get("title"):
        merged["title"] = live.get("title")
    if not merged.get("description"):
        merged["description"] = live.get("description")
    if not merged.get("vendor"):
        merged["vendor"] = live.get("vendor")
    if not merged.get("product_type"):
        merged["product_type"] = live.get("product_type")
    if not merged.get("image_url"):
        merged["image_url"] = live.get("image_url")
    if not merged.get("images"):
        merged["images"] = live.get("images") or []

    cached_variants = {str(variant.id): variant for variant in cached_product.variants or []}
    live_variants = {str(variant.id): variant for variant in live_product.variants or []}
    merged_variants: List[StandardProductVariant] = []
    seen_variant_ids: set[str] = set()

    for variant in cached_product.variants or []:
        live_variant = live_variants.get(str(variant.id))
        if live_variant is None:
            merged_variants.append(variant)
        else:
            merged_variants.append(_merge_variant_with_live_overlay(variant, live_variant))
            seen_variant_ids.add(str(variant.id))

    for variant in live_product.variants or []:
        if str(variant.id) not in seen_variant_ids and str(variant.id) not in cached_variants:
            merged_variants.append(variant)

    merged["variants"] = merged_variants
    return StandardProduct(**merged)


def _merge_cached_products_with_live_overlay(
    cached_products: List[StandardProduct],
    live_products: List[StandardProduct],
) -> List[StandardProduct]:
    live_by_id = {str(product.id): product for product in live_products}
    merged_products: List[StandardProduct] = []
    seen_product_ids: set[str] = set()

    for product in cached_products:
        live_product = live_by_id.get(str(product.id))
        if live_product is None:
            merged_products.append(product)
        else:
            merged_products.append(_merge_product_with_live_overlay(product, live_product))
            seen_product_ids.add(str(product.id))

    for product in live_products:
        if str(product.id) not in seen_product_ids and not any(str(existing.id) == str(product.id) for existing in cached_products):
            merged_products.append(product)

    return merged_products


class ShopifyLiveMerchantSource:
    async def load(self, merchant_id: str) -> MerchantSourceDataset:
        overall_started = datetime.now(timezone.utc)
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
        reference_time = datetime.now(timezone.utc)

        shop_domain = str(shopify_cfg.get("shop_domain") or (store or {}).get("domain") or "").strip()
        access_token = str(shopify_cfg.get("access_token") or "").strip()
        shopify_connected = bool(shop_domain and access_token)
        if not shopify_connected:
            merchant_blockers.append("shopify_configuration_missing")

        # Does this merchant still have a storefront attached at all?
        #
        # `shopify_connected` cannot answer that. `_get_shopify_config_for_merchant`
        # resolves credentials in three steps, and the FIRST is the merchant's own
        # encrypted `connector_credentials` row — which NO detach path deletes
        # (not the portal DELETE, not the app/uninstalled webhook, not the
        # auto-disconnect job). So a merchant keeps returning a decryptable
        # shop_domain + access_token long after their last store is gone, and
        # `shopify_connected` stays True. Its third step also falls back to the
        # GLOBAL settings/env Shopify credentials, which would have the same
        # effect wherever those are configured.
        #
        # `get_primary_store` is the structural signal, but `is not None` is NOT
        # the right test on it. Its merchant_stores leg does filter
        # `status IN ('active','connected')`; its LEGACY leg
        # (`get_merchant_active_stores`, services/merchant_store_service.py)
        # does not — it appends a store dict whenever
        # `merchant_onboarding.mcp_platform` is truthy and merely LABELS it
        # `status='disconnected'` when `mcp_connected` is false.
        #
        # That distinction decides which detach paths this gate covers.
        # `DELETE /merchant/integrations/store/{store_id}` — the portal's detach
        # button — clears `mcp_platform = NULL` when the last store goes, so the
        # legacy leg goes quiet and `is not None` would have sufficed. But the
        # Shopify `app/uninstalled` webhook and the hourly auto-disconnect job
        # both set `merchant_stores.status='disconnected'` and touch NO `mcp_*`
        # column, leaving `mcp_platform` set forever. Those merchants would come
        # back from `get_primary_store` as a non-None, 'disconnected' legacy
        # store and sail straight through an `is not None` gate — the exact bug
        # this function is here to fix, on the two paths a merchant is most
        # likely to hit. So: require the store to actually be ACTIVE.
        #
        # This matters because the product cache is keyed on merchant_id +
        # platform ALONE — no store_id — and readiness reads it with
        # include_expired=True (`_load_runtime_cache_rows`). So every product a
        # store ever synced stays readable forever after that store is detached:
        # DELETE /merchant/integrations/store/{store_id} drops the merchant_stores
        # row and nothing retires the cached rows. Readiness kept enumerating
        # them and minting per-variant blockers (out_of_stock, inventory_stale,
        # ...) for a store that no longer exists, which is what the merchant
        # portal rendered as "historical issues" on the overview card and the
        # product-optimization workspace.
        #
        # Detaching a store must retire its catalog from this report, not merely
        # add one more merchant-level blocker alongside a full issue list.
        store_status = str((store or {}).get("status") or "").strip().lower()
        store_connected = store is not None and store_status in _ATTACHED_STORE_STATUSES
        # An attached store AND usable credentials — the precondition for any
        # capability that actually talks to the storefront.
        storefront_live = store_connected and shopify_connected
        if not store_connected:
            merchant_blockers.append("store_connection_missing")
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
        if store_connected:
            try:
                cached_rows = await _load_runtime_cache_rows(merchant_id)
            except Exception:
                merchant_warnings.append("products_cache_lookup_failed")
                logger.warning("products_cache lookup failed for merchant=%s", merchant_id, exc_info=True)
        else:
            audit_notes.append(
                "No storefront is connected, so this report does not read the product cache. "
                "Cached products from a detached store are not assessed."
            )

        products: List[StandardProduct] = []
        product_diagnostics: Dict[str, Dict[str, Any]] = {}
        variant_diagnostics: Dict[str, Dict[str, Any]] = {}
        used_live_overlay = False
        live_products: List[StandardProduct] = []

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
                        "catalog": {"source": source, "fallback_source": "shopify_admin.products.v2025-10"},
                        "price": {"source": "shopify_cache.variant_offer.v1", "fallback_source": "shopify_admin.products.v2025-10"},
                        "inventory": {"source": "shopify_cache.inventory.v1", "fallback_source": "shopify_admin.inventory.v2025-10"},
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
                            "catalog": {"source": source, "fallback_source": "shopify_admin.products.v2025-10"},
                            "price": {"source": "shopify_cache.variant_offer.v1", "fallback_source": "shopify_admin.products.v2025-10"},
                            "inventory": {"source": "shopify_cache.inventory.v1", "fallback_source": "shopify_admin.inventory.v2025-10"},
                        },
                    }

        # `store_connected` gates the overlay too: without it a merchant with no
        # stores would still hit the live Admin API on the global env credentials.
        live_overlay_needed = bool(
            store_connected
            and shopify_connected
            and (not products or _cached_rows_need_live_overlay(cached_rows, reference_time))
        )
        live_fetch_error: Optional[str] = None
        live_overlay_started_at: Optional[datetime] = None
        live_overlay_elapsed_ms: Optional[float] = None
        if live_overlay_needed:
            live_overlay_started_at = datetime.now(timezone.utc)
            live_products, live_error = await _fetch_live_products(merchant_id, shop_domain, access_token)
            live_overlay_elapsed_ms = round(
                (datetime.now(timezone.utc) - live_overlay_started_at).total_seconds() * 1000.0,
                2,
            )
            if live_products:
                used_live_overlay = True
                now_iso = _iso(reference_time)
                if products:
                    products = _merge_cached_products_with_live_overlay(products, live_products)
                    audit_notes.append("Fresh price/inventory overlay pulled from live Shopify Admin API because cached offer/inventory snapshots were stale.")
                else:
                    products = live_products
                    audit_notes.append("products_cache was empty; readiness report fell back to live Shopify Admin API.")

                live_product_ids = {str(product.id) for product in live_products}
                for product in products:
                    if str(product.id) not in live_product_ids:
                        continue
                    if product.id not in product_diagnostics:
                        product_diagnostics[product.id] = {
                            "catalog_last_refreshed_at": now_iso,
                            "media_last_refreshed_at": now_iso,
                            "price_last_refreshed_at": now_iso,
                            "inventory_last_refreshed_at": now_iso,
                            "field_sources": {
                                "catalog": {"source": "shopify_admin.products.v2025-10"},
                                "price": {"source": "shopify_admin.products.v2025-10"},
                                "inventory": {"source": "shopify_admin.inventory.v2025-10", "fallback_source": "shopify_cache.inventory.v1"},
                            },
                        }
                    else:
                        diag = product_diagnostics[product.id]
                        diag["price_last_refreshed_at"] = now_iso
                        diag["inventory_last_refreshed_at"] = now_iso
                        field_sources = diag.setdefault("field_sources", {})
                        field_sources["price"] = {"source": "shopify_admin.products.v2025-10", "fallback_source": "shopify_cache.variant_offer.v1"}
                        field_sources["inventory"] = {"source": "shopify_admin.inventory.v2025-10", "fallback_source": "shopify_cache.inventory.v1"}

                    for variant in product.variants or []:
                        if variant.id not in variant_diagnostics:
                            variant_diagnostics[variant.id] = {
                                "catalog_last_refreshed_at": now_iso,
                                "media_last_refreshed_at": now_iso,
                                "price_last_refreshed_at": now_iso,
                                "inventory_last_refreshed_at": now_iso,
                                "shipping_profile": policy.get("shipping_profile") or "alpha_default",
                                "field_sources": {
                                    "catalog": {"source": "shopify_admin.products.v2025-10"},
                                    "price": {"source": "shopify_admin.products.v2025-10"},
                                    "inventory": {"source": "shopify_admin.inventory.v2025-10", "fallback_source": "shopify_cache.inventory.v1"},
                                },
                            }
                        else:
                            diag = variant_diagnostics[variant.id]
                            diag["price_last_refreshed_at"] = now_iso
                            diag["inventory_last_refreshed_at"] = now_iso
                            diag.setdefault("shipping_profile", policy.get("shipping_profile") or "alpha_default")
                            field_sources = diag.setdefault("field_sources", {})
                            field_sources["price"] = {"source": "shopify_admin.products.v2025-10", "fallback_source": "shopify_cache.variant_offer.v1"}
                            field_sources["inventory"] = {"source": "shopify_admin.inventory.v2025-10", "fallback_source": "shopify_cache.inventory.v1"}
            if live_error:
                live_fetch_error = live_error
        if live_fetch_error:
            if not products:
                merchant_blockers.append("catalog_unavailable")
                merchant_warnings.append("live_shopify_fetch_failed")
            else:
                merchant_warnings.append("live_shopify_overlay_failed")
            audit_notes.append(f"Live Shopify catalog fetch failed: {live_fetch_error}")

        # "catalog_missing" means "you have a store but we cannot see its
        # products" — an actionable sync problem. A merchant with no store at all
        # already carries `store_connection_missing`; adding `catalog_missing`
        # there would point them at a sync they have no store to run.
        if not products and store_connected:
            merchant_blockers.append("catalog_missing")

        product_review_summaries: Dict[str, Dict[str, Any]] = {}
        variant_review_summaries: Dict[str, Dict[str, Any]] = {}
        review_diagnostics: Dict[str, Any] = {
            "integration_status": "blocked",
            "observed_at": None,
            "products_with_reviews": 0,
            "grouped_products_with_reviews": 0,
            "products_without_reviews": len(products),
        }
        review_warnings: List[str] = []
        review_audit_notes: List[str] = []
        review_started_at = datetime.now(timezone.utc)
        if products:
            product_review_summaries, review_diagnostics, review_warnings, review_audit_notes = await load_product_review_summaries(
                merchant_id=merchant_id,
                platform="shopify",
                products=products,
            )
            for product in products:
                summary = product_review_summaries.get(product.id)
                if not summary:
                    continue
                for variant in product.variants or []:
                    variant_review_summaries[variant.id] = dict(summary)
        merchant_warnings.extend(review_warnings)
        audit_notes.extend(review_audit_notes)

        if not psp_config:
            merchant_warnings.append("active_psp_configuration_missing")

        payment_capabilities = {
            # `store_connected and shopify_connected`, not credentials alone:
            # surviving connector_credentials keep `shopify_connected` True after
            # a detach, and a merchant with no storefront cannot execute checkout
            # or write orders back to one. Leaving these on credentials alone is
            # what made a detached merchant's card read "ready" beside a summary
            # saying "blocked".
            "merchant_native_checkout_supported": bool(storefront_live and psp_config),
            "merchant_platform_writeback_supported": bool(storefront_live),
            "ucp_checkout_supported": bool(storefront_live and psp_config),
            "acp_checkout_supported": bool(storefront_live and psp_config),
            "payment_mode": "merchant_psp" if psp_config else "blocked",
            "psp_provider": (psp_config or {}).get("provider"),
            "psp_id": (psp_config or {}).get("psp_id"),
        }

        capability_status = {
            "merchant_adapter": "ready" if storefront_live else "blocked",
            "channel_export": "ready" if products else "blocked",
            "checkout": "ready" if payment_capabilities["merchant_native_checkout_supported"] else "blocked",
            "order_sync": "ready" if storefront_live else "blocked",
            "reviews_confidence": "ready"
            if review_diagnostics.get("integration_status") == "ready" and review_diagnostics.get("products_with_reviews")
            else "partial"
            if review_diagnostics.get("integration_status") == "ready"
            else "blocked",
        }

        source_of_truth = {
            "catalog": "shopify_cache.standard_product.v1" if cached_rows else "shopify_admin.products.v2025-10",
            "price": "shopify_admin.products.v2025-10" if used_live_overlay else ("shopify_cache.variant_offer.v1" if cached_rows else "shopify_admin.products.v2025-10"),
            "inventory": "shopify_admin.inventory.v2025-10" if used_live_overlay else ("shopify_cache.inventory.v1" if cached_rows else "shopify_admin.inventory.v2025-10"),
            "fulfillment_policy": str(policy.get("policy_source") or "readiness.alpha_policy_config.v1"),
            "checkout_capability": "readiness.checkout_capability.v1",
            "order_status": "readiness.order_sync.v2",
            "reviews_confidence": "reviews_center.review_group.v1",
        }

        evaluation_reference_time = _iso(reference_time) or "2026-03-17T00:00:00Z"
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
            ]
        )

        logger.info(
            "shopify_live_readiness_dataset merchant=%s product_count=%s cached_rows=%s live_overlay_needed=%s live_overlay_used=%s live_overlay_ms=%s review_summary_ms=%.2f total_ms=%.2f",
            merchant_id,
            len(products),
            len(cached_rows),
            live_overlay_needed,
            used_live_overlay,
            live_overlay_elapsed_ms,
            round((datetime.now(timezone.utc) - review_started_at).total_seconds() * 1000.0, 2),
            round((datetime.now(timezone.utc) - overall_started).total_seconds() * 1000.0, 2),
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
            product_review_summaries=product_review_summaries,
            variant_review_summaries=variant_review_summaries,
            review_diagnostics=review_diagnostics,
            products=products,
            product_diagnostics=product_diagnostics,
            variant_diagnostics=variant_diagnostics,
            audit_notes=audit_notes,
        )


async def load_shopify_live_merchant_dataset(merchant_id: str) -> MerchantSourceDataset:
    return await ShopifyLiveMerchantSource().load(merchant_id)
