from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import logging
from typing import Any, Dict, Iterable, Optional

from db.database import database
from db.product_enrichment import get_enrichments_for_products
from db.product_quality import product_quality_snapshot
from db.product_quality_backfill_jobs import get_active_quality_backfill_job
from db.products import products_cache
import time
from sqlalchemy import select
from readiness.flags import (
    readiness_alpha_merchant_id,
    readiness_real_merchant_alpha_enabled,
    readiness_router_enabled,
)
from readiness.models import (
    DashboardSnapshot,
    MerchantReadinessAction,
    MerchantReadinessOptimizationPayload,
    MerchantReadinessSnapshot,
    OptimizationPlan,
    ProductQueueIssue,
    ProductReadinessQueueItem,
    QualityCoverageSummary,
    ReadinessIssueBucket,
    ReadinessSummary,
    ScoreBundle,
)
from readiness.service import UnsupportedMerchantError, build_readiness_snapshot
from services.product_quality_service import (
    QUALITY_SOURCE_NONE,
    build_quality_payload_from_cache_row,
    build_quality_projection,
    fetch_latest_quality_rows,
    make_product_key,
    summarize_quality_coverage,
)

logger = logging.getLogger(__name__)

_WORKSPACE_VERSION = "agent_commerce_optimization.v1"
_PRIORITY_POLICY_VERSION = "merchant_readiness_priority.v1"
_PLAN_TTL_HOURS = 6
_PAID_PAYMENT_STATUSES_SQL = "('paid','completed','succeeded','success','settled','partially_refunded')"
_OPTIMIZATION_CACHE_TTL_SECONDS = 60.0


