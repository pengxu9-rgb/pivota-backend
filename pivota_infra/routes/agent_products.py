"""
Agent Product Browsing API
Allows agents to view merchant products via hybrid query (cache or realtime)
"""

from services.merchant_store_service import get_merchant_active_stores, get_primary_store
from services.product_query_service import get_products_hybrid, log_query_source
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import List, Optional, Dict, Any
import httpx
import logging
import time

from routes.agent_api import get_agent_context, AgentContext, log_agent_request
from routes.merchant_onboarding_routes import get_merchant_onboarding
from fastapi import BackgroundTasks

router = APIRouter(prefix="/agent/v1/products", tags=["Agent Products"])
logger = logging.getLogger(__name__)


@router.get("/merchants/{merchant_id}")
async def get_merchant_products(
    merchant_id: str,
    limit: int = Query(default=50, le=250),
    context: AgentContext = Depends(get_agent_context),
    background_tasks: BackgroundTasks = BackgroundTasks()
):
    """
    Get merchant products via hybrid query (cache or realtime merchant API)
    
    Automatically decides between:
    - Cached products (fast, <100ms)
    - Realtime merchant API (if configured, <1s)
    
    Returns unified format regardless of source
    """
    start_time = time.time()
    
    try:
        # Verify agent has access to this merchant
        if not context.can_access_merchant(merchant_id):
            raise HTTPException(status_code=403, detail="Not authorized for this merchant")
        
        # Use hybrid query service (decides cache vs realtime)
        products, query_source, error = await get_products_hybrid(
            merchant_id=merchant_id,
            limit=limit,
            agent_id=context.agent_id,
            background_tasks=background_tasks
        )
        
        if error and not products:
            # Both realtime and cache failed
            raise HTTPException(status_code=502, detail=f"Failed to fetch products: {error}")
        
        # Calculate response time
        response_time_ms = int((time.time() - start_time) * 1000)
        
        # Log query with source tracking
        log_query_source(
            agent_id=context.agent_id,
            merchant_id=merchant_id,
            endpoint="/agent/v1/products/merchants/{merchant_id}",
            query_source=query_source,
            response_time_ms=response_time_ms,
            product_count=len(products)
        )
        
        # Transform StandardProduct to agent-friendly format
        agent_products = []
        for product in products:
            # Handle variants (if exists) or create default variant
            if product.variants and len(product.variants) > 0:
                for variant in product.variants:
                    agent_products.append({
                        "product_id": product.id,
                        "variant_id": variant.id,
                        "title": product.title,
                        "variant_title": variant.title,
                        "price": variant.price,
                        "sku": variant.sku,
                        "inventory_quantity": variant.inventory_quantity,
                        "available": variant.inventory_quantity > 0,
                        "image_url": variant.image_url or product.image_url,
                        "currency": product.currency
                    })
            else:
                # No variants - single product
                agent_products.append({
                    "product_id": product.id,
                    "variant_id": product.id,  # Use product_id as variant_id
                    "title": product.title,
                    "variant_title": "Default",
                    "price": product.price,
                    "sku": product.sku,
                    "inventory_quantity": product.inventory_quantity,
                    "available": product.inventory_quantity > 0,
                    "image_url": product.image_url,
                    "currency": product.currency
                })
        
        # Log request for analytics
        background_tasks.add_task(
            log_agent_request,
            context=context,
            status_code=200,
            merchant_id=merchant_id
        )
        
        return {
            "status": "success",
            "merchant_id": merchant_id,
            "query_source": query_source,  # NEW: indicate data source
            "total_products": len(agent_products),
            "products": agent_products,
            "response_time_ms": response_time_ms  # NEW: performance metric
        }
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get merchant products: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get products: {str(e)}")


@router.get("/merchants/{merchant_id}/product/{product_id}")
async def get_product_details(
    merchant_id: str,
    product_id: str,
    context: AgentContext = Depends(get_agent_context),
    background_tasks: BackgroundTasks = BackgroundTasks()
):
    """Get detailed information about a specific product"""
    try:
        # Verify access
        if not context.can_access_merchant(merchant_id):
            raise HTTPException(status_code=403, detail="Not authorized")
        
        merchant = await get_merchant_onboarding(merchant_id)
        if not merchant or not True:
            raise HTTPException(status_code=400, detail="Merchant not connected to Shopify")
        
        shop_domain = store_info.get("domain")
        access_token = store_info.get("api_key")
        
        # Fetch product from Shopify
        url = f"https://{shop_domain}/admin/api/2024-01/products/{product_id}.json"
        headers = {
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json"
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, timeout=10.0)
            
            if response.status_code != 200:
                raise HTTPException(status_code=404, detail="Product not found")
            
            product = response.json()["product"]
            
            # Transform for agent
            variants = []
            for variant in product.get("variants", []):
                variants.append({
                    "variant_id": str(variant["id"]),
                    "title": variant.get("title", "Default"),
                    "price": float(variant.get("price", 0)),
                    "sku": variant.get("sku"),
                    "inventory_quantity": variant.get("inventory_quantity", 0),
                    "available": variant.get("inventory_quantity", 0) > 0,
                    "weight": variant.get("weight"),
                    "weight_unit": variant.get("weight_unit")
                })
            
            background_tasks.add_task(
                log_agent_request,
                context=context,
                status_code=200,
                merchant_id=merchant_id
            )
            
            return {
                "status": "success",
                "product": {
                    "id": str(product["id"]),
                    "title": product["title"],
                    "description": product.get("body_html", ""),
                    "vendor": product.get("vendor"),
                    "product_type": product.get("product_type"),
                    "variants": variants,
                    "images": [img.get("src") for img in product.get("images", [])],
                    "tags": product.get("tags", "").split(",") if product.get("tags") else []
                }
            }
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get product details: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get product: {str(e)}")


