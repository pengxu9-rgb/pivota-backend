"""Debug endpoint to check MCP data in database"""
from fastapi import APIRouter, Depends, HTTPException
from utils.auth import ADMIN_ROLES, get_current_user
from db.database import database
import logging

router = APIRouter(prefix="/admin/debug-mcp", tags=["Debug MCP"])
logger = logging.getLogger(__name__)

@router.get("/check-tables")
async def check_mcp_tables(current_user: dict = Depends(get_current_user)):
    """Check what data exists in MCP-related tables"""
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin only")
    
    try:
        # Check merchant_stores (new MCP store system)
        stores_count = await database.fetch_one(
            "SELECT COUNT(*) as count FROM merchant_stores"
        )
        
        stores_sample = await database.fetch_all(
            """SELECT merchant_id, platform, name as store_name, status
               FROM merchant_stores
               LIMIT 5"""
        )
        
        # Check merchant_psps
        psp_count = await database.fetch_one(
            "SELECT COUNT(*) as count FROM merchant_psps WHERE status = 'active'"
        )
        
        psp_sample = await database.fetch_all(
            """SELECT merchant_id, provider, status 
               FROM merchant_psps 
               LIMIT 5"""
        )
        
        # Check merchant_onboarding (legacy MCP fields)
        mcp_legacy = await database.fetch_all(
            """SELECT merchant_id, business_name, mcp_platform, mcp_shop_domain, mcp_connected
               FROM merchant_onboarding
               WHERE mcp_connected = true OR mcp_platform IS NOT NULL
               LIMIT 5"""
        )
        
        # Check products_cache
        products_count = await database.fetch_one(
            "SELECT COUNT(*) as count FROM products_cache"
        )
        
        return {
            "status": "success",
            "data": {
                "merchant_stores": {
                    "total": dict(stores_count).get("count", 0) if stores_count else 0,
                    "sample": [dict(r) for r in stores_sample]
                },
                "merchant_psps": {
                    "total": dict(psp_count).get("count", 0) if psp_count else 0,
                    "sample": [dict(r) for r in psp_sample]
                },
                "legacy_mcp_fields": {
                    "total": len(mcp_legacy),
                    "sample": [dict(r) for r in mcp_legacy]
                },
                "products_cache": {
                    "total": dict(products_count).get("count", 0) if products_count else 0
                }
            }
        }
    
    except Exception as e:
        logger.error(f"Error checking MCP data: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/merchant/{merchant_id}/full-data")
