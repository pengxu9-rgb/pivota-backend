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
import json

from routes.agent_api import get_agent_context, AgentContext, log_agent_request
from routes.merchant_onboarding_routes import get_merchant_onboarding
from fastapi import BackgroundTasks
from db.products import get_product_cache_row
from models.standard_product import StandardProduct

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
        if not merchant:
            raise HTTPException(status_code=404, detail="Merchant not found")

        # Get primary store for this merchant (may be Shopify, Wix, etc.)
        from services.merchant_store_service import get_primary_store
        store = await get_primary_store(merchant_id)
        if not store:
            raise HTTPException(
                status_code=404,
                detail="No connected stores found for this merchant",
                headers={"X-Error-Code": "STORE_NOT_FOUND"},
            )

        platform = store.get("platform")

        # For non-Shopify platforms (e.g. Wix), serve details from cached StandardProduct
        if platform != "shopify":
            cache_row = await get_product_cache_row(
                merchant_id=merchant_id,
                platform=platform,
                platform_product_id=product_id,
                include_expired=False,
            )

            if not cache_row:
                raise HTTPException(status_code=404, detail="Product not found")

            product_data = cache_row.get("product_data") or {}
            if isinstance(product_data, str):
                try:
                    product_data = json.loads(product_data)
                except Exception:
                    product_data = {}

            try:
                sp = StandardProduct(**product_data)
                prod = sp.dict()
            except Exception:
                prod = product_data

            product_id_out = str(prod.get("product_id") or prod.get("id") or product_id)
            title = prod.get("title") or prod.get("name") or ""
            description = prod.get("description") or ""
            product_type = prod.get("product_type")
            tags = prod.get("tags") or []

            images = prod.get("images") or []
            if isinstance(images, dict):
                if images.get("image_url"):
                    images = [images.get("image_url")]
                else:
                    images = []

            image_url = prod.get("image_url")
            if image_url and image_url not in images:
                images = [image_url] + images

            variants_raw = prod.get("variants") or []
            variants: List[Dict[str, Any]] = []
            for v in variants_raw:
                vid = str(v.get("variant_id") or v.get("id") or product_id_out)
                price = float(v.get("price") or prod.get("price") or 0)
                inv_qty = int(v.get("inventory_quantity") or 0)
                variants.append(
                    {
                        "variant_id": vid,
                        "title": v.get("title") or "Default",
                        "price": price,
                        "sku": v.get("sku"),
                        "inventory_quantity": inv_qty,
                        "available": inv_qty > 0,
                    }
                )

            if not variants:
                inv_qty = int(prod.get("inventory_quantity") or 0)
                variants.append(
                    {
                        "variant_id": product_id_out,
                        "title": "Default",
                        "price": float(prod.get("price") or 0),
                        "sku": prod.get("sku"),
                        "inventory_quantity": inv_qty,
                        "available": inv_qty > 0,
                    }
                )

            background_tasks.add_task(
                log_agent_request,
                context=context,
                status_code=200,
                merchant_id=merchant_id,
            )

            return {
                "status": "success",
                "product": {
                    "id": product_id_out,
                    "merchant_id": merchant_id,
                    "title": title,
                    "description": description,
                    "vendor": prod.get("vendor"),
                    "product_type": product_type,
                    "variants": variants,
                    "images": images,
                    "tags": tags,
                },
            }

        # Shopify path: fetch fresh details from Shopify Admin API
        shop_domain = store.get("domain") or store.get("shop_domain")
        if not shop_domain:
            raise HTTPException(
                status_code=400,
                detail="Store configuration incomplete - missing domain",
                headers={"X-Error-Code": "INVALID_STORE_CONFIG"},
            )

        api_key_raw = store.get("api_key") or store.get("access_token")

        # Parse JSON token if needed (same logic as product_sync)
        access_token = api_key_raw
        if api_key_raw and api_key_raw.strip().startswith("{"):
            try:
                token_data = json.loads(api_key_raw)
                access_token = (
                    token_data.get("access_token")
                    or token_data.get("token")
                    or api_key_raw
                )
            except Exception:
                pass

        if not access_token:
            raise HTTPException(
                status_code=400,
                detail="Store configuration incomplete - missing credentials",
                headers={"X-Error-Code": "INVALID_STORE_CONFIG"},
            )

        # Fetch product from Shopify
        url = f"https://{shop_domain}/admin/api/2024-01/products/{product_id}.json"
        headers = {
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, timeout=10.0)

        if response.status_code != 200:
            raise HTTPException(status_code=404, detail="Product not found")

        product = response.json()["product"]

        # Transform for agent
        variants = []
        for variant in product.get("variants", []):
            variants.append(
                {
                    "variant_id": str(variant["id"]),
                    "title": variant.get("title", "Default"),
                    "price": float(variant.get("price", 0)),
                    "sku": variant.get("sku"),
                    "inventory_quantity": variant.get("inventory_quantity", 0),
                    "available": variant.get("inventory_quantity", 0) > 0,
                    "weight": variant.get("weight"),
                    "weight_unit": variant.get("weight_unit"),
                }
            )

        background_tasks.add_task(
            log_agent_request,
            context=context,
            status_code=200,
            merchant_id=merchant_id,
        )

        return {
            "status": "success",
            "product": {
                "id": str(product["id"]),
                "merchant_id": merchant_id,
                "title": product["title"],
                "description": product.get("body_html", ""),
                "vendor": product.get("vendor"),
                "product_type": product.get("product_type"),
                "variants": variants,
                "images": [img.get("src") for img in product.get("images", [])],
                "tags": product.get("tags", "").split(",")
                if product.get("tags")
                else [],
            },
        }
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get product details: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get product: {str(e)}")



