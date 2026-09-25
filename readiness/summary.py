from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import logging
import re
import time
from typing import Any, Dict, Iterable, Optional
from urllib.parse import quote, urlparse

from sqlalchemy import func, select

from db.catalog import catalog_field_facts
from db.database import database
from db.product_enrichment import get_enrichments_for_products
from db.product_quality_backfill_jobs import get_active_quality_backfill_job
from db.products import products_cache
from db.readiness_source_data_decisions import list_source_data_decisions_by_reason_codes
from models.standard_product import StandardProduct
from readiness.flags import (
    readiness_alpha_merchant_id,
    readiness_real_merchant_alpha_enabled,
    readiness_router_enabled,
)
from readiness.models import (
    AgentPushSummary,
    MerchantReadinessAction,
    MerchantReadinessOptimizationPayload,
    MerchantReadinessSnapshot,
    OptimizationPlan,
    ProductQueueAppliedFilters,
    ProductQueuePage,
    ProductQueueSegmentCounts,
    ProductQueueIssue,
    ProductReadinessQueueItem,
    QualityCoverageSummary,
    ReadinessIssueBucket,
    ReadinessLaneDelta,
    ReadinessSummary,
    ScoreBundle,
    SourceDataLaneDecisionCount,
    SourceDataLaneNextProduct,
    SourceDataLaneStateCount,
    SourceDataLaneSummary,
)
from readiness.service import (
    UnsupportedMerchantError,
    build_readiness_snapshot,
    invalidate_readiness_snapshot_cache,
)
from services.catalog_sync_service import make_catalog_product_key
from services.merchant_store_service import get_merchant_active_stores, get_primary_store
from services.product_enrichment_ai import generate_title_suggestion
from services.product_exposure_service import (
    AGENT_PUSH_STATUS_EXCLUDED,
    build_agent_push_projection_from_ready_product,
    summarize_agent_push_projections,
)
from services.product_quality_service import (
    QUALITY_SOURCE_NONE,
    build_quality_payload_from_cache_row,
    build_quality_projection,
    fetch_latest_quality_rows,
    make_product_key,
    summarize_quality_coverage,
)
from utils.availability_vocabulary import is_out_of_stock

logger = logging.getLogger(__name__)

_WORKSPACE_VERSION = "agent_commerce_optimization.v1"
_PRIORITY_POLICY_VERSION = "merchant_readiness_priority.v1"
_PLAN_TTL_HOURS = 6
_OPTIMIZATION_CACHE_TTL_SECONDS = 300.0
_DEFAULT_QUEUE_PAGE_SIZE = 50
_MAX_QUEUE_PAGE_SIZE = 100


_OPTIMIZATION_CACHE: dict[
    str,
    tuple[
        float,
        MerchantReadinessOptimizationPayload,
        Optional[MerchantReadinessSnapshot],
        dict[str, Any],
    ],
] = {}
_OPTIMIZATION_REFRESH_TASKS: dict[str, asyncio.Task[None]] = {}
_OPTIMIZATION_CACHE_METRICS: dict[str, int] = {
    "hits": 0,
    "misses": 0,
    "stores": 0,
    "expired": 0,
    "stale_served": 0,
    "refreshes": 0,
    "background_refreshes": 0,
    "background_refresh_successes": 0,
    "background_refresh_failures": 0,
    "warmup_scheduled": 0,
    "coalesced_waits": 0,
    "invalidations": 0,
    "invalidated_entries": 0,
}


_BUCKET_DEFINITIONS: dict[str, dict[str, str]] = {
    "catalog_content": {
        "label": "Catalog content",
        "fix_surface": "product_content",
        "scope": "product",
        "impact": "discovery_only",
        "direct_target": "/dashboard/product-optimization?focus=catalog_content",
    },
    "price_currency": {
        "label": "Price / currency",
        "fix_surface": "catalog_data",
        "scope": "product",
        "impact": "full_agent_commerce",
        "direct_target": "/dashboard/product-optimization?focus=price_currency",
    },
    "inventory_availability": {
        "label": "Inventory / availability",
        "fix_surface": "catalog_data",
        "scope": "product",
        "impact": "checkout",
        "direct_target": "/dashboard/product-optimization?focus=inventory_availability",
    },
    "shipping_returns_setup": {
        "label": "Shipping / returns setup",
        "fix_surface": "policy",
        "scope": "merchant",
        "impact": "full_agent_commerce",
        "direct_target": "/dashboard/product-optimization?focus=shipping_delivery_completeness",
    },
    "trust_support_setup": {
        "label": "Trust / support setup",
        "fix_surface": "policy",
        "scope": "merchant",
        "impact": "discovery_only",
        "direct_target": "/dashboard/product-optimization?focus=trust_support_policy_completeness",
    },
    "checkout_payment_setup": {
        "label": "Checkout / payment setup",
        "fix_surface": "integrations",
        "scope": "merchant",
        "impact": "full_agent_commerce",
        "direct_target": "/dashboard/integrations",
    },
    "reviews_trust": {
        "label": "Reviews / trust signals",
        "fix_surface": "pivota_managed",
        "scope": "merchant",
        "impact": "discovery_only",
        "direct_target": "/dashboard/product-optimization?focus=reviews_trust",
    },
    "order_sync_operations": {
        "label": "Order / sync operations",
        "fix_surface": "integrations",
        "scope": "merchant",
        "impact": "full_agent_commerce",
        "direct_target": "/dashboard/integrations",
    },
    "other": {
        "label": "Other readiness issues",
        "fix_surface": "pivota_managed",
        "scope": "merchant",
        "impact": "full_agent_commerce",
        "direct_target": "/dashboard/product-optimization",
    },
}

_CODE_TO_BUCKET = {
    "missing_title": "catalog_content",
    "missing_primary_image": "catalog_content",
    "missing_description": "catalog_content",
    "missing_price": "price_currency",
    "missing_currency": "price_currency",
    "out_of_stock": "inventory_availability",
    "inventory_stale": "inventory_availability",
    "missing_shipping_profile": "shipping_returns_setup",
    "merchant_shipping_policy_missing": "shipping_returns_setup",
    "merchant_return_policy_missing": "shipping_returns_setup",
    "merchant_shipping_destinations_missing": "shipping_returns_setup",
    "merchant_shipping_sla_missing": "shipping_returns_setup",
    "merchant_delivery_costs_missing": "shipping_returns_setup",
    "merchant_return_window_missing": "shipping_returns_setup",
    "merchant_warranty_policy_missing": "trust_support_setup",
    "merchant_authenticity_guarantee_missing": "trust_support_setup",
    "merchant_customer_support_contact_missing": "trust_support_setup",
    "merchant_checkout_capability_missing": "checkout_payment_setup",
    "store_connection_missing": "checkout_payment_setup",
    "shopify_configuration_missing": "checkout_payment_setup",
    "checkout_stub_missing": "checkout_payment_setup",
    "payment_execution_stubbed": "checkout_payment_setup",
    "reviews_summary_unavailable": "reviews_trust",
    "cross_merchant_review_group_unresolved": "reviews_trust",
    "review_coverage_partial": "reviews_trust",
    "no_reviews_available": "reviews_trust",
    "merchant_writeback_unavailable": "order_sync_operations",
    "order_sync_stubbed": "order_sync_operations",
    "merchant_not_assessed_for_readiness_alpha": "other",
    "readiness_assessment_disabled": "other",
    "readiness_summary_unavailable": "other",
}

_EXECUTABLE_PRODUCT_CONTENT_REASON_CODES = {
    "missing_title",
    "missing_description",
    "generic_low_information_title",
    "missing_audience_signal",
    "missing_color_signal",
    "missing_feature_signal",
    "missing_size_guidance",
    "missing_material_or_ingredient_info",
    "missing_usage_scenario",
}

_CATALOG_REVIEW_REASON_CODES = {
    "missing_primary_image",
    "missing_price",
    "missing_currency",
    "out_of_stock",
    "inventory_stale",
}

_SOURCE_DATA_LANE_DEFS: dict[str, dict[str, str]] = {
    "missing_price": {
        "label": "Missing price",
        "scope": "variant",
    },
    "out_of_stock": {
        "label": "Out of stock",
        "scope": "variant",
    },
    "missing_primary_image": {
        "label": "Missing primary image",
        "scope": "product",
    },
    "shipping_delivery_completeness": {
        "label": "Shipping / delivery completeness",
        "scope": "product",
    },
    "trust_support_policy_completeness": {
        "label": "Trust / support policy completeness",
        "scope": "product",
    },
    "product_fit_composition_completeness": {
        "label": "Product fit / composition completeness",
        "scope": "product",
    },
}

_SOURCE_DATA_LANE_STATE_LABELS: dict[str, list[tuple[str, str]]] = {
    "missing_price": [
        ("whole_product_missing_price", "Whole product still missing price"),
        ("partially_priced", "Partially priced"),
        ("priced_waiting_refresh", "Price visible now"),
    ],
    "out_of_stock": [
        ("whole_product_unavailable", "Whole product unavailable"),
        ("partially_recovered", "Partially back in stock"),
        ("restocked_waiting_refresh", "Back in stock now"),
    ],
    "missing_primary_image": [
        ("hero_image_missing", "Hero image still missing"),
        ("image_visible_now", "Primary image visible now"),
    ],
    "shipping_delivery_completeness": [
        ("policy_facts_missing", "Shipping facts still missing"),
        ("policy_facts_partial", "Some shipping facts visible"),
        ("policy_facts_visible_now", "Shipping facts visible now"),
    ],
    "trust_support_policy_completeness": [
        ("trust_facts_missing", "Trust facts still missing"),
        ("trust_facts_partial", "Some trust facts visible"),
        ("trust_facts_visible_now", "Trust facts visible now"),
    ],
    "product_fit_composition_completeness": [
        ("product_guidance_missing", "Product guidance still missing"),
        ("product_guidance_partial", "Some product guidance visible"),
        ("product_guidance_visible_now", "Product guidance visible now"),
    ],
}

_OUT_OF_STOCK_DECISION_LABELS: dict[str, str] = {
    "restock_planned": "Restock planned",
    "archive_planned": "Archive / discontinue",
    "manual_review": "Manual review",
}

_SOURCE_DATA_DECISION_LABELS: dict[str, dict[str, str]] = {
    "out_of_stock": _OUT_OF_STOCK_DECISION_LABELS,
    "missing_price": {
        "pricing_fix_saved": "Saved for pricing fix",
    },
    "missing_primary_image": {
        "image_fix_saved": "Saved for image repair",
    },
    "shipping_delivery_completeness": {
        "pending_review": "Pending review",
        "merchant_fix_in_progress": "Fix in progress",
        "waiting_on_platform_or_policy": "Waiting on platform or policy",
        "not_applicable": "Not applicable",
    },
    "trust_support_policy_completeness": {
        "pending_review": "Pending review",
        "merchant_fix_in_progress": "Fix in progress",
        "waiting_on_platform_or_policy": "Waiting on platform or policy",
        "not_applicable": "Not applicable",
    },
    "product_fit_composition_completeness": {
        "pending_review": "Pending review",
        "merchant_fix_in_progress": "Fix in progress",
        "waiting_on_platform_or_policy": "Waiting on platform or policy",
        "not_applicable": "Not applicable",
    },
}

_CATALOG_HEALTH_LANE_REASON_CODES: dict[str, list[str]] = {
    "shipping_delivery_completeness": [
        "merchant_shipping_destinations_missing",
        "merchant_shipping_sla_missing",
        "merchant_delivery_costs_missing",
        "merchant_return_window_missing",
    ],
    "trust_support_policy_completeness": [
        "merchant_warranty_policy_missing",
        "merchant_authenticity_guarantee_missing",
        "merchant_customer_support_contact_missing",
    ],
    "product_fit_composition_completeness": [
        "product_size_guidance_missing",
        "product_material_or_ingredient_info_missing",
        "product_usage_scenario_missing",
    ],
}


def _dedupe_codes(*groups: Iterable[str]) -> list[str]:
    codes: list[str] = []
    for group in groups:
        for code in group:
            normalized = str(code or "").strip()
            if not normalized or normalized in codes:
                continue
            codes.append(normalized)
    return codes


def _coerce_price_value(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, dict):
        return _coerce_price_value(value.get("value") if "value" in value else value.get("amount"))
    try:
        return float(value)
    except Exception:
        return None


