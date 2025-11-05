"""
Manual trigger for commission calculation on existing order
"""

from fastapi import APIRouter, Depends
from utils.auth import require_admin
from services.order_commission_service import OrderCommissionService
from db.database import database
import logging

router = APIRouter(
    prefix="/test",
    tags=["Test - Commission"]
)

logger = logging.getLogger(__name__)


@router.post("/trigger-commission/{order_id}")
async def trigger_commission_calculation(
    order_id: str,
    current_user: dict = Depends(require_admin)
):
    """手动触发佣金计算"""
    
    try:
        logger.info(f"=== Manual commission calculation triggered for {order_id} ===")
        
        # Initialize service
        commission_service = OrderCommissionService(database)
        
        # Calculate commission
        result = await commission_service.calculate_commission_for_order(order_id)
        
        logger.info(f"Commission calculation result: {result}")
        
        # Query results
        commissions = await database.fetch_all(
            "SELECT * FROM commissions WHERE order_id = :order_id",
            {"order_id": order_id}
        )
        
        revenue_logs = await database.fetch_all(
            "SELECT * FROM revenue_matching_logs WHERE order_id = :order_id",
            {"order_id": order_id}
        )
        
        return {
            "success": True,
            "calculation_result": result,
            "commissions_found": len(commissions),
            "commissions": [dict(c) for c in commissions],
            "revenue_logs_found": len(revenue_logs),
            "revenue_logs": [dict(r) for r in revenue_logs]
        }
        
    except Exception as e:
        logger.error(f"Commission calculation failed: {e}", exc_info=True)
        import traceback
        return {
            "success": False,
            "error": str(e),
            "error_type": type(e).__name__,
            "traceback": traceback.format_exc()
        }

