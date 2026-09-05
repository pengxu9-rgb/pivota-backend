"""
[Phase 4++] Admin endpoint to clean up routing test data
"""

from fastapi import APIRouter, HTTPException, Depends

from db.database import database
from utils.auth import ADMIN_ROLES, get_current_user

router = APIRouter(
    prefix="/admin/cleanup",
    tags=["[Phase 4++] Admin Cleanup"]
)


@router.post("/routing-test-data")
async def cleanup_routing_test_data(current_user: dict = Depends(get_current_user)):
    """Clean up all routing test data"""
    
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin access required")
    
    try:
        results = []
        
        # 1. Delete test routing logs
        deleted_logs = await database.execute(
            """
            DELETE FROM routing_logs
            WHERE merchant_id LIKE 'merchant_high_risk_%'
               OR merchant_id LIKE 'merchant_cost_sensitive_%'
               OR merchant_id LIKE 'merchant_test_%'
               OR order_id LIKE 'test_order_%'
            """
        )
        results.append(f"✅ Deleted {deleted_logs} routing logs")
        
        # 2. Delete AP2 test transactions
        deleted_ap2 = await database.execute(
            """
            DELETE FROM ap2_transactions
            WHERE order_id LIKE 'test_order_%'
               OR order_id LIKE 'ap2_test_%'
            """
        )
        results.append(f"✅ Deleted {deleted_ap2} AP2 transactions")
        
        # 3. Delete test routing policies
        deleted_policies = await database.execute(
            """
            DELETE FROM routing_policies
            WHERE owner_id LIKE 'merchant_high_risk_%'
               OR owner_id LIKE 'merchant_cost_sensitive_%'
               OR owner_id LIKE 'merchant_test_%'
               OR owner_id LIKE 'agent_cost_test_%'
            """
        )
        results.append(f"✅ Deleted {deleted_policies} routing policies")
        
        # 4. Keep real agent policy but can optionally remove
        # Uncomment if you want to also clean real agent routing policy:
        # await database.execute(
        #     "DELETE FROM routing_policies WHERE owner_id = 'agent_ee38f2b3645a2ec2'"
        # )
        
        # Get remaining counts
        remaining = await database.fetch_one("""
            SELECT 
                (SELECT COUNT(*) FROM routing_logs) as logs,
                (SELECT COUNT(*) FROM routing_policies) as policies,
                (SELECT COUNT(*) FROM ap2_transactions) as ap2_txs
        """)
        
        return {
            "status": "success",
            "message": "Test data cleanup completed",
            "deleted": results,
            "remaining": {
                "routing_logs": remaining["logs"],
                "routing_policies": remaining["policies"],
                "ap2_transactions": remaining["ap2_txs"]
            }
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cleanup failed: {str(e)}")


@router.post("/all-routing-data")
async def cleanup_all_routing_data(current_user: dict = Depends(get_current_user)):
    """⚠️ DANGEROUS: Clean up ALL routing data including production data"""
    
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin access required")
    
    try:
        # Delete all routing-related data
        await database.execute("DELETE FROM ap2_transactions")
        await database.execute("DELETE FROM routing_logs")
        await database.execute("DELETE FROM routing_policies")
        
        return {
            "status": "success",
            "message": "⚠️ All routing data deleted",
            "warning": "This includes production data!"
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cleanup failed: {str(e)}")


print("[Phase 4++] Admin cleanup routing data route initialized")