async def get_merchant_full_data(
    merchant_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get all data for a specific merchant"""
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin only")
    
    try:
        # Get merchant
        merchant = await database.fetch_one(
            "SELECT * FROM merchant_onboarding WHERE merchant_id = :merchant_id",
            {"merchant_id": merchant_id}
        )
        
        # Get stores (new system)
        stores = await database.fetch_all(
            "SELECT * FROM merchant_stores WHERE merchant_id = :merchant_id",
            {"merchant_id": merchant_id}
        )
        
        # Get PSPs
        psps = await database.fetch_all(
            "SELECT * FROM merchant_psps WHERE merchant_id = :merchant_id",
            {"merchant_id": merchant_id}
        )
        
        # Get products
        products = await database.fetch_all(
            "SELECT COUNT(*) as count, platform FROM products_cache WHERE merchant_id = :merchant_id GROUP BY platform",
            {"merchant_id": merchant_id}
        )
        
        # Get orders
        orders = await database.fetch_all(
            "SELECT COUNT(*) as count, payment_status FROM orders WHERE merchant_id = :merchant_id GROUP BY payment_status",
            {"merchant_id": merchant_id}
        )
        
        return {
            "status": "success",
            "merchant_id": merchant_id,
            "merchant": dict(merchant) if merchant else None,
            "stores": {
                "count": len(stores),
                "data": [dict(s) for s in stores]
            },
            "psps": {
                "count": len(psps),
                "data": [dict(p) for p in psps]
            },
            "products": {
                "count": len(products),
                "by_platform": [dict(p) for p in products]
            },
            "orders": {
                "count": len(orders),
                "by_status": [dict(o) for o in orders]
            }
        }
    
    except Exception as e:
        logger.error(f"Error getting merchant full data: {e}")
        raise HTTPException(status_code=500, detail=str(e))

from fastapi import APIRouter, Depends, HTTPException
# NOTE: this module's whole body appears TWICE; `router` is rebound here, so
# main.py mounts THIS copy's routes and the ones above are dead. ADMIN_ROLES
# is therefore imported on both sides -- the live guards below use it, and
# relying on the dead copy's import to bind it would turn the obvious
# de-duplication cleanup into a NameError on every admin route here.
from utils.auth import ADMIN_ROLES, get_current_user
from db.database import database
import logging

router = APIRouter(prefix="/admin/debug-mcp", tags=["Debug MCP"])
logger = logging.getLogger(__name__)

@router.get("/check-tables")
async def check_mcp_tables(current_user: dict = Depends(get_current_user)):
    """Check what data exists in MCP-related tables"""
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin only")
    
    try:
        # Check merchant_store_integrations
        msi_count = await database.fetch_one(
            "SELECT COUNT(*) as count FROM merchant_store_integrations"
        )
        
        msi_sample = await database.fetch_all(
            """SELECT merchant_id, platform, store_name, status 
               FROM merchant_store_integrations 
               LIMIT 5"""
        )
        
        # Check merchant_psps
        psp_count = await database.fetch_one(
            "SELECT COUNT(*) as count FROM merchant_psps WHERE status = 'active'"
        )
        
        psp_sample = await database.fetch_all(
            """SELECT merchant_id, provider, status 
               FROM merchant_psps 
               LIMIT 5"""
        )
        
        # Check merchant_onboarding (legacy MCP fields)
        mcp_legacy = await database.fetch_all(
            """SELECT merchant_id, business_name, mcp_platform, mcp_shop_domain, mcp_connected
               FROM merchant_onboarding
               WHERE mcp_connected = true OR mcp_platform IS NOT NULL
               LIMIT 5"""
        )
        
        # Check products_cache
        products_count = await database.fetch_one(
            "SELECT COUNT(*) as count FROM products_cache"
        )
        
        return {
            "status": "success",
            "data": {
                "merchant_store_integrations": {
                    "total": dict(msi_count).get("count", 0) if msi_count else 0,
                    "sample": [dict(r) for r in msi_sample]
                },
                "merchant_psps": {
                    "total": dict(psp_count).get("count", 0) if psp_count else 0,
                    "sample": [dict(r) for r in psp_sample]
                },
                "legacy_mcp_fields": {
                    "total": len(mcp_legacy),
                    "sample": [dict(r) for r in mcp_legacy]
                },
                "products_cache": {
                    "total": dict(products_count).get("count", 0) if products_count else 0
                }
            }
        }
    
    except Exception as e:
        logger.error(f"Error checking MCP data: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/merchant/{merchant_id}/full-data")
async def get_merchant_full_data(
    merchant_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get all data for a specific merchant"""
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin only")
    
    try:
        # Get merchant
        merchant = await database.fetch_one(
            "SELECT * FROM merchant_onboarding WHERE merchant_id = :merchant_id",
            {"merchant_id": merchant_id}
        )
        
        # Get stores
        stores = await database.fetch_all(
            "SELECT * FROM merchant_store_integrations WHERE merchant_id = :merchant_id",
            {"merchant_id": merchant_id}
        )
        
        # Get PSPs
        psps = await database.fetch_all(
            "SELECT * FROM merchant_psps WHERE merchant_id = :merchant_id",
            {"merchant_id": merchant_id}
        )
        
        # Get products
        products = await database.fetch_all(
            "SELECT COUNT(*) as count, platform FROM products_cache WHERE merchant_id = :merchant_id GROUP BY platform",
            {"merchant_id": merchant_id}
        )
        
        # Get orders
        orders = await database.fetch_all(
            "SELECT COUNT(*) as count, payment_status FROM orders WHERE merchant_id = :merchant_id GROUP BY payment_status",
            {"merchant_id": merchant_id}
        )
        
        return {
            "status": "success",
            "merchant_id": merchant_id,
            "merchant": dict(merchant) if merchant else None,
            "stores": {
                "count": len(stores),
                "data": [dict(s) for s in stores]
            },
            "psps": {
                "count": len(psps),
                "data": [dict(p) for p in psps]
            },
            "products": {
                "count": len(products),
                "by_platform": [dict(p) for p in products]
            },
            "orders": {
                "count": len(orders),
                "by_status": [dict(o) for o in orders]
            }
        }
    
    except Exception as e:
        logger.error(f"Error getting merchant full data: {e}")
        raise HTTPException(status_code=500, detail=str(e))
