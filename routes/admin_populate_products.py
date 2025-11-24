"""
Test endpoint to populate products_cache with sample data
"""
from fastapi import APIRouter, HTTPException
from db.database import database
from db.products import upsert_product_cache
from datetime import datetime
import json

router = APIRouter(prefix="/test", tags=["test"])

@router.post("/populate-products-cache")
async def populate_test_products():
    """Populate products_cache with test data for development"""
    try:
        # Sample product data
        test_products = [
            {
                "id": "prod_001",
                "name": "Premium Coffee Beans",
                "description": "High quality arabica coffee beans from Colombia",
                "price": 24.99,
                "currency": "USD",
                "category": "Beverages",
                "in_stock": True,
                "image_url": "https://example.com/coffee.jpg",
                "url": "https://example.com/products/coffee",
                "sku": "COF-001",
                "vendor": "Colombian Farms"
            },
            {
                "id": "prod_002",
                "name": "Organic Green Tea",
                "description": "Premium organic green tea leaves",
                "price": 18.50,
                "currency": "USD",
                "category": "Beverages",
                "in_stock": True,
                "image_url": "https://example.com/tea.jpg",
                "url": "https://example.com/products/tea",
                "sku": "TEA-001",
                "vendor": "Tea Gardens"
            },
            {
                "id": "prod_003",
                "name": "Dark Chocolate Bar",
                "description": "70% cacao dark chocolate, ethically sourced",
                "price": 8.99,
                "currency": "USD",
                "category": "Food",
                "in_stock": False,
                "image_url": "https://example.com/chocolate.jpg",
                "url": "https://example.com/products/chocolate",
                "sku": "CHO-001",
                "vendor": "Artisan Chocolates"
            }
        ]
        
        # Get a test merchant
        merchant = await database.fetch_one(
            "SELECT merchant_id FROM merchant_onboarding WHERE status = 'approved' LIMIT 1"
        )
        
        if not merchant:
            return {"error": "No approved merchant found"}
        
        merchant_id = merchant["merchant_id"]
        
        # Insert products into cache
        inserted = []
        for product in test_products:
            cache_id = await upsert_product_cache(
                merchant_id=merchant_id,
                platform="shopify",
                platform_product_id=product["id"],
                product_data=product,
                ttl_seconds=86400  # 24 hours
            )
            inserted.append({
                "product_id": product["id"],
                "name": product["name"],
                "cache_id": cache_id
            })
        
        return {
            "status": "success",
            "merchant_id": merchant_id,
            "products_inserted": len(inserted),
            "products": inserted
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to populate cache: {str(e)}")

@router.get("/check-products-cache")
async def check_products_cache():
    """Check products_cache status"""
    try:
        # Get count
        count_result = await database.fetch_one("SELECT COUNT(*) as count FROM products_cache")
        
        # Get sample
        sample = await database.fetch_all("""
            SELECT 
                p.merchant_id,
                p.platform,
                p.platform_product_id,
                p.product_data->>'name' as product_name,
                p.product_data->>'price' as price,
                p.cached_at,
                p.expires_at
            FROM products_cache p
            LIMIT 5
        """)
        
        return {
            "total_products": count_result["count"],
            "sample": [dict(s) for s in sample]
        }
    
    except Exception as e:
        return {"error": str(e)}
