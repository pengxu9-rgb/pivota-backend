"""Debug endpoints for integration tables"""
from fastapi import APIRouter, Depends
from db import database
from utils.auth import get_current_user

router = APIRouter()

@router.get("/debug/integrations/tables")
async def debug_tables(current_user: dict = Depends(get_current_user)):
    """Check if integration tables exist and their contents"""
    result = {}
    
    # Check if tables exist
    try:
        tables_query = """
            SELECT table_name 
            FROM information_schema.tables 
            WHERE table_schema = 'public' 
            AND table_name IN ('merchant_stores', 'merchant_psps')
        """
        tables = await database.fetch_all(tables_query)
        result["tables_exist"] = [t["table_name"] for t in tables]
    except Exception as e:
        result["tables_error"] = str(e)
    
    # Count records in merchant_stores
    try:
        count_query = "SELECT COUNT(*) as count FROM merchant_stores"
        count = await database.fetch_one(count_query)
        result["merchant_stores_count"] = count["count"]
        
        # Get all stores
        stores = await database.fetch_all("SELECT * FROM merchant_stores LIMIT 10")
        result["merchant_stores_sample"] = [dict(s) for s in stores]
    except Exception as e:
        result["merchant_stores_error"] = str(e)
    
    # Count records in merchant_psps
    try:
        count_query = "SELECT COUNT(*) as count FROM merchant_psps"
        count = await database.fetch_one(count_query)
        result["merchant_psps_count"] = count["count"]
        
        # Get all PSPs
        psps = await database.fetch_all("SELECT * FROM merchant_psps LIMIT 10")
        result["merchant_psps_sample"] = [dict(p) for p in psps]
    except Exception as e:
        result["merchant_psps_error"] = str(e)
    
    return result

@router.post("/debug/integrations/test-insert")
async def test_insert(current_user: dict = Depends(get_current_user)):
    """Test inserting a record directly"""
    try:
        # Test insert into merchant_stores
        await database.execute("""
            INSERT INTO merchant_stores (store_id, merchant_id, platform, name, domain, api_key, status)
            VALUES ('test_store_001', 'merch_6b90dc9838d5fd9c', 'shopify', 'Test Store', 'test.myshopify.com', 'test_key', 'connected')
            ON CONFLICT (store_id) DO NOTHING
        """)
        
        # Test insert into merchant_psps
        await database.execute("""
            INSERT INTO merchant_psps (psp_id, merchant_id, provider, name, api_key, status)
            VALUES ('test_psp_001', 'merch_6b90dc9838d5fd9c', 'stripe', 'Test Stripe', 'sk_test_123', 'active')
            ON CONFLICT (psp_id) DO NOTHING
        """)
        
        return {"status": "success", "message": "Test records inserted"}
    except Exception as e:
        return {"status": "error", "message": str(e)}
