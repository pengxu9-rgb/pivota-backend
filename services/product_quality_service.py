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

from typing import Any, Dict, List, Tuple, Optional

from db.database import database
from db.product_quality import product_quality_snapshot


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


def preview_quality(payload: Dict[str, Any]) -> Dict[str, Any]:
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
    summary_score = _text_length_score(summary, min_len=20, max_len=120)
    bullets = payload.get("bullet_points") or []
    bullets_count = len(bullets)
    bullets_ok = bullets_count >= 3
    if not summary:
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

    # Model readiness: simple proxy based on L3 + attributes presence
    model_readiness_score = round(
        (summary_score * 0.4 + (1.0 if bullets_ok else 0.0) * 0.3 + attribute_score * 0.3)
        * 100.0,
        1,
    )

    # Conversion potential is not computed in preview; just stub for now.
    conversion_potential_score = None

    return {
        "content_quality_score": content_quality_score,
        "model_readiness_score": model_readiness_score,
        "conversion_potential_score": conversion_potential_score,
        "problems": problems,
        "components": [
            {"name": name, "score": round(score * 100.0, 1)}
            for name, score in raw_components
        ],
    }


async def full_quality_eval(
    merchant_id: str,
    platform: str,
    platform_product_id: str,
    geo_code: Optional[str],
    payload: Dict[str, Any],
    rules_version: str = "v1-lite",
    model_version: str = "none",
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
    result = preview_quality(payload)

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
        "rules_version": rules_version,
        "model_version": model_version,
        "details": result,
    }

    await database.execute(product_quality_snapshot.insert().values(row))
    return result
