from fastapi import APIRouter
from db.database import database
from adapters.product_adapters import WixProductAdapter
import logging

router = APIRouter(prefix="/debug", tags=["debug"])
logger = logging.getLogger(__name__)

@router.post("/test-wix-sync/{merchant_id}")
async def debug_wix_sync(merchant_id: str):
    """Debug Wix product sync"""
    try:
        # Get store from merchant_stores
        store_query = """
            SELECT platform, domain, api_key, status 
            FROM merchant_stores 
            WHERE merchant_id = :merchant_id AND platform = 'wix' AND status = 'active'
            LIMIT 1
        """
        store = await database.fetch_one(store_query, {"merchant_id": merchant_id})
        
        if not store:
            return {"error": "No Wix store found", "merchant_id": merchant_id}
        
        site_id = store["domain"]
        api_key = store["api_key"]
        
        # Test Wix adapter
        products, next_token, error = await WixProductAdapter.fetch_products(
            site_id=site_id,
            api_key=api_key,
            merchant_id=merchant_id,
            limit=10
        )
        
        if error:
            return {
                "status": "error",
                "error": error,
                "site_id": site_id,
                "has_api_key": bool(api_key)
            }
        
        return {
            "status": "success",
            "products_count": len(products),
            "products": [{"id": p.id, "title": p.title, "price": p.price} for p in products[:3]],
            "site_id": site_id
        }
        
    except Exception as e:
        import traceback
        return {
            "status": "error",
            "error": str(e),
            "error_type": type(e).__name__,
            "traceback": traceback.format_exc()
        }

