"""
Product Management Routes
列表、搜索、分页、筛选产品
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional, Dict, Any
from datetime import datetime

from db.products import get_products, get_product_count, get_sync_history
from routes.auth_routes import require_admin

router = APIRouter(prefix="/products", tags=["products"])


@router.get("/", response_model=Dict[str, Any])
async def list_products(
    merchant_id: Optional[str] = Query(None, description="Filter by merchant ID"),
    platform: Optional[str] = Query(None, description="Filter by platform (shopify/wix/woocommerce)"),
    status: Optional[str] = Query(None, description="Filter by status (active/draft/archived)"),
    search: Optional[str] = Query(None, description="Search in title, description, SKU"),
    page: int = Query(1, ge=1, description="Page number (starts from 1)"),
    page_size: int = Query(50, ge=1, le=100, description="Items per page"),
    current_user: dict = Depends(require_admin)
):
    """
    Admin: 获取产品列表（支持筛选、搜索、分页）
    """
    offset = (page - 1) * page_size
    
    products = await get_products(
        merchant_id=merchant_id,
        platform=platform,
        status=status,
        search=search,
        limit=page_size,
        offset=offset
    )
    
    total = await get_product_count(
        merchant_id=merchant_id,
        platform=platform,
        status=status,
        search=search
    )
    
    return {
        "status": "success",
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size,
        "products": [
            {
                "id": p["id"],
                "merchant_id": p["merchant_id"],
                "platform": p["platform"],
                "platform_product_id": p["platform_product_id"],
                "title": p["title"],
                "description": p.get("description", "")[:200] + "..." if p.get("description") and len(p.get("description", "")) > 200 else p.get("description", ""),
                "vendor": p.get("vendor"),
                "product_type": p.get("product_type"),
                "price": p.get("price"),
                "currency": p.get("currency", "USD"),
                "inventory_quantity": p.get("inventory_quantity", 0),
                "sku": p.get("sku"),
                "image_url": p.get("image_url"),
                "status": p.get("status", "active"),
                "synced_at": p["synced_at"].isoformat() if p.get("synced_at") else None,
                "created_at": p["created_at"].isoformat() if p.get("created_at") else None,
            }
            for p in products
        ]
    }


@router.get("/{product_id}", response_model=Dict[str, Any])
async def get_product_details(
    product_id: int,
    current_user: dict = Depends(require_admin)
):
    """
    Admin: 获取产品详情（包含所有字段）
    """
    from db.products import products
    from db.database import database
    
    product = await database.fetch_one(
        products.select().where(products.c.id == product_id)
    )
    
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    
    return {
        "status": "success",
        "product": dict(product)
    }


@router.get("/sync/history", response_model=Dict[str, Any])
async def get_sync_history_api(
    merchant_id: Optional[str] = Query(None, description="Filter by merchant ID"),
    limit: int = Query(20, ge=1, le=100, description="Number of records to return"),
    current_user: dict = Depends(require_admin)
):
    """
    Admin: 获取产品同步历史记录
    """
    history = await get_sync_history(merchant_id=merchant_id, limit=limit)
    
    return {
        "status": "success",
        "count": len(history),
        "history": [
            {
                "id": h["id"],
                "merchant_id": h["merchant_id"],
                "platform": h["platform"],
                "sync_type": h["sync_type"],
                "status": h["status"],
                "total_products": h.get("total_products", 0),
                "synced_products": h.get("synced_products", 0),
                "new_products": h.get("new_products", 0),
                "updated_products": h.get("updated_products", 0),
                "error_message": h.get("error_message"),
                "started_at": h["started_at"].isoformat() if h.get("started_at") else None,
                "completed_at": h["completed_at"].isoformat() if h.get("completed_at") else None,
                "created_at": h["created_at"].isoformat() if h.get("created_at") else None,
            }
            for h in history
        ]
    }