@router.get("/merchants/{merchant_id}/product/{product_id}/related")
async def get_related_products(
    merchant_id: str,
    product_id: str,
    limit: int = Query(default=10, ge=1, le=50),
    context: AgentContext = Depends(get_agent_context),
    background_tasks: BackgroundTasks = BackgroundTasks()
):
    """
    Recommend related products for a given product.

    Strategy:
    - Fetch products via hybrid query (cache or realtime)
    - Compute similarity based on vendor, product_type, tags, and title tokens
    - Return top-N related items (excluding the current product)
    """
    start_time = time.time()

    # Verify access
    if not context.can_access_merchant(merchant_id):
        raise HTTPException(status_code=403, detail="Not authorized for this merchant")

    try:
        # Load a reasonably sized candidate pool
        candidates, query_source, _ = await get_products_hybrid(
            merchant_id=merchant_id,
            limit=250,
            agent_id=context.agent_id,
            background_tasks=background_tasks,
        )

        # Find target product
        target = None
        for p in candidates:
            if str(p.id) == str(product_id):
                target = p
                break

        if not target and candidates:
            # If exact not found, pick first as fallback basis
            target = candidates[0]

        if not target:
            raise HTTPException(status_code=404, detail="No products available for recommendations")

        def tokenize(text: str) -> set:
            if not text:
                return set()
            return set([t for t in text.lower().replace("/", " ").replace("-", " ").split() if len(t) > 2])

        target_vendor = (target.vendor or "").strip().lower()
        target_type = (target.product_type or "").strip().lower()
        target_tags = set([t.strip().lower() for t in (target.tags or []) if t])
        target_tokens = tokenize(target.title) | tokenize(target.description or "")

        scored = []
        for p in candidates:
            if str(p.id) == str(product_id):
                continue
            score = 0.0
            reasons = []

            # Vendor boost
            if p.vendor and p.vendor.strip().lower() == target_vendor and target_vendor:
                score += 3.0
                reasons.append("same_vendor")

            # Product type boost
            if p.product_type and p.product_type.strip().lower() == target_type and target_type:
                score += 2.0
                reasons.append("same_type")

            # Tag overlap
            p_tags = set([t.strip().lower() for t in (p.tags or []) if t])
            overlap = len(p_tags & target_tags)
            if overlap:
                score += min(2.0, 0.6 * overlap)
                reasons.append("tag_overlap")

            # Title token overlap
            p_tokens = tokenize(p.title) | tokenize(p.description or "")
            token_overlap = len(p_tokens & target_tokens)
            if token_overlap:
                score += min(1.5, 0.3 * token_overlap)
                reasons.append("title_overlap")

            if score > 0:
                # Map to agent product shape
                image_url = p.image_url
                price = p.price
                if p.variants and len(p.variants) > 0:
                    # pick cheapest variant
                    v = sorted(p.variants, key=lambda v: v.price)[0]
                    image_url = v.image_url or image_url
                    price = v.price
                scored.append({
                    "product_id": p.id,
                    "title": p.title,
                    "price": price,
                    "currency": p.currency,
                    "image_url": image_url,
                    "vendor": p.vendor,
                    "product_type": p.product_type,
                    "relevance_score": round(score, 3),
                    "reasons": reasons,
                })

        scored.sort(key=lambda x: x["relevance_score"], reverse=True)
        top_related = scored[:limit]

        response_time_ms = int((time.time() - start_time) * 1000)
        log_query_source(
            agent_id=context.agent_id,
            merchant_id=merchant_id,
            endpoint="/agent/v1/products/merchants/{merchant_id}/product/{product_id}/related",
            query_source=query_source,
            response_time_ms=response_time_ms,
            product_count=len(top_related),
        )

        background_tasks.add_task(
            log_agent_request,
            context=context,
            status_code=200,
            merchant_id=merchant_id,
        )

        return {
            "status": "success",
            "merchant_id": merchant_id,
            "product_id": product_id,
            "query_source": query_source,
            "response_time_ms": response_time_ms,
            "total": len(top_related),
            "related": top_related,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Related products error: {e}")
        raise HTTPException(status_code=500, detail="Failed to compute related products")
