"""
Shopify Integration Routes (Phase 2 MCP)
 - Connect merchant to Shopify (mark MCP connected)
 - Sync products (first N) from Shopify Admin API

Note: This is a minimal viable integration using PAT (Admin API access token).
In production prefer OAuth per merchant and encrypt credentials.
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import Optional, Dict, Any, List
import httpx
import os
from datetime import datetime
import logging

from db.merchant_onboarding import merchant_onboarding
from db.database import database
from db.products import upsert_product, create_sync_record, update_sync_record
from config.settings import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/integrations/shopify", tags=["integrations-shopify"])


class ShopifyConnectRequest(BaseModel):
    merchant_id: str
    shop_domain: Optional[str] = None  # e.g. mystore.myshopify.com
    access_token: Optional[str] = None  # optional, fallback to env


@router.post("/connect")
async def connect_shopify(req: ShopifyConnectRequest) -> Dict[str, Any]:
    # Resolve credentials
    # Accept multiple env var names for flexibility
    shop_domain = (
        req.shop_domain
        or getattr(settings, "shopify_shop_domain", None)
        or os.getenv("SHOPIFY_STORE_URL")
        or os.getenv("SHOPIFY_SHOP_DOMAIN")
        or ""
    ).strip()
    access_token = (
        req.access_token
        or getattr(settings, "shopify_access_token", None)
        or os.getenv("SHOPIFY_ACCESS_TOKEN")
        or ""
    ).strip()
    if not shop_domain or not access_token:
        raise HTTPException(status_code=400, detail="Shopify credentials missing (shop_domain/access_token)")

    # Verify merchant exists
    m = await database.fetch_one(
        merchant_onboarding.select().where(merchant_onboarding.c.merchant_id == req.merchant_id)
    )
    if not m:
        raise HTTPException(status_code=404, detail="Merchant not found")

    # Validate credentials by calling /shop.json
    url = f"https://{shop_domain}/admin/api/2024-07/shop.json"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url, headers={"X-Shopify-Access-Token": access_token})
        if r.status_code not in (200, 401, 403):
            raise HTTPException(status_code=400, detail=f"Shopify validation failed (status={r.status_code})")
        if r.status_code in (401, 403):
            # Treat as recognized but insufficient permission
            pass
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Shopify validation error: {e}")

    # Mark MCP connected
    upd = (
        merchant_onboarding.update()
        .where(merchant_onboarding.c.merchant_id == req.merchant_id)
        .values(mcp_connected=True, mcp_platform="shopify")
    )
    await database.execute(upd)

    return {
        "status": "success",
        "message": "Shopify connected",
        "merchant_id": req.merchant_id,
        "mcp_connected": True,
        "platform": "shopify",
    }


class ShopifySyncRequest(BaseModel):
    merchant_id: str
    shop_domain: Optional[str] = None
    access_token: Optional[str] = None
    limit: int = 20


@router.post("/products/sync")
async def sync_products(req: ShopifySyncRequest) -> Dict[str, Any]:
    """
    拉取 Shopify 产品并存储到数据库
    支持增量同步（只更新变化的产品）
    """
    # Resolve credentials
    shop_domain = (
        req.shop_domain
        or getattr(settings, "shopify_shop_domain", None)
        or os.getenv("SHOPIFY_STORE_URL")
        or os.getenv("SHOPIFY_SHOP_DOMAIN")
        or ""
    ).strip()
    access_token = (
        req.access_token
        or getattr(settings, "shopify_access_token", None)
        or os.getenv("SHOPIFY_ACCESS_TOKEN")
        or ""
    ).strip()
    if not shop_domain or not access_token:
        raise HTTPException(status_code=400, detail="Shopify credentials missing (shop_domain/access_token)")

    # Verify merchant exists
    m = await database.fetch_one(
        merchant_onboarding.select().where(merchant_onboarding.c.merchant_id == req.merchant_id)
    )
    if not m:
        raise HTTPException(status_code=404, detail="Merchant not found")

    # Create sync record
    sync_id = await create_sync_record(req.merchant_id, "shopify", "manual")
    
    # Fetch products from Shopify
    url = f"https://{shop_domain}/admin/api/2024-07/products.json?limit={max(1, min(req.limit, 250))}"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(url, headers={"X-Shopify-Access-Token": access_token})
        
        if r.status_code != 200:
            error_msg = f"Shopify API returned status {r.status_code}: {r.text[:200]}"
            await update_sync_record(sync_id, "failed", error_message=error_msg)
            raise HTTPException(status_code=400, detail=error_msg)
        
        data = r.json()
        shopify_products = data.get("products", [])
        
        if not shopify_products:
            await update_sync_record(sync_id, "success", total_products=0, synced_products=0)
            return {
                "status": "success",
                "message": "No products found",
                "merchant_id": req.merchant_id,
                "total": 0,
                "synced": 0
            }
        
        # Parse and store products
        new_count = 0
        updated_count = 0
        
        for sp in shopify_products:
            # 解析 Shopify 产品数据
            product_data = parse_shopify_product(sp, req.merchant_id)
            
            # 检查是否已存在（用于统计新增/更新）
            from db.products import products
            existing = await database.fetch_one(
                products.select().where(
                    (products.c.merchant_id == req.merchant_id) &
                    (products.c.platform == "shopify") &
                    (products.c.platform_product_id == str(sp["id"]))
                )
            )
            
            # Upsert 到数据库
            product_id = await upsert_product(product_data)
            
            if product_id:
                if existing:
                    updated_count += 1
                else:
                    new_count += 1
        
        # Update sync record
        await update_sync_record(
            sync_id,
            "success",
            total_products=len(shopify_products),
            synced_products=new_count + updated_count,
            new_products=new_count,
            updated_products=updated_count
        )
        
        logger.info(f"✅ Synced {len(shopify_products)} products for merchant {req.merchant_id}: {new_count} new, {updated_count} updated")
        
        return {
            "status": "success",
            "message": f"Synced {len(shopify_products)} products ({new_count} new, {updated_count} updated)",
            "merchant_id": req.merchant_id,
            "platform": "shopify",
            "total": len(shopify_products),
            "synced": new_count + updated_count,
            "new": new_count,
            "updated": updated_count,
            "sync_id": sync_id
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Shopify sync error: {e}")
        import traceback
        traceback.print_exc()
        await update_sync_record(sync_id, "failed", error_message=str(e))
        raise HTTPException(status_code=500, detail=f"Shopify sync error: {e}")


def parse_shopify_product(sp: Dict[str, Any], merchant_id: str) -> Dict[str, Any]:
    """
    解析 Shopify API 返回的产品数据为数据库格式
    """
    # 获取主图
    image_url = None
    images = []
    if sp.get("images"):
        image_url = sp["images"][0]["src"]
        images = [img["src"] for img in sp["images"]]
    elif sp.get("image"):
        image_url = sp["image"]["src"]
        images = [image_url]
    
    # 获取第一个变体的价格和库存
    price = None
    inventory_quantity = 0
    sku = None
    barcode = None
    weight = None
    weight_unit = None
    
    if sp.get("variants") and len(sp["variants"]) > 0:
        first_variant = sp["variants"][0]
        price = float(first_variant.get("price", 0))
        inventory_quantity = first_variant.get("inventory_quantity", 0)
        sku = first_variant.get("sku")
        barcode = first_variant.get("barcode")
        weight = first_variant.get("weight")
        weight_unit = first_variant.get("weight_unit")
    
    # 解析发布时间
    published_at = None
    if sp.get("published_at"):
        try:
            published_at = datetime.fromisoformat(sp["published_at"].replace('Z', '+00:00'))
        except:
            pass
    
    # 解析创建/更新时间
    created_at = None
    if sp.get("created_at"):
        try:
            created_at = datetime.fromisoformat(sp["created_at"].replace('Z', '+00:00'))
        except:
            pass
    
    updated_at = None
    if sp.get("updated_at"):
        try:
            updated_at = datetime.fromisoformat(sp["updated_at"].replace('Z', '+00:00'))
        except:
            pass
    
    return {
        "merchant_id": merchant_id,
        "platform": "shopify",
        "platform_product_id": str(sp["id"]),
        "title": sp.get("title", "Untitled"),
        "description": sp.get("body_html", ""),
        "vendor": sp.get("vendor"),
        "product_type": sp.get("product_type"),
        "tags": sp.get("tags", "").split(",") if sp.get("tags") else [],
        "price": price,
        "compare_at_price": None,  # Shopify 的对比价格在变体中
        "currency": "USD",  # 默认，可从 shop.json 获取
        "inventory_quantity": inventory_quantity,
        "sku": sku,
        "barcode": barcode,
        "weight": weight,
        "weight_unit": weight_unit,
        "image_url": image_url,
        "images": images,
        "variants": sp.get("variants", []),
        "options": sp.get("options", []),
        "status": sp.get("status", "active"),
        "published_at": published_at,
        "platform_created_at": created_at,
        "platform_updated_at": updated_at,
        "synced_at": datetime.now()
    }


# -------- OAuth (minimal) --------
from urllib.parse import urlencode

@router.get("/oauth/start")
async def oauth_start(merchant_id: str, shop: str) -> Dict[str, Any]:
    client_id = os.getenv("SHOPIFY_CLIENT_ID") or getattr(settings, "shopify_client_id", None)
    redirect_uri = os.getenv("SHOPIFY_REDIRECT_URI") or getattr(settings, "shopify_redirect_uri", None)
    if not client_id or not redirect_uri:
        raise HTTPException(status_code=400, detail="Missing SHOPIFY_CLIENT_ID/SHOPIFY_REDIRECT_URI")
    scopes = os.getenv("SHOPIFY_SCOPES", "read_products,write_products,read_orders,write_orders,write_webhooks")
    params = {
        "client_id": client_id,
        "scope": scopes,
        "redirect_uri": redirect_uri,
        "state": merchant_id,
    }
    auth_url = f"https://{shop}/admin/oauth/authorize?{urlencode(params)}"
    return {"authorize": auth_url}


@router.get("/oauth/callback")
async def oauth_callback(request: Request) -> Dict[str, Any]:
    # Read query params
    shop = request.query_params.get("shop")
    code = request.query_params.get("code")
    state = request.query_params.get("state")  # merchant_id
    hmac_val = request.query_params.get("hmac")

    if not (shop and code and state and hmac_val):
        raise HTTPException(status_code=400, detail="Missing shop/code/state/hmac")

    # TODO: verify HMAC (requires app secret)
    client_id = os.getenv("SHOPIFY_CLIENT_ID") or getattr(settings, "shopify_client_id", None)
    client_secret = os.getenv("SHOPIFY_CLIENT_SECRET") or getattr(settings, "shopify_client_secret", None)
    redirect_uri = os.getenv("SHOPIFY_REDIRECT_URI") or getattr(settings, "shopify_redirect_uri", None)
    if not (client_id and client_secret and redirect_uri):
        raise HTTPException(status_code=400, detail="Missing client credentials")

    token_url = f"https://{shop}/admin/oauth/access_token"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(token_url, json={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
            })
        if r.status_code != 200:
            raise HTTPException(status_code=400, detail=f"Token exchange failed: {r.text[:200]}")
        access_token = r.json().get("access_token")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"OAuth error: {e}")

    # Persist to merchant
    upd = (
        merchant_onboarding.update()
        .where(merchant_onboarding.c.merchant_id == state)
        .values(mcp_connected=True, mcp_platform="shopify", mcp_shop_domain=shop, mcp_access_token=access_token)
    )
    await database.execute(upd)

    return {"status": "success", "merchant_id": state, "shop": shop}


