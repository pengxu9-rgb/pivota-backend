"""
Debug endpoint for products issues
"""
from fastapi import APIRouter, HTTPException
from db.database import database
from utils.logger import logger

router = APIRouter(prefix="/debug", tags=["debug"])

@router.get("/products-cache-info")
async def get_products_cache_info():
    """Get info about products_cache table"""
    try:
        # Check if table exists
        table_check = await database.fetch_one("""
            SELECT COUNT(*) as count 
            FROM information_schema.tables 
            WHERE table_name = 'products_cache'
        """)
        
        if not table_check or table_check["count"] == 0:
            return {"error": "products_cache table does not exist"}
        
        # Get row count
        count_result = await database.fetch_one("SELECT COUNT(*) as count FROM products_cache")
        
        # Get sample data
        sample = await database.fetch_all("SELECT * FROM products_cache LIMIT 2")
        
        # Get column info
        columns = await database.fetch_all("""
            SELECT column_name, data_type 
            FROM information_schema.columns 
            WHERE table_name = 'products_cache'
            ORDER BY ordinal_position
        """)
        
        return {
            "table_exists": True,
            "row_count": count_result["count"] if count_result else 0,
            "columns": [{"name": c["column_name"], "type": c["data_type"]} for c in columns],
            "sample_data": [dict(s) for s in sample] if sample else []
        }
    
    except Exception as e:
        logger.error(f"Debug error: {e}")
        return {"error": str(e)}

@router.post("/test-products-query")
async def test_products_query():
    """Test the products query step by step"""
    try:
        results = {}
        
        # Test 1: Simple select
        try:
            test1 = await database.fetch_one("SELECT 1 as test")
            results["db_connection"] = "OK"
        except Exception as e:
            results["db_connection"] = f"FAILED: {str(e)}"
            return results
        
        # Test 2: Check products_cache
        try:
            test2 = await database.fetch_one("SELECT COUNT(*) as count FROM products_cache")
            results["products_cache_count"] = test2["count"]
        except Exception as e:
            results["products_cache_count"] = f"FAILED: {str(e)}"
            return results
        
        # Test 3: Simple query
        try:
            test3 = await database.fetch_all("""
                SELECT 
                    p.id,
                    p.merchant_id,
                    p.platform
                FROM products_cache p
                LIMIT 1
            """)
            results["simple_query"] = "OK" if test3 else "No data"
        except Exception as e:
            results["simple_query"] = f"FAILED: {str(e)}"
        
        # Test 4: With JSON field
        try:
            test4 = await database.fetch_all("""
                SELECT 
                    p.id,
                    p.merchant_id,
                    p.product_data
                FROM products_cache p
                LIMIT 1
            """)
            results["json_field_query"] = "OK" if test4 else "No data"
            if test4:
                results["sample_product_data"] = dict(test4[0]) if test4 else None
        except Exception as e:
            results["json_field_query"] = f"FAILED: {str(e)}"
        
        # Test 5: Full query
        try:
            test5 = await database.fetch_all("""
                SELECT 
                    p.id,
                    p.merchant_id,
                    p.platform,
                    p.platform_product_id,
                    p.product_data,
                    p.cached_at,
                    m.business_name as merchant_name
                FROM products_cache p
                JOIN merchant_onboarding m ON p.merchant_id = m.merchant_id
                WHERE m.status != 'deleted'
                AND p.cache_status != 'expired'
                ORDER BY p.cached_at DESC
                LIMIT 3
            """)
            results["full_query"] = f"OK - {len(test5)} rows" if test5 else "No data"
        except Exception as e:
            results["full_query"] = f"FAILED: {str(e)}"
        
        return results
    
    except Exception as e:
        logger.error(f"Test error: {e}")
        return {"error": str(e)}
