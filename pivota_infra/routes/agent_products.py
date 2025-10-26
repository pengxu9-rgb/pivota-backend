"""
Agent Product Browsing API
Allows agents to view merchant's Shopify products for order creation
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from typing import List, Optional, Dict, Any
import httpx
import logging

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
    Get merchant's Shopify products for order creation
    
    Returns real products with variant_ids for proper inventory management
    """
    try:
        # Verify agent has access to this merchant
        if not context.can_access_merchant(merchant_id):
            raise HTTPException(status_code=403, detail="Not authorized for this merchant")
        
        # Get merchant info
        merchant = await get_merchant_onboarding(merchant_id)
        if not merchant:
            raise HTTPException(status_code=404, detail="Merchant not found")
        
        # Check if merchant has connected Shopify
        if not merchant.get("mcp_connected") or merchant.get("mcp_platform") != "shopify":
            raise HTTPException(
                status_code=400, 
                detail="Merchant has not connected Shopify store"
            )
        
        shop_domain = merchant.get("mcp_shop_domain")
        access_token = merchant.get("mcp_access_token")
        
        if not shop_domain or not access_token:
            raise HTTPException(status_code=400, detail="Missing Shopify credentials")
        
        logger.info(f"Fetching products from Shopify store: {shop_domain}")
        
        # Fetch products from Shopify
        url = f"https://{shop_domain}/admin/api/2024-01/products.json"
        params = {"limit": limit, "status": "active"}
        headers = {
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json"
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params, headers=headers, timeout=15.0)
            
            if response.status_code != 200:
                logger.error(f"Shopify API error: {response.status_code} - {response.text[:200]}")
                raise HTTPException(
                    status_code=502,
                    detail=f"Failed to fetch products from Shopify: {response.status_code}"
                )
            
            shopify_data = response.json()
            products = shopify_data.get("products", [])
            
            logger.info(f"Retrieved {len(products)} products from Shopify")
            
            # Transform to agent-friendly format
            agent_products = []
            for product in products:
                # Get all variants
                variants = product.get("variants", [])
                
                for variant in variants:
                    agent_products.append({
                        "product_id": str(product["id"]),
                        "variant_id": str(variant["id"]),
                        "title": product["title"],
                        "variant_title": variant.get("title", "Default"),
                        "price": float(variant.get("price", 0)),
                        "sku": variant.get("sku"),
                        "inventory_quantity": variant.get("inventory_quantity", 0),
                        "available": variant.get("inventory_quantity", 0) > 0,
                        "image_url": product.get("image", {}).get("src") if product.get("image") else None,
                        "currency": merchant.get("currency", "USD")
                    })
            
            # Log request
            background_tasks.add_task(
                log_agent_request,
                context=context,
                status_code=200,
                merchant_id=merchant_id
            )
            
            return {
                "status": "success",
                "merchant_id": merchant_id,
                "store": shop_domain,
                "total_products": len(agent_products),
                "products": agent_products
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
        if not merchant or not merchant.get("mcp_connected"):
            raise HTTPException(status_code=400, detail="Merchant not connected to Shopify")
        
        shop_domain = merchant.get("mcp_shop_domain")
        access_token = merchant.get("mcp_access_token")
        
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

