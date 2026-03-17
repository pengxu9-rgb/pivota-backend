from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from db.database import database
from db.reviews_center import product_reviews, review_featured, review_group, review_group_membership
from models.standard_product import StandardProduct
from services.reviews_service import GLOBAL_IMPORT_MERCHANT_ID, build_product_key

logger = logging.getLogger(__name__)


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


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        return mapping.get(key, default)
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        return default


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


def _aggregate_row_to_dict(row: Any) -> Dict[str, Any]:
    rated_total = int(_row_get(row, "rated_total", 0) or 0)
    rating_sum = float(_row_get(row, "rating_sum", 0.0) or 0.0)
    latest_review_at = _iso(_row_get(row, "latest_review_at"))
    return {
        "review_count": int(_row_get(row, "review_count", 0) or 0),
        "rating_count": rated_total,
        "rating_sum": rating_sum,
        "average_rating": round(rating_sum / rated_total, 2) if rated_total > 0 else None,
        "verified_review_count": int(_row_get(row, "verified_review_count", 0) or 0),
        "latest_review_at": latest_review_at,
    }


def _append_warning(
    warnings: List[str],
    audit_notes: List[str],
    *,
    code: str,
    audit_note: str,
) -> None:
    if code not in warnings:
        warnings.append(code)
    audit_notes.append(audit_note)


def _build_in_clause(param_prefix: str, values: Iterable[Any]) -> Tuple[str, Dict[str, Any]]:
    params: Dict[str, Any] = {}
    placeholders: List[str] = []
    for idx, value in enumerate(values):
        key = f"{param_prefix}_{idx}"
        params[key] = value
        placeholders.append(f":{key}")
    return ", ".join(placeholders) or "NULL", params


def _combine_review_totals(*totals: Dict[str, Any]) -> Dict[str, Any]:
    latest_candidates = [_parse_datetime(item.get("latest_review_at")) for item in totals if item]
    latest_review_at = max((item for item in latest_candidates if item is not None), default=None)
    review_count = sum(int(item.get("review_count", 0) or 0) for item in totals if item)
    rating_count = sum(int(item.get("rating_count", 0) or 0) for item in totals if item)
    rating_sum = sum(float(item.get("rating_sum", 0.0) or 0.0) for item in totals if item)
    verified_review_count = sum(int(item.get("verified_review_count", 0) or 0) for item in totals if item)
    return {
        "review_count": review_count,
        "rating_count": rating_count,
        "rating_sum": rating_sum,
        "average_rating": round(rating_sum / rating_count, 2) if rating_count > 0 else None,
        "verified_review_count": verified_review_count,
        "latest_review_at": _iso(latest_review_at),
    }


def _build_review_aggregate_query_sql(
    *,
    bucket_expression: str,
    param_prefix: str,
    values: Iterable[Any],
) -> Tuple[str, Dict[str, Any]]:
    in_clause, params = _build_in_clause(param_prefix, values)
    sql = f"""
    SELECT {bucket_expression} AS bucket,
           COUNT(id)::int AS review_count,
           SUM(CASE WHEN rating IS NOT NULL AND rating > 0 THEN 1 ELSE 0 END)::int AS rated_total,
           SUM(CASE WHEN rating IS NOT NULL AND rating > 0 THEN rating ELSE 0 END)::float AS rating_sum,
           SUM(CASE WHEN verification IN ('verified_purchase', 'partner_verified') THEN 1 ELSE 0 END)::int AS verified_review_count,
           MAX(created_at) AS latest_review_at
    FROM product_reviews
    WHERE status = 'active'
      AND {bucket_expression} IN ({in_clause})
    GROUP BY {bucket_expression}
    """
    return sql, params


