from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import select

from db.catalog import catalog_offers, catalog_products, catalog_skus
from db.database import database
from db.surface_listing_registry import surface_listing_states
from services.canonical_commerce_service import (
    make_canonical_product_id,
    make_canonical_variant_id,
)


def _normalize_surface(surface: Optional[str]) -> Optional[str]:
    raw = str(surface or "").strip().lower()
    return raw or None


async def _fetch_surface_listing_rows(merchant_id: str, surface: Optional[str]) -> List[Dict[str, Any]]:
    query = select(surface_listing_states).where(surface_listing_states.c.merchant_id == merchant_id)
    normalized_surface = _normalize_surface(surface)
    if normalized_surface:
        query = query.where(surface_listing_states.c.surface == normalized_surface)
    rows = await database.fetch_all(query)
    return [dict(row) for row in rows]


async def _fetch_catalog_listing_fallback_rows(merchant_id: str, surface: Optional[str]) -> List[Dict[str, Any]]:
    normalized_surface = _normalize_surface(surface)
    if normalized_surface and normalized_surface not in {"default", "catalog"}:
        return []

    rows = await database.fetch_all(
        """
        SELECT
            sku_rows.product_key,
            sku_rows.sku_key,
            sku_rows.source_product_id,
            sku_rows.source_variant_id,
            sku_rows.channel,
            sku_rows.platform,
            sku_rows.offer_id,
            sku_rows.updated_at
        FROM (
            SELECT
                p.product_key AS product_key,
                s.sku_key AS sku_key,
                p.source_product_id AS source_product_id,
                s.source_variant_id AS source_variant_id,
                COALESCE(NULLIF(LOWER(o.channel), ''), 'default') AS channel,
                LOWER(COALESCE(NULLIF(p.platform, ''), 'unknown')) AS platform,
                o.offer_id AS offer_id,
                GREATEST(
                    COALESCE(o.updated_at, o.created_at),
                    COALESCE(s.updated_at, s.created_at),
                    COALESCE(p.updated_at, p.created_at)
                ) AS updated_at,
                ROW_NUMBER() OVER (
                    PARTITION BY s.sku_key
                    ORDER BY
                        GREATEST(
                            COALESCE(o.updated_at, o.created_at),
                            COALESCE(s.updated_at, s.created_at),
                            COALESCE(p.updated_at, p.created_at)
                        ) DESC,
                        o.offer_id ASC
                ) AS row_number
            FROM catalog_products p
            JOIN catalog_skus s ON s.product_key = p.product_key
            JOIN catalog_offers o
              ON o.sku_key = s.sku_key
             AND o.suppressed_at IS NULL
            WHERE p.merchant_id = :merchant_id
              AND COALESCE(NULLIF(LOWER(o.offer_mode), ''), 'merchant_checkout') = 'merchant_checkout'
        ) AS sku_rows
        WHERE sku_rows.row_number = 1
        ORDER BY sku_rows.updated_at DESC NULLS LAST, sku_rows.sku_key ASC
        """,
        {"merchant_id": merchant_id},
    )

    listing_rows: List[Dict[str, Any]] = []
    for row in rows:
        data = dict(row)
        row_surface = _normalize_surface(data.get("channel")) or "default"
        if normalized_surface and row_surface != normalized_surface:
            continue
        canonical_product_id = None
        canonical_variant_id = None
        platform = str(data.get("platform") or "").strip().lower()
        source_product_id = str(data.get("source_product_id") or "").strip()
        source_variant_id = str(data.get("source_variant_id") or "").strip()
        if merchant_id and platform and source_product_id:
            canonical_product_id = make_canonical_product_id(
                merchant_id,
                platform,
                source_product_id,
            )
        if merchant_id and platform and source_product_id and source_variant_id:
            canonical_variant_id = make_canonical_variant_id(
                merchant_id,
                platform,
                source_product_id,
                source_variant_id,
            )
        listing_rows.append(
            {
                "listing_id": f"catalog_fallback::{merchant_id}::{data.get('sku_key')}",
                "merchant_id": merchant_id,
                "interaction_id": None,
                "surface": row_surface,
                "listing_key": f"catalog_fallback:{merchant_id}:{data.get('product_key')}:{data.get('sku_key')}",
                "canonical_product_id": canonical_product_id or data.get("product_key"),
                "canonical_variant_id": canonical_variant_id or data.get("sku_key"),
                "status": "indexed",
                "last_exported_at": data.get("updated_at"),
                "last_indexed_at": data.get("updated_at"),
                "last_tradeable_at": None,
                "latest_receipt_id": None,
                "latest_error_id": None,
                "metadata": {
                    "source": "catalog_offer_fallback",
                    "offer_id": data.get("offer_id"),
                    "platform": data.get("platform"),
                    "catalog_product_key": data.get("product_key"),
                    "catalog_sku_key": data.get("sku_key"),
                    "source_product_id": data.get("source_product_id"),
                    "source_variant_id": data.get("source_variant_id"),
                },
                "created_at": data.get("updated_at"),
                "updated_at": data.get("updated_at"),
            }
        )
    return listing_rows


async def fetch_listing_rows_with_catalog_fallback(merchant_id: str, surface: Optional[str] = None) -> List[Dict[str, Any]]:
    rows = await _fetch_surface_listing_rows(merchant_id, surface)
    if rows:
        return rows
    return await _fetch_catalog_listing_fallback_rows(merchant_id, surface)
