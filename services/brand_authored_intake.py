"""Store-less brand-authored product intake (the MANUAL index-population path).

Sibling of services/audit_index_intake.py. Where audit-seed mints an OBSERVED,
unclaimed catalog_products row from a fetched product URL, this module mints one
from a merchant's MANUAL create/edit in the portal — for brands with no connected
store. The merchant-supplied content (title/description/bullets/...) lands in
product_enrichment as an overlay; the canonical identity row lands in
catalog_products keyed by make_catalog_product_key.

Like audit-seed, the catalog row lands UN-SERVED: only the same small column set
is written, so pdp_lifecycle_stage stays NULL and no catalog_skus / serving_eligible
rows are created — recall + serving gates require those, which we never write.

Everything here is flag-gated by the caller (readiness.flags.storeless_brand_catalog_enabled).
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as _pg_insert

from services.catalog_identity import make_content_key

logger = logging.getLogger(__name__)

# Brand-authored products use a dedicated synthetic platform — keeps them
# identifiable + de-conflated from Shopify/marketplace/url_audit rows.
PLATFORM_BRAND_AUTHORED = "brand_authored"

# merchant_alpha_mode carried by the readiness dataset built from these rows.
MERCHANT_ALPHA_MODE_BRAND_AUTHORED = "brand_authored"


def _slugify(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return text[:80]


def generate_source_product_id(title: str) -> str:
    """Stable-ish, collision-resistant server-side source_product_id for a
    newly-created brand-authored product. A slug (so it's human-recognizable in
    the index) plus a short uuid suffix (so two products with the same title
    don't collide on the unique (merchant, platform, source_product_id) index)."""
    slug = _slugify(title) or "product"
    return f"ba-{slug}-{uuid.uuid4().hex[:12]}"


# Only the columns we set; everything else (catalog_track, truth_tier,
# readiness_tier, sync_status, claim_state='unclaimed', created_at, ...) takes its
# server_default — and pdp_lifecycle_stage stays NULL, so a brand-authored row is
# NOT recalled / served until it graduates or is claimed. pdp_scope is set to
# 'unverified' explicitly (matches its server_default + audit-seed semantics).
_CATALOG_INSERT_COLUMNS = (
    "product_key", "merchant_id", "platform", "source_product_id",
    "title", "brand", "content_key", "description", "product_type",
    "category", "image_url", "tags", "pdp_scope",
)

# Enrichment overlay fields the merchant may supply on create/edit. These are the
# subset of upsert_enrichment's allowed_keys that map cleanly to the create body.
_ENRICHMENT_FIELDS = (
    "title_override", "summary_short", "description_markdown",
    "bullet_points", "usage_scenarios", "audience_tags", "topic_tags",
    "regulatory_disclaimer_local", "extra_images",
)


def build_catalog_fields(
    merchant_id: str,
    source_product_id: str,
    *,
    title: str,
    brand: Optional[str] = None,
    product_type: Optional[str] = None,
    category: Optional[str] = None,
    description: Optional[str] = None,
    image_url: Optional[str] = None,
    tags: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Map a merchant create/edit payload -> canonical catalog_products fields.

    content_key resolved the SAME way audit-seed does: make_content_key(brand,
    title) (GTIN enrichment is a follow-up; the deliberately non-unique key is
    de-conflated downstream by the identity gate, exactly as for other seeds)."""
    from services.catalog_sync_service import make_catalog_product_key

    brand_norm = (brand or "").strip() or None
    title_norm = (title or "").strip()
    return {
        "merchant_id": merchant_id,
        "platform": PLATFORM_BRAND_AUTHORED,
        "source_product_id": source_product_id,
        "product_key": make_catalog_product_key(
            merchant_id, PLATFORM_BRAND_AUTHORED, source_product_id
        ),
        "title": title_norm,
        "brand": brand_norm,
        "content_key": make_content_key(brand_norm, title_norm),
        "description": (description or "").strip() or None,
        "product_type": (product_type or "").strip() or None,
        "category": (category or "").strip() or None,
        "image_url": (image_url or "").strip() or None,
        "tags": list(tags) if tags else None,
        "pdp_scope": "unverified",
    }


async def upsert_brand_authored_catalog_row(fields: Dict[str, Any]) -> Optional[str]:
    """Upsert one brand-authored product into catalog_products keyed on
    product_key (REUSES the exact pg_insert ON CONFLICT pattern from
    services.audit_index_intake.upsert_audited_sku_to_index). Returns the
    product_key on success, None otherwise."""
    from db.catalog import catalog_products
    from db.database import database

    values = {k: fields.get(k) for k in _CATALOG_INSERT_COLUMNS}
    stmt = _pg_insert(catalog_products).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["product_key"],
        set_={
            "title": stmt.excluded.title,
            "brand": func.coalesce(stmt.excluded.brand, catalog_products.c.brand),
            "content_key": func.coalesce(
                stmt.excluded.content_key, catalog_products.c.content_key
            ),
            "description": func.coalesce(
                stmt.excluded.description, catalog_products.c.description
            ),
            "product_type": func.coalesce(
                stmt.excluded.product_type, catalog_products.c.product_type
            ),
            "category": func.coalesce(
                stmt.excluded.category, catalog_products.c.category
            ),
            "image_url": func.coalesce(
                stmt.excluded.image_url, catalog_products.c.image_url
            ),
            "tags": func.coalesce(stmt.excluded.tags, catalog_products.c.tags),
            "updated_at": func.now(),
            "content_changed_at": func.now(),
        },
    )
    await database.execute(stmt)
    return fields.get("product_key")


def extract_enrichment_overlay(body: Dict[str, Any]) -> Dict[str, Any]:
    """Pull the enrichment-overlay subset out of a create/edit body. Only the
    known overlay fields are kept (upsert_enrichment also re-filters)."""
    return {k: body[k] for k in _ENRICHMENT_FIELDS if k in body and body[k] is not None}
