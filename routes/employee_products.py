"""
Employee Products (MVP)

Purpose:
- Provide an employee-facing products list/search and product detail view backed by products_cache.
- Designed to degrade gracefully while metrics/supply signals are sparse in early stages.

NOTE (v0):
- This is not the final 10M-scale read model. It is a bridge that enables the
  employee portal UX while the `employee_products_index` rollups are built.
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from db.database import database
from db.products import products_cache
from models.standard_product import StandardProduct
from utils.auth import get_current_employee


router = APIRouter(prefix="/employee/products", tags=["employee-products"])


def _as_product_card(row: Dict[str, Any]) -> Dict[str, Any]:
    merchant_id = row.get("merchant_id")
    platform = row.get("platform")
    platform_product_id = row.get("platform_product_id")
    product_data = row.get("product_data") or {}

    try:
        sp = StandardProduct.parse_obj(product_data)
        title = sp.title
        image_url = sp.image_url or (sp.images[0] if sp.images else None)
        product_id = sp.product_id or sp.id or platform_product_id
        variants = sp.variants or []
        currency = getattr(sp, "currency", None) or product_data.get("currency")
        price = getattr(sp, "price", None) if hasattr(sp, "price") else product_data.get("price")
    except Exception:
        title = product_data.get("title") or product_data.get("name") or platform_product_id
        image_url = product_data.get("image_url") or None
        product_id = product_data.get("product_id") or product_data.get("id") or platform_product_id
        variants = product_data.get("variants") or []
        currency = product_data.get("currency")
        price = product_data.get("price")

    return {
        "product_key": f"{merchant_id}|{platform}|{platform_product_id}",
        "merchant_id": merchant_id,
        "platform": platform,
        "platform_product_id": platform_product_id,
        "product_id": product_id,
        "title": title,
        "image_url": image_url,
        "variants_count": len(variants) if isinstance(variants, list) else 0,
        "price": {"value": price, "currency": currency},
        "cached_at": row.get("cached_at").isoformat() if row.get("cached_at") else None,
        "expires_at": row.get("expires_at").isoformat() if row.get("expires_at") else None,
    }


@router.get("/search")
async def search_products(
    q: Optional[str] = Query(default=None, description="Search by product_id/platform_product_id/title (best-effort)"),
    merchant_id: Optional[str] = Query(default=None),
    platform: Optional[str] = Query(default=None),
    limit: int = Query(default=30, ge=1, le=100),
    after_id: Optional[int] = Query(default=None, description="Cursor: return rows with id < after_id"),
    current_user: dict = Depends(get_current_employee),
):
    """
    Employee-facing product search over products_cache.

    v0 behavior:
    - Sort: most recently inserted cache rows (id desc).
    - Pagination: keyset on products_cache.id (after_id).
    - Search: best-effort exact id match + title ILIKE when supported.
    """
    where = []
    values: Dict[str, Any] = {"limit": limit}

    if merchant_id:
        where.append("merchant_id = :merchant_id")
        values["merchant_id"] = merchant_id
    if platform:
        where.append("platform = :platform")
        values["platform"] = platform
    if after_id is not None:
        where.append("id < :after_id")
        values["after_id"] = after_id

    base = "SELECT id, merchant_id, platform, platform_product_id, product_data, cached_at, expires_at FROM products_cache"
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    order_limit = " ORDER BY id DESC LIMIT :limit"

    rows: List[Dict[str, Any]] = []
    if q:
        q = q.strip()

    if q:
        # Best-effort: attempt title ILIKE + JSON product_id match (Postgres).
        try:
            q_clause = (
                " (platform_product_id = :q"
                " OR product_data->>'product_id' = :q"
                " OR product_data->>'id' = :q"
                " OR product_data->>'title' ILIKE :q_like"
                " OR product_data->>'name' ILIKE :q_like)"
            )
            values["q"] = q
            values["q_like"] = f"%{q}%"
            rows = await database.fetch_all(
                f"{base}{clause}{' AND ' if clause else ' WHERE '}{q_clause}{order_limit}",
                values,
            )
        except Exception:
            # Fallback: exact matches only.
            q_clause = " (platform_product_id = :q OR product_data->>'product_id' = :q OR product_data->>'id' = :q)"
            values["q"] = q
            rows = await database.fetch_all(
                f"{base}{clause}{' AND ' if clause else ' WHERE '}{q_clause}{order_limit}",
                values,
            )
    else:
        rows = await database.fetch_all(f"{base}{clause}{order_limit}", values)

    cards = [_as_product_card(dict(r)) for r in rows]
    next_after_id = int(rows[-1]["id"]) if rows else None

    return {
        "status": "success",
        "items": cards,
        "next": {"after_id": next_after_id},
    }


@router.get("/{product_key}")
async def get_product_by_key(
    product_key: str,
    current_user: dict = Depends(get_current_employee),
):
    """
    Product detail by product_key, where product_key = "{merchant_id}|{platform}|{platform_product_id}".
    """
    parts = [p.strip() for p in (product_key or "").split("|")]
    if len(parts) != 3:
        raise HTTPException(status_code=400, detail="INVALID_PRODUCT_KEY")

    merchant_id, platform, platform_product_id = parts
    row = await database.fetch_one(
        products_cache.select().where(
            (products_cache.c.merchant_id == merchant_id)
            & (products_cache.c.platform == platform)
            & (products_cache.c.platform_product_id == platform_product_id)
        )
    )
    if not row:
        raise HTTPException(status_code=404, detail="PRODUCT_NOT_FOUND")

    row = dict(row)
    product_data = row.get("product_data") or {}

    # Parse best-effort StandardProduct for normalized fields, but return the raw JSON as well.
    try:
        sp = StandardProduct.parse_obj(product_data)
        normalized = sp.dict()
    except Exception:
        normalized = None

    return {
        "status": "success",
        "product_key": product_key,
        "merchant_id": merchant_id,
        "platform": platform,
        "platform_product_id": platform_product_id,
        "cached_at": row.get("cached_at").isoformat() if row.get("cached_at") else None,
        "expires_at": row.get("expires_at").isoformat() if row.get("expires_at") else None,
        "product": normalized,
        "raw": product_data,
        # v0 placeholders for the employee page; these will be replaced by rollups/index later.
        "metrics": {
            "sales_7d": 0,
            "sales_30d": 0,
            "gmv_7d": {"currency": product_data.get("currency") or "USD", "amount": 0},
            "gmv_30d": {"currency": product_data.get("currency") or "USD", "amount": 0},
            "merchants_selling": 1,
        },
    }