_OPTIMIZATION_CACHE: dict[str, tuple[float, MerchantReadinessOptimizationPayload]] = {}
_OPTIMIZATION_CACHE_METRICS: dict[str, int] = {
    "hits": 0,
    "misses": 0,
    "stores": 0,
    "expired": 0,
    "refreshes": 0,
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
        "direct_target": "/dashboard/integrations",
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
    "merchant_checkout_capability_missing": "checkout_payment_setup",
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
        "merchant_checkout_capability_missing": "Checkout not connected",
        "merchant_writeback_unavailable": "Order sync unavailable",
        "reviews_summary_unavailable": "Reviews summary unavailable",
        "cross_merchant_review_group_unresolved": "Cross-merchant review grouping incomplete",
        "review_coverage_partial": "Review coverage partial",
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
    if merchant_id is None and channel is None:
        removed = len(_OPTIMIZATION_CACHE)
        _OPTIMIZATION_CACHE.clear()
        if removed:
            _OPTIMIZATION_CACHE_METRICS["invalidations"] += 1
            _OPTIMIZATION_CACHE_METRICS["invalidated_entries"] += removed
        return removed

    keys_to_drop = []
    for key in _OPTIMIZATION_CACHE:
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
    return len(keys_to_drop)


def reset_readiness_optimization_cache_observability() -> None:
    _OPTIMIZATION_CACHE.clear()
    for key in list(_OPTIMIZATION_CACHE_METRICS.keys()):
        _OPTIMIZATION_CACHE_METRICS[key] = 0


def get_readiness_optimization_cache_metrics() -> dict[str, Any]:
    total_requests = _OPTIMIZATION_CACHE_METRICS["hits"] + _OPTIMIZATION_CACHE_METRICS["misses"]
    hit_rate = (_OPTIMIZATION_CACHE_METRICS["hits"] / total_requests * 100.0) if total_requests else 0.0
    now_mono = time.monotonic()
    entries = []
    for key, (cached_at, payload) in sorted(_OPTIMIZATION_CACHE.items()):
        merchant_id, channel = key.split("|", 1)
        age_seconds = max(0.0, now_mono - cached_at)
        entries.append(
            {
                "merchant_id": merchant_id,
                "channel": channel,
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
        "refreshes": _OPTIMIZATION_CACHE_METRICS["refreshes"],
        "invalidations": _OPTIMIZATION_CACHE_METRICS["invalidations"],
        "invalidated_entries": _OPTIMIZATION_CACHE_METRICS["invalidated_entries"],
        "total_requests": total_requests,
        "hit_rate": round(hit_rate, 2),
        "entries": len(_OPTIMIZATION_CACHE),
        "ttl_seconds": _OPTIMIZATION_CACHE_TTL_SECONDS,
        "active_keys": entries,
    }


def _cached_optimization_payload(
    merchant_id: str,
    *,
    channel: str,
) -> Optional[MerchantReadinessOptimizationPayload]:
    cache_key = _optimization_cache_key(merchant_id, channel)
    entry = _OPTIMIZATION_CACHE.get(cache_key)
    if not entry:
        _OPTIMIZATION_CACHE_METRICS["misses"] += 1
        return None
    cached_at, payload = entry
    if time.monotonic() - cached_at > _OPTIMIZATION_CACHE_TTL_SECONDS:
        _OPTIMIZATION_CACHE.pop(cache_key, None)
        _OPTIMIZATION_CACHE_METRICS["misses"] += 1
        _OPTIMIZATION_CACHE_METRICS["expired"] += 1
        return None
    _OPTIMIZATION_CACHE_METRICS["hits"] += 1
    return payload.model_copy(deep=True)


def _store_optimization_payload(
    merchant_id: str,
    *,
    channel: str,
    payload: MerchantReadinessOptimizationPayload,
) -> MerchantReadinessOptimizationPayload:
    cache_key = _optimization_cache_key(merchant_id, channel)
    _OPTIMIZATION_CACHE[cache_key] = (time.monotonic(), payload.model_copy(deep=True))
    _OPTIMIZATION_CACHE_METRICS["stores"] += 1
    return payload


def _severity_for_bucket(bucket_code: str, affected_count: int) -> str:
    if bucket_code in {"checkout_payment_setup", "shipping_returns_setup", "order_sync_operations"}:
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


async def _load_latest_quality_map(merchant_id: str) -> dict[str, dict[str, Any]]:
    query = (
        select(
            product_quality_snapshot.c.platform,
            product_quality_snapshot.c.platform_product_id,
            product_quality_snapshot.c.snapshot_date,
            product_quality_snapshot.c.content_quality_score,
            product_quality_snapshot.c.model_readiness_score,
            product_quality_snapshot.c.conversion_potential_score,
        )
        .where(product_quality_snapshot.c.merchant_id == merchant_id)
        .order_by(
            product_quality_snapshot.c.platform,
            product_quality_snapshot.c.platform_product_id,
            product_quality_snapshot.c.snapshot_date.desc(),
        )
    )
    rows = await database.fetch_all(query)
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        payload = dict(row)
        key = f"{payload.get('platform') or 'unknown'}|{payload.get('platform_product_id') or ''}"
        if key in latest:
            continue
        snapshot_date = payload.get("snapshot_date")
        latest[key] = {
            "content_quality_score": payload.get("content_quality_score"),
            "model_readiness_score": payload.get("model_readiness_score"),
            "conversion_potential_score": payload.get("conversion_potential_score"),
            "quality_last_evaluated_at": snapshot_date.isoformat() if snapshot_date else None,
        }
    return latest


async def _load_dashboard_snapshot(merchant_id: str) -> DashboardSnapshot:
    analytics_query = """
        SELECT
            COUNT(*) as total_orders_all_time,
            COALESCE(SUM(total), 0) as gmv_all_time,
            COALESCE(SUM(CASE WHEN payment_status IN """ + _PAID_PAYMENT_STATUSES_SQL + """ THEN total ELSE 0 END), 0) as confirmed_revenue_all_time,
            SUM(CASE WHEN payment_status IN """ + _PAID_PAYMENT_STATUSES_SQL + """ THEN 1 ELSE 0 END) as paid_orders_all_time,
            COUNT(DISTINCT customer_email) as total_customers,
            SUM(CASE WHEN created_at >= CURRENT_DATE - INTERVAL '30 days' THEN 1 ELSE 0 END) as orders_last_30_days,
            SUM(CASE WHEN created_at >= CURRENT_DATE - INTERVAL '30 days' AND payment_status IN """ + _PAID_PAYMENT_STATUSES_SQL + """ THEN 1 ELSE 0 END) as paid_orders_last_30_days,
            COALESCE(SUM(CASE WHEN created_at >= CURRENT_DATE - INTERVAL '30 days' AND payment_status IN """ + _PAID_PAYMENT_STATUSES_SQL + """ THEN total ELSE 0 END), 0) as confirmed_revenue_last_30_days
        FROM orders
        WHERE merchant_id = :merchant_id AND (is_deleted IS NULL OR is_deleted = FALSE)
    """
    growth_query = """
        SELECT
            COUNT(CASE WHEN created_at >= CURRENT_DATE - INTERVAL '60 days'
                      AND created_at < CURRENT_DATE - INTERVAL '30 days' THEN 1 END) as orders_prev_30,
            COALESCE(SUM(CASE WHEN created_at >= CURRENT_DATE - INTERVAL '60 days'
                    AND created_at < CURRENT_DATE - INTERVAL '30 days'
                    AND payment_status IN """ + _PAID_PAYMENT_STATUSES_SQL + """ THEN total ELSE 0 END), 0) as confirmed_revenue_prev_30
        FROM orders
        WHERE merchant_id = :merchant_id AND (is_deleted IS NULL OR is_deleted = FALSE)
    """
    products_query = """
        SELECT COUNT(*) as count
        FROM products_cache
        WHERE merchant_id = :merchant_id
          AND (expires_at IS NULL OR expires_at > NOW())
    """

    def _to_int(value: Any) -> int:
        try:
            if value is None:
                return 0
            return int(value)
        except (TypeError, ValueError):
            return 0

    try:
        analytics = await database.fetch_one(analytics_query, {"merchant_id": merchant_id})
        growth = await database.fetch_one(growth_query, {"merchant_id": merchant_id})
        products = await database.fetch_one(products_query, {"merchant_id": merchant_id})

        total_orders = _to_int(analytics["orders_last_30_days"]) if analytics else 0
        paid_orders = _to_int(analytics["paid_orders_last_30_days"]) if analytics else 0
        total_revenue = float(analytics["confirmed_revenue_last_30_days"] or 0) if analytics else 0.0
        total_customers = _to_int(analytics["total_customers"]) if analytics else 0
        total_products = _to_int(products["count"]) if products else 0

        orders_prev_30 = _to_int(growth["orders_prev_30"]) if growth else 0
        confirmed_revenue_prev_30 = float(growth["confirmed_revenue_prev_30"] or 0) if growth else 0.0

        order_growth = 0.0
        revenue_growth = 0.0
        if orders_prev_30 > 0:
            order_growth = round(((total_orders - orders_prev_30) / orders_prev_30) * 100, 1)
        elif total_orders > 0:
            order_growth = 100.0

        if confirmed_revenue_prev_30 > 0:
            revenue_growth = round(((total_revenue - confirmed_revenue_prev_30) / confirmed_revenue_prev_30) * 100, 1)
        elif total_revenue > 0:
            revenue_growth = 100.0

        return DashboardSnapshot(
            total_orders=total_orders,
            paid_orders=paid_orders,
            total_revenue=total_revenue,
            total_customers=total_customers,
            total_products=total_products,
            order_growth=order_growth,
            revenue_growth=revenue_growth,
        )
    except Exception:
        try:
            products = await database.fetch_one(products_query, {"merchant_id": merchant_id})
            total_products = _to_int(products["count"]) if products else 0
        except Exception:
            total_products = 0
        return DashboardSnapshot(total_products=total_products)


def _queue_price(product: Any) -> tuple[Optional[float], Optional[str]]:
    for variant in product.variants:
        price = variant.price or {}
        raw_value = price.get("amount")
        if raw_value is None:
            raw_value = price.get("value")
        currency = str(price.get("currency") or "USD").strip() or "USD"
        if raw_value is None:
            continue
        try:
            return float(raw_value), currency
        except (TypeError, ValueError):
            continue
    return None, None


def _product_queue_item(
    product: Any,
    channel: str,
    *,
    quality_by_product: Optional[dict[str, dict[str, Any]]] = None,
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
    fixability = _fixability_for_surface(primary_bucket["fix_surface"])
    priority_score = _product_priority_score(
        blocked_variant_count=blocked_variant_count,
        impact=impact,
        fix_surface=primary_bucket["fix_surface"],
        affected_variant_count=affected_variant_count,
    )
    priority_reason = _product_priority_reason(
        blocked_variant_count=blocked_variant_count,
        impact=impact,
    )
    price_value, price_currency = _queue_price(product)
    quality = (quality_by_product or {}).get(
        f"{product.platform or 'unknown'}|{product.product_id}"
    ) or {}

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
        price_value=price_value,
        price_currency=price_currency,
        content_quality_score=quality.get("content_quality_score"),
        model_readiness_score=quality.get("model_readiness_score"),
        conversion_potential_score=quality.get("conversion_potential_score"),
        quality_last_evaluated_at=quality.get("quality_last_evaluated_at"),
        blocked_variant_count=blocked_variant_count,
        ready_variant_count=ready_variant_count,
        top_issues=top_issues,
        primary_action=primary_action,
        fix_surface=primary_bucket["fix_surface"],
        fixability=fixability,
        impact=impact,
        priority_score=priority_score,
        priority_reason=priority_reason,
        recommended_action_id=f"act_product:{product.platform or 'unknown'}:{product.product_id}",
        recommended_action_type="run_product_enrichment" if primary_bucket["fix_surface"] == "product_content" else "review_catalog_data",
    )


async def _load_cache_rows_for_product_keys(
    merchant_id: str,
    product_keys: list[tuple[str, str]],
) -> Dict[tuple[str, str], Dict[str, Any]]:
    if not product_keys:
        return {}

    platforms = sorted({platform for platform, _ in product_keys})
    product_key_set = set(product_keys)

    query = (
        products_cache.select()
        .where(products_cache.c.merchant_id == merchant_id)
        .where(products_cache.c.platform.in_(platforms))
        .where(products_cache.c.expires_at > datetime.now())
        .order_by(products_cache.c.cached_at.desc())
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


async def _apply_quality_projection(
    merchant_id: str,
    *,
    snapshot_products: list[Any],
    product_queue: list[ProductReadinessQueueItem],
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

        cache_rows_by_key = await _load_cache_rows_for_product_keys(merchant_id, product_keys)
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


def _build_issue_buckets(snapshot: MerchantReadinessSnapshot) -> list[ReadinessIssueBucket]:
    bucket_reason_counts: dict[str, Counter[str]] = {}
    bucket_affected_counts: Counter[str] = Counter()

    for code in snapshot.blockers:
        bucket = _bucket_code_for_reason(code)
        bucket_reason_counts.setdefault(bucket, Counter())[code] += 1
        bucket_affected_counts[bucket] += 1

    for code in snapshot.warnings:
        bucket = _bucket_code_for_reason(code)
        bucket_reason_counts.setdefault(bucket, Counter())[code] += 1

    for product in snapshot.products:
        issue_counts = _product_issue_counts(product, snapshot.channel)
        for code, count in issue_counts.items():
            bucket = _bucket_code_for_reason(code)
            bucket_reason_counts.setdefault(bucket, Counter())[code] += count
            bucket_affected_counts[bucket] += count

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


def _fallback_optimization_payload(
    summary: ReadinessSummary,
    *,
    dashboard_snapshot: Optional[DashboardSnapshot] = None,
) -> MerchantReadinessOptimizationPayload:
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
        dashboard_snapshot=dashboard_snapshot,
        issue_buckets=issue_buckets,
        merchant_actions=actions,
        product_queue=[],
        last_generated_at=summary.generated_at,
    )


async def _load_readiness_snapshot_or_summary(
    merchant_id: str,
    *,
    channel: str,
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


async def build_readiness_optimization(
    merchant_id: str,
    *,
    channel: str = "ucp",
    force_refresh: bool = False,
) -> MerchantReadinessOptimizationPayload:
    if force_refresh:
        _OPTIMIZATION_CACHE_METRICS["refreshes"] += 1
    else:
        cached_payload = _cached_optimization_payload(merchant_id, channel=channel)
        if cached_payload is not None:
            return cached_payload

    dashboard_snapshot = await _load_dashboard_snapshot(merchant_id)
    snapshot, fallback = await _load_readiness_snapshot_or_summary(merchant_id, channel=channel)
    if fallback is not None or snapshot is None:
        payload = _fallback_optimization_payload(
            fallback
            or _fallback_summary(
                assessment_state="assessed",
                blocker="readiness_summary_unavailable",
                channel=channel,
                merchant_alpha_mode="assessment_error",
            ),
            dashboard_snapshot=dashboard_snapshot,
        )
        return _store_optimization_payload(merchant_id, channel=channel, payload=payload)

    summary = summarize_readiness_snapshot(snapshot, channel=channel)
    issue_buckets = _build_issue_buckets(snapshot)
    merchant_actions = _build_merchant_actions(summary, issue_buckets)
    product_queue = [_product_queue_item(product, snapshot.channel) for product in snapshot.products]
    product_queue, quality_coverage = await _apply_quality_projection(
        merchant_id,
        snapshot_products=snapshot.products,
        product_queue=product_queue,
    )
    product_queue.sort(
        key=lambda item: (
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
    payload = MerchantReadinessOptimizationPayload(
        plan=plan,
        score_bundle=ScoreBundle(readiness_score=summary.score),
        readiness_summary=summary,
        dashboard_snapshot=dashboard_snapshot,
        issue_buckets=issue_buckets,
        merchant_actions=merchant_actions,
        product_queue=product_queue,
        quality_coverage=quality_coverage,
        last_generated_at=summary.generated_at,
    )
    return _store_optimization_payload(merchant_id, channel=channel, payload=payload)
