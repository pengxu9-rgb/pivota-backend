"""
Pivota canonical PDP resolver — public read-only routes that turn a
sig_* signature into the product data needed to render the PDP page
at agent.pivota.cc/products/{sig_id}, and that enumerate sigs for
sitemap generation.

These routes back the dynamic Pivota canonical PDP surface (Phase C-2
of the canonical-PDP build). Phase C-1 (PR #327) added the schema +
sig generator + audit fallback so every onboarded merchant product
gets a sig_*. This PR makes those URLs actually serve content +
appear in the sitemap so Google can index them.

Surface:
  - GET /api/canonical/products/{sig_id}
        Returns { product: {title, brand, description, image_url,
                            canonical_url, vendor, product_type, ...} }
        404 if sig_id doesn't exist.
        Public — no auth (it's a discovery surface).
  - GET /api/canonical/products?limit=N&offset=M
        Returns { items: [{sig_id, canonical_url, last_modified}, ...],
                  total, limit, offset }
        For sitemap generation (pivota-agent-ui sitemap-products.xml).
        Bounded list (max 1000 per page) to keep response size sane.
        Public — no auth.

Why not gate on auth? These endpoints serve data we WANT public
indexing for — anyone who can see the agent.pivota.cc/products/ URL
can already see the PDP. Gating the resolver would just block our
own gateway/sitemap from working.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select, func

from db.catalog import catalog_products
from db.database import database
from utils.logger import logger

router = APIRouter(
    prefix="/api/canonical",
    tags=["canonical-pdp"],
)


def _shape_product_for_pdp(row: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a catalog_products row into the flat product object the
    pivota-agent-ui PDP page expects (see
    pivota-agent-ui/src/app/products/[id]/productJsonLd.ts +
    page.tsx:readCanonicalPdpProduct for the consumer shape).

    Falls back to product_payload fields where the top-level catalog
    columns are sparse — that's why the catalog stores the full raw
    payload alongside the normalized columns."""
    payload = row.get("product_payload") or {}
    if not isinstance(payload, dict):
        payload = {}

    # Title: catalog.title is required at sync time, so this always populates.
    title = (row.get("title") or "").strip() or payload.get("title") or ""

    # Brand: catalog.brand may be null for older syncs; payload often has it.
    brand_str = (
        (row.get("brand") or "").strip()
        or (payload.get("brand") or "")
        or (payload.get("vendor") or "")
        or ""
    )

    description = (
        (row.get("description") or "").strip()
        or (payload.get("description") or "")
        or (payload.get("description_text") or "")
        or ""
    )

    image = (row.get("image_url") or "").strip() or payload.get("image_url") or ""

    return {
        "id": row.get("pivota_signature_id"),
        "product_id": row.get("pivota_signature_id"),
        "title": title,
        "name": title,
        "brand": brand_str or None,
        "vendor": brand_str or None,
        "product_type": row.get("product_type"),
        "description": description or None,
        "image_url": image or None,
        "main_image_url": image or None,
        "canonical_url": row.get("pivota_canonical_url"),
        # Echo the merchant's own URL too (when set) so consumers can
        # link out to the storefront from the canonical PDP.
        "merchant_canonical_url": row.get("canonical_url"),
        "platform": row.get("platform"),
        "source_product_id": row.get("source_product_id"),
        # Carry the full upstream payload for consumers that need
        # variants / price / inventory beyond what we normalized.
        "payload": payload or None,
    }


@router.get("/products/{sig_id}")
async def get_canonical_pdp_by_signature(sig_id: str) -> Dict[str, Any]:
    """Resolve a sig_* to product fields. Backs the SSR + client-side
    data fetch for agent.pivota.cc/products/{sig_id}."""
    sig = (sig_id or "").strip()
    if not sig.startswith("sig_") or len(sig) < 5:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="sig_id must look like sig_<hex>",
        )
    query = (
        select(
            catalog_products.c.product_key,
            catalog_products.c.merchant_id,
            catalog_products.c.platform,
            catalog_products.c.source_product_id,
            catalog_products.c.title,
            catalog_products.c.description,
            catalog_products.c.brand,
            catalog_products.c.product_type,
            catalog_products.c.canonical_url,
            catalog_products.c.image_url,
            catalog_products.c.product_payload,
            catalog_products.c.pivota_signature_id,
            catalog_products.c.pivota_canonical_url,
            catalog_products.c.updated_at,
        )
        .where(catalog_products.c.pivota_signature_id == sig)
        .limit(1)
    )
    row = await database.fetch_one(query)
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "message": "No canonical PDP for this signature",
                "sig_id": sig,
            },
        )
    row_dict = dict(row)
    return {
        "product": _shape_product_for_pdp(row_dict),
        "updated_at": (
            row_dict["updated_at"].isoformat()
            if isinstance(row_dict.get("updated_at"), datetime)
            else None
        ),
    }


@router.get("/products")
async def list_canonical_pdp_signatures(
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> Dict[str, Any]:
    """Paginated list of all canonical PDP signatures. Backs the
    pivota-agent-ui sitemap-products.xml route. Returns minimal fields
    (sig_id + canonical_url + last_modified) — no need for the full
    product object; sitemap only needs URL + lastmod."""
    # Total count (cached upstream by the sitemap consumer; this is a
    # simple SELECT COUNT — fine on the indexed column for now).
    total_q = (
        select(func.count())
        .select_from(catalog_products)
        .where(catalog_products.c.pivota_signature_id.isnot(None))
    )
    total = await database.fetch_val(total_q) or 0

    rows_q = (
        select(
            catalog_products.c.pivota_signature_id,
            catalog_products.c.pivota_canonical_url,
            catalog_products.c.updated_at,
        )
        .where(catalog_products.c.pivota_signature_id.isnot(None))
        .order_by(catalog_products.c.updated_at.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = await database.fetch_all(rows_q)
    items = [
        {
            "sig_id": r["pivota_signature_id"],
            "canonical_url": r["pivota_canonical_url"],
            "last_modified": (
                r["updated_at"].isoformat()
                if isinstance(r["updated_at"], datetime)
                else None
            ),
        }
        for r in rows
    ]
    return {
        "items": items,
        "total": int(total),
        "limit": limit,
        "offset": offset,
    }
