"""
Agent-facing product merge service.

This module exposes a helper that:
- Reads StandardProduct from products_cache
- Applies Pivota-specific enrichment overlay
so that Agent APIs can use a unified AgentProduct view.

V1 is intentionally simple and can be extended later with:
- Quality scores
- Behavior metrics
"""

from typing import Any, Dict, Optional

from db.database import database
from db.products import products_cache
from db.product_enrichment import get_enrichment
from models.standard_product import StandardProduct


async def get_agent_product_view(
    merchant_id: str,
    platform: str,
    platform_product_id: str,
    geo_code: str = "default",
) -> Optional[Dict[str, Any]]:
    """
    Build a merged AgentProduct-like view from cache + enrichment.

    This does not change any existing Agent API behavior yet; it is a
    dedicated helper that other routes can start adopting gradually.
    """
    cache_row = await database.fetch_one(
        products_cache.select().where(
            (products_cache.c.merchant_id == merchant_id)
            & (products_cache.c.platform == platform)
            & (products_cache.c.platform_product_id == platform_product_id)
        )
    )
    if not cache_row:
        return None

    cache_row = dict(cache_row)
    product_json = cache_row.get("product_data") or {}

    try:
        standard = StandardProduct.parse_obj(product_json)
    except Exception:
        # Fallback: return raw JSON if parsing fails
        standard = None

    enrichment = await get_enrichment(
        merchant_id=merchant_id,
        platform=platform,
        platform_product_id=platform_product_id,
        geo_code=geo_code,
    ) or {}

    # Basic merge rules:
    title = enrichment.get("title_override") or (standard.title if standard else product_json.get("title"))

    summary = enrichment.get("summary_short")
    if not summary:
        # Fallback: use description truncated
        desc = (
            product_json.get("description")
            or product_json.get("description_html")
            or ""
        )
        summary = (desc or "")[:120]

    # Images
    if standard:
        main_image = standard.image_url or (standard.images[0] if standard.images else None)
        images = list(standard.images)
    else:
        main_image = product_json.get("image_url")
        images = product_json.get("images") or []

    extra_images = enrichment.get("extra_images") or []
    merged_images = images + extra_images

    return {
        "id": f"{merchant_id}|{platform}|{platform_product_id}",
        "merchant_id": merchant_id,
        "platform": platform,
        "platform_product_id": platform_product_id,
        "title": title,
        "summary": summary,
        "price": {
            "value": getattr(standard, "price", None) if standard else product_json.get("price"),
            "currency": getattr(standard, "currency", None) if standard else product_json.get("currency"),
        },
        "main_image_url": main_image,
        "images": merged_images,
        "enrichment": enrichment,
    }

