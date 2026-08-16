"""
Admin (internal) catalog debug helpers.

Purpose:
- Inspect a merchant's catalog/cache state without requiring merchant JWTs.
- Help debug "products page has items but integrations count is 0" issues.

Safety:
- Do NOT return PII (emails/addresses) or secrets (tokens).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, status
from pydantic import BaseModel, Field

from db.database import database

router = APIRouter(prefix="/agent/internal/catalog", tags=["internal-catalog-debug"])


async def require_admin_key(
    x_admin_key: str = Header(..., alias="X-ADMIN-KEY"),
) -> None:
    expected = (os.getenv("ADMIN_API_KEY") or os.getenv("PROMOTIONS_ADMIN_KEY") or "").strip()
    if not expected or x_admin_key != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="UNAUTHORIZED")


def _row_to_dict(row: Any) -> Dict[str, Any]:
    return dict(row) if row is not None else {}


class ProductGroupMemberInput(BaseModel):
    merchant_id: str = Field(..., description="Internal merchant id")
    platform: str = Field(..., description="Platform identifier (shopify/wix/...)")
    platform_product_id: str = Field(..., description="Platform product id (aka agent product_id)")
    is_primary: bool = Field(default=False, description="Primary/anchor member in the group")


class ProductGroupUpsertRequest(BaseModel):
    product_group_id: str = Field(..., description="Stable product group id")
    members: List[ProductGroupMemberInput] = Field(default_factory=list)


class CategoryPathBackfillRequest(BaseModel):
    mode: str = Field(default="dry_run", description="'dry_run' or 'apply'")
    batch_size: int = Field(default=5000, ge=1, le=10000)
    limit: int = Field(default=0, ge=0, le=50000)
    sample_limit: int = Field(default=20, ge=0, le=100)


@router.get("/merchant/{merchant_id}", response_model=Dict[str, Any])
async def debug_merchant_catalog(
    merchant_id: str = Path(..., description="Internal merchant ID"),
    _: None = Depends(require_admin_key),
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)

    stores = await database.fetch_all(
        """
        SELECT store_id, platform, domain, status, product_count, last_sync, connected_at
        FROM merchant_stores
        WHERE merchant_id = :merchant_id
        ORDER BY connected_at DESC NULLS LAST
        LIMIT 5
        """,
        {"merchant_id": merchant_id},
    )

    cache_counts = await database.fetch_all(
        """
        SELECT platform,
               COUNT(*) AS total_cached,
               COUNT(CASE WHEN expires_at IS NULL OR expires_at > NOW() THEN 1 END) AS active_cached,
               MAX(cached_at) AS last_cached_at,
               MAX(expires_at) AS max_expires_at
        FROM products_cache
        WHERE merchant_id = :merchant_id
        GROUP BY platform
        ORDER BY platform
        """,
        {"merchant_id": merchant_id},
    )

    shopify_import_tasks = await database.fetch_all(
        """
        SELECT id, status, attempt, counts, error, created_at, updated_at, next_run_at
        FROM platform_import_tasks
        WHERE merchant_id = :merchant_id
          AND source_type = 'connector'
          AND connector = 'shopify'
        ORDER BY created_at DESC
        LIMIT 10
        """,
        {"merchant_id": merchant_id},
    )

    # Heuristic: task is "stuck" if running for > 10 minutes without updates.
    stuck_threshold = now - timedelta(minutes=10)
    tasks_out: List[Dict[str, Any]] = []
    for t in shopify_import_tasks or []:
        d = _row_to_dict(t)
        updated_at = d.get("updated_at")
        is_stuck = False
        try:
            if str(d.get("status") or "").lower() == "running" and isinstance(updated_at, datetime):
                upd = updated_at if updated_at.tzinfo else updated_at.replace(tzinfo=timezone.utc)
                is_stuck = upd < stuck_threshold
        except Exception:
            is_stuck = False
        d["is_stuck_running"] = is_stuck
        tasks_out.append(d)

    return {
        "ok": True,
        "merchant_id": merchant_id,
        "now": now.isoformat(),
        "stores": [_row_to_dict(s) for s in stores or []],
        "products_cache_by_platform": [_row_to_dict(r) for r in cache_counts or []],
        "shopify_import_tasks": tasks_out,
    }


@router.post("/merchant/{merchant_id}/reconcile-catalog", response_model=Dict[str, Any])
async def reconcile_merchant_catalog(
    merchant_id: str = Path(..., description="Internal merchant ID"),
    platform: Optional[str] = Query(
        None, description="Limit to one platform (e.g. shopify). Omit for all."
    ),
    limit: int = Query(
        500, ge=1, le=2000,
        description="Max products to ingest this call (bounds LLM-bearing fan-out).",
    ),
    dry_run: bool = Query(
        False, description="When true, only report the diff; no writes."
    ),
    _: None = Depends(require_admin_key),
) -> Dict[str, Any]:
    """Backfill `catalog_products` from `products_cache` for a merchant.

    Heals the gap where a product is visible on the front-end
    (GET /merchant/products) but missing from `catalog_products`, which makes
    the AI Commerce Readiness audit (POST /api/audits) return 404 missing_refs
    for a product the merchant can see. Idempotent; safe to re-run.
    """
    from services.catalog_reconcile_service import reconcile_catalog_from_cache

    report = await reconcile_catalog_from_cache(
        merchant_id=merchant_id,
        platform=platform,
        limit=limit,
        dry_run=dry_run,
    )
    return {"ok": True, **report}


@router.post("/merchant/{merchant_id}/reconcile-store-counts", response_model=Dict[str, Any])
async def reconcile_store_product_counts(
    merchant_id: str = Path(..., description="Internal merchant ID"),
    _: None = Depends(require_admin_key),
) -> Dict[str, Any]:
    """
    Backfill merchant_stores.product_count from products_cache active rows.

    This is an operational helper when the UI depends on merchant_stores.product_count
    but catalog imports only populated products_cache.
    """
    rows = await database.fetch_all(
        """
        SELECT platform,
               COUNT(CASE WHEN expires_at IS NULL OR expires_at > NOW() THEN 1 END) AS active_cached
        FROM products_cache
        WHERE merchant_id = :merchant_id
        GROUP BY platform
        """,
        {"merchant_id": merchant_id},
    )
    counts_by_platform: Dict[str, int] = {}
    for r in rows or []:
        d = dict(r)
        plat = str(d.get("platform") or "").strip().lower()
        if not plat:
            continue
        counts_by_platform[plat] = int(d.get("active_cached") or 0)

    stores = await database.fetch_all(
        """
        SELECT store_id, platform, status, product_count
        FROM merchant_stores
        WHERE merchant_id = :merchant_id
          AND status IN ('active', 'connected')
        ORDER BY connected_at DESC NULLS LAST
        """,
        {"merchant_id": merchant_id},
    )

    updated: List[Dict[str, Any]] = []
    for s in stores or []:
        sd = dict(s)
        store_id = sd.get("store_id")
        platform = str(sd.get("platform") or "").strip().lower()
        target = counts_by_platform.get(platform, 0)
        current = int(sd.get("product_count") or 0)
        if store_id and current != target:
            await database.execute(
                """
                UPDATE merchant_stores
                SET product_count = :count, last_sync = CURRENT_TIMESTAMP
                WHERE store_id = :store_id
                """,
                {"count": target, "store_id": store_id},
            )
            updated.append({"store_id": store_id, "platform": platform, "from": current, "to": target})

    return {"ok": True, "merchant_id": merchant_id, "counts_by_platform": counts_by_platform, "updated": updated}


@router.post("/category-path-backfill", response_model=Dict[str, Any])
async def run_category_path_backfill(
    payload: CategoryPathBackfillRequest,
    _: None = Depends(require_admin_key),
) -> Dict[str, Any]:
    """Run the Phase 2 category_path classifier inside the production network.

    This endpoint exists for ops backfills where the database is only reachable
    from Railway private networking. It calls the same script function used by
    scripts/backfill_pdp_category_path.py.
    """
    mode = str(payload.mode or "").strip().lower()
    if mode not in {"dry_run", "apply"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="mode must be 'dry_run' or 'apply'",
        )

    from scripts.backfill_pdp_category_path import run_category_path_backfill as _run_backfill

    report = await _run_backfill(
        batch_size=int(payload.batch_size),
        limit=int(payload.limit or 0),
        dry_run=(mode == "dry_run"),
        sample_limit=int(payload.sample_limit),
    )
    return {"ok": True, "mode": mode, "report": report}


@router.get("/catalog-products/invariants", response_model=Dict[str, Any])
async def catalog_products_invariants(
    _: None = Depends(require_admin_key),
) -> Dict[str, Any]:
    """Audit invariants for external seed -> catalog_products migration."""
    from scripts.mirror_external_seeds_to_catalog_products import (
        count_missing_catalog_mirrors,
    )

    legacy_external_seed_catalog_rows = await database.fetch_val(
        """
        SELECT count(*)
        FROM catalog_products
        WHERE merchant_id = 'external_seed'
          AND platform = 'external_seed'
          AND coalesce(source_system, '') <> 'external_product_seeds_mirror_v1'
        """
    )
    sig_duplicate_groups = await database.fetch_val(
        """
        SELECT count(*)
        FROM (
          SELECT pivota_signature_id
          FROM catalog_products
          WHERE pivota_signature_id IS NOT NULL
          GROUP BY pivota_signature_id
          HAVING count(*) > 1
        ) d
        """
    )
    identity_duplicate_groups = await database.fetch_val(
        """
        SELECT count(*)
        FROM (
          SELECT merchant_id, platform, source_product_id
          FROM catalog_products
          WHERE source_product_id IS NOT NULL
          GROUP BY merchant_id, platform, source_product_id
          HAVING count(*) > 1
        ) d
        """
    )
    # Same number the report's totals.missing_catalog_products carries, off the
    # cheap chain: this endpoint wants the count, not the seed_data-derived
    # columns COMMON_CTES exists to build (~50s vs ~0.15s on production).
    missing_external_mirror = await count_missing_catalog_mirrors()
    null_category_path = await database.fetch_val(
        """
        SELECT count(*)
        FROM catalog_products
        WHERE category_path IS NULL
        """
    )
    source_rows = await database.fetch_all(
        """
        SELECT coalesce(category_label_source, 'NULL') AS category_label_source,
               count(*) AS rows
        FROM catalog_products
        GROUP BY 1
        ORDER BY rows DESC, category_label_source ASC
        LIMIT 20
        """
    )
    path_rows = await database.fetch_all(
        """
        SELECT category_path, count(*) AS rows
        FROM catalog_products
        WHERE category_label_source IN ('regex_backfill', 'regex_backfill_at_mirror')
        GROUP BY category_path
        ORDER BY rows DESC, category_path ASC
        LIMIT 30
        """
    )
    return {
        "ok": True,
        "legacy_external_seed_catalog_rows": int(legacy_external_seed_catalog_rows or 0),
        "sig_duplicate_groups": int(sig_duplicate_groups or 0),
        "identity_duplicate_groups": int(identity_duplicate_groups or 0),
        "missing_external_mirror": int(missing_external_mirror or 0),
        "catalog_products_null_category_path": int(null_category_path or 0),
        "category_label_source_counts": [_row_to_dict(row) for row in source_rows or []],
        "regex_backfill_path_counts": [_row_to_dict(row) for row in path_rows or []],
    }


@router.post("/product-groups/upsert", response_model=Dict[str, Any])
async def upsert_product_group_members(
    payload: ProductGroupUpsertRequest,
    _: None = Depends(require_admin_key),
) -> Dict[str, Any]:
    """
    Upsert product_group_members rows.

    This is an internal operational helper to curate multi-seller group mappings.
    """
    product_group_id = str(payload.product_group_id or "").strip()
    if not product_group_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="product_group_id is required")

    members = payload.members or []
    if not members:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="members[] is required")

    upserted: List[Dict[str, Any]] = []
    for m in members:
        merchant_id = str(m.merchant_id or "").strip()
        platform = str(m.platform or "").strip().lower()
        platform_product_id = str(m.platform_product_id or "").strip()
        if not merchant_id or not platform or not platform_product_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Each member requires merchant_id, platform, platform_product_id",
            )

        await database.execute(
            """
            INSERT INTO product_group_members (
              product_group_id, merchant_id, platform, platform_product_id, is_primary, updated_at
            ) VALUES (
              :product_group_id, :merchant_id, :platform, :platform_product_id, :is_primary, NOW()
            )
            ON CONFLICT (merchant_id, platform, platform_product_id)
            DO UPDATE SET
              product_group_id = EXCLUDED.product_group_id,
              is_primary = EXCLUDED.is_primary,
              updated_at = NOW()
            """,
            {
                "product_group_id": product_group_id,
                "merchant_id": merchant_id,
                "platform": platform,
                "platform_product_id": platform_product_id,
                "is_primary": bool(m.is_primary),
            },
        )
        upserted.append(
            {
                "product_group_id": product_group_id,
                "merchant_id": merchant_id,
                "platform": platform,
                "product_id": platform_product_id,
                "is_primary": bool(m.is_primary),
            }
        )

    return {"ok": True, "product_group_id": product_group_id, "upserted": upserted}