def _coerce_inventory_quantity(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _normalized_lower(value: Any) -> str:
    return _normalize_text(value).lower()


def _dedupe_strings(values: Iterable[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _normalize_text(value)
        if not normalized:
            continue
        lowered = normalized.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        deduped.append(normalized)
    return deduped


def _coerce_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item or "").strip() for item in value if str(item or "").strip()]
    if isinstance(value, tuple):
        return [str(item or "").strip() for item in value if str(item or "").strip()]
    if isinstance(value, str):
        normalized = _normalize_text(value)
        return [normalized] if normalized else []
    return []


def _value_present(value: Any) -> bool:
    if value in (None, "", [], {}):
        return False
    if isinstance(value, str):
        return bool(_normalize_text(value))
    if isinstance(value, (list, tuple, set)):
        return any(_value_present(item) for item in value)
    if isinstance(value, dict):
        return any(_value_present(item) for item in value.values())
    return True


def _find_nested_value(payload: Any, *key_candidates: str) -> Any:
    normalized_candidates = {str(key or "").strip().lower() for key in key_candidates if str(key or "").strip()}
    if not normalized_candidates:
        return None

    stack = [payload]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        current_id = id(current)
        if current_id in seen:
            continue
        seen.add(current_id)
        if isinstance(current, dict):
            for key, value in current.items():
                if str(key or "").strip().lower() in normalized_candidates and _value_present(value):
                    return value
                if isinstance(value, (dict, list, tuple)):
                    stack.append(value)
        elif isinstance(current, (list, tuple)):
            for value in current:
                if isinstance(value, (dict, list, tuple)):
                    stack.append(value)
    return None


def _current_product_to_standard_product(
    current_product: dict[str, Any],
    *,
    merchant_id: str,
    platform: str,
    product_id: str,
) -> Optional[StandardProduct]:
    payload = dict(current_product or {})
    if not payload:
        return None
    payload.setdefault("id", payload.get("product_id") or product_id)
    payload.setdefault("product_id", payload.get("id") or product_id)
    payload.setdefault("merchant_id", merchant_id)
    payload.setdefault("platform", platform)
    payload.setdefault("title", payload.get("title") or "")
    payload["price"] = _coerce_price_value(payload.get("price")) or 0.0
    payload["compare_at_price"] = _coerce_price_value(payload.get("compare_at_price"))
    payload["inventory_quantity"] = _coerce_inventory_quantity(payload.get("inventory_quantity")) or 0
    try:
        return StandardProduct.model_validate(payload)
    except Exception:
        return None


def _snapshot_product_to_current_product(snapshot_product: Any, *, merchant_id: str) -> dict[str, Any]:
    variants: list[dict[str, Any]] = []
    for variant in snapshot_product.variants or []:
        variants.append(
            {
                "id": variant.variant_id,
                "variant_id": variant.variant_id,
                "title": variant.title,
                "sku": variant.sku,
                "price": _coerce_price_value((variant.price or {}).get("amount")),
                "currency": str((variant.price or {}).get("currency") or "").strip() or "USD",
                "inventory_quantity": _coerce_inventory_quantity((variant.inventory or {}).get("quantity")) or 0,
                "image_url": (variant.price or {}).get("image_url"),
                "visible_option_labels": list(variant.visible_option_labels or []),
                "platform_metadata": {},
            }
        )

    first_variant = variants[0] if variants else {}
    return {
        "id": snapshot_product.product_id,
        "product_id": snapshot_product.product_id,
        "merchant_id": merchant_id,
        "platform": snapshot_product.platform or "unknown",
        "title": snapshot_product.title,
        "description": snapshot_product.description,
        "description_text": snapshot_product.description,
        "vendor": snapshot_product.brand,
        "product_type": snapshot_product.category,
        "visible_attributes": snapshot_product.visible_attributes,
        "ingredient_ids": list(snapshot_product.ingredient_ids or []),
        "price": first_variant.get("price") or 0.0,
        "currency": first_variant.get("currency") or "USD",
        "inventory_quantity": first_variant.get("inventory_quantity") or 0,
        "image_url": snapshot_product.default_image_url,
        "variants": variants,
        "platform_metadata": {},
    }


def _catalog_health_language(store_context: dict[str, Any], current_product: dict[str, Any]) -> str:
    locale = (
        _find_nested_value(current_product, "locale", "language", "shop_locale")
        or _find_nested_value(store_context, "locale", "language", "portal_language", "shop_locale")
    )
    locale_norm = _normalized_lower(locale)
    if locale_norm:
        return locale_norm.split("-")[0]

    country = _normalized_lower(_find_nested_value(store_context, "country", "country_code"))
    if country in {"cn", "zh"}:
        return "zh"
    if country == "jp":
        return "ja"
    if country == "kr":
        return "ko"
    if country in {"fr", "de", "es", "pt", "it"}:
        return country
    return "en"


def _description_text(current_product: dict[str, Any], parsed_product: Optional[StandardProduct]) -> str:
    if parsed_product and parsed_product.description_text:
        return parsed_product.description_text
    return _normalize_text(
        current_product.get("description_text")
        or current_product.get("description")
        or ""
    )


def _product_family(parsed_product: Optional[StandardProduct], current_product: dict[str, Any]) -> str:
    blob = " ".join(
        [
            _normalize_text(getattr(parsed_product, "product_type", None) if parsed_product else ""),
            _normalize_text(getattr(parsed_product, "title", None) if parsed_product else current_product.get("title")),
            _normalize_text(" ".join(getattr(parsed_product, "tags", []) or []) if parsed_product else ""),
            _normalize_text(current_product.get("product_type")),
        ]
    ).lower()
    if any(term in blob for term in ("serum", "cream", "cleanser", "moisturizer", "skincare", "lipstick", "foundation", "spf", "mask")):
        return "beauty"
    if any(term in blob for term in ("shoe", "sneaker", "boot", "shirt", "dress", "jacket", "hoodie", "pant", "sock", "apparel", "footwear")):
        return "apparel_footwear"
    return "generic"


def _catalog_health_field_fact_values(field_facts: dict[str, dict[str, Any]], family: str, key: str) -> list[str]:
    family_payload = field_facts.get(family, {}) if isinstance(field_facts, dict) else {}
    return _coerce_string_list(family_payload.get(key))


def _has_size_guidance(parsed_product: Optional[StandardProduct], current_product: dict[str, Any]) -> bool:
    if _value_present(_find_nested_value(current_product, "size_guide", "size_chart", "sizing", "fit_notes", "measurements")):
        return True
    if parsed_product:
        for variant in parsed_product.variants or []:
            if any("size" in _normalized_lower(name) for name in ((variant.options or {}).keys() if isinstance(variant.options, dict) else [])):
                return True
            if any(str(label or "").lower().startswith("size_") for label in (variant.visible_option_labels or [])):
                return True
    description_blob = _description_text(current_product, parsed_product).lower()
    return any(term in description_blob for term in ("true to size", "size guide", "size chart", "fit note", "sizing advice"))


def _has_material_or_ingredient_info(
    parsed_product: Optional[StandardProduct],
    current_product: dict[str, Any],
    *,
    family: str,
    field_facts: dict[str, dict[str, Any]],
) -> bool:
    if family == "beauty":
        if parsed_product and parsed_product.ingredient_ids:
            return True
        if _catalog_health_field_fact_values(field_facts, "beauty_knowledge", "ingredient_ids"):
            return True
        if _value_present(_find_nested_value(current_product, "ingredient_ids", "reviewed_ingredient_ids", "canonical_ingredient_ids", "active_ingredients")):
            return True
        description_blob = _description_text(current_product, parsed_product).lower()
        return any(term in description_blob for term in ("ingredient", "retinol", "vitamin c", "hyaluronic"))

    if _value_present(_find_nested_value(current_product, "material", "materials", "composition", "fabric", "specifications", "specs")):
        return True
    description_blob = _description_text(current_product, parsed_product).lower()
    return any(term in description_blob for term in ("cotton", "leather", "polyester", "nylon", "wool", "silk", "material"))


def _has_usage_guidance(
    parsed_product: Optional[StandardProduct],
    current_product: dict[str, Any],
    *,
    field_facts: dict[str, dict[str, Any]],
) -> bool:
    if _value_present(_find_nested_value(current_product, "usage", "usage_scenarios", "use_cases", "how_to_use", "ideal_for", "compatible_with")):
        return True
    if _catalog_health_field_fact_values(field_facts, "beauty_knowledge", "how_to_use"):
        return True
    description_blob = _description_text(current_product, parsed_product).lower()
    return any(
        term in description_blob
        for term in ("ideal for", "best for", "suitable for", "designed for", "perfect for", "how to use", "use for")
    )


def _merchant_policy_missing_codes(store_context: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    if not _value_present(_find_nested_value(store_context, "shipping_destinations", "shipping_countries", "supported_countries", "ships_to", "destinations")):
        missing.append("merchant_shipping_destinations_missing")
    if not _value_present(_find_nested_value(store_context, "shipping_sla", "average_shipping_time", "shipping_time", "delivery_time", "delivery_sla")):
        missing.append("merchant_shipping_sla_missing")
    if not _value_present(_find_nested_value(store_context, "free_shipping", "shipping_costs", "duties_included", "taxes_included", "shipping_cost_summary", "delivery_costs")):
        missing.append("merchant_delivery_costs_missing")
    if not _value_present(_find_nested_value(store_context, "return_window", "return_window_days", "returns_window", "return_days")):
        missing.append("merchant_return_window_missing")
    if not _value_present(_find_nested_value(store_context, "warranty_policy", "warranty_summary", "after_sales_policy", "after_sales")):
        missing.append("merchant_warranty_policy_missing")
    if not _value_present(_find_nested_value(store_context, "authenticity_guarantee", "genuine_guarantee", "official_guarantee", "authenticity")):
        missing.append("merchant_authenticity_guarantee_missing")
    if not _value_present(_find_nested_value(store_context, "support_email", "support_phone", "contact_method", "contact_url", "customer_service", "support_contact")):
        missing.append("merchant_customer_support_contact_missing")
    return missing


def _product_completeness_missing_codes(
    parsed_product: Optional[StandardProduct],
    current_product: dict[str, Any],
    *,
    field_facts: dict[str, dict[str, Any]],
) -> list[str]:
    family = _product_family(parsed_product, current_product)
    missing: list[str] = []
    if family == "apparel_footwear" and not _has_size_guidance(parsed_product, current_product):
        missing.append("product_size_guidance_missing")
    if not _has_material_or_ingredient_info(
        parsed_product,
        current_product,
        family=family,
        field_facts=field_facts,
    ):
        missing.append("product_material_or_ingredient_info_missing")
    if not _has_usage_guidance(parsed_product, current_product, field_facts=field_facts):
        missing.append("product_usage_scenario_missing")
    return missing


def _lane_reason_codes_for_product(
    parsed_product: Optional[StandardProduct],
    current_product: dict[str, Any],
    *,
    store_context: dict[str, Any],
    field_facts: dict[str, dict[str, Any]],
) -> dict[str, list[str]]:
    merchant_missing = _merchant_policy_missing_codes(store_context)
    product_missing = _product_completeness_missing_codes(
        parsed_product,
        current_product,
        field_facts=field_facts,
    )
    return {
        "shipping_delivery_completeness": [
            code for code in merchant_missing if code in _CATALOG_HEALTH_LANE_REASON_CODES["shipping_delivery_completeness"]
        ],
        "trust_support_policy_completeness": [
            code for code in merchant_missing if code in _CATALOG_HEALTH_LANE_REASON_CODES["trust_support_policy_completeness"]
        ],
        "product_fit_composition_completeness": [
            code for code in product_missing if code in _CATALOG_HEALTH_LANE_REASON_CODES["product_fit_composition_completeness"]
        ],
    }


def _lane_state_for_reason_codes(lane_code: str, present_reason_codes: list[str]) -> Optional[str]:
    total = len(_CATALOG_HEALTH_LANE_REASON_CODES.get(lane_code, []))
    present = len(present_reason_codes)
    if present <= 0 or total <= 0:
        return None
    if lane_code == "shipping_delivery_completeness":
        return "policy_facts_missing" if present >= total else "policy_facts_partial"
    if lane_code == "trust_support_policy_completeness":
        return "trust_facts_missing" if present >= total else "trust_facts_partial"
    return "product_guidance_missing" if present >= total else "product_guidance_partial"


async def _load_store_context(merchant_id: str) -> dict[str, Any]:
    if not getattr(database, "is_connected", False):
        return {}
    try:
        store = await get_primary_store(merchant_id)
    except Exception:
        logger.warning("Failed to load primary store context for merchant=%s", merchant_id, exc_info=True)
        return {}
    return dict(store or {})


async def _load_catalog_field_facts_for_product_keys(
    merchant_id: str,
    product_keys: list[tuple[str, str]],
) -> dict[tuple[str, str], dict[str, dict[str, Any]]]:
    if not product_keys:
        return {}
    if not getattr(database, "is_connected", False):
        return {}
    entity_ids = {
        make_catalog_product_key(merchant_id, platform, product_id): (platform, product_id)
        for platform, product_id in product_keys
    }
    query = (
        catalog_field_facts.select()
        .where(catalog_field_facts.c.entity_type == "product")
        .where(catalog_field_facts.c.entity_id.in_(list(entity_ids.keys())))
    )
    try:
        rows = await database.fetch_all(query)
    except Exception:
        logger.warning("Failed to load catalog field facts for merchant=%s", merchant_id, exc_info=True)
        return {}

    facts_by_key: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for row in rows or []:
        payload = dict(row)
        product_key = entity_ids.get(str(payload.get("entity_id") or ""))
        if product_key is None:
            continue
        family = str(payload.get("field_family") or "").strip()
        field_key = str(payload.get("field_key") or "").strip()
        if not family or not field_key:
            continue
        facts_by_key.setdefault(product_key, {}).setdefault(family, {})[field_key] = payload.get("value_json")
    return facts_by_key


def _snapshot_variant_agent_push_projection(variant: Any) -> dict[str, Any]:
    blocker_codes = set(
        _dedupe_codes(
            variant.blockers.get("discovery", []),
            variant.blockers.get("checkout", []),
        )
    )
    price_data = variant.price or {}
    inventory_data = variant.inventory or {}
    price_amount = _coerce_price_value(price_data.get("amount"))
    currency = str(price_data.get("currency") or "").strip().upper() or None
    availability = str(inventory_data.get("availability") or "").strip().lower()
    inventory_quantity = _coerce_inventory_quantity(inventory_data.get("quantity"))

    # One shared vocabulary (utils.availability_vocabulary) instead of a local denylist. The
    # denylist omitted the SPACE-separated forms, so a lowercased "out of stock" read as IN
    # STOCK. That is unreachable today — readiness/scoring.py is the only producer of this
    # field and it writes canonical tokens only — so this is behaviour-preserving for every
    # value that actually arrives, and correct if a future producer ever passes raw text
    # through. Unknown stays IN STOCK here, exactly as the denylist treated it.
    in_stock = not is_out_of_stock(availability)
    if inventory_quantity is not None:
        in_stock = in_stock and inventory_quantity > 0

    reason_codes: list[str] = []
    if "out_of_stock" in blocker_codes or not in_stock:
        reason_codes.append("out_of_stock")
    if "missing_price" in blocker_codes or price_amount is None:
        reason_codes.append("missing_price")
    if "missing_currency" in blocker_codes or currency is None:
        reason_codes.append("missing_currency")

    if reason_codes:
        return {
            "agent_push_status": AGENT_PUSH_STATUS_EXCLUDED,
            "agent_push_reason_codes": _dedupe_codes(reason_codes),
        }
    return {
        "agent_push_status": "eligible_for_agent_push",
        "agent_push_reason_codes": [],
    }


def _variant_matches_source_data_reason(
    reason_code: str,
    *,
    readiness_blocker_codes: list[str],
    agent_push_reason_codes: list[str],
) -> bool:
    blocker_codes = set(readiness_blocker_codes)
    push_codes = set(agent_push_reason_codes)

    if reason_code == "missing_price":
        return bool(
            blocker_codes.intersection({"missing_price", "missing_currency"})
            or push_codes.intersection({"missing_price", "missing_currency"})
        )
    if reason_code == "out_of_stock":
        return "out_of_stock" in blocker_codes or "out_of_stock" in push_codes
    return False


def _product_matches_source_data_reason(reason_code: str, *, queue_item: ProductReadinessQueueItem) -> tuple[bool, int]:
    if reason_code != "missing_primary_image":
        return False, 0

    affected_variants = sum(
        int(issue.affected_variant_count or 0)
        for issue in (queue_item.top_issues or [])
        if issue.code == "missing_primary_image"
    )
    if affected_variants <= 0:
        affected_variants = int(queue_item.blocked_variant_count or 0) or int(
            queue_item.excluded_variant_count or 0
        )

    has_issue = any(issue.code == "missing_primary_image" for issue in (queue_item.top_issues or []))
    return has_issue, affected_variants


def _current_product_data(cache_row: Optional[Dict[tuple[str, str], Dict[str, Any]] | Dict[str, Any]]):
    if not cache_row:
        return {}
    if isinstance(cache_row, dict) and "product_data" in cache_row:
        payload = cache_row.get("product_data") or {}
        if isinstance(payload, dict):
            return payload
    if isinstance(cache_row, dict):
        return cache_row
    return {}


def _current_variant_lookup(current_product: Dict[str, Any]) -> dict[str, Dict[str, Any]]:
    lookup: dict[str, Dict[str, Any]] = {}
    for variant in current_product.get("variants") or []:
        variant_id = str(variant.get("variant_id") or variant.get("id") or "").strip()
        if not variant_id or variant_id in lookup:
            continue
        lookup[variant_id] = variant
    return lookup


def _current_variant_price(variant: Optional[Dict[str, Any]]) -> Optional[float]:
    if not variant:
        return None
    return _coerce_price_value(variant.get("price"))


def _current_variant_currency(variant: Optional[Dict[str, Any]], current_product: Dict[str, Any]) -> Optional[str]:
    if variant and isinstance(variant.get("price"), dict):
        currency = str(variant["price"].get("currency") or "").strip().upper()
        if currency:
            return currency
    if variant and str(variant.get("currency") or "").strip():
        return str(variant.get("currency")).strip().upper()
    currency = str(current_product.get("currency") or "").strip().upper()
    return currency or None


def _current_variant_inventory(variant: Optional[Dict[str, Any]]) -> Optional[int]:
    if not variant:
        return None
    return _coerce_inventory_quantity(
        variant.get("inventory_quantity", variant.get("stock", variant.get("inventory")))
    )


def _current_product_has_visible_image(current_product: Dict[str, Any]) -> bool:
    if current_product.get("image_url"):
        return True
    images = current_product.get("images") or []
    return bool(images and images[0])


def _is_current_product_sellable(current_product: Dict[str, Any]) -> bool:
    explicit = current_product.get("sellable")
    if isinstance(explicit, bool):
        return explicit

    orderable = current_product.get("orderable")
    if isinstance(orderable, bool):
        if not orderable:
            return False

    raw_status = str(current_product.get("status") or "").strip().lower()
    if raw_status and raw_status != "active":
        return False

    if isinstance(orderable, bool):
        return orderable and raw_status in {"", "active"}
    return raw_status in {"", "active"}


def _default_out_of_stock_decision_state(current_product: Dict[str, Any]) -> str:
    raw_status = str(current_product.get("status") or "").strip().lower()
    if raw_status and raw_status != "active":
        return "archive_planned"

    if current_product.get("orderable") is False:
        return "archive_planned"

    variants = current_product.get("variants") or []
    has_any_priced_variant = any((_current_variant_price(variant) or 0) > 0 for variant in variants)
    if not has_any_priced_variant:
        has_any_priced_variant = (_coerce_price_value(current_product.get("price")) or 0) > 0

    if _is_current_product_sellable(current_product) and has_any_priced_variant and (
        _current_product_has_visible_image(current_product)
        or bool(str(current_product.get("description") or "").strip())
    ):
        return "restock_planned"

    return "manual_review"


def _build_source_data_variant_matches(
    reason_code: str,
    *,
    snapshot_product: Any,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for variant in snapshot_product.variants:
        readiness_blocker_codes = _dedupe_codes(
            variant.blockers.get("discovery", []),
            variant.blockers.get("checkout", []),
        )
        agent_push_reason_codes = list(
            _snapshot_variant_agent_push_projection(variant).get("agent_push_reason_codes") or []
        )
        if not _variant_matches_source_data_reason(
            reason_code,
            readiness_blocker_codes=readiness_blocker_codes,
            agent_push_reason_codes=agent_push_reason_codes,
        ):
            continue
        matches.append(
            {
                "variant_id": str(variant.variant_id or "").strip(),
                "title": str(variant.title or variant.variant_id or "Variant"),
            }
        )
    return matches


def _missing_price_batch_state(
    matches: list[dict[str, Any]],
    current_product: Dict[str, Any],
) -> str:
    if not matches:
        return "whole_product_missing_price"
    variant_lookup = _current_variant_lookup(current_product)
    pending = 0
    resolved = 0
    for match in matches:
        current_variant = variant_lookup.get(match["variant_id"])
        price_value = _current_variant_price(current_variant)
        price_currency = _current_variant_currency(current_variant, current_product)
        if (price_value or 0) > 0 and price_currency:
            resolved += 1
        else:
            pending += 1
    if pending <= 0:
        return "priced_waiting_refresh"
    if resolved > 0:
        return "partially_priced"
    return "whole_product_missing_price"


def _out_of_stock_batch_state(
    matches: list[dict[str, Any]],
    current_product: Dict[str, Any],
) -> str:
    if not matches:
        return "whole_product_unavailable"
    variant_lookup = _current_variant_lookup(current_product)
    pending = 0
    resolved = 0
    for match in matches:
        current_variant = variant_lookup.get(match["variant_id"])
        inventory_quantity = _current_variant_inventory(current_variant)
        if inventory_quantity is not None and inventory_quantity > 0:
            resolved += 1
        else:
            pending += 1
    if pending <= 0:
        return "restocked_waiting_refresh"
    if resolved > 0:
        return "partially_recovered"
    return "whole_product_unavailable"


def _dedupe(values: Iterable[str], *, limit: int = 3) -> list[str]:
    seen: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.append(normalized)
        if len(seen) >= limit:
            break
    return seen


def _label_for_tier(tier: str) -> str:
    return {
        "green": "Ready",
        "yellow": "Needs Attention",
        "red": "Blocked",
    }.get(tier, "Blocked")


def _humanize_code(code: str) -> str:
    labels = {
        "merchant_not_assessed_for_readiness_alpha": "Merchant not yet assessed",
        "readiness_assessment_disabled": "Readiness assessment disabled",
        "readiness_summary_unavailable": "Readiness summary unavailable",
        "out_of_stock": "Out of stock",
        "missing_price": "Missing price",
        "missing_currency": "Missing currency",
        "inventory_stale": "Inventory data stale",
        "missing_primary_image": "Missing primary image",
        "missing_title": "Missing product title",
        "missing_description": "Missing description",
        "missing_shipping_profile": "Missing shipping setup",
        "merchant_shipping_policy_missing": "Shipping policy missing",
        "merchant_return_policy_missing": "Returns policy missing",
        "merchant_shipping_destinations_missing": "Shipping destinations missing",
        "merchant_shipping_sla_missing": "Shipping timing missing",
        "merchant_delivery_costs_missing": "Shipping / duty details missing",
        "merchant_return_window_missing": "Return window missing",
        "merchant_warranty_policy_missing": "Warranty policy missing",
        "merchant_authenticity_guarantee_missing": "Authenticity guarantee missing",
        "merchant_customer_support_contact_missing": "Customer support contact missing",
        "merchant_checkout_capability_missing": "Checkout not connected",
        "store_connection_missing": "Store not connected",
        "shopify_configuration_missing": "Store credentials missing",
        "merchant_writeback_unavailable": "Order sync unavailable",
        "reviews_summary_unavailable": "Reviews summary unavailable",
        "cross_merchant_review_group_unresolved": "Cross-merchant review grouping incomplete",
        "review_coverage_partial": "Review coverage partial",
        "generic_low_information_title": "Low-information title",
        "missing_category_signal": "Style / product type missing",
        "missing_audience_signal": "Audience / gender missing",
        "missing_color_signal": "Color missing",
        "missing_feature_signal": "Key feature missing",
        "missing_size_guidance": "Size guidance missing",
        "missing_material_or_ingredient_info": "Material / ingredient info missing",
        "missing_usage_scenario": "Usage scenario missing",
        "product_size_guidance_missing": "Size guidance missing",
        "product_material_or_ingredient_info_missing": "Material / ingredient info missing",
        "product_usage_scenario_missing": "Usage scenario missing",
    }
    if code in labels:
        return labels[code]
    return str(code or "").replace("_", " ").strip().capitalize()


def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        normalized = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _format_timestamp(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _build_snapshot_id(snapshot: MerchantReadinessSnapshot) -> str:
    raw = "|".join(
        [
            snapshot.report_version,
            snapshot.merchant_id,
            snapshot.channel,
            snapshot.generated_at,
            snapshot.merchant_alpha_mode,
        ]
    )
    return f"rdsnap_{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]}"


def _build_plan_id(
    snapshot_id: str,
    *,
    summary: ReadinessSummary,
    issue_buckets: list[ReadinessIssueBucket],
    product_queue: list[ProductReadinessQueueItem],
    content_opportunity_count: int = 0,
    source_data_lanes: Optional[list[SourceDataLaneSummary]] = None,
) -> str:
    raw = "|".join(
        [
            snapshot_id,
            _WORKSPACE_VERSION,
            _PRIORITY_POLICY_VERSION,
            summary.tier,
            str(summary.blocked_variant_count),
            ",".join(bucket.code for bucket in issue_buckets[:5]),
            ",".join(item.queue_item_id for item in product_queue[:10]),
            str(content_opportunity_count),
            ",".join(
                f"{lane.reason_code}:{lane.affected_products}:{lane.affected_variants}"
                for lane in (source_data_lanes or [])
            ),
        ]
    )
    return f"rdplan_{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]}"


def _refresh_state(generated_at: Optional[str]) -> str:
    generated_dt = _parse_timestamp(generated_at)
    if generated_dt is None:
        return "unavailable"
    age = datetime.now(timezone.utc) - generated_dt
    if age <= timedelta(hours=_PLAN_TTL_HOURS):
        return "fresh"
    if age <= timedelta(hours=24):
        return "stale"
    return "expired"


def _plan_expiry(generated_at: Optional[str]) -> Optional[str]:
    generated_dt = _parse_timestamp(generated_at)
    if generated_dt is None:
        return None
    return _format_timestamp(generated_dt + timedelta(hours=_PLAN_TTL_HOURS))


def _fixability_for_surface(fix_surface: str) -> str:
    if fix_surface in {"product_content", "catalog_data", "integrations", "policy"}:
        return "merchant_fixable"
    if fix_surface == "pivota_managed":
        return "pivota_managed"
    return "informational"


def _impact_weight(impact: str) -> int:
    return {
        "full_agent_commerce": 100,
        "checkout": 75,
        "discovery_only": 45,
    }.get(impact, 30)


def _bucket_priority_score(bucket_code: str, affected_count: int, *, impact: str, fix_surface: str) -> float:
    severity_weight = {
        "high": 90,
        "medium": 55,
        "low": 25,
    }[_severity_for_bucket(bucket_code, affected_count)]
    fixability_bonus = {
        "merchant_fixable": 12,
        "pivota_managed": 4,
        "informational": 0,
    }[_fixability_for_surface(fix_surface)]
    return float(severity_weight + _impact_weight(impact) + min(affected_count, 50) + fixability_bonus)


def _bucket_priority_reason(bucket: str, *, impact: str, fix_surface: str, scope: str) -> str:
    if fix_surface in {"integrations", "policy"}:
        return "Fixing this setup can unlock merchant-level agent commerce flows."
    if impact == "full_agent_commerce":
        return "Fixing this issue can unlock blocked agent commerce actions."
    if impact == "checkout":
        return "Fixing this issue can improve checkout readiness for blocked variants."
    if scope == "product":
        return "Fixing this issue can improve how agents understand and retrieve products."
    return "This issue should be resolved before broader agent exposure."


def _product_priority_score(
    *,
    blocked_variant_count: int,
    impact: str,
    fix_surface: str,
    affected_variant_count: int,
) -> float:
    fixability_bonus = {
        "merchant_fixable": 10,
        "pivota_managed": 3,
        "informational": 0,
    }[_fixability_for_surface(fix_surface)]
    return float(
        _impact_weight(impact)
        + min(blocked_variant_count * 12, 72)
        + min(affected_variant_count * 5, 40)
        + fixability_bonus
    )


def _product_priority_reason(*, blocked_variant_count: int, impact: str) -> str:
    if blocked_variant_count > 0 and impact == "full_agent_commerce":
        return "Fixing this product can unlock checkout for blocked variants."
    if blocked_variant_count > 0:
        return "Fixing this product can reduce blocked variants in the catalog."
    if impact == "discovery_only":
        return "Improving this product can increase agent understanding and retrieval."
    return "Review this product to improve agent commerce performance."


def _variant_blocker_counts(snapshot: MerchantReadinessSnapshot) -> Counter[str]:
    counts: Counter[str] = Counter()
    for product in snapshot.products:
        for variant in product.variants:
            if variant.channel_coverage.get(snapshot.channel) == "ready":
                continue
            codes = set(variant.blockers.get("discovery", []) + variant.blockers.get("checkout", []))
            for code in codes:
                counts[code] += 1
    return counts


def _bucket_code_for_reason(code: str) -> str:
    return _CODE_TO_BUCKET.get(code, "other")


def _optimization_cache_key(merchant_id: str, channel: str) -> str:
    return f"{merchant_id}|{channel}"


def invalidate_readiness_optimization_cache(
    merchant_id: Optional[str] = None,
    *,
    channel: Optional[str] = None,
) -> int:
    task_keys_to_cancel: list[str] = []
    for key in list(_OPTIMIZATION_REFRESH_TASKS.keys()):
        cached_merchant_id, cached_channel = key.split("|", 1)
        if merchant_id is not None and cached_merchant_id != merchant_id:
            continue
        if channel is not None and cached_channel != channel:
            continue
        task_keys_to_cancel.append(key)

    for key in task_keys_to_cancel:
        task = _OPTIMIZATION_REFRESH_TASKS.pop(key, None)
        if task is not None and not task.done():
            task.cancel()

    if merchant_id is None and channel is None:
        removed = len(_OPTIMIZATION_CACHE)
        _OPTIMIZATION_CACHE.clear()
        if removed:
            _OPTIMIZATION_CACHE_METRICS["invalidations"] += 1
            _OPTIMIZATION_CACHE_METRICS["invalidated_entries"] += removed
        return removed

    keys_to_drop: list[str] = []
    for key in list(_OPTIMIZATION_CACHE.keys()):
        cached_merchant_id, cached_channel = key.split("|", 1)
        if merchant_id is not None and cached_merchant_id != merchant_id:
            continue
        if channel is not None and cached_channel != channel:
            continue
        keys_to_drop.append(key)

    for key in keys_to_drop:
        _OPTIMIZATION_CACHE.pop(key, None)

    if keys_to_drop:
        _OPTIMIZATION_CACHE_METRICS["invalidations"] += 1
        _OPTIMIZATION_CACHE_METRICS["invalidated_entries"] += len(keys_to_drop)
    invalidate_readiness_snapshot_cache(merchant_id, channel=channel)
    return len(keys_to_drop)


def reset_readiness_optimization_cache_observability() -> None:
    _OPTIMIZATION_CACHE.clear()
    for key in list(_OPTIMIZATION_REFRESH_TASKS.keys()):
        task = _OPTIMIZATION_REFRESH_TASKS.pop(key, None)
        if task is not None and not task.done():
            task.cancel()
    for key in list(_OPTIMIZATION_CACHE_METRICS.keys()):
        _OPTIMIZATION_CACHE_METRICS[key] = 0


def get_readiness_optimization_cache_metrics() -> dict[str, Any]:
    total_requests = (
        _OPTIMIZATION_CACHE_METRICS["hits"]
        + _OPTIMIZATION_CACHE_METRICS["misses"]
        + _OPTIMIZATION_CACHE_METRICS["stale_served"]
    )
    hit_rate = (_OPTIMIZATION_CACHE_METRICS["hits"] / total_requests * 100.0) if total_requests else 0.0
    stale_hit_rate = (
        _OPTIMIZATION_CACHE_METRICS["stale_served"] / total_requests * 100.0
    ) if total_requests else 0.0
    now_mono = time.monotonic()
    entries = []
    for key, (cached_at, payload, _snapshot, _aux) in sorted(_OPTIMIZATION_CACHE.items()):
        merchant_id, cached_channel = key.split("|", 1)
        age_seconds = max(0.0, now_mono - cached_at)
        entries.append(
            {
                "merchant_id": merchant_id,
                "channel": cached_channel,
                "plan_id": payload.plan.plan_id,
                "snapshot_id": payload.plan.snapshot_id,
                "age_seconds": round(age_seconds, 3),
                "expires_in_seconds": round(max(0.0, _OPTIMIZATION_CACHE_TTL_SECONDS - age_seconds), 3),
            }
        )

    return {
        "hits": _OPTIMIZATION_CACHE_METRICS["hits"],
        "misses": _OPTIMIZATION_CACHE_METRICS["misses"],
        "stores": _OPTIMIZATION_CACHE_METRICS["stores"],
        "expired": _OPTIMIZATION_CACHE_METRICS["expired"],
        "stale_served": _OPTIMIZATION_CACHE_METRICS["stale_served"],
        "refreshes": _OPTIMIZATION_CACHE_METRICS["refreshes"],
        "background_refreshes": _OPTIMIZATION_CACHE_METRICS["background_refreshes"],
        "background_refresh_successes": _OPTIMIZATION_CACHE_METRICS["background_refresh_successes"],
        "background_refresh_failures": _OPTIMIZATION_CACHE_METRICS["background_refresh_failures"],
        "warmup_scheduled": _OPTIMIZATION_CACHE_METRICS["warmup_scheduled"],
        "coalesced_waits": _OPTIMIZATION_CACHE_METRICS["coalesced_waits"],
        "invalidations": _OPTIMIZATION_CACHE_METRICS["invalidations"],
        "invalidated_entries": _OPTIMIZATION_CACHE_METRICS["invalidated_entries"],
        "total_requests": total_requests,
        "hit_rate": round(hit_rate, 2),
        "stale_hit_rate": round(stale_hit_rate, 2),
        "entries": len(_OPTIMIZATION_CACHE),
        "refresh_tasks": sum(1 for task in _OPTIMIZATION_REFRESH_TASKS.values() if not task.done()),
        "ttl_seconds": _OPTIMIZATION_CACHE_TTL_SECONDS,
        "active_keys": entries,
    }


def _cached_optimization_payload(
    merchant_id: str,
    *,
    channel: str,
) -> Optional[MerchantReadinessOptimizationPayload]:
    context = _cached_optimization_context(merchant_id, channel=channel)
    if context is None:
        return None
    payload, _snapshot, _aux = context
    return payload


def _cached_optimization_context(
    merchant_id: str,
    *,
    channel: str,
) -> Optional[tuple[MerchantReadinessOptimizationPayload, Optional[MerchantReadinessSnapshot], dict[str, Any]]]:
    cache_key = _optimization_cache_key(merchant_id, channel)
    entry = _OPTIMIZATION_CACHE.get(cache_key)
    if not entry:
        _OPTIMIZATION_CACHE_METRICS["misses"] += 1
        return None
    cached_at, payload, snapshot, aux_context = entry
    if time.monotonic() - cached_at > _OPTIMIZATION_CACHE_TTL_SECONDS:
        _OPTIMIZATION_CACHE_METRICS["misses"] += 1
        _OPTIMIZATION_CACHE_METRICS["expired"] += 1
        return None
    _OPTIMIZATION_CACHE_METRICS["hits"] += 1
    return (
        payload.model_copy(deep=True),
        snapshot.model_copy(deep=True) if snapshot is not None else None,
        aux_context,
    )


def _store_optimization_payload(
    merchant_id: str,
    *,
    channel: str,
    payload: MerchantReadinessOptimizationPayload,
    snapshot: Optional[MerchantReadinessSnapshot] = None,
    aux_context: Optional[dict[str, Any]] = None,
) -> MerchantReadinessOptimizationPayload:
    cache_key = _optimization_cache_key(merchant_id, channel)
    _OPTIMIZATION_CACHE[cache_key] = (
        time.monotonic(),
        payload.model_copy(deep=True),
        snapshot.model_copy(deep=True) if snapshot is not None else None,
        dict(aux_context or {}),
    )
    _OPTIMIZATION_CACHE_METRICS["stores"] += 1
    return payload


def _optimization_cache_entry(
    merchant_id: str,
    *,
    channel: str,
) -> Optional[
    tuple[
        float,
        MerchantReadinessOptimizationPayload,
        Optional[MerchantReadinessSnapshot],
        dict[str, Any],
    ]
]:
    return _OPTIMIZATION_CACHE.get(_optimization_cache_key(merchant_id, channel))


def _optimization_refresh_task(
    merchant_id: str,
    *,
    channel: str,
) -> Optional[asyncio.Task[None]]:
    task = _OPTIMIZATION_REFRESH_TASKS.get(_optimization_cache_key(merchant_id, channel))
    if task is not None and not task.done():
        return task
    return None


async def _load_readiness_snapshot_or_summary(
    merchant_id: str,
    *,
    channel: str,
    force_snapshot_refresh: bool = False,
) -> tuple[Optional[MerchantReadinessSnapshot], Optional[ReadinessSummary]]:
    if not readiness_router_enabled():
        return None, _fallback_summary(
            assessment_state="disabled",
            blocker="readiness_assessment_disabled",
            channel=channel,
        )

    if merchant_id != "synthetic-demo-merchant":
        alpha_merchant_id = readiness_alpha_merchant_id()
        if not readiness_real_merchant_alpha_enabled() or merchant_id != alpha_merchant_id:
            return None, _fallback_summary(
                assessment_state="not_assessed",
                blocker="merchant_not_assessed_for_readiness_alpha",
                channel=channel,
            )

    try:
        if force_snapshot_refresh:
            return (
                await build_readiness_snapshot(
                    merchant_id,
                    channel=channel,
                    force_refresh=True,
                ),
                None,
            )
        return await build_readiness_snapshot(merchant_id, channel=channel), None
    except UnsupportedMerchantError:
        return None, _fallback_summary(
            assessment_state="not_assessed",
            blocker="merchant_not_assessed_for_readiness_alpha",
            channel=channel,
        )
    except Exception:
        return None, _fallback_summary(
            assessment_state="assessed",
            blocker="readiness_summary_unavailable",
            channel=channel,
            merchant_alpha_mode="assessment_error",
        )


def _normalize_queue_mode(value: Optional[str]) -> str:
    normalized = str(value or "full").strip().lower()
    if normalized in {"full", "page", "none"}:
        return normalized
    return "full"


def _normalize_sort_by(value: Optional[str]) -> str:
    normalized = str(value or "default").strip().lower()
    if normalized in {"default", "cq_desc", "mr_desc"}:
        return normalized
    return "default"


def _normalize_push_status(value: Optional[str]) -> str:
    normalized = str(value or "all").strip().lower()
    if normalized in {"all", "eligible", "excluded"}:
        return normalized
    return "all"


def _normalize_page(value: Optional[int]) -> int:
    try:
        return max(1, int(value or 1))
    except Exception:
        return 1


def _normalize_page_size(value: Optional[int]) -> int:
    try:
        normalized = int(value or _DEFAULT_QUEUE_PAGE_SIZE)
    except Exception:
        normalized = _DEFAULT_QUEUE_PAGE_SIZE
    return max(1, min(_MAX_QUEUE_PAGE_SIZE, normalized))


def _queue_item_matches_search(item: ProductReadinessQueueItem, query: str) -> bool:
    normalized_query = _normalized_lower(query)
    if not normalized_query:
        return True
    haystacks = [
        item.title,
        item.brand,
        item.category,
        item.platform_product_id,
        item.product_id,
    ]
    return any(normalized_query in _normalized_lower(value) for value in haystacks if value)


def _queue_item_matches_issue_bucket(
    item: ProductReadinessQueueItem,
    issue_bucket: Optional[str],
) -> bool:
    normalized_bucket = str(issue_bucket or "").strip()
    if not normalized_bucket or normalized_bucket == "all":
        return True

    if any(_bucket_code_for_reason(issue.code) == normalized_bucket for issue in (item.top_issues or [])):
        return True
    return any(_bucket_code_for_reason(code) == normalized_bucket for code in (item.content_gap_codes or []))


_VALID_QUEUE_SEGMENTS = {"all", "fix_here", "in_store", "other"}


def _normalize_segment(value: Optional[str]) -> str:
    normalized = str(value or "all").strip().lower()
    return normalized if normalized in _VALID_QUEUE_SEGMENTS else "all"


def _segment_for_queue_item(item: ProductReadinessQueueItem) -> str:
    """Fix-location segment for a queue item.

    Mirrors the frontend precedence in getProductActionLabel so the chip the
    merchant clicks resolves to exactly one segment:
    ``run_product_enrichment`` -> fix here, ``catalog_data`` -> in your store,
    everything else -> other.
    """
    if item.recommended_action_type == "run_product_enrichment":
        return "fix_here"
    if item.fix_surface == "catalog_data":
        return "in_store"
    return "other"


def _compute_segment_counts(
    items: list[ProductReadinessQueueItem],
) -> ProductQueueSegmentCounts:
    counts = {"fix_here": 0, "in_store": 0, "other": 0}
    for item in items:
        counts[_segment_for_queue_item(item)] += 1
    return ProductQueueSegmentCounts(
        all=len(items),
        fix_here=counts["fix_here"],
        in_store=counts["in_store"],
        other=counts["other"],
    )


def _filter_product_queue_items(
    product_queue: list[ProductReadinessQueueItem],
    *,
    search: Optional[str],
    issue_bucket: Optional[str],
    push_status: str,
    blocked_only: bool,
    low_quality_only: bool,
) -> list[ProductReadinessQueueItem]:
    filtered: list[ProductReadinessQueueItem] = []
    for item in product_queue:
        if search and not _queue_item_matches_search(item, search):
            continue
        if not _queue_item_matches_issue_bucket(item, issue_bucket):
            continue
        if blocked_only and int(item.blocked_variant_count or 0) <= 0:
            continue
        if push_status == "excluded" and item.agent_push_status != AGENT_PUSH_STATUS_EXCLUDED:
            continue
        if push_status == "eligible" and item.agent_push_status == AGENT_PUSH_STATUS_EXCLUDED:
            continue
        if low_quality_only:
            score = item.content_quality_score
            if not isinstance(score, (int, float)) or float(score) >= 60.0:
                continue
        filtered.append(item)
    return filtered


def _sort_product_queue_items(
    product_queue: list[ProductReadinessQueueItem],
    *,
    sort_by: str,
) -> list[ProductReadinessQueueItem]:
    if sort_by == "cq_desc":
        return sorted(
            product_queue,
            key=lambda item: (
                -(float(item.content_quality_score) if isinstance(item.content_quality_score, (int, float)) else -1.0),
                -float(item.priority_score or 0.0),
                item.title.lower(),
            ),
        )
    if sort_by == "mr_desc":
        return sorted(
            product_queue,
            key=lambda item: (
                -(float(item.model_readiness_score) if isinstance(item.model_readiness_score, (int, float)) else -1.0),
                -float(item.priority_score or 0.0),
                item.title.lower(),
            ),
        )
    return list(product_queue)


def _build_product_queue_page(
    *,
    total_items: int,
    page: int,
    page_size: int,
    search: Optional[str],
    issue_bucket: Optional[str],
    push_status: str,
    blocked_only: bool,
    low_quality_only: bool,
    sort_by: str,
) -> ProductQueuePage:
    total_pages = max(1, (total_items + page_size - 1) // page_size) if total_items else 0
    normalized_page = min(page, total_pages) if total_pages > 0 else 1
    return ProductQueuePage(
        page=normalized_page,
        page_size=page_size,
        total_items=total_items,
        total_pages=total_pages,
        has_next=total_pages > 0 and normalized_page < total_pages,
        has_prev=normalized_page > 1 and total_pages > 0,
        applied_filters=ProductQueueAppliedFilters(
            search=_normalize_text(search) or None,
            issue_bucket=str(issue_bucket or "").strip() or None,
            push_status=push_status,
            blocked_only=bool(blocked_only),
            low_quality_only=bool(low_quality_only),
            sort_by=sort_by,
        ),
    )


def _severity_for_bucket(bucket_code: str, affected_count: int) -> str:
    if bucket_code in {"checkout_payment_setup", "shipping_returns_setup", "trust_support_setup", "order_sync_operations"}:
        return "high"
    if affected_count >= 25:
        return "high"
    if affected_count > 0:
        return "medium"
    return "low"


def _product_issue_counts(product: Any, channel: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    for variant in product.variants:
        if variant.channel_coverage.get(channel) == "ready":
            warning_codes = set(variant.warnings.get("discovery", []) + variant.warnings.get("checkout", []))
            for code in warning_codes:
                counts[code] += 1
            continue
        blocker_codes = set(variant.blockers.get("discovery", []) + variant.blockers.get("checkout", []))
        for code in blocker_codes:
            counts[code] += 1
    return counts


def _variant_impact(variant: Any) -> str:
    if variant.blockers.get("checkout"):
        return "full_agent_commerce"
    if variant.blockers.get("discovery"):
        return "discovery_only"
    if variant.warnings.get("checkout"):
        return "checkout"
    if variant.warnings.get("discovery"):
        return "discovery_only"
    return "discovery_only"


_WIX_SITE_ID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _normalize_store_domain(domain: Optional[str]) -> str:
    normalized = str(domain or "").strip()
    if not normalized:
        return ""
    if "://" not in normalized:
        normalized = f"https://{normalized}"
    parsed = urlparse(normalized)
    host = parsed.netloc or parsed.path
    return str(host or "").strip().strip("/")


def _normalize_store_base_url(domain: Optional[str]) -> str:
    normalized = str(domain or "").strip()
    if not normalized:
        return ""
    if "://" not in normalized:
        normalized = f"https://{normalized}"
    parsed = urlparse(normalized)
    host = parsed.netloc or parsed.path
    if not host:
        return ""
    path = parsed.path if parsed.netloc else ""
    normalized_path = path.rstrip("/")
    return f"https://{host}{normalized_path}"


def _extract_wix_site_id(domain: Optional[str]) -> str:
    normalized = str(domain or "").strip()
    if not normalized:
        return ""
    return normalized if _WIX_SITE_ID_PATTERN.fullmatch(normalized) else ""


def _normalize_bigcommerce_admin_origin(domain: Optional[str]) -> str:
    normalized_domain = _normalize_store_domain(domain)
    if not normalized_domain:
        return ""
    if normalized_domain.endswith(".mybigcommerce.com"):
        return f"https://{normalized_domain}"
    if "." not in normalized_domain:
        return f"https://{normalized_domain}.mybigcommerce.com"
    return ""


def _build_platform_admin_url(
    *,
    platform: Optional[str],
    platform_product_id: Optional[str],
    store_domains_by_platform: Optional[dict[str, str]] = None,
) -> Optional[str]:
    normalized_platform = str(platform or "").strip().lower()
    normalized_product_id = str(platform_product_id or "").strip()
    if not normalized_platform or not normalized_product_id:
        return None

    if normalized_platform == "shopify":
        domain = _normalize_store_domain((store_domains_by_platform or {}).get(normalized_platform))
        if not domain:
            return None
        return f"https://{domain}/admin/products/{quote(normalized_product_id, safe='')}"

    if normalized_platform == "woocommerce":
        base_url = _normalize_store_base_url((store_domains_by_platform or {}).get(normalized_platform))
        if not base_url:
            return None
        return (
            f"{base_url}/wp-admin/post.php?"
            f"post={quote(normalized_product_id, safe='')}&action=edit"
        )

    if normalized_platform == "wix":
        site_id = _extract_wix_site_id((store_domains_by_platform or {}).get(normalized_platform))
        if not site_id:
            return None
        return (
            "https://manage.wix.com/dashboard/"
            f"{quote(site_id, safe='')}/store/products/product/{quote(normalized_product_id, safe='')}"
        )

    if normalized_platform == "bigcommerce":
        admin_origin = _normalize_bigcommerce_admin_origin(
            (store_domains_by_platform or {}).get(normalized_platform)
        )
        if not admin_origin:
            return None
        return (
            f"{admin_origin}/manage/products/{quote(normalized_product_id, safe='')}/edit"
        )

    return None


async def _load_store_domains_by_platform(merchant_id: str) -> dict[str, str]:
    try:
        stores = await get_merchant_active_stores(merchant_id)
    except Exception as exc:
        logger.warning("Failed to load merchant stores for readiness links: %s", str(exc)[:200])
        return {}

    store_domains_by_platform: dict[str, str] = {}
    for store in stores:
        platform = str((store or {}).get("platform") or "").strip().lower()
        domain = _normalize_store_domain((store or {}).get("domain"))
        if not platform or not domain or platform in store_domains_by_platform:
            continue
        store_domains_by_platform[platform] = domain

    return store_domains_by_platform


def _product_queue_item(
    product: Any,
    channel: str,
    *,
    store_domains_by_platform: Optional[dict[str, str]] = None,
) -> ProductReadinessQueueItem:
    issue_counts = _product_issue_counts(product, channel)
    blocked_variant_count = sum(1 for variant in product.variants if variant.channel_coverage.get(channel) != "ready")
    ready_variant_count = max(0, len(product.variants) - blocked_variant_count)

    top_codes = [code for code, _ in issue_counts.most_common(3)]
    top_issues = [
        ProductQueueIssue(
            code=code,
            label=_humanize_code(code),
            impact=_BUCKET_DEFINITIONS[_bucket_code_for_reason(code)]["impact"],
            affected_variant_count=issue_counts[code],
        )
        for code in top_codes
    ]

    primary_code = top_codes[0] if top_codes else ""
    primary_bucket = _BUCKET_DEFINITIONS[_bucket_code_for_reason(primary_code)] if primary_code else _BUCKET_DEFINITIONS["catalog_content"]
    item_fix_surface = primary_bucket["fix_surface"]
    if primary_code in _CATALOG_REVIEW_REASON_CODES:
        item_fix_surface = "catalog_data"
    primary_action = None
    if primary_code == "missing_price":
        primary_action = "Fix missing prices for this product before enabling AI checkout."
    elif primary_code == "out_of_stock":
        primary_action = "Restock or exclude the blocked variants for this product."
    elif primary_code == "missing_primary_image":
        primary_action = "Add a primary image so this product can be shown safely."
    elif primary_code == "missing_title":
        primary_action = "Add a clear product title before exposing this product to agents."
    elif primary_code:
        primary_action = f"Resolve { _humanize_code(primary_code).lower() } for this product."
    else:
        primary_action = "Review this product and improve its enrichment if you want better agent performance."

    impact = "discovery_only"
    if any(_variant_impact(variant) == "full_agent_commerce" for variant in product.variants):
        impact = "full_agent_commerce"
    elif any(_variant_impact(variant) == "checkout" for variant in product.variants):
        impact = "checkout"

    affected_variant_count = sum(issue.affected_variant_count for issue in top_issues)
    fixability = _fixability_for_surface(item_fix_surface)
    priority_score = _product_priority_score(
        blocked_variant_count=blocked_variant_count,
        impact=impact,
        fix_surface=item_fix_surface,
        affected_variant_count=affected_variant_count,
    )
    priority_reason = _product_priority_reason(
        blocked_variant_count=blocked_variant_count,
        impact=impact,
    )

    recommended_action_type = "review_catalog_data"
    if (
        item_fix_surface == "product_content"
        and primary_code in _EXECUTABLE_PRODUCT_CONTENT_REASON_CODES
    ):
        recommended_action_type = "run_product_enrichment"

    return ProductReadinessQueueItem(
        queue_item_scope="product",
        queue_item_id=f"product:{product.platform or 'unknown'}:{product.product_id}",
        product_id=product.product_id,
        platform=product.platform or "unknown",
        platform_product_id=product.product_id,
        title=product.title,
        image_url=product.default_image_url,
        brand=product.brand,
        category=product.category,
        blocked_variant_count=blocked_variant_count,
        ready_variant_count=ready_variant_count,
        top_issues=top_issues,
        primary_action=primary_action,
        fix_surface=item_fix_surface,
        fixability=fixability,
        impact=impact,
        priority_score=priority_score,
        priority_reason=priority_reason,
        platform_admin_url=_build_platform_admin_url(
            platform=product.platform or "unknown",
            platform_product_id=product.product_id,
            store_domains_by_platform=store_domains_by_platform,
        ),
        recommended_action_id=f"act_product:{product.platform or 'unknown'}:{product.product_id}",
        recommended_action_type=recommended_action_type,
    )


def _apply_agent_push_projection(
    *,
    snapshot_products: list[Any],
    product_queue: list[ProductReadinessQueueItem],
    checked_at: Optional[str],
) -> tuple[list[ProductReadinessQueueItem], AgentPushSummary]:
    if not product_queue:
        return product_queue, AgentPushSummary()

    projections_by_key: Dict[tuple[str, str], Dict[str, Any]] = {}
    for product in snapshot_products:
        key = make_product_key(product.platform or "unknown", product.product_id)
        if key is None:
            continue
        projections_by_key[key] = build_agent_push_projection_from_ready_product(
            product,
            checked_at=checked_at,
        )

    updated_queue: list[ProductReadinessQueueItem] = []
    active_blocked_variants = 0
    for item in product_queue:
        key = make_product_key(item.platform, item.platform_product_id or item.product_id)
        projection = projections_by_key.get(key or ("", ""), {})
        excluded_variant_count = int(projection.get("excluded_variant_count") or 0)
        active_blocked_count = max(0, int(item.blocked_variant_count or 0) - excluded_variant_count)
        active_blocked_variants += active_blocked_count

        item.blocked_variant_count = active_blocked_count
        item.agent_push_status = projection.get("agent_push_status", item.agent_push_status)
        item.agent_push_reason_codes = list(projection.get("agent_push_reason_codes") or [])
        item.eligible_variant_count = int(
            projection.get("eligible_variant_count")
            if projection.get("eligible_variant_count") is not None
            else item.ready_variant_count
        )
        item.excluded_variant_count = excluded_variant_count
        item.store_data_last_checked_at = projection.get(
            "store_data_last_checked_at",
            item.store_data_last_checked_at,
        )

        if item.agent_push_status == AGENT_PUSH_STATUS_EXCLUDED and active_blocked_count == 0:
            item.priority_score = max(1.0, float(item.priority_score or 0.0) * 0.3)
            item.primary_action = (
                "Pivota is excluding this product from agent push until the store platform sends an in-stock variant with valid pricing."
            )
            item.priority_reason = (
                "This product is still visible here for diagnosis, but it is no longer treated as an active agent-exposure blocker."
            )
        elif item.excluded_variant_count > 0 and active_blocked_count == 0:
            item.priority_score = max(1.0, float(item.priority_score or 0.0) * 0.55)
            item.priority_reason = (
                "Some variants are auto-excluded from agent push, but the remaining sellable variants can still be exposed."
            )

        updated_queue.append(item)

    summary_payload = summarize_agent_push_projections(
        projections_by_key.values(),
        active_blocked_variants=active_blocked_variants,
    )
    return updated_queue, AgentPushSummary(**summary_payload)


async def _load_cache_rows_for_product_keys(
    merchant_id: str,
    product_keys: list[tuple[str, str]],
) -> Dict[tuple[str, str], Dict[str, Any]]:
    if not product_keys:
        return {}

    platforms = sorted({platform for platform, _ in product_keys})
    platform_product_ids = sorted(
        {platform_product_id for _, platform_product_id in product_keys if platform_product_id}
    )
    product_key_set = set(product_keys)

    ranked = (
        select(
            products_cache,
            func.row_number().over(
                partition_by=[
                    products_cache.c.merchant_id,
                    products_cache.c.platform,
                    products_cache.c.platform_product_id,
                ],
                order_by=[
                    products_cache.c.cached_at.desc(),
                    products_cache.c.id.desc(),
                ],
            ).label("row_num"),
        )
        .where(products_cache.c.merchant_id == merchant_id)
        .where(products_cache.c.platform.in_(platforms))
        .where(products_cache.c.platform_product_id.in_(platform_product_ids))
        .where(products_cache.c.expires_at > datetime.now())
    )
    ranked_subquery = ranked.subquery("ranked_products_cache")
    query = select(*[col for col in ranked_subquery.c if col.key != "row_num"]).where(
        ranked_subquery.c.row_num == 1
    )
    rows = await database.fetch_all(query)

    cache_rows_by_key: Dict[tuple[str, str], Dict[str, Any]] = {}
    for row in rows:
        payload = dict(row)
        key = make_product_key(payload.get("platform"), payload.get("platform_product_id"))
        if key is None or key not in product_key_set or key in cache_rows_by_key:
            continue
        cache_rows_by_key[key] = payload
    return cache_rows_by_key


def _snapshot_products_by_key(snapshot_products: list[Any]) -> dict[tuple[str, str], Any]:
    products_by_key: dict[tuple[str, str], Any] = {}
    for product in snapshot_products:
        key = make_product_key(product.platform or "unknown", product.product_id)
        if key is not None:
            products_by_key[key] = product
    return products_by_key


async def _build_catalog_projection_context(
    merchant_id: str,
    *,
    snapshot_products: list[Any],
    include_field_facts: bool = False,
) -> dict[str, Any]:
    snapshot_products_by_key = _snapshot_products_by_key(snapshot_products)
    product_keys = list(snapshot_products_by_key.keys())
    cache_rows_by_key = await _load_cache_rows_for_product_keys(merchant_id, product_keys)
    store_context = await _load_store_context(merchant_id)
    field_facts_by_key = (
        await _load_catalog_field_facts_for_product_keys(merchant_id, product_keys)
        if include_field_facts
        else {}
    )

    current_products_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    parsed_products_by_key: dict[tuple[str, str], StandardProduct] = {}
    for product_key, snapshot_product in snapshot_products_by_key.items():
        current_product = _current_product_data(cache_rows_by_key.get(product_key))
        if not current_product:
            current_product = _snapshot_product_to_current_product(
                snapshot_product,
                merchant_id=merchant_id,
            )
        current_products_by_key[product_key] = current_product
        parsed_product = _current_product_to_standard_product(
            current_product,
            merchant_id=merchant_id,
            platform=product_key[0],
            product_id=product_key[1],
        )
        if parsed_product is not None:
            parsed_products_by_key[product_key] = parsed_product

    return {
        "product_keys": product_keys,
        "snapshot_products_by_key": snapshot_products_by_key,
        "cache_rows_by_key": cache_rows_by_key,
        "current_products_by_key": current_products_by_key,
        "parsed_products_by_key": parsed_products_by_key,
        "store_context": store_context,
        "field_facts_by_key": field_facts_by_key,
    }


async def _apply_quality_projection(
    merchant_id: str,
    *,
    snapshot_products: list[Any],
    product_queue: list[ProductReadinessQueueItem],
    cache_rows_by_key: Optional[Dict[tuple[str, str], Dict[str, Any]]] = None,
) -> tuple[list[ProductReadinessQueueItem], QualityCoverageSummary]:
    try:
        product_keys = [
            key
            for key in (
                make_product_key(product.platform or "unknown", product.product_id)
                for product in snapshot_products
            )
            if key is not None
        ]
        if not product_keys:
            return product_queue, QualityCoverageSummary()

        cache_rows_by_key = cache_rows_by_key or await _load_cache_rows_for_product_keys(
            merchant_id,
            product_keys,
        )
        latest_quality_rows = await fetch_latest_quality_rows(
            merchant_id,
            platforms=sorted({platform for platform, _ in product_keys}),
            product_keys=product_keys,
        )
        enrichments_by_key = await get_enrichments_for_products(
            merchant_id,
            product_keys=product_keys,
            geo_code="default",
        )
        active_job = await get_active_quality_backfill_job(merchant_id)

        projections_by_key: Dict[tuple[str, str], Dict[str, Any]] = {}
        for key in product_keys:
            cache_row = cache_rows_by_key.get(key)
            payload = (
                build_quality_payload_from_cache_row(cache_row, enrichments_by_key.get(key) or {})
                if cache_row
                else None
            )
            projections_by_key[key] = build_quality_projection(
                snapshot_row=latest_quality_rows.get(key),
                payload=payload,
            )

        for item in product_queue:
            key = make_product_key(item.platform, item.platform_product_id or item.product_id)
            projection = projections_by_key.get(key or ("", ""), {})
            item.content_quality_score = projection.get("content_quality_score")
            item.model_readiness_score = projection.get("model_readiness_score")
            item.conversion_potential_score = projection.get("conversion_potential_score")
            item.quality_last_evaluated_at = projection.get("last_evaluated_at")
            item.quality_source = projection.get("quality_source", QUALITY_SOURCE_NONE)

        coverage = QualityCoverageSummary.model_validate(
            summarize_quality_coverage(
                product_keys,
                projections_by_key=projections_by_key,
                snapshot_rows_by_key=latest_quality_rows,
                active_backfill_job=active_job,
            )
        )
        return product_queue, coverage
    except Exception as exc:
        logger.warning("Failed to project quality coverage into readiness payload: %s", str(exc)[:200])
        return product_queue, QualityCoverageSummary()


def _content_gap_priority_weight(code: str) -> int:
    return {
        "generic_low_information_title": 6,
        "missing_size_guidance": 5,
        "missing_material_or_ingredient_info": 5,
        "missing_usage_scenario": 4,
        "missing_feature_signal": 3,
        "missing_color_signal": 2,
        "missing_audience_signal": 2,
        "missing_category_signal": 2,
    }.get(code, 1)


def _title_primary_action(item: ProductReadinessQueueItem) -> str:
    if item.title_health == "rewrite_candidate":
        return "Review the suggested richer title and refresh the enrichment draft for this product."
    if item.title_health == "needs_more_facts":
        return "Add the missing product facts before generating a fuller market-facing title."
    if item.title_health == "generic_low_information":
        return "Expand the product title with style, audience, color, feature, and size signals."
    return item.primary_action or "Review this product and improve its catalog content."


def _queue_item_has_workspace_signal(item: ProductReadinessQueueItem) -> bool:
    if int(item.blocked_variant_count or 0) > 0 or int(item.excluded_variant_count or 0) > 0:
        return True
    if item.recommended_action_type == "run_product_enrichment":
        return True
    if item.title_health and item.title_health != "healthy":
        return True
    return False


def _is_content_only_opportunity(item: ProductReadinessQueueItem) -> bool:
    return (
        int(item.blocked_variant_count or 0) <= 0
        and int(item.excluded_variant_count or 0) <= 0
        and _queue_item_has_workspace_signal(item)
    )


async def _apply_catalog_health_projection(
    *,
    product_queue: list[ProductReadinessQueueItem],
    current_products_by_key: dict[tuple[str, str], dict[str, Any]],
    parsed_products_by_key: dict[tuple[str, str], StandardProduct],
    store_context: dict[str, Any],
    include_title_preview: bool = True,
    target_product_keys: Optional[set[tuple[str, str]]] = None,
) -> list[ProductReadinessQueueItem]:
    if not product_queue:
        return product_queue

    for item in product_queue:
        product_key = make_product_key(item.platform, item.platform_product_id or item.product_id)
        if product_key is None:
            continue
        if target_product_keys is not None and product_key not in target_product_keys:
            continue
        current_product = current_products_by_key.get(product_key, {})
        parsed_product = parsed_products_by_key.get(product_key)
        if parsed_product is None:
            continue

        suggestion_language = _catalog_health_language(store_context, current_product)
        title_suggestion = generate_title_suggestion(
            parsed_product,
            preferred_language=suggestion_language,
        )
        item.content_gap_codes = _dedupe_codes(item.content_gap_codes, title_suggestion.content_gap_codes)
        item.missing_attribute_labels = _dedupe_strings(
            [*(item.missing_attribute_labels or []), *(title_suggestion.missing_attribute_labels or [])]
        )
        item.title_health = title_suggestion.title_health
        item.suggested_title_preview = title_suggestion.suggested_title if include_title_preview else None
        item.suggestion_language = title_suggestion.suggestion_language
        item.suggestion_confidence = title_suggestion.suggestion_confidence
        item.suggestion_rationale = title_suggestion.suggestion_rationale

        issue_map: dict[str, ProductQueueIssue] = {
            issue.code: issue for issue in (item.top_issues or [])
        }
        content_issue_count = max(
            1,
            int(item.ready_variant_count or 0)
            + int(item.blocked_variant_count or 0)
            + int(item.excluded_variant_count or 0),
        )
        for code in item.content_gap_codes:
            issue_map.setdefault(
                code,
                ProductQueueIssue(
                    code=code,
                    label=_humanize_code(code),
                    impact="discovery_only",
                    affected_variant_count=content_issue_count,
                ),
            )
        item.top_issues = sorted(
            issue_map.values(),
            key=lambda issue: (
                -_content_gap_priority_weight(issue.code),
                -int(issue.affected_variant_count or 0),
                issue.label.lower(),
            ),
        )[:3]

        if _is_content_only_opportunity(item):
            item.fix_surface = "product_content"
            item.fixability = "merchant_fixable"
            item.impact = "discovery_only"
            item.recommended_action_type = "run_product_enrichment"
            item.primary_action = _title_primary_action(item)
            item.priority_reason = (
                item.suggestion_rationale
                or "Improving this title and the missing product facts can increase how reliably agents retrieve and present this item."
            )
            item.priority_score = max(
                float(item.priority_score or 0.0),
                _product_priority_score(
                    blocked_variant_count=0,
                    impact="discovery_only",
                    fix_surface="product_content",
                    affected_variant_count=len(item.content_gap_codes) or 1,
                ),
            )
    return product_queue


async def _build_source_data_lanes(
    merchant_id: str,
    *,
    product_queue: list[ProductReadinessQueueItem],
    snapshot_products_by_key: dict[tuple[str, str], Any],
    current_products_by_key: dict[tuple[str, str], dict[str, Any]],
    parsed_products_by_key: dict[tuple[str, str], StandardProduct],
    field_facts_by_key: dict[tuple[str, str], dict[str, dict[str, Any]]],
    store_context: dict[str, Any],
) -> list[SourceDataLaneSummary]:
    product_keys = [
        key
        for key in (
            make_product_key(item.platform, item.platform_product_id or item.product_id)
            for item in product_queue
        )
        if key is not None
    ]
    decisions_by_reason_key = await list_source_data_decisions_by_reason_codes(
        merchant_id,
        reason_codes=_SOURCE_DATA_DECISION_LABELS.keys(),
        product_keys=product_keys,
    )

    lane_stats: dict[str, dict[str, Any]] = {
        reason_code: {
            "label": definition["label"],
            "affected_products": 0,
            "affected_variants": 0,
            "blocked_products": 0,
            "excluded_products": 0,
            "next_product": None,
            "state_counts": Counter(),
            "decision_counts": Counter(),
            "reason_codes": set(),
        }
        for reason_code, definition in _SOURCE_DATA_LANE_DEFS.items()
    }

    for item in product_queue:
        product_key = make_product_key(item.platform, item.platform_product_id or item.product_id)
        if product_key is None:
            continue
        snapshot_product = snapshot_products_by_key.get(product_key)
        if snapshot_product is None:
            continue
        current_product = current_products_by_key.get(product_key, {})
        parsed_product = parsed_products_by_key.get(product_key)
        field_facts = field_facts_by_key.get(product_key, {})
        lane_reason_codes = _lane_reason_codes_for_product(
            parsed_product,
            current_product,
            store_context=store_context,
            field_facts=field_facts,
        )
        decisions_key = f"{product_key[0]}|{product_key[1]}"

        for reason_code in ("missing_price", "out_of_stock", "missing_primary_image"):
            lane = lane_stats[reason_code]
            sample_variant_id: Optional[str] = None
            affected_variants = 0
            queue_state_key: Optional[str] = None

            if reason_code == "missing_primary_image":
                matched, affected_variants = _product_matches_source_data_reason(
                    reason_code,
                    queue_item=item,
                )
                if not matched:
                    continue
                affected_variants = max(1, affected_variants)
                queue_state_key = (
                    "image_visible_now"
                    if _current_product_has_visible_image(current_product)
                    else "hero_image_missing"
                )
                persisted_decision = str(
                    (
                        decisions_by_reason_key
                        .get("missing_primary_image", {})
                        .get(decisions_key, {})
                        .get("decision_state")
                    )
                    or ""
                ).strip()
                if queue_state_key == "hero_image_missing" and persisted_decision:
                    lane["decision_counts"][persisted_decision] += 1
            else:
                matches = _build_source_data_variant_matches(
                    reason_code,
                    snapshot_product=snapshot_product,
                )
                if not matches:
                    continue
                affected_variants = len(matches)
                sample_variant_id = matches[0]["variant_id"] if matches else None
                if reason_code == "missing_price":
                    queue_state_key = _missing_price_batch_state(matches, current_product)
                    persisted_decision = str(
                        (
                            decisions_by_reason_key
                            .get("missing_price", {})
                            .get(decisions_key, {})
                            .get("decision_state")
                        )
                        or ""
                    ).strip()
                    if queue_state_key in {"whole_product_missing_price", "partially_priced"} and persisted_decision:
                        lane["decision_counts"][persisted_decision] += 1
                else:
                    queue_state_key = _out_of_stock_batch_state(matches, current_product)
                    persisted_decision = str(
                        (
                            decisions_by_reason_key
                            .get("out_of_stock", {})
                            .get(decisions_key, {})
                            .get("decision_state")
                        )
                        or ""
                    ).strip()
                    effective_decision = persisted_decision or (
                        _default_out_of_stock_decision_state(current_product)
                        if queue_state_key == "whole_product_unavailable"
                        else ""
                    )
                    if queue_state_key == "whole_product_unavailable" and effective_decision:
                        item.decision_state = effective_decision
                        lane["decision_counts"][effective_decision] += 1

            lane["affected_products"] += 1
            lane["affected_variants"] += affected_variants
            lane["blocked_products"] += 1 if int(item.blocked_variant_count or 0) > 0 else 0
            lane["excluded_products"] += 1 if int(item.excluded_variant_count or 0) > 0 else 0
            lane["reason_codes"].add(reason_code)
            if lane["next_product"] is None:
                lane["next_product"] = SourceDataLaneNextProduct(
                    platform=item.platform,
                    platform_product_id=item.platform_product_id or item.product_id or "",
                    product_id=item.product_id,
                    title=item.title,
                    blocked_variant_count=int(item.blocked_variant_count or 0),
                    excluded_variant_count=int(item.excluded_variant_count or 0),
                    sample_variant_id=sample_variant_id,
                    platform_admin_url=item.platform_admin_url,
                )
            if queue_state_key:
                lane["state_counts"][queue_state_key] += 1

        for lane_code in _CATALOG_HEALTH_LANE_REASON_CODES:
            present_reason_codes = lane_reason_codes.get(lane_code) or []
            if not present_reason_codes:
                continue
            lane = lane_stats[lane_code]
            affected_variants = max(
                1,
                int(item.ready_variant_count or 0)
                + int(item.blocked_variant_count or 0)
                + int(item.excluded_variant_count or 0),
            )
            queue_state_key = _lane_state_for_reason_codes(lane_code, present_reason_codes)
            persisted_decision = str(
                (
                    decisions_by_reason_key
                    .get(lane_code, {})
                    .get(decisions_key, {})
                    .get("decision_state")
                )
                or ""
            ).strip()
            effective_decision = persisted_decision or "pending_review"

            lane["affected_products"] += 1
            lane["affected_variants"] += affected_variants
            lane["blocked_products"] += 1 if int(item.blocked_variant_count or 0) > 0 else 0
            lane["excluded_products"] += 1 if int(item.excluded_variant_count or 0) > 0 else 0
            lane["reason_codes"].update(present_reason_codes)
            lane["decision_counts"][effective_decision] += 1
            if lane["next_product"] is None:
                sample_variant_id = None
                if snapshot_product.variants:
                    sample_variant_id = str((snapshot_product.variants[0].variant_id or "")).strip() or None
                lane["next_product"] = SourceDataLaneNextProduct(
                    platform=item.platform,
                    platform_product_id=item.platform_product_id or item.product_id or "",
                    product_id=item.product_id,
                    title=item.title,
                    blocked_variant_count=int(item.blocked_variant_count or 0),
                    excluded_variant_count=int(item.excluded_variant_count or 0),
                    sample_variant_id=sample_variant_id,
                )
            if queue_state_key:
                lane["state_counts"][queue_state_key] += 1

    summaries: list[SourceDataLaneSummary] = []
    for reason_code, definition in _SOURCE_DATA_LANE_DEFS.items():
        lane = lane_stats[reason_code]
        summaries.append(
            SourceDataLaneSummary(
                reason_code=reason_code,
                label=definition["label"],
                reason_codes=sorted(
                    list(lane["reason_codes"])
                    or list(_CATALOG_HEALTH_LANE_REASON_CODES.get(reason_code, []))
                    or [reason_code]
                ),
                affected_products=int(lane["affected_products"]),
                affected_variants=int(lane["affected_variants"]),
                blocked_products=int(lane["blocked_products"]),
                excluded_products=int(lane["excluded_products"]),
                next_product=lane["next_product"],
                queue_state_counts=[
                    SourceDataLaneStateCount(
                        key=state_key,
                        label=state_label,
                        count=int(lane["state_counts"].get(state_key, 0)),
                    )
                    for state_key, state_label in _SOURCE_DATA_LANE_STATE_LABELS[reason_code]
                ],
                decision_counts=[
                    SourceDataLaneDecisionCount(
                        key=decision_key,
                        label=decision_label,
                        count=int(lane["decision_counts"].get(decision_key, 0)),
                    )
                    for decision_key, decision_label in _SOURCE_DATA_DECISION_LABELS[reason_code].items()
                ]
                if reason_code in _SOURCE_DATA_DECISION_LABELS
                else [],
            )
        )
    return summaries


def build_lane_delta(
    *,
    reason_code: str,
    before_payload: MerchantReadinessOptimizationPayload,
    after_payload: MerchantReadinessOptimizationPayload,
) -> Optional[ReadinessLaneDelta]:
    if reason_code not in _SOURCE_DATA_LANE_DEFS:
        return None

    before_lane = next(
        (lane for lane in before_payload.source_data_lanes if lane.reason_code == reason_code),
        None,
    )
    after_lane = next(
        (lane for lane in after_payload.source_data_lanes if lane.reason_code == reason_code),
        None,
    )
    if before_lane is None and after_lane is None:
        return None

    before_counts = {item.key: item.count for item in (before_lane.queue_state_counts if before_lane else [])}
    after_counts = {item.key: item.count for item in (after_lane.queue_state_counts if after_lane else [])}

    return ReadinessLaneDelta(
        reason_code=reason_code,
        before_products=int(before_lane.affected_products if before_lane else 0),
        after_products=int(after_lane.affected_products if after_lane else 0),
        before_variants=int(before_lane.affected_variants if before_lane else 0),
        after_variants=int(after_lane.affected_variants if after_lane else 0),
        resolved_products=max(
            0,
            int((before_lane.affected_products if before_lane else 0) - (after_lane.affected_products if after_lane else 0)),
        ),
        resolved_variants=max(
            0,
            int((before_lane.affected_variants if before_lane else 0) - (after_lane.affected_variants if after_lane else 0)),
        ),
        state_counts_before=[
            SourceDataLaneStateCount(
                key=state_key,
                label=state_label,
                count=int(before_counts.get(state_key, 0)),
            )
            for state_key, state_label in _SOURCE_DATA_LANE_STATE_LABELS[reason_code]
        ],
        state_counts_after=[
            SourceDataLaneStateCount(
                key=state_key,
                label=state_label,
                count=int(after_counts.get(state_key, 0)),
            )
            for state_key, state_label in _SOURCE_DATA_LANE_STATE_LABELS[reason_code]
        ],
    )


def _build_issue_buckets(
    snapshot: MerchantReadinessSnapshot,
    *,
    product_queue: Optional[list[ProductReadinessQueueItem]] = None,
    source_data_lanes: Optional[list[SourceDataLaneSummary]] = None,
) -> list[ReadinessIssueBucket]:
    bucket_reason_counts: dict[str, Counter[str]] = {}
    bucket_affected_counts: Counter[str] = Counter()

    for code in snapshot.blockers:
        bucket = _bucket_code_for_reason(code)
        bucket_reason_counts.setdefault(bucket, Counter())[code] += 1
        bucket_affected_counts[bucket] += 1

    for code in snapshot.warnings:
        bucket = _bucket_code_for_reason(code)
        bucket_reason_counts.setdefault(bucket, Counter())[code] += 1

    if product_queue is None:
        for product in snapshot.products:
            issue_counts = _product_issue_counts(product, snapshot.channel)
            for code, count in issue_counts.items():
                bucket = _bucket_code_for_reason(code)
                bucket_reason_counts.setdefault(bucket, Counter())[code] += count
                bucket_affected_counts[bucket] += count
    else:
        for item in product_queue:
            for issue in item.top_issues or []:
                bucket = _bucket_code_for_reason(issue.code)
                bucket_reason_counts.setdefault(bucket, Counter())[issue.code] += int(issue.affected_variant_count or 0) or 1
                bucket_affected_counts[bucket] += int(issue.affected_variant_count or 0) or 1
            for code in item.content_gap_codes or []:
                bucket = _bucket_code_for_reason(code)
                bucket_reason_counts.setdefault(bucket, Counter())[code] += 1
                bucket_affected_counts[bucket] += 1

    for lane in source_data_lanes or []:
        if int(lane.affected_products or 0) <= 0:
            continue
        reason_codes = list(lane.reason_codes or []) or [lane.reason_code]
        bucket = _bucket_code_for_reason(reason_codes[0])
        bucket_affected_counts[bucket] += int(lane.affected_products or 0)
        for code in reason_codes:
            bucket_reason_counts.setdefault(bucket, Counter())[code] += 1

    buckets: list[ReadinessIssueBucket] = []
    for bucket_code, reason_counts in bucket_reason_counts.items():
        definition = _BUCKET_DEFINITIONS[bucket_code]
        affected_count = int(bucket_affected_counts.get(bucket_code, 0))
        fixability = _fixability_for_surface(definition["fix_surface"])
        priority_score = _bucket_priority_score(
            bucket_code,
            affected_count,
            impact=definition["impact"],
            fix_surface=definition["fix_surface"],
        )
        buckets.append(
            ReadinessIssueBucket(
                code=bucket_code,
                label=definition["label"],
                severity=_severity_for_bucket(bucket_code, affected_count),
                scope=definition["scope"],
                affected_count=affected_count,
                fix_surface=definition["fix_surface"],
                fixability=fixability,
                impact=definition["impact"],
                direct_target=definition["direct_target"],
                priority_score=priority_score,
                priority_reason=_bucket_priority_reason(
                    bucket_code,
                    impact=definition["impact"],
                    fix_surface=definition["fix_surface"],
                    scope=definition["scope"],
                ),
                reason_codes=[code for code, _ in reason_counts.most_common(5)],
            )
        )
    buckets.sort(
        key=lambda bucket: (
            {"high": 0, "medium": 1, "low": 2}.get(bucket.severity, 3),
            -bucket.priority_score,
            bucket.label,
        )
    )
    return buckets


def _build_merchant_actions(summary: ReadinessSummary, issue_buckets: list[ReadinessIssueBucket]) -> list[MerchantReadinessAction]:
    actions: list[MerchantReadinessAction] = []
    for bucket in issue_buckets:
        if bucket.fix_surface == "integrations":
            label = "Review integrations"
            description = f"{bucket.label} is blocking checkout or order sync for this merchant."
        elif bucket.code == "trust_support_setup":
            label = "Review trust and support info"
            description = "Add warranty, authenticity, and customer support details so agents can represent this merchant more safely."
        elif bucket.fix_surface == "policy":
            label = "Review shipping and returns setup"
            description = "Complete shipping and returns setup before enabling agent commerce."
        elif bucket.fix_surface in {"product_content", "catalog_data"}:
            label = "Fix products in Product Optimization"
            description = f"{bucket.label} is affecting discoverability or checkout for part of the catalog."
        else:
            label = "Review readiness details"
            description = f"{bucket.label} still needs review before broader LLM exposure."

        actions.append(
            MerchantReadinessAction(
                action_id=f"act_{bucket.code}",
                action_type="review_and_fix",
                label=label,
                description=description,
                target_url=bucket.direct_target,
                fix_surface=bucket.fix_surface,
                fixability=bucket.fixability,
                scope=bucket.scope,
                impact=bucket.impact,
                affected_count=bucket.affected_count,
                priority_score=bucket.priority_score,
                priority_reason=bucket.priority_reason,
                related_bucket_codes=[bucket.code],
            )
        )

    if not actions and summary.next_action:
        actions.append(
            MerchantReadinessAction(
                action_id="act_review_readiness",
                action_type="review",
                label="Review readiness",
                description=summary.next_action,
                target_url="/dashboard/product-optimization",
                fix_surface="product_content",
                fixability="merchant_fixable",
                scope="merchant",
                impact="discovery_only",
                affected_count=summary.blocked_variant_count,
                priority_score=float(max(summary.blocked_variant_count, 1) * 10),
                priority_reason="Review the current readiness plan before broader agent exposure.",
            )
        )

    deduped: list[MerchantReadinessAction] = []
    seen: set[tuple[str, str]] = set()
    for action in actions:
        key = (action.label, action.target_url)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(action)
    return deduped[:6]


def _recommended_actions(
    *,
    assessment_state: str,
    blocker_counts: Counter[str],
    blockers: list[str],
    warnings: list[str],
    blocked_variant_count: int,
    capability_status: dict[str, str],
) -> list[str]:
    if assessment_state == "disabled":
        return ["Turn on readiness assessment before enabling LLM commerce for this merchant."]
    if assessment_state == "not_assessed":
        return ["Run the merchant readiness assessment and review the remediation checklist."]

    actions: list[str] = []
    # First, because with no storefront there are no products, so every
    # product-shaped branch below is silent and the function used to fall all
    # the way through to "ready for supervised LLM commerce" — rendered on the
    # overview card directly beside a summary reading "currently blocked".
    if "store_connection_missing" in blockers:
        actions.append("Reconnect your store to restore this merchant's catalog.")
    elif "shopify_configuration_missing" in blockers:
        actions.append("Repair the store connection credentials so the catalog can sync.")
    if blocker_counts.get("missing_price") or blocker_counts.get("missing_currency"):
        actions.append("Fix pricing sync for variants with missing price or currency.")
    if blocker_counts.get("out_of_stock"):
        actions.append("Restock blocked variants or keep them excluded from AI checkout.")
    if blocker_counts.get("inventory_stale"):
        actions.append("Refresh inventory so checkout decisions use current stock.")
    if (
        "merchant_checkout_capability_missing" in blockers
        or capability_status.get("checkout_execution") == "blocked"
    ):
        actions.append("Connect checkout and payment processing before enabling AI checkout.")
    if (
        "merchant_shipping_policy_missing" in blockers
        or "merchant_return_policy_missing" in blockers
        or blocker_counts.get("missing_shipping_profile")
    ):
        actions.append("Complete shipping and returns setup for blocked products.")
    if blocker_counts.get("missing_primary_image") or blocker_counts.get("missing_title"):
        actions.append("Fill missing catalog fields such as product titles and primary images.")
    if (
        "merchant_writeback_unavailable" in blockers
        or capability_status.get("order_writeback_state_sync") == "blocked"
    ):
        actions.append("Repair order write-back and sync before enabling autonomous order actions.")
    if warnings and not actions:
        actions.append("Clear the remaining warnings and rerun readiness.")
    if blocked_variant_count > 0 and not actions:
        actions.append("Resolve the blocked variants before enabling checkout broadly.")
    if not actions:
        actions.append("This merchant is ready for supervised LLM commerce.")
    return actions[:3]


def _summary_text(
    *,
    assessment_state: str,
    tier: str,
    ready_variant_count: int,
    blocked_variant_count: int,
) -> str:
    if assessment_state == "disabled":
        return "Readiness assessment is currently turned off, so LLM commerce should stay disabled."
    if assessment_state == "not_assessed":
        return "This merchant has not been assessed yet, so LLM commerce should stay off until the first review is complete."
    if tier == "green":
        return f"This merchant is ready for supervised LLM commerce. {ready_variant_count} variants are ready and no variants are currently blocked."
    if tier == "yellow":
        return f"Most of the catalog is usable, but not all of it is safe to expose yet. {ready_variant_count} variants are ready and {blocked_variant_count} variants are still blocked."
    return f"This merchant is currently blocked for LLM commerce. {blocked_variant_count} variants are blocked and the critical setup issues need to be fixed first."


def _next_action(
    *,
    assessment_state: str,
    blockers: list[str],
    warnings: list[str],
    ready_variant_count: int,
    blocked_variant_count: int,
    capability_status: dict[str, str],
) -> str:
    if assessment_state == "disabled":
        return "Enable readiness assessment before using LLM commerce for this merchant."
    if assessment_state == "not_assessed":
        return "Run readiness assessment and merchant remediation before enabling LLM commerce."
    # Before the checkout branch: with no storefront there is nothing to connect
    # checkout TO, and `ready_variant_count == 0` would otherwise answer with
    # "make a variant discovery-ready" for a merchant who has no catalog.
    if "store_connection_missing" in blockers:
        return "Reconnect a storefront before this merchant can be assessed for LLM commerce."
    if "merchant_checkout_capability_missing" in blockers or capability_status.get("checkout_execution") == "blocked":
        return "Connect and verify checkout and payment execution before enabling checkout flows."
    if "merchant_writeback_unavailable" in blockers or capability_status.get("order_writeback_state_sync") == "blocked":
        return "Fix merchant write-back and order sync before allowing autonomous order actions."
    if "merchant_shipping_policy_missing" in blockers or "merchant_return_policy_missing" in blockers:
        return "Publish shipping and returns policy configuration for this merchant."
    if blocked_variant_count > 0:
        return "Resolve blocked variants before enabling checkout broadly."
    if ready_variant_count == 0:
        return "Make at least one variant discovery-ready before enabling LLM commerce."
    if warnings:
        return "Clear the remaining warnings and rerun readiness before broader rollout."
    return "Merchant can be enabled for supervised LLM commerce."


def summarize_readiness_snapshot(
    snapshot: MerchantReadinessSnapshot,
    *,
    channel: str = "ucp",
) -> ReadinessSummary:
    channel_coverage = next(
        (coverage for coverage in snapshot.channel_coverage if coverage.channel == channel),
        None,
    )
    ready_variant_count = int(channel_coverage.ready_variant_count if channel_coverage else 0)
    blocked_variant_count = int(channel_coverage.blocked_variant_count if channel_coverage else 0)
    blockers = _dedupe(snapshot.blockers)
    warnings = _dedupe(snapshot.warnings)
    blocker_counts = _variant_blocker_counts(snapshot)

    capability_status = dict(snapshot.capability_status or {})
    checkout_blocked = capability_status.get("checkout_execution") == "blocked"
    order_sync_blocked = capability_status.get("order_writeback_state_sync") == "blocked"
    critical_blockers = {
        "merchant_checkout_capability_missing",
        "merchant_writeback_unavailable",
        "merchant_shipping_policy_missing",
        "merchant_return_policy_missing",
    }

    if (
        snapshot.readiness_score >= 80
        and not checkout_blocked
        and not order_sync_blocked
        and blocked_variant_count == 0
        and not any(blocker in critical_blockers for blocker in snapshot.blockers)
    ):
        tier = "green"
    elif (
        snapshot.readiness_score < 50
        or ready_variant_count == 0
        or checkout_blocked
        or order_sync_blocked
        or any(blocker in critical_blockers for blocker in snapshot.blockers)
    ):
        tier = "red"
    else:
        tier = "yellow"

    recommended_actions = _recommended_actions(
        assessment_state="assessed",
        blocker_counts=blocker_counts,
        blockers=blockers,
        warnings=warnings,
        blocked_variant_count=blocked_variant_count,
        capability_status=capability_status,
    )

    return ReadinessSummary(
        tier=tier,
        label=_label_for_tier(tier),
        assessment_state="assessed",
        assessment_scope="one_merchant_alpha",
        channel=channel,
        score=snapshot.readiness_score,
        merchant_alpha_mode=snapshot.merchant_alpha_mode,
        ready_variant_count=ready_variant_count,
        blocked_variant_count=blocked_variant_count,
        top_blockers=blockers,
        top_warnings=warnings,
        summary_text=_summary_text(
            assessment_state="assessed",
            tier=tier,
            ready_variant_count=ready_variant_count,
            blocked_variant_count=blocked_variant_count,
        ),
        action_text=recommended_actions[0] if recommended_actions else None,
        recommended_actions=recommended_actions,
        blocker_breakdown=[
            {
                "code": code,
                "label": _humanize_code(code),
                "count": count,
            }
            for code, count in blocker_counts.most_common(3)
        ],
        capability_status=capability_status,
        generated_at=snapshot.generated_at,
        next_action=_next_action(
            assessment_state="assessed",
            blockers=blockers,
            warnings=warnings,
            ready_variant_count=ready_variant_count,
            blocked_variant_count=blocked_variant_count,
            capability_status=capability_status,
        ),
    )


def _fallback_summary(
    *,
    assessment_state: str,
    blocker: str,
    channel: str,
    merchant_alpha_mode: Optional[str] = None,
) -> ReadinessSummary:
    blockers = [blocker]
    recommended_actions = _recommended_actions(
        assessment_state=assessment_state,
        blocker_counts=Counter(),
        blockers=blockers,
        warnings=[],
        blocked_variant_count=0,
        capability_status={},
    )
    return ReadinessSummary(
        tier="red",
        label=_label_for_tier("red"),
        assessment_state=assessment_state,
        assessment_scope="one_merchant_alpha",
        channel=channel,
        score=None,
        merchant_alpha_mode=merchant_alpha_mode,
        ready_variant_count=0,
        blocked_variant_count=0,
        top_blockers=blockers,
        top_warnings=[],
        summary_text=_summary_text(
            assessment_state=assessment_state,
            tier="red",
            ready_variant_count=0,
            blocked_variant_count=0,
        ),
        action_text=recommended_actions[0] if recommended_actions else None,
        recommended_actions=recommended_actions,
        blocker_breakdown=[
            {
                "code": blocker,
                "label": _humanize_code(blocker),
                "count": 0,
            }
        ],
        capability_status={},
        generated_at=None,
        next_action=_next_action(
            assessment_state=assessment_state,
            blockers=blockers,
            warnings=[],
            ready_variant_count=0,
            blocked_variant_count=0,
            capability_status={},
        ),
    )


def _fallback_optimization_payload(summary: ReadinessSummary) -> MerchantReadinessOptimizationPayload:
    snapshot_id = _build_snapshot_id(
        MerchantReadinessSnapshot(
            merchant_id="unknown",
            merchant_name="unknown",
            channel=summary.channel,
            generated_at=summary.generated_at or "1970-01-01T00:00:00Z",
            merchant_alpha_mode=summary.merchant_alpha_mode or "summary_fallback",
            readiness_score=summary.score or 0,
            blockers=summary.top_blockers,
            warnings=summary.top_warnings,
        )
    )
    actions: list[MerchantReadinessAction] = []
    for action in summary.recommended_actions[:3]:
        actions.append(
            MerchantReadinessAction(
                action_id="act_review_readiness",
                action_type="review",
                label="Review readiness",
                description=action,
                target_url="/dashboard/product-optimization",
                fix_surface="product_content",
                fixability="merchant_fixable",
                scope="merchant",
                impact="full_agent_commerce",
                affected_count=summary.blocked_variant_count,
                priority_score=float(max(summary.blocked_variant_count, 1) * 10),
                priority_reason="Review the current readiness plan before broader agent exposure.",
                related_bucket_codes=["other"],
            )
        )

    issue_buckets = [
        ReadinessIssueBucket(
            code=item["code"],
            label=item["label"],
            severity="high",
            scope="merchant",
            affected_count=int(item.get("count", 0)),
            fix_surface="pivota_managed",
            fixability="pivota_managed",
            impact="full_agent_commerce",
            direct_target="/dashboard/product-optimization",
            priority_score=float(100 + int(item.get("count", 0))),
            priority_reason="Pivota needs to review this readiness issue before broader agent exposure.",
            reason_codes=[item["code"]],
        )
        for item in summary.blocker_breakdown
    ]
    plan = OptimizationPlan(
        plan_id=f"rdplan_fallback_{snapshot_id[-8:]}",
        snapshot_id=snapshot_id,
        workspace_version=_WORKSPACE_VERSION,
        priority_policy_version=_PRIORITY_POLICY_VERSION,
        refresh_state=_refresh_state(summary.generated_at),
        generated_at=summary.generated_at,
        expires_at=_plan_expiry(summary.generated_at),
        can_apply_actions=summary.assessment_state == "assessed",
        last_successful_rescore_at=summary.generated_at,
    )
    return MerchantReadinessOptimizationPayload(
        plan=plan,
        score_bundle=ScoreBundle(readiness_score=summary.score),
        readiness_summary=summary,
        issue_buckets=issue_buckets,
        merchant_actions=actions,
        product_queue=[],
        last_generated_at=summary.generated_at,
    )


async def build_readiness_summary(
    merchant_id: str,
    *,
    channel: str = "ucp",
) -> ReadinessSummary:
    snapshot, fallback = await _load_readiness_snapshot_or_summary(merchant_id, channel=channel)
    if fallback is not None or snapshot is None:
        return fallback or _fallback_summary(
            assessment_state="assessed",
            blocker="readiness_summary_unavailable",
            channel=channel,
            merchant_alpha_mode="assessment_error",
        )
    return summarize_readiness_snapshot(snapshot, channel=channel)


async def _render_readiness_optimization_payload(
    payload: MerchantReadinessOptimizationPayload,
    *,
    aux_context: Optional[dict[str, Any]],
    queue_mode: str,
    page: int,
    page_size: int,
    search: Optional[str],
    issue_bucket: Optional[str],
    push_status: str,
    blocked_only: bool,
    low_quality_only: bool,
    sort_by: str,
    segment: str = "all",
) -> MerchantReadinessOptimizationPayload:
    queue_mode = _normalize_queue_mode(queue_mode)
    page = _normalize_page(page)
    page_size = _normalize_page_size(page_size)
    push_status = _normalize_push_status(push_status)
    sort_by = _normalize_sort_by(sort_by)
    segment = _normalize_segment(segment)

    base_filtered = _filter_product_queue_items(
        payload.product_queue,
        search=search,
        issue_bucket=issue_bucket,
        push_status=push_status,
        blocked_only=blocked_only,
        low_quality_only=low_quality_only,
    )
    # Counts are over the set matching every filter EXCEPT segment, so the chips
    # show how the current view splits and stay stable as the merchant clicks one.
    payload.queue_segment_counts = _compute_segment_counts(base_filtered)
    if segment != "all":
        base_filtered = [
            item for item in base_filtered if _segment_for_queue_item(item) == segment
        ]
    filtered_queue = _sort_product_queue_items(base_filtered, sort_by=sort_by)

    applied_filters = ProductQueueAppliedFilters(
        search=_normalize_text(search) or None,
        issue_bucket=str(issue_bucket or "").strip() or None,
        push_status=push_status,
        blocked_only=bool(blocked_only),
        low_quality_only=bool(low_quality_only),
        sort_by=sort_by,
        segment=segment,
    )

    if queue_mode == "full":
        total_items = len(filtered_queue)
        payload.product_queue = filtered_queue
        payload.product_queue_page = ProductQueuePage(
            page=1,
            page_size=total_items,
            total_items=total_items,
            total_pages=1 if total_items > 0 else 0,
            has_next=False,
            has_prev=False,
            applied_filters=applied_filters,
        )
    else:
        page_meta = _build_product_queue_page(
            total_items=len(filtered_queue),
            page=page,
            page_size=page_size,
            search=search,
            issue_bucket=issue_bucket,
            push_status=push_status,
            blocked_only=blocked_only,
            low_quality_only=low_quality_only,
            sort_by=sort_by,
        )
        page_meta.applied_filters = applied_filters
        payload.product_queue_page = page_meta
        if queue_mode == "none":
            payload.product_queue = []
        else:
            start = (page_meta.page - 1) * page_meta.page_size
            end = start + page_meta.page_size
            payload.product_queue = filtered_queue[start:end]

    if payload.product_queue and aux_context:
        page_started = time.perf_counter()
        target_product_keys = {
            key
            for key in (
                make_product_key(item.platform, item.platform_product_id or item.product_id)
                for item in payload.product_queue
            )
            if key is not None
        }
        await _apply_catalog_health_projection(
            product_queue=payload.product_queue,
            current_products_by_key=aux_context.get("current_products_by_key", {}),
            parsed_products_by_key=aux_context.get("parsed_products_by_key", {}),
            store_context=aux_context.get("store_context", {}),
            include_title_preview=True,
            target_product_keys=target_product_keys,
        )
        logger.info(
            "readiness_optimization_page_materialization queue_mode=%s page=%s page_size=%s returned_items=%s catalog_health_projection_ms=%.2f",
            queue_mode,
            payload.product_queue_page.page if payload.product_queue_page else 1,
            payload.product_queue_page.page_size if payload.product_queue_page else len(payload.product_queue),
            len(payload.product_queue),
            round((time.perf_counter() - page_started) * 1000.0, 2),
        )

    return payload


async def build_readiness_optimization(
    merchant_id: str,
    *,
    force_refresh: bool = False,
    channel: str = "ucp",
    queue_mode: str = "full",
    page: int = 1,
    page_size: int = _DEFAULT_QUEUE_PAGE_SIZE,
    search: Optional[str] = None,
    issue_bucket: Optional[str] = None,
    push_status: str = "all",
    blocked_only: bool = False,
    low_quality_only: bool = False,
    sort_by: str = "default",
    segment: str = "all",
) -> MerchantReadinessOptimizationPayload:
    payload, _snapshot, aux_context = await get_readiness_optimization_context(
        merchant_id,
        channel=channel,
        force_refresh=force_refresh,
    )
    return await _render_readiness_optimization_payload(
        payload,
        aux_context=aux_context,
        queue_mode=queue_mode,
        page=page,
        page_size=page_size,
        search=search,
        issue_bucket=issue_bucket,
        push_status=push_status,
        blocked_only=blocked_only,
        low_quality_only=low_quality_only,
        sort_by=sort_by,
        segment=segment,
    )


async def _build_and_store_readiness_optimization_context(
    merchant_id: str,
    *,
    channel: str,
    force_snapshot_refresh: bool = False,
) -> tuple[MerchantReadinessOptimizationPayload, Optional[MerchantReadinessSnapshot], dict[str, Any]]:
    snapshot, fallback = await _load_readiness_snapshot_or_summary(
        merchant_id,
        channel=channel,
        force_snapshot_refresh=force_snapshot_refresh,
    )
    if fallback is not None or snapshot is None:
        return (
            _fallback_optimization_payload(
                fallback
                or _fallback_summary(
                    assessment_state="assessed",
                    blocker="readiness_summary_unavailable",
                    channel=channel,
                    merchant_alpha_mode="assessment_error",
                )
            ),
            None,
            {},
        )

    overall_started = time.perf_counter()
    stage_timings_ms: dict[str, float] = {}
    summary = summarize_readiness_snapshot(snapshot, channel=channel)
    stage_started = time.perf_counter()
    store_domains_by_platform = await _load_store_domains_by_platform(merchant_id)
    stage_timings_ms["store_domains"] = round((time.perf_counter() - stage_started) * 1000.0, 2)

    stage_started = time.perf_counter()
    full_product_queue = [
        _product_queue_item(
            product,
            snapshot.channel,
            store_domains_by_platform=store_domains_by_platform,
        )
        for product in snapshot.products
    ]
    stage_timings_ms["queue_base"] = round((time.perf_counter() - stage_started) * 1000.0, 2)

    stage_started = time.perf_counter()
    full_product_queue, agent_push_summary = _apply_agent_push_projection(
        snapshot_products=snapshot.products,
        product_queue=full_product_queue,
        checked_at=snapshot.generated_at,
    )
    stage_timings_ms["agent_push_projection"] = round((time.perf_counter() - stage_started) * 1000.0, 2)

    stage_started = time.perf_counter()
    catalog_projection_context = await _build_catalog_projection_context(
        merchant_id,
        snapshot_products=snapshot.products,
        include_field_facts=True,
    )
    stage_timings_ms["projection_context"] = round((time.perf_counter() - stage_started) * 1000.0, 2)

    stage_started = time.perf_counter()
    full_product_queue, quality_coverage = await _apply_quality_projection(
        merchant_id,
        snapshot_products=snapshot.products,
        product_queue=full_product_queue,
        cache_rows_by_key=catalog_projection_context["cache_rows_by_key"],
    )
    stage_timings_ms["quality_projection"] = round((time.perf_counter() - stage_started) * 1000.0, 2)

    stage_started = time.perf_counter()
    full_product_queue = await _apply_catalog_health_projection(
        product_queue=full_product_queue,
        current_products_by_key=catalog_projection_context["current_products_by_key"],
        parsed_products_by_key=catalog_projection_context["parsed_products_by_key"],
        store_context=catalog_projection_context["store_context"],
        include_title_preview=False,
    )
    stage_timings_ms["catalog_health_projection"] = round((time.perf_counter() - stage_started) * 1000.0, 2)

    stage_started = time.perf_counter()
    source_data_lanes = await _build_source_data_lanes(
        merchant_id,
        product_queue=full_product_queue,
        snapshot_products_by_key=catalog_projection_context["snapshot_products_by_key"],
        current_products_by_key=catalog_projection_context["current_products_by_key"],
        parsed_products_by_key=catalog_projection_context["parsed_products_by_key"],
        field_facts_by_key=catalog_projection_context["field_facts_by_key"],
        store_context=catalog_projection_context["store_context"],
    )
    stage_timings_ms["source_data_lanes"] = round((time.perf_counter() - stage_started) * 1000.0, 2)

    stage_started = time.perf_counter()
    issue_buckets = _build_issue_buckets(
        snapshot,
        product_queue=full_product_queue,
        source_data_lanes=source_data_lanes,
    )
    merchant_actions = _build_merchant_actions(summary, issue_buckets)
    stage_timings_ms["bucket_and_action_build"] = round((time.perf_counter() - stage_started) * 1000.0, 2)
    product_queue = [
        item
        for item in full_product_queue
        if _queue_item_has_workspace_signal(item)
    ]
    content_opportunity_count = sum(1 for item in product_queue if _is_content_only_opportunity(item))
    product_queue.sort(
        key=lambda item: (
            _is_content_only_opportunity(item),
            -item.priority_score,
            -item.blocked_variant_count,
            item.title.lower(),
        )
    )
    snapshot_id = _build_snapshot_id(snapshot)
    plan = OptimizationPlan(
        plan_id=_build_plan_id(
            snapshot_id,
            summary=summary,
            issue_buckets=issue_buckets,
            product_queue=product_queue,
            content_opportunity_count=content_opportunity_count,
            source_data_lanes=source_data_lanes,
        ),
        snapshot_id=snapshot_id,
        workspace_version=_WORKSPACE_VERSION,
        priority_policy_version=_PRIORITY_POLICY_VERSION,
        refresh_state=_refresh_state(summary.generated_at),
        generated_at=summary.generated_at,
        expires_at=_plan_expiry(summary.generated_at),
        can_apply_actions=summary.assessment_state == "assessed",
        last_successful_rescore_at=summary.generated_at,
    )
    payload = _store_optimization_payload(
        merchant_id,
        channel=channel,
        snapshot=snapshot,
        aux_context=catalog_projection_context,
        payload=MerchantReadinessOptimizationPayload(
            plan=plan,
            score_bundle=ScoreBundle(readiness_score=summary.score),
            readiness_summary=summary,
            issue_buckets=issue_buckets,
            merchant_actions=merchant_actions,
            product_queue=product_queue,
            content_opportunity_count=content_opportunity_count,
            source_data_lanes=source_data_lanes,
            quality_coverage=quality_coverage,
            agent_push_summary=agent_push_summary,
            last_generated_at=summary.generated_at,
        ),
    )
    logger.info(
        "readiness_optimization_profile merchant=%s channel=%s cache_hit=%s projection_context_ms=%s quality_projection_ms=%s catalog_health_projection_ms=%s source_data_lanes_ms=%s total_ms=%.2f queue_total=%s content_opportunities=%s",
        merchant_id,
        channel,
        False,
        stage_timings_ms.get("projection_context"),
        stage_timings_ms.get("quality_projection"),
        stage_timings_ms.get("catalog_health_projection"),
        stage_timings_ms.get("source_data_lanes"),
        round((time.perf_counter() - overall_started) * 1000.0, 2),
        len(payload.product_queue),
        payload.content_opportunity_count,
    )
    return payload, snapshot, catalog_projection_context


async def _refresh_optimization_cache_entry(
    merchant_id: str,
    *,
    channel: str,
    cache_key: str,
    reason: str,
) -> None:
    try:
        await _build_and_store_readiness_optimization_context(
            merchant_id,
            channel=channel,
            force_snapshot_refresh=True,
        )
        _OPTIMIZATION_CACHE_METRICS["background_refresh_successes"] += 1
        logger.info(
            "readiness_optimization_background_refresh merchant=%s channel=%s reason=%s status=success",
            merchant_id,
            channel,
            reason,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _OPTIMIZATION_CACHE_METRICS["background_refresh_failures"] += 1
        logger.warning(
            "readiness_optimization_background_refresh merchant=%s channel=%s reason=%s status=failed error=%s",
            merchant_id,
            channel,
            reason,
            str(exc)[:200],
        )
    finally:
        task = _OPTIMIZATION_REFRESH_TASKS.get(cache_key)
        if task is asyncio.current_task():
            _OPTIMIZATION_REFRESH_TASKS.pop(cache_key, None)


def _schedule_optimization_refresh(
    merchant_id: str,
    *,
    channel: str,
    reason: str,
) -> bool:
    cache_key = _optimization_cache_key(merchant_id, channel)
    existing_task = _OPTIMIZATION_REFRESH_TASKS.get(cache_key)
    if existing_task is not None and not existing_task.done():
        return False

    task = asyncio.create_task(
        _refresh_optimization_cache_entry(
            merchant_id,
            channel=channel,
            cache_key=cache_key,
            reason=reason,
        )
    )
    _OPTIMIZATION_REFRESH_TASKS[cache_key] = task
    _OPTIMIZATION_CACHE_METRICS["background_refreshes"] += 1
    return True


def schedule_readiness_optimization_warmup(
    merchant_id: str,
    *,
    channel: str = "ucp",
) -> bool:
    cache_entry = _optimization_cache_entry(merchant_id, channel=channel)
    if cache_entry is not None:
        cached_at, _payload, _snapshot, _aux_context = cache_entry
        if time.monotonic() - cached_at <= _OPTIMIZATION_CACHE_TTL_SECONDS:
            return False
    scheduled = _schedule_optimization_refresh(
        merchant_id,
        channel=channel,
        reason="warmup",
    )
    if scheduled:
        _OPTIMIZATION_CACHE_METRICS["warmup_scheduled"] += 1
    return scheduled


async def get_readiness_optimization_context(
    merchant_id: str,
    *,
    force_refresh: bool = False,
    channel: str = "ucp",
) -> tuple[MerchantReadinessOptimizationPayload, Optional[MerchantReadinessSnapshot], dict[str, Any]]:
    if force_refresh:
        invalidate_readiness_optimization_cache(merchant_id, channel=channel)
        _OPTIMIZATION_CACHE_METRICS["refreshes"] += 1

    cache_entry = _optimization_cache_entry(merchant_id, channel=channel)
    if cache_entry is not None:
        cached_at, payload, snapshot, aux_context = cache_entry
        age_seconds = time.monotonic() - cached_at
        if age_seconds <= _OPTIMIZATION_CACHE_TTL_SECONDS:
            _OPTIMIZATION_CACHE_METRICS["hits"] += 1
            return (
                payload.model_copy(deep=True),
                snapshot.model_copy(deep=True) if snapshot is not None else None,
                dict(aux_context or {}),
            )
        _OPTIMIZATION_CACHE_METRICS["expired"] += 1
        _OPTIMIZATION_CACHE_METRICS["stale_served"] += 1
        _schedule_optimization_refresh(
            merchant_id,
            channel=channel,
            reason="ttl_expired",
        )
        return (
            payload.model_copy(deep=True),
            snapshot.model_copy(deep=True) if snapshot is not None else None,
            dict(aux_context or {}),
        )

    inflight_task = _optimization_refresh_task(merchant_id, channel=channel)
    if inflight_task is not None and not force_refresh:
        _OPTIMIZATION_CACHE_METRICS["coalesced_waits"] += 1
        await inflight_task
        cache_entry = _optimization_cache_entry(merchant_id, channel=channel)
        if cache_entry is not None:
            _cached_at, payload, snapshot, aux_context = cache_entry
            return (
                payload.model_copy(deep=True),
                snapshot.model_copy(deep=True) if snapshot is not None else None,
                dict(aux_context or {}),
            )

    _OPTIMIZATION_CACHE_METRICS["misses"] += 1
    return await _build_and_store_readiness_optimization_context(
        merchant_id,
        channel=channel,
        force_snapshot_refresh=force_refresh,
    )
