"""
Product quality scoring service.

V1 scope:
- Lightweight, rule-based preview scoring on a partial product payload
  for realtime Merchant Portal feedback.
- Simple full eval that reuses the same rules and persists a snapshot
  for later analytics / ranking.

The scoring here is intentionally simple and fast. A fuller scoring
pipeline (with behavior data and models) can be added separately.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import func, select

from db.database import database
from db.product_quality import product_quality_snapshot
from models.standard_product import StandardProduct
from utils.rich_text import rich_text_to_plain_text

ProductKey = Tuple[str, str]

logger = logging.getLogger(__name__)

QUALITY_SOURCE_SNAPSHOT = "snapshot"
QUALITY_SOURCE_PREVIEW = "preview"
QUALITY_SOURCE_NONE = "none"
DEFAULT_QUALITY_RULES_VERSION = "v1-lite"
SOURCE_BACKED_COMPONENTS_RULES_VERSION = "v1-source-backed-components"
SOURCE_BACKED_OPTIONAL_COMPONENTS_FLAG = (
    "PDP_QUALITY_SCORE_SOURCE_BACKED_OPTIONAL_COMPONENTS"
)

_QUALITY_SCORE_FIELDS = (
    "content_quality_score",
    "model_readiness_score",
    "conversion_potential_score",
)


def _text_length_score(text: str, min_len: int, max_len: int) -> float:
    """
    Score text length in [0, 1].

    - 0 if empty
    - best when between min_len and max_len
    - linearly decays outside that range
    """
    if not text:
        return 0.0

    length = len(text.strip())
    if length <= 0:
        return 0.0

    # Ideal range
    if min_len <= length <= max_len:
        return 1.0

    # Too short
    if length < min_len:
        return max(0.0, length / float(min_len))

    # Too long
    # Simple linear decay from max_len to 3 * max_len
    if length >= 3 * max_len:
        return 0.4

    # Between max_len and 3 * max_len
    # Map [max_len, 3*max_len] -> [1.0, 0.4]
    span = 2.0 * max_len
    over = length - max_len
    return max(0.4, 1.0 - (over / span) * 0.6)


def _has_any(values: List[str], payload: Dict[str, Any]) -> bool:
    for key in values:
        v = payload.get(key)
        if v:
            return True
    return False


def _normalize_timestamp(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        else:
            value = value.astimezone(timezone.utc)
        return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return str(value)


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _as_source_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _as_source_array(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _flag_enabled(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def quality_source_backed_optional_components_enabled() -> bool:
    return _flag_enabled(os.getenv(SOURCE_BACKED_OPTIONAL_COMPONENTS_FLAG))


def _coerce_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        normalized = value.strip()
        return [normalized] if normalized else []
    return [value]


def _llm_attributes_field_count(llm_attributes: Any) -> int:
    """Count the populated buyer-facing attribute fields in a structural-depth
    llm_attributes envelope (Fix Plan G). Tolerates a jsonb dict or a JSON
    string, the versioned envelope ({"attributes": {...}}) OR a bare attributes
    dict, and the legacy grounded-span extractor cache ({"attributes": [...]}).
    Returns 0 for anything empty/unparseable — a durable depth signal for
    model-readiness, never raises."""
    data = llm_attributes
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (ValueError, TypeError):
            return 0
    if not isinstance(data, dict):
        return 0
    attrs = data.get("attributes", data)
    if isinstance(attrs, list):  # legacy extractor cache: list of grounded spans
        return len([a for a in attrs if isinstance(a, dict) and a.get("value")])
    if not isinstance(attrs, dict):
        return 0
    reserved = {"schema_version", "vertical", "category_kind", "generated_at",
                "model", "provenance", "source_hash", "attributes"}
    return sum(
        1 for k, v in attrs.items()
        if k not in reserved and v not in (None, [], "", {})
    )


def _first_non_empty(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, list) and not value:
            continue
        return value
    return None


def _first_source_text(*values: Any) -> str:
    for value in values:
        normalized = _as_text(value)
        if normalized:
            return normalized
    return ""


def _object_text_value_count(value: Any) -> int:
    obj = _as_source_dict(value)
    return sum(1 for item in obj.values() if _as_text(item))


def _read_what_it_is(product_intel: Any) -> str:
    intel = _as_source_dict(product_intel)
    core = _as_source_dict(intel.get("product_intel_core"))
    legacy_core = _as_source_dict(intel.get("core"))
    return _first_source_text(
        _as_source_dict(core.get("what_it_is")).get("body"),
        _as_source_dict(core.get("whatItIs")).get("body"),
        _as_source_dict(legacy_core.get("what_it_is")).get("body"),
        _as_source_dict(legacy_core.get("whatItIs")).get("body"),
    )


def _source_backed_roots(
    payload: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    product_payload = _as_source_dict(payload.get("product_payload")) or payload
    seed_data = _as_source_dict(
        payload.get("seed_data") or payload.get("seed_data_json")
    )
    snapshot = _as_source_dict(seed_data.get("snapshot"))
    return product_payload, seed_data, snapshot


def source_backed_summary_text(payload: Dict[str, Any]) -> str:
    product_payload, seed_data, snapshot = _source_backed_roots(payload)
    return _first_source_text(
        seed_data.get("summary"),
        seed_data.get("pdp_summary"),
        seed_data.get("short_description"),
        seed_data.get("product_summary"),
        snapshot.get("summary"),
        snapshot.get("pdp_summary"),
        snapshot.get("short_description"),
        _read_what_it_is(seed_data.get("product_intel")),
        _read_what_it_is(snapshot.get("product_intel")),
        product_payload.get("summary"),
        product_payload.get("short_description"),
        product_payload.get("product_summary"),
        _read_what_it_is(product_payload.get("product_intel")),
    )


def source_backed_attribute_signal_count(payload: Dict[str, Any]) -> int:
    product_payload, seed_data, snapshot = _source_backed_roots(payload)
    ingredient_intel = _as_source_dict(seed_data.get("ingredient_intel"))
    snapshot_ingredient_intel = _as_source_dict(snapshot.get("ingredient_intel"))
    explicit_product_payload = _as_source_dict(payload.get("product_payload"))
    nested_payload_seed_data = (
        _as_source_dict(explicit_product_payload.get("seed_data"))
        if explicit_product_payload
        else {}
    )

    detail_sections = sum(
        len(_as_source_array(value))
        for value in (
            seed_data.get("pdp_details_sections"),
            snapshot.get("pdp_details_sections"),
            product_payload.get("pdp_details_sections"),
            nested_payload_seed_data.get("pdp_details_sections"),
        )
    )
    structured_ingredients = sum(
        len(_as_source_array(value))
        for value in (
            seed_data.get("active_ingredients"),
            seed_data.get("ingredients_inci"),
            seed_data.get("inci_list"),
            ingredient_intel.get("inci_list"),
            snapshot.get("active_ingredients"),
            snapshot.get("ingredients_inci"),
            snapshot.get("inci_list"),
            snapshot_ingredient_intel.get("inci_list"),
        )
    )
    source_backed_text_fields = sum(
        1
        for value in (
            seed_data.get("pdp_how_to_use_raw"),
            seed_data.get("pdp_ingredients_raw"),
            seed_data.get("raw_ingredient_text_clean"),
            snapshot.get("pdp_how_to_use_raw"),
            snapshot.get("pdp_ingredients_raw"),
            snapshot.get("raw_ingredient_text_clean"),
        )
        if _as_text(value)
    )
    explicit_attributes = (
        _object_text_value_count(seed_data.get("attributes"))
        + _object_text_value_count(snapshot.get("attributes"))
        + _object_text_value_count(product_payload.get("attributes"))
    )
    return (
        detail_sections
        + structured_ingredients
        + source_backed_text_fields
        + explicit_attributes
    )


def score_source_backed_summary(
    payload: Dict[str, Any],
    *,
    enabled: bool,
) -> Tuple[float, str]:
    if not enabled:
        return 0.0, ""
    summary = source_backed_summary_text(payload)
    if len(summary) >= 80:
        return 1.0, summary
    if len(summary) >= 30:
        return 0.7, summary
    return 0.0, summary


def score_source_backed_attributes(
    payload: Dict[str, Any],
    *,
    enabled: bool,
) -> Tuple[float, int]:
    if not enabled:
        return 0.0, 0
    signal_count = source_backed_attribute_signal_count(payload)
    if signal_count >= 3:
        return 1.0, signal_count
    if signal_count >= 1:
        return 0.7, signal_count
    return 0.0, signal_count


def _extract_main_image(product_data: Dict[str, Any]) -> Optional[str]:
    candidate = _first_non_empty(
        product_data.get("image_url"),
        product_data.get("main_image_url"),
        product_data.get("featured_image"),
    )
    if isinstance(candidate, dict):
        return _as_text(
            candidate.get("src")
            or candidate.get("url")
            or candidate.get("image_url")
        ) or None
    if candidate:
        return _as_text(candidate) or None

    images = _coerce_list(product_data.get("images") or product_data.get("image_list"))
    for image in images:
        if isinstance(image, dict):
            url = _as_text(image.get("src") or image.get("url") or image.get("image_url"))
            if url:
                return url
        else:
            url = _as_text(image)
            if url:
                return url
    return None


def _extract_price_value(product_data: Dict[str, Any]) -> Any:
    direct = _first_non_empty(
        product_data.get("price"),
        product_data.get("price_local_value"),
        product_data.get("base_price_value"),
    )
    if direct is not None:
        return direct

    price_obj = product_data.get("price_data")
    if isinstance(price_obj, dict):
        return _first_non_empty(
            price_obj.get("value"),
            price_obj.get("amount"),
        )

    variants = _coerce_list(product_data.get("variants"))
    for variant in variants:
        if isinstance(variant, dict):
            value = _first_non_empty(
                variant.get("price"),
                variant.get("price_local_value"),
                variant.get("base_price_value"),
            )
            if value is not None:
                return value
    return None


def _parse_standard_product(product_data: Dict[str, Any]) -> Optional[StandardProduct]:
    try:
        return StandardProduct.parse_obj(product_data)
    except Exception:
        return None


def make_product_key(platform: Any, platform_product_id: Any) -> Optional[ProductKey]:
    platform_value = _as_text(platform)
    product_value = _as_text(platform_product_id)
    if not platform_value or not product_value:
        return None
    return platform_value, product_value


def quality_row_has_scores(row: Optional[Dict[str, Any]]) -> bool:
    if not row:
        return False
    return any(row.get(field) is not None for field in _QUALITY_SCORE_FIELDS)


def quality_projection_has_scores(row: Optional[Dict[str, Any]]) -> bool:
    if not row:
        return False
    return any(row.get(field) is not None for field in _QUALITY_SCORE_FIELDS)


def build_quality_payload(
    product: Any,
    enrichment: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    enrichment = enrichment or {}

    if isinstance(product, StandardProduct):
        product_data: Dict[str, Any] = (
            product.model_dump() if hasattr(product, "model_dump") else product.dict()
        )
        title = product.title
        description = product.description_text or product.description or ""
        price_value = product.price
        main_image_url = product.image_url or (product.images[0] if product.images else None)
        brand = product.vendor or None
        category = product.product_type or None
    else:
        product_data = dict(product or {})
        title = _as_text(_first_non_empty(product_data.get("title"), product_data.get("name")))
        description = rich_text_to_plain_text(
            _first_non_empty(
                product_data.get("description_text"),
                product_data.get("description"),
                product_data.get("body_html"),
                product_data.get("description_local"),
            )
        )
        price_value = _extract_price_value(product_data)
        main_image_url = _extract_main_image(product_data)
        brand = _first_non_empty(product_data.get("vendor"), product_data.get("brand"))
        category = _first_non_empty(
            product_data.get("product_type"),
            product_data.get("category"),
            product_data.get("global_category_id"),
        )

    title_local = _as_text(_first_non_empty(enrichment.get("title_override"), title))
    summary_short = _as_text(enrichment.get("summary_short"))

    image_list = _coerce_list(product_data.get("images") or product_data.get("image_list"))
    extra_images = _coerce_list(enrichment.get("extra_images"))
    normalized_images: List[str] = []
    for image in image_list + extra_images:
        if isinstance(image, dict):
            url = _as_text(image.get("src") or image.get("url") or image.get("image_url"))
        else:
            url = _as_text(image)
        if url:
            normalized_images.append(url)

    if main_image_url:
        main_image_url = _as_text(main_image_url)
        if main_image_url and main_image_url not in normalized_images:
            normalized_images.insert(0, main_image_url)

    return {
        "title_local": title_local,
        "title_canonical": _as_text(title),
        "description_local": description,
        "price_local_value": price_value,
        "main_image_url": main_image_url,
        "image_list": normalized_images,
        "summary_short": summary_short,
        "bullet_points": _coerce_list(enrichment.get("bullet_points")),
        "usage_scenarios": _coerce_list(enrichment.get("usage_scenarios")),
        "audience_tags": _coerce_list(enrichment.get("audience_tags")),
        "topic_tags": _coerce_list(enrichment.get("topic_tags")),
        "brand": _as_text(brand) or None,
        "global_category_id": _as_text(category) or None,
        # Fix Plan G (T2) — durable structure-depth signals for model_readiness.
        # Additive; absent for legacy callers (StandardProduct / portal preview),
        # in which case readiness simply omits their contribution.
        "resolved_vertical": _as_text(product_data.get("resolved_vertical")) or None,
        "llm_attribute_field_count": _llm_attributes_field_count(
            product_data.get("llm_attributes")
        ),
    }


def build_quality_payload_from_cache_row(
    cache_row: Dict[str, Any],
    enrichment: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    product_data = dict(cache_row.get("product_data") or {})
    if not product_data:
        return None

    product = _parse_standard_product(product_data)
    if product is not None:
        return build_quality_payload(product, enrichment)
    return build_quality_payload(product_data, enrichment)


def build_quality_projection(
    *,
    snapshot_row: Optional[Dict[str, Any]] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if quality_row_has_scores(snapshot_row):
        return {
            "content_quality_score": snapshot_row.get("content_quality_score"),
            "model_readiness_score": snapshot_row.get("model_readiness_score"),
            "conversion_potential_score": snapshot_row.get("conversion_potential_score"),
            "last_evaluated_at": _normalize_timestamp(snapshot_row.get("snapshot_date")),
            "quality_source": QUALITY_SOURCE_SNAPSHOT,
        }

    if payload:
        preview = preview_quality(payload)
        if quality_projection_has_scores(preview):
            return {
                "content_quality_score": preview.get("content_quality_score"),
                "model_readiness_score": preview.get("model_readiness_score"),
                "conversion_potential_score": preview.get("conversion_potential_score"),
                "last_evaluated_at": None,
                "quality_source": QUALITY_SOURCE_PREVIEW,
            }

    return {
        "content_quality_score": None,
        "model_readiness_score": None,
        "conversion_potential_score": None,
        "last_evaluated_at": None,
        "quality_source": QUALITY_SOURCE_NONE,
    }


async def fetch_latest_quality_rows(
    merchant_id: str,
    *,
    platforms: Optional[Iterable[str]] = None,
    product_keys: Optional[Iterable[ProductKey]] = None,
) -> Dict[ProductKey, Dict[str, Any]]:
    platform_values = sorted({_as_text(platform) for platform in (platforms or []) if _as_text(platform)})
    key_filter = {key for key in (product_keys or []) if key and key[0] and key[1]}
    product_ids = sorted({key[1] for key in key_filter if key[1]})

    ranked = (
        select(
            product_quality_snapshot,
            func.row_number().over(
                partition_by=[
                    product_quality_snapshot.c.merchant_id,
                    product_quality_snapshot.c.platform,
                    product_quality_snapshot.c.platform_product_id,
                ],
                order_by=[
                    product_quality_snapshot.c.snapshot_date.desc(),
                    product_quality_snapshot.c.id.desc(),
                ],
            ).label("row_num"),
        )
        .where(product_quality_snapshot.c.merchant_id == merchant_id)
    )
    if platform_values:
        ranked = ranked.where(product_quality_snapshot.c.platform.in_(platform_values))
    if product_ids:
        ranked = ranked.where(product_quality_snapshot.c.platform_product_id.in_(product_ids))

    ranked_subquery = ranked.subquery("ranked_quality")
    query = select(*[col for col in ranked_subquery.c if col.key != "row_num"]).where(ranked_subquery.c.row_num == 1)
    rows = await database.fetch_all(query)

    latest: Dict[ProductKey, Dict[str, Any]] = {}
    for row in rows:
        payload = dict(row)
        key = make_product_key(payload.get("platform"), payload.get("platform_product_id"))
        if key is None:
            continue
        if key_filter and key not in key_filter:
            continue
        latest[key] = payload
    return latest


def summarize_quality_coverage(
    product_keys: Sequence[ProductKey],
    *,
    projections_by_key: Dict[ProductKey, Dict[str, Any]],
    snapshot_rows_by_key: Dict[ProductKey, Dict[str, Any]],
    active_backfill_job: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    unique_product_keys = list(dict.fromkeys(product_keys))
    total_products = len(unique_product_keys)
    snapshot_scored_products = 0
    effective_scored_products = 0
    preview_only_products = 0
    latest_snapshot_at: Optional[str] = None

    for key in unique_product_keys:
        snapshot_row = snapshot_rows_by_key.get(key)
        projection = projections_by_key.get(key) or {}
        if quality_row_has_scores(snapshot_row):
            snapshot_scored_products += 1
            snapshot_timestamp = _normalize_timestamp(snapshot_row.get("snapshot_date"))
            if snapshot_timestamp and (latest_snapshot_at is None or snapshot_timestamp > latest_snapshot_at):
                latest_snapshot_at = snapshot_timestamp
        if quality_projection_has_scores(projection):
            effective_scored_products += 1
            if projection.get("quality_source") == QUALITY_SOURCE_PREVIEW:
                preview_only_products += 1

    unscored_products = max(total_products - effective_scored_products, 0)
    if total_products <= 0 or effective_scored_products <= 0:
        coverage_state = "empty"
    elif effective_scored_products >= total_products:
        coverage_state = "full"
    else:
        coverage_state = "partial"

    return {
        "total_products": total_products,
        "snapshot_scored_products": snapshot_scored_products,
        "effective_scored_products": effective_scored_products,
        "preview_only_products": preview_only_products,
        "unscored_products": unscored_products,
        "coverage_state": coverage_state,
        "latest_snapshot_at": latest_snapshot_at,
        "backfill_recommended": total_products > 0 and snapshot_scored_products < total_products,
        "active_backfill_job": active_backfill_job,
    }


def preview_quality(
    payload: Dict[str, Any],
    *,
    score_source_backed_components: Optional[bool] = None,
) -> Dict[str, Any]:
    """
    Compute a lightweight content quality preview score based on the
    partial product payload coming from the Merchant Portal.

    The payload is expected to follow the v2 schema shape loosely, but
    this function is defensive and only relies on a few high‑level keys:
    - L0: title_canonical, brand, global_category_id
    - L2: title_local, description_local, price_local_value
    - L3: summary_short, bullet_points
    """
    problems: List[Dict[str, Any]] = []
    source_backed_enabled = (
        quality_source_backed_optional_components_enabled()
        if score_source_backed_components is None
        else bool(score_source_backed_components)
    )

    # Title / naming
    title = (
        payload.get("title_local")
        or payload.get("title_canonical")
        or ""
    )
    title_score = _text_length_score(title, min_len=10, max_len=80)
    if not title:
        problems.append({
            "field": "title_local",
            "code": "missing_title",
            "message": "缺少商品标题（建议先填写本地化标题或规范标题）。",
        })

    # Description
    description = (
        payload.get("description_local")
        or payload.get("description_raw")
        or ""
    )
    desc_score = _text_length_score(description, min_len=40, max_len=600)
    if not description:
        problems.append({
            "field": "description_local",
            "code": "missing_description",
            "message": "缺少商品描述，建议补充一句话 summary 和 3–5 条卖点。",
        })

    # Brand / category
    brand_present = bool(payload.get("brand"))
    category_present = bool(payload.get("global_category_id"))
    if not brand_present:
        problems.append({
            "field": "brand",
            "code": "missing_brand",
            "message": "建议填写品牌/供应商，有助于搜索和推荐。",
        })
    if not category_present:
        problems.append({
            "field": "global_category_id",
            "code": "missing_category",
            "message": "缺少标准类目，无法使用类目模板做完整度校验。",
        })

    # Price
    price_value = payload.get("price_local_value") or payload.get("base_price_value")
    price_ok = isinstance(price_value, (int, float)) and price_value > 0
    if not price_ok:
        problems.append({
            "field": "price_local_value",
            "code": "invalid_price",
            "message": "价格缺失或无效，需填入大于 0 的数值。",
        })

    # Images
    image_list = payload.get("image_list") or []
    main_image = payload.get("main_image_url")
    has_any_image = bool(main_image or image_list)
    if not has_any_image:
        problems.append({
            "field": "image_list",
            "code": "missing_images",
            "message": "缺少商品图片，至少需要 1 张主图。",
        })

    # L3: summary / bullets
    summary = payload.get("summary_short") or ""
    source_summary_score, source_summary = score_source_backed_summary(
        payload,
        enabled=source_backed_enabled,
    )
    summary_score = max(
        _text_length_score(summary, min_len=20, max_len=120),
        source_summary_score,
    )
    bullets = payload.get("bullet_points") or []
    bullets_count = len(bullets)
    bullets_ok = bullets_count >= 3
    if not summary and not source_summary:
        problems.append({
            "field": "summary_short",
            "code": "missing_summary",
            "message": "建议补充 1–2 句 summary，说明适合谁、解决什么问题。",
        })
    if not bullets_ok:
        problems.append({
            "field": "bullet_points",
            "code": "insufficient_bullets",
            "message": "建议提供 3–8 条卖点 bullet，便于 Agent 推荐。",
        })

    # Basic completeness for attributes: check presence of a few keys
    has_size_or_dim = _has_any(
        ["size", "screen_size_inch", "capacity_ml", "dimensions"],
        payload,
    )
    has_usage = _has_any(["usage_scenarios", "usage_notes"], payload)
    attribute_score = 0.0
    if has_size_or_dim:
        attribute_score += 0.5
    if has_usage:
        attribute_score += 0.5
    source_attribute_score, source_attribute_signal_count = (
        score_source_backed_attributes(payload, enabled=source_backed_enabled)
    )
    attribute_score = max(attribute_score, source_attribute_score)

    # Aggregate into a 0–100 content quality preview
    # This is intentionally simple and easy to tweak.
    raw_components: List[Tuple[str, float]] = [
        ("title", title_score),
        ("description", desc_score),
        ("summary", summary_score),
        ("attributes", attribute_score),
        ("images", 1.0 if has_any_image else 0.0),
        ("brand_category", 1.0 if brand_present and category_present else 0.0),
        ("price", 1.0 if price_ok else 0.0),
    ]

    # Weighted average (all equal for now)
    if raw_components:
        avg_score = sum(s for _, s in raw_components) / len(raw_components)
    else:
        avg_score = 0.0

    content_quality_score = round(avg_score * 100.0, 1)

    # Model readiness: can a frontier agent GROUND a citation on this product?
    #
    # Fix Plan G (T2) re-derivation. The prior formula read ONLY the L3
    # enrichment overlay (summary_short * 0.4 + bullets * 0.3 + L3 attributes *
    # 0.3). That overlay is unpopulated for ~all of the live catalog, so
    # readiness sat at ~0 (prod: avg 2.5/100, only 570/12,551 > 0) even for
    # fully-structured products — the score was effectively unwired. It now reads
    # the BASE catalog structure that actually exists (title / description /
    # image / brand+category / price — the same signals content_quality uses)
    # plus the two durable depth signals the index now carries: a resolved
    # vertical (Fix Plan B) and a populated llm_attributes payload (Fix Plan G
    # T1). Weights sum to 1.0; absent signals contribute 0 (honest under-fill,
    # never a fabricated score). content_quality_score is unchanged.
    vertical = str(payload.get("resolved_vertical") or "").strip().lower()
    vertical_resolved = bool(vertical) and vertical != "other"
    # Depth: how many buyer-facing attribute fields the structural-depth payload
    # resolved, saturating at 5 (skin_type/concerns/key_ingredients/texture/
    # finish/spf/volume — a product rarely carries all).
    attr_field_count = int(payload.get("llm_attribute_field_count") or 0)
    attribute_depth = min(attr_field_count / 5.0, 1.0)
    readiness_components: List[Tuple[str, float, float]] = [
        ("description", desc_score, 0.20),
        ("attribute_depth", attribute_depth, 0.20),
        ("images", 1.0 if has_any_image else 0.0, 0.15),
        ("vertical_resolved", 1.0 if vertical_resolved else 0.0, 0.15),
        ("title", title_score, 0.10),
        ("brand_category", 1.0 if (brand_present and category_present) else 0.0, 0.10),
        ("price", 1.0 if price_ok else 0.0, 0.10),
    ]
    model_readiness_score = round(
        sum(s * w for _, s, w in readiness_components) * 100.0, 1
    )

    # Conversion potential is not computed in preview; just stub for now.
    conversion_potential_score = None

    result = {
        "content_quality_score": content_quality_score,
        "model_readiness_score": model_readiness_score,
        "conversion_potential_score": conversion_potential_score,
        "problems": problems,
        "components": [
            {"name": name, "score": round(score * 100.0, 1)}
            for name, score in raw_components
        ],
        "readiness_components": [
            {"name": name, "score": round(score * 100.0, 1), "weight": weight}
            for name, score, weight in readiness_components
        ],
    }
    if source_backed_enabled:
        result["source_backed_fields"] = {
            "optional_components_enabled": True,
            "summary_length": len(source_summary),
            "attribute_signal_count": source_attribute_signal_count,
        }
    return result


async def full_quality_eval(
    merchant_id: str,
    platform: str,
    platform_product_id: str,
    geo_code: Optional[str],
    payload: Dict[str, Any],
    rules_version: str = DEFAULT_QUALITY_RULES_VERSION,
    model_version: str = "none",
    score_source_backed_components: Optional[bool] = None,
) -> Dict[str, Any]:
    """
    Full evaluation entrypoint.

    V1 implementation simply reuses preview_quality(), then persists the
    result to product_quality_snapshot. Later versions can extend this
    to:
    - fetch canonical product from catalog (if payload is omitted)
    - incorporate behavior metrics
    - call ML models for richer scoring
    """
    result = preview_quality(
        payload,
        score_source_backed_components=score_source_backed_components,
    )
    effective_rules_version = rules_version
    if (
        rules_version == DEFAULT_QUALITY_RULES_VERSION
        and result.get("source_backed_fields", {}).get("optional_components_enabled")
    ):
        effective_rules_version = SOURCE_BACKED_COMPONENTS_RULES_VERSION

    # Optional convenience product_id (composite)
    product_id = f"{merchant_id}|{platform}|{platform_product_id}"

    row = {
        "merchant_id": merchant_id,
        "platform": platform,
        "platform_product_id": platform_product_id,
        "geo_code": geo_code,
        "product_id": product_id,
        "content_quality_score": result.get("content_quality_score"),
        "model_readiness_score": result.get("model_readiness_score"),
        "conversion_potential_score": result.get("conversion_potential_score"),
        "rules_version": effective_rules_version,
        "model_version": model_version,
        "details": result,
    }

    await database.execute(product_quality_snapshot.insert().values(row))
    try:
        catalog_row = await database.fetch_one(
            """
            SELECT content_key
            FROM catalog_products
            WHERE merchant_id = :merchant_id
              AND platform = :platform
              AND source_product_id = :source_product_id
            LIMIT 1
            """,
            {
                "merchant_id": merchant_id,
                "platform": platform,
                "source_product_id": platform_product_id,
            },
        )
        content_key = (dict(catalog_row) if catalog_row else {}).get("content_key")
        if content_key:
            from services.index_pipeline_state_service import recompute_serving_eligibility

            await recompute_serving_eligibility(
                content_key,
                reason="quality_snapshot",
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning({
            "event": "serving_eligibility_hook_failed",
            "site": "full_quality_eval",
            "merchant_id": merchant_id,
            "platform": platform,
            "source_product_id": platform_product_id,
            "error": str(exc),
        })
    return result
