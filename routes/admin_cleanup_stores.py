from fastapi import APIRouter, Depends, HTTPException, Query
from db.database import database
from utils.auth import ADMIN_ROLES, get_current_user
from utils.runtime_safety import require_runtime_gate
import logging

router = APIRouter(prefix="/admin/cleanup", tags=["admin-cleanup"])
logger = logging.getLogger(__name__)


def _require_admin_store_mutation_access(current_user: dict, *, mutation: bool) -> None:
    require_runtime_gate("ALLOW_PROD_ADMIN_STORE_MUTATION")
    role = str((current_user or {}).get("role") or "").strip().lower()
    if mutation:
        # NOTE: "superadmin" (no underscore) is NOT a role this system can mint
        # -- the canonical spelling is `super_admin` (utils.auth.ADMIN_ROLES,
        # routes.auth._validate_role_value). This comparison therefore never
        # matches and the mutation lane is closed to everyone, including real
        # super_admins. Left AS-IS on purpose: correcting the spelling would not
        # restore lost access, it would newly OPEN destructive prod store
        # mutation behind nothing but ALLOW_PROD_ADMIN_STORE_MUTATION. Opening
        # it is a deliberate decision, not a typo fix -- see the PR that added
        # this note.
        if role != "superadmin":
            raise HTTPException(status_code=403, detail="Superadmin access required")
    # Read-only branch: `admin` already worked here, so spelling `super_admin`
    # correctly only restores the privileged role that was wrongly excluded.
    elif role not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin access required")

@router.get("/stores/wix")
async def list_wix_stores(
    current_user: dict = Depends(get_current_user)
):
    """List all Wix store connections (admin only)"""
    _require_admin_store_mutation_access(current_user, mutation=False)
    
    try:
        query = """
            SELECT store_id, merchant_id, platform, domain, name, status, connected_at, api_key
            FROM merchant_stores
            WHERE platform = 'wix'
            ORDER BY connected_at DESC
        """
        
        stores = await database.fetch_all(query)
        
        # Don't expose full API keys
        result = []
        for store in stores:
            store_dict = dict(store)
            if store_dict.get('api_key'):
                store_dict['api_key'] = f"{store_dict['api_key'][:10]}..." if len(store_dict['api_key']) > 10 else "***"
            result.append(store_dict)
        
        return {
            "count": len(result),
            "stores": result
        }
        
    except Exception as e:
        logger.error(f"Error listing Wix stores: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/stores/wix/{store_id}")
async def delete_wix_store(
    store_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Delete a specific Wix store connection (admin only)"""
    _require_admin_store_mutation_access(current_user, mutation=True)
    
    try:
        # Check if store exists
        check_query = "SELECT store_id FROM merchant_stores WHERE store_id = :store_id AND platform = 'wix'"
        exists = await database.fetch_one(check_query, {"store_id": store_id})
        
        if not exists:
            raise HTTPException(status_code=404, detail="Wix store not found")
        
        # Delete the store
        delete_query = "DELETE FROM merchant_stores WHERE store_id = :store_id"
        await database.execute(delete_query, {"store_id": store_id})
        
        logger.info(f"Deleted Wix store: {store_id}")
        
        return {
            "status": "success",
            "message": f"Wix store {store_id} deleted successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting Wix store: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/stores/wix")
async def delete_all_wix_stores(
    confirm: bool = Query(False, description="Set to true to confirm deletion"),
    current_user: dict = Depends(get_current_user)
):
    """Delete ALL Wix store connections (admin only, requires confirmation)"""
    _require_admin_store_mutation_access(current_user, mutation=True)
    
    if not confirm:
        raise HTTPException(
            status_code=400, 
            detail="Confirmation required. Add ?confirm=true to delete all Wix stores"
        )
    
    try:
        # Count stores before deletion
        count_query = "SELECT COUNT(*) as count FROM merchant_stores WHERE platform = 'wix'"
        result = await database.fetch_one(count_query)
        count = result["count"]
        
        if count == 0:
            return {
                "status": "success",
                "message": "No Wix stores to delete",
                "deleted_count": 0
            }
        
        # Delete all Wix stores
        delete_query = "DELETE FROM merchant_stores WHERE platform = 'wix'"
        await database.execute(delete_query)
        
        logger.info(f"Deleted {count} Wix stores")
        
        return {
            "status": "success",
            "message": f"Deleted all {count} Wix store(s)",
            "deleted_count": count
        }
        
    except Exception as e:
        logger.error(f"Error deleting all Wix stores: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/stores/by-domain")
async def delete_store_by_domain(
    platform: str = Query(..., description="Platform (wix, shopify, etc)"),
    domain: str = Query(..., description="Store domain or site ID"),
    current_user: dict = Depends(get_current_user)
):
    """Delete store by platform and domain (admin only)"""
    _require_admin_store_mutation_access(current_user, mutation=True)
    
    try:
        # Find and delete stores
        query = """
            DELETE FROM merchant_stores 
            WHERE platform = :platform AND domain = :domain
            RETURNING store_id, merchant_id
        """
        
        deleted = await database.fetch_all(query, {
            "platform": platform,
            "domain": domain
        })
        
        if not deleted:
            raise HTTPException(status_code=404, detail=f"No {platform} store found with domain {domain}")
        
        return {
            "status": "success",
            "message": f"Deleted {len(deleted)} store(s)",
            "deleted_stores": [dict(d) for d in deleted]
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting store by domain: {e}")
        raise HTTPException(status_code=500, detail=str(e))