async def load_product_review_summaries(
    *,
    merchant_id: str,
    platform: str,
    products: Iterable[StandardProduct],
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any], List[str], List[str]]:
    products_list = list(products)
    observed_at = _iso(datetime.now(timezone.utc))
    if not products_list:
        return {}, {
            "integration_status": "ready",
            "observed_at": observed_at,
            "products_with_reviews": 0,
            "grouped_products_with_reviews": 0,
            "products_without_reviews": 0,
        }, [], []

    product_keys_by_id = {
        str(product.id): build_product_key(
            merchant_id=merchant_id,
            platform=platform,
            platform_product_id=str(product.id),
        )
        for product in products_list
    }
    global_product_keys_by_id = {
        str(product.id): build_product_key(
            merchant_id=GLOBAL_IMPORT_MERCHANT_ID,
            platform=platform,
            platform_product_id=str(product.id),
        )
        for product in products_list
    }

    warnings: List[str] = []
    audit_notes: List[str] = []
    summaries: Dict[str, Dict[str, Any]] = {}

    try:
        membership_by_product_key: Dict[str, Dict[str, Any]] = {}
        group_aggregates: Dict[int, Dict[str, Any]] = {}
        featured_counts: Dict[int, int] = {}
        product_aggregates: Dict[str, Dict[str, Any]] = {}
        group_lookup_available = False
        product_lookup_available = False

        try:
            product_key_clause, membership_params = _build_in_clause("membership_product_key", product_keys_by_id.values())
            membership_rows = await database.fetch_all(
                f"""
                SELECT m.product_key,
                       m.group_id,
                       m.confidence AS membership_confidence,
                       g.group_key,
                       g.confidence AS group_confidence
                FROM review_group_membership m
                JOIN review_group g ON g.id = m.group_id
                WHERE m.status = 'active'
                  AND g.status = 'active'
                  AND m.product_key IN ({product_key_clause})
                ORDER BY m.product_key ASC, m.confidence DESC, m.updated_at DESC, m.id DESC
                """,
                membership_params,
            )
        except Exception:
            logger.warning("Review-group membership lookup failed for merchant=%s", merchant_id, exc_info=True)
            _append_warning(
                warnings,
                audit_notes,
                code="reviews_group_membership_lookup_failed",
                audit_note="Readiness alpha could not resolve review-group membership and fell back to direct product-review summaries where available.",
            )
            membership_rows = []

        for row in membership_rows:
            product_key = str(_row_get(row, "product_key") or "").strip()
            if product_key and product_key not in membership_by_product_key:
                membership_by_product_key[product_key] = {
                    "group_id": int(_row_get(row, "group_id")),
                    "group_key": str(_row_get(row, "group_key") or "").strip() or None,
                    "group_confidence": float(_row_get(row, "group_confidence") or 0.0),
                    "membership_confidence": float(_row_get(row, "membership_confidence") or 0.0),
                }

        group_ids = sorted(
            {
                int(item["group_id"])
                for item in membership_by_product_key.values()
                if item.get("group_id") is not None
            }
        )

        if group_ids:
            try:
                group_sql, group_params = _build_review_aggregate_query_sql(
                    bucket_expression="group_id",
                    param_prefix="group_id",
                    values=group_ids,
                )
                group_rows = await database.fetch_all(group_sql, group_params)
                group_lookup_available = True
                group_aggregates = {
                    int(_row_get(row, "bucket")): _aggregate_row_to_dict(row)
                    for row in group_rows
                    if _row_get(row, "bucket") is not None
                }
            except Exception:
                logger.warning("Review-group aggregate lookup failed for merchant=%s", merchant_id, exc_info=True)
                _append_warning(
                    warnings,
                    audit_notes,
                    code="reviews_group_aggregate_lookup_failed",
                    audit_note="Readiness alpha could not aggregate review-group totals and fell back to direct product-review summaries where available.",
                )
                group_aggregates = {}

            try:
                group_id_clause, featured_params = _build_in_clause("featured_group_id", group_ids)
                featured_rows = await database.fetch_all(
                    f"""
                    SELECT group_id AS bucket,
                           COUNT(review_id)::int AS featured_review_count
                    FROM review_featured
                    WHERE group_id IN ({group_id_clause})
                    GROUP BY group_id
                    """,
                    featured_params,
                )
                featured_counts = {
                    int(_row_get(row, "bucket")): int(_row_get(row, "featured_review_count", 0) or 0)
                    for row in featured_rows
                }
            except Exception:
                logger.warning("Featured review lookup failed for merchant=%s", merchant_id, exc_info=True)
                _append_warning(
                    warnings,
                    audit_notes,
                    code="reviews_featured_lookup_failed",
                    audit_note="Readiness alpha could not hydrate featured-review counts and continued with featured counts set to zero.",
                )
                featured_counts = {}

        all_product_keys = list(product_keys_by_id.values()) + list(global_product_keys_by_id.values())
        try:
            product_sql, product_params = _build_review_aggregate_query_sql(
                bucket_expression="product_key",
                param_prefix="product_key",
                values=all_product_keys,
            )
            product_rows = await database.fetch_all(product_sql, product_params)
            product_lookup_available = True
            product_aggregates = {
                str(_row_get(row, "bucket") or "").strip(): _aggregate_row_to_dict(row)
                for row in product_rows
                if str(_row_get(row, "bucket") or "").strip()
            }
        except Exception:
            logger.warning("Product-review aggregate lookup failed for merchant=%s", merchant_id, exc_info=True)
            _append_warning(
                warnings,
                audit_notes,
                code="reviews_product_summary_lookup_failed",
                audit_note="Readiness alpha could not aggregate direct product-review summaries for this merchant.",
            )
            product_aggregates = {}

        if not group_lookup_available and not product_lookup_available:
            return {}, {
                "integration_status": "blocked",
                "observed_at": observed_at,
                "products_with_reviews": 0,
                "grouped_products_with_reviews": 0,
                "products_without_reviews": len(products_list),
            }, warnings + ["reviews_summary_lookup_failed"], audit_notes + [
                "Readiness alpha could not hydrate Reviews Center summaries for this merchant."
            ]

        for product in products_list:
            product_id = str(product.id)
            product_key = product_keys_by_id[product_id]
            global_product_key = global_product_keys_by_id[product_id]
            membership = membership_by_product_key.get(product_key) or {}
            group_id = membership.get("group_id")
            group_summary = group_aggregates.get(int(group_id)) if group_id is not None else None
            local_summary = product_aggregates.get(product_key) or {}
            global_summary = product_aggregates.get(global_product_key) or {}
            fallback_summary = _combine_review_totals(local_summary, global_summary)
            active_summary = group_summary if group_summary and int(group_summary.get("review_count", 0) or 0) > 0 else fallback_summary

            has_reviews = int(active_summary.get("review_count", 0) or 0) > 0
            uses_group = bool(group_summary and int(group_summary.get("review_count", 0) or 0) > 0)
            source = "reviews_center.review_group.v1" if uses_group else "reviews_center.product_reviews.v1"
            summaries[product_id] = {
                "scope": "product",
                "source": source,
                "default_view": "group" if uses_group else "merchant" if has_reviews else "none",
                "has_group": bool(group_id is not None),
                "has_reviews": has_reviews,
                "group_id": int(group_id) if group_id is not None else None,
                "group_key": membership.get("group_key"),
                "group_confidence": membership.get("group_confidence"),
                "membership_confidence": membership.get("membership_confidence"),
                "review_count": int(active_summary.get("review_count", 0) or 0),
                "rating_count": int(active_summary.get("rating_count", 0) or 0),
                "average_rating": active_summary.get("average_rating"),
                "verified_review_count": int(active_summary.get("verified_review_count", 0) or 0),
                "featured_review_count": int(featured_counts.get(int(group_id), 0) if group_id is not None else 0),
                "latest_review_at": active_summary.get("latest_review_at"),
            }

        grouped_products_with_reviews = sum(
            1 for summary in summaries.values() if summary.get("has_group") and summary.get("has_reviews")
        )
        products_with_reviews = sum(1 for summary in summaries.values() if summary.get("has_reviews"))
        products_without_reviews = len(products_list) - products_with_reviews
        if products_with_reviews:
            audit_notes.append(
                "Readiness alpha now projects product-level review summaries from Reviews Center using review-group matches first and merchant/global fallbacks second."
            )
        else:
            warnings.append("reviews_present_but_no_product_summaries_found")

        return summaries, {
            "integration_status": "ready",
            "observed_at": observed_at,
            "products_with_reviews": products_with_reviews,
            "grouped_products_with_reviews": grouped_products_with_reviews,
            "products_without_reviews": products_without_reviews,
        }, warnings, audit_notes
    except Exception:
        logger.warning("Review summary lookup failed for merchant=%s", merchant_id, exc_info=True)
        return {}, {
            "integration_status": "blocked",
            "observed_at": observed_at,
            "products_with_reviews": 0,
            "grouped_products_with_reviews": 0,
            "products_without_reviews": len(products_list),
        }, ["reviews_summary_lookup_failed"], [
            "Readiness alpha could not hydrate Reviews Center summaries for this merchant."
        ]
