"""
[Phase 6A] Admin endpoint to clean up Phase 5.x test data
"""

from fastapi import APIRouter, Depends, HTTPException
from typing import Dict
import asyncpg
from utils.auth import get_current_admin_user
from db.database import get_db_pool

router = APIRouter()

@router.post("/admin/cleanup/phase5-data")
async def cleanup_phase5_test_data(
    current_admin = Depends(get_current_admin_user),
    pool = Depends(get_db_pool)
):
    """Clean up Phase 5.x test data from database"""
    
    try:
        async with pool.acquire() as conn:
            # Delete test routing logs
            result1 = await conn.execute("""
                DELETE FROM agent_routing_history
                WHERE notes LIKE '%test%' OR notes LIKE '%demo%'
            """)
            
            # Delete test revenue matching logs
            result2 = await conn.execute("""
                DELETE FROM revenue_matching_logs
                WHERE revenue_id IN (
                    SELECT id FROM dual_sided_revenue
                    WHERE notes LIKE '%test%' OR notes LIKE '%Phase 5%'
                )
            """)
            
            # Delete test settlements
            result3 = await conn.execute("""
                DELETE FROM agent_settlements
                WHERE notes LIKE '%test%' OR notes LIKE '%demo%'
            """)
            
            # Delete test integration logs
            result4 = await conn.execute("""
                DELETE FROM agent_integration_logs
                WHERE event_data::text LIKE '%test%'
            """)
            
            # Count remaining real data
            real_routing = await conn.fetchval("SELECT COUNT(*) FROM agent_routing_history")
            real_revenue = await conn.fetchval("SELECT COUNT(*) FROM dual_sided_revenue")
            real_settlements = await conn.fetchval("SELECT COUNT(*) FROM agent_settlements")
            
            return {
                "success": True,
                "deleted": {
                    "routing_history": result1.split()[-1] if result1 else 0,
                    "revenue_logs": result2.split()[-1] if result2 else 0,
                    "settlements": result3.split()[-1] if result3 else 0,
                    "integration_logs": result4.split()[-1] if result4 else 0
                },
                "remaining": {
                    "real_routing": real_routing,
                    "real_revenue": real_revenue,
                    "real_settlements": real_settlements
                },
                "message": "Phase 5.x test data cleaned up successfully"
            }
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
