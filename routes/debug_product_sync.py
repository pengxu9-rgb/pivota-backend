from fastapi import APIRouter, Depends
from db.database import database
from adapters.product_adapters import WixProductAdapter
from services.wix_connection import extract_wix_site_id, normalize_wix_api_key
from utils.auth import require_admin
import logging

router = APIRouter(prefix="/debug", tags=["debug"])
logger = logging.getLogger(__name__)

@router.post("/test-wix-sync/{merchant_id}")
async def debug_wix_sync(merchant_id: str, current_user: dict = Depends(require_admin)):
    """Debug Wix product sync

    SECURITY: admin-gated. This was the byte-twin of GET /debug/test-shopify/{merchant_id}, mounted
    one line above it and with the same defect: no auth dependency, a caller-supplied merchant_id
    selecting any merchant's row, and that merchant's stored Wix API key spent to fetch their
    products. It was the worse of the pair, because its handler returned traceback.format_exc() to
    the caller. Gating one of an adjacent pair and leaving the other mounted is not a fix.
    """
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
        
        # The stored credential may be a JSON blob (`api_key`/`site_id`/
        # `instance_id`), and sending that verbatim as the Authorization header
        # leaks the whole blob to Wix and fails the call. One reader, the same
        # one every other Wix caller uses.
        raw_credential = store["api_key"]
        api_key = normalize_wix_api_key(raw_credential)
        try:
            site_id = extract_wix_site_id(store["domain"], raw_credential)
        except Exception:
            site_id = store["domain"]
        
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
        # The traceback is LOGGED, not returned. It carried module paths, local state and, on a DB
        # error, connection detail straight to the caller.
        logger.exception("debug_wix_sync failed merchant=%s", merchant_id)
        return {
            "status": "error",
            "error_type": type(e).__name__,
        }


