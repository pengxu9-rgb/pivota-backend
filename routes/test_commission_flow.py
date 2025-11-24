"""
Test endpoint to demonstrate the complete commission automation flow
"""

from fastapi import APIRouter, Depends, BackgroundTasks
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from decimal import Decimal
import logging
import uuid

from db.database import database
from utils.auth import require_admin
from services.order_commission_service import OrderCommissionService

router = APIRouter(
    prefix="/test/commission-flow",
    tags=["Test - Commission Flow"]
)

logger = logging.getLogger(__name__)

class TestOrderRequest(BaseModel):
    amount: float = 150.00
    merchant_id: Optional[str] = None  # If not provided, will use first active merchant
    agent_id: Optional[str] = "agent_ee38f2b3645a2ec2"  # Default to our test agent
    description: Optional[str] = "Commission Flow Test Order"


@router.post("/create-and-complete")
async def create_and_complete_test_order(
    request: TestOrderRequest,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(require_admin)
):
    """
    创建订单并立即确认支付，触发佣金自动化流程
    
    这将演示：
    1. 订单创建
    2. 支付确认  
    3. 自动佣金计算
    4. 双边收入匹配
    5. 结算记录生成
    """
    
    try:
        # Step 1: Get merchant ID
        if request.merchant_id:
            merchant_id = request.merchant_id
        else:
            # Get first merchant from merchant_onboarding (where foreign key points)
            merchant = await database.fetch_one("SELECT id FROM merchant_onboarding LIMIT 1")
            if not merchant:
                return {"success": False, "error": "No merchants found in merchant_onboarding"}
            merchant_id = str(merchant['id'])  # Convert to string
        
        logger.info(f"Using merchant: {merchant_id}")
        
        # Step 2: Get agent ID
        agent_id = request.agent_id
        if not agent_id:
            # Get first agent
            agent = await database.fetch_one("SELECT agent_id FROM agents LIMIT 1")
            if not agent:
                return {"success": False, "error": "No agents found"}
            agent_id = agent['agent_id']
        
        logger.info(f"Using agent: {agent_id}")
        
        # Step 3: Create order
        order_id = f"ORDER_{uuid.uuid4().hex[:12].upper()}"
        
        await database.execute(
            """
            INSERT INTO orders (
                order_id,
                merchant_id,
                agent_id,
                total,
                currency,
                status,
                payment_status,
                created_at
            ) VALUES (
                :order_id,
                :merchant_id,
                :agent_id,
                :amount,
                'USD',
                'confirmed',
                'paid',
                NOW()
            )
            """,
            {
                "order_id": order_id,
                "merchant_id": merchant_id,
                "agent_id": agent_id,
                "amount": request.amount
            }
        )
        
        logger.info(f"Created order: {order_id} for ${request.amount}")
        
        # Step 4: Trigger commission calculation (background task)
        async def calculate_commission_task():
            try:
                commission_service = OrderCommissionService(database)
                result = await commission_service.calculate_commission_for_order(order_id)
                logger.info(f"Commission calculation result: {result}")
            except Exception as e:
                logger.error(f"Commission calculation failed: {e}", exc_info=True)
        
        background_tasks.add_task(calculate_commission_task)
        
        # Step 5: Query the order and commission immediately (before background task completes)
        # Wait a moment for background task to run
        import asyncio
        await asyncio.sleep(2)
        
        # Check commission records
        commissions = await database.fetch_all(
            """
            SELECT 
                type,
                amount,
                rate,
                matched,
                created_at
            FROM commissions
            WHERE order_id = :order_id
            ORDER BY created_at DESC
            """,
            {"order_id": order_id}
        )
        
        # Check revenue matching logs  
        revenue_logs = await database.fetch_all(
            """
            SELECT 
                match_status,
                actual_commission_rate,
                match_source,
                merchant_offered_rate,
                agent_expected_rate,
                matched_at
            FROM revenue_matching_logs
            WHERE order_id = :order_id
            ORDER BY matched_at DESC
            LIMIT 1
            """,
            {"order_id": order_id}
        )
        
        # Check settlement records
        settlements = await database.fetch_all(
            """
            SELECT 
                settlement_id,
                agent_id,
                settlement_amount,
                status,
                settlement_period_start,
                settlement_period_end
            FROM agent_settlements
            WHERE agent_id = :agent_id
            ORDER BY created_at DESC
            LIMIT 1
            """,
            {"agent_id": agent_id}
        )
        
        return {
            "success": True,
            "message": "测试订单创建并触发佣金计算",
            "order": {
                "order_id": order_id,
                "amount": request.amount,
                "merchant_id": merchant_id,
                "agent_id": agent_id
            },
            "commissions": [dict(c) for c in commissions] if commissions else [],
            "revenue_matching": [dict(r) for r in revenue_logs] if revenue_logs else [],
            "settlements": [dict(s) for s in settlements] if settlements else [],
            "note": "佣金计算在后台异步执行，可能需要几秒钟完成"
        }
        
    except Exception as e:
        logger.error(f"Test commission flow failed: {e}", exc_info=True)
        import traceback
        return {
            "success": False,
            "error": str(e),
            "error_type": type(e).__name__,
            "traceback": traceback.format_exc()
        }


@router.get("/check-order/{order_id}")
async def check_order_commission(
    order_id: str,
    current_user: dict = Depends(require_admin)
):
    """检查特定订单的佣金计算结果"""
    
    try:
        # Get order
        order = await database.fetch_one(
            "SELECT * FROM orders WHERE order_id = :order_id",
            {"order_id": order_id}
        )
        
        if not order:
            return {
                "success": False,
                "error": f"Order not found: {order_id}"
            }
        
        # Get commissions
        commissions = await database.fetch_all(
            """
            SELECT * FROM commissions 
            WHERE order_id = :order_id
            ORDER BY created_at DESC
            """,
            {"order_id": order_id}
        )
        
        # Get revenue logs
        revenue_logs = await database.fetch_all(
            """
            SELECT * FROM revenue_match_logs
            WHERE order_id = :order_id
            ORDER BY created_at DESC
            """,
            {"order_id": order_id}
        )
        
        return {
            "success": True,
            "order": dict(order),
            "commissions": [dict(c) for c in commissions],
            "revenue_logs": [dict(r) for r in revenue_logs]
        }
        
    except Exception as e:
        logger.error(f"Check order failed: {e}", exc_info=True)
        return {
            "success": False,
            "error": str(e)
        }

