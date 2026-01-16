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

from fastapi import APIRouter, Depends, Header, HTTPException, Path, status

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
