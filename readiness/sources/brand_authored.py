"""Readiness source for store-less brand-authored products.

Reads a merchant's platform="brand_authored" rows from catalog_products (the
storefront-optional registry), overlays product_enrichment, and maps each row to
a StandardProduct so the EXISTING readiness pipeline can score them — without a
connected store.

Construction mirrors readiness/sources/synthetic.py: it returns a valid
MerchantSourceDataset with merchant_connection={}, a brand-catalog source_of_truth,
and merchant_alpha_mode="brand_authored" (the gate the scorer keys off to mark the
commerce field families N/A — see readiness/scoring.py).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from models.standard_product import StandardProduct
from readiness.models import MerchantSourceDataset
from services.brand_authored_intake import (
    MERCHANT_ALPHA_MODE_BRAND_AUTHORED,
    PLATFORM_BRAND_AUTHORED,
)


def _coerce_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        # tags may be stored as a JSON array string in some paths
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    return [str(v).strip() for v in parsed if str(v).strip()]
            except Exception:
                pass
        return [raw]
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def _row_to_standard_product(
    merchant_id: str, row: Dict[str, Any], enrichment: Optional[Dict[str, Any]]
) -> StandardProduct:
    enrichment = enrichment or {}
    source_product_id = str(row.get("source_product_id") or "").strip()

    title = (
        str(enrichment.get("title_override") or "").strip()
        or str(row.get("title") or "").strip()
        or "Untitled product"
    )
    description = (
        enrichment.get("description_markdown")
        or enrichment.get("summary_short")
        or row.get("description")
        or None
    )
    vendor = (row.get("brand") or None)
    product_type = (row.get("product_type") or row.get("category") or None)

    images: List[str] = []
    main_image = row.get("image_url")
    if main_image:
        images.append(str(main_image))
    for extra in _coerce_list(enrichment.get("extra_images")):
        if extra and extra not in images:
            images.append(extra)

    tags = _coerce_list(row.get("tags"))
    for topic in _coerce_list(enrichment.get("topic_tags")):
        if topic and topic not in tags:
            tags.append(topic)

    return StandardProduct(
        id=source_product_id,
        platform=PLATFORM_BRAND_AUTHORED,
        merchant_id=merchant_id,
        title=title,
        description=description,
        vendor=vendor,
        product_type=product_type,
        tags=tags,
        # Brand-authored = no commerce truth from a store. Default price/currency;
        # the scorer marks the price/inventory/checkout families N/A in this mode,
        # so these defaults never produce a commerce blocker.
        price=0.0,
        currency="USD",
        inventory_quantity=0,
        image_url=images[0] if images else None,
        images=images,
        variants=[],
    )


async def count_brand_authored_products(merchant_id: str) -> int:
    """How many platform='brand_authored' catalog_products rows this merchant has.
    Used by the source selector to decide whether to route to this source."""
    from db.database import database

    row = await database.fetch_one(
        """
        SELECT COUNT(*) AS n
        FROM catalog_products
        WHERE merchant_id = :mid AND platform = :platform
        """,
        {"mid": merchant_id, "platform": PLATFORM_BRAND_AUTHORED},
    )
    if not row:
        return 0
    return int(dict(row).get("n") or 0)


async def load_brand_authored_merchant_dataset(merchant_id: str) -> MerchantSourceDataset:
    from db.database import database
    from db.product_enrichment import get_enrichments_for_products

    rows = await database.fetch_all(
        """
        SELECT
          source_product_id, title, brand, description, product_type,
          category, image_url, tags, content_key, updated_at
        FROM catalog_products
        WHERE merchant_id = :mid AND platform = :platform
        ORDER BY updated_at DESC NULLS LAST, source_product_id ASC
        """,
        {"mid": merchant_id, "platform": PLATFORM_BRAND_AUTHORED},
    )
    rows = [dict(r) for r in rows or []]

    product_keys = [
        (PLATFORM_BRAND_AUTHORED, str(r.get("source_product_id") or "").strip())
        for r in rows
        if str(r.get("source_product_id") or "").strip()
    ]
    enrichments_by_key: Dict[Any, Dict[str, Any]] = {}
    if product_keys:
        enrichments_by_key = await get_enrichments_for_products(
            merchant_id, product_keys=product_keys, geo_code="default"
        )

    products: List[StandardProduct] = []
    for row in rows:
        spid = str(row.get("source_product_id") or "").strip()
        if not spid:
            continue
        enrichment = enrichments_by_key.get((PLATFORM_BRAND_AUTHORED, spid))
        products.append(_row_to_standard_product(merchant_id, row, enrichment))

    now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    return MerchantSourceDataset(
        merchant_id=merchant_id,
        merchant_name=merchant_id,
        evaluation_reference_time=now_iso,
        merchant_alpha_mode=MERCHANT_ALPHA_MODE_BRAND_AUTHORED,
        source_of_truth={
            "catalog": "pivota_brand_catalog.v1",
            "reviews_confidence": "readiness.brand_catalog_reviews.none.v1",
        },
        capability_status={
            "merchant_adapter": "not_applicable",
            "checkout": "not_applicable",
            "order_sync": "not_applicable",
            "channel_export": "ready",
            "reviews_confidence": "blocked",
        },
        merchant_blockers=[],
        merchant_warnings=["brand_authored_mode"],
        stubbed_capabilities=[],
        merchant_policy={},
        payment_capabilities={},
        merchant_connection={},
        review_diagnostics={
            "integration_status": "blocked",
            "observed_at": None,
            "products_with_reviews": 0,
            "grouped_products_with_reviews": 0,
            "products_without_reviews": len(products),
        },
        products=products,
        product_diagnostics={},
        variant_diagnostics={},
        audit_notes=[
            "Brand-authored catalog: products created in Pivota without a connected store.",
            "Commerce readiness (price / inventory / checkout / order status) is N/A in this mode.",
        ],
    )
