"""
Order Commission Service
Automatically calculates and records commission when orders are completed
"""

from typing import Dict, Optional, Any
from decimal import Decimal
from datetime import datetime
import logging

from databases import Database
from services.revenue_share_service import RevenueShareService

logger = logging.getLogger(__name__)


class OrderCommissionService:
    """
    Handles automatic commission calculation when orders are completed
    
    Workflow:
    1. Order payment confirmed → trigger commission calculation
    2. Fetch merchant commission offers
    3. Match with agent expectations using RevenueShareService
    4. Log to revenue_matching_logs
    5. Update order with commission details
    """
    
    def __init__(self, database: Database):
        self.database = database
        self.revenue_service = RevenueShareService(database)
    
    async def calculate_commission_for_order(
        self,
        order_id: str
    ) -> Dict[str, Any]:
        """
        Calculate and record commission for a completed order
        
        Args:
            order_id: Order ID
            
        Returns:
            Commission calculation result with rate, amount, and matching details
        """
        try:
            # 1. Get order details
            order = await self._get_order_details(order_id)
            if not order:
                logger.error(f"Order not found: {order_id}")
                return {"status": "error", "message": "Order not found"}
            
            # Skip if order doesn't have agent or is not paid
            if not order.get('agent_id'):
                logger.info(f"Order {order_id} has no agent, skipping commission")
                return {"status": "skipped", "reason": "no_agent"}
            
            if order.get('payment_status') != 'paid':
                logger.info(f"Order {order_id} not paid yet, skipping commission")
                return {"status": "skipped", "reason": "not_paid"}
            
            # Check if already processed
            existing = await self._check_existing_commission(order_id)
            if existing:
                logger.info(f"Commission already calculated for order {order_id}")
                return {"status": "skipped", "reason": "already_calculated"}
            
            # 2. Calculate commission using revenue share service
            order_amount = Decimal(str(order.get('total', 0)))
            currency = order.get('currency', 'USD')
            
            match_result = await self.revenue_service.match_commission(
                agent_id=order['agent_id'],
                merchant_id=order['merchant_id'],
                order_amount=order_amount,
                currency=currency
            )
            
            commission_rate = match_result.get('actual_rate', Decimal('0'))
            commission_amount = order_amount * commission_rate
            
            # 3. Log revenue matching
            await self._log_revenue_matching(
                order_id=order_id,
                order=order,
                match_result=match_result,
                commission_amount=commission_amount
            )
            
            # 4. Update order with commission details
            await self._update_order_commission(
                order_id=order_id,
                commission_rate=commission_rate,
                commission_amount=commission_amount
            )
            
            logger.info(
                f"Commission calculated for order {order_id}: "
                f"${commission_amount} ({commission_rate * 100}%)"
            )
            
            return {
                "status": "success",
                "order_id": order_id,
                "commission_rate": float(commission_rate),
                "commission_amount": float(commission_amount),
                "match_type": match_result.get('match_type'),
                "match_source": match_result.get('match_source')
            }
            
        except Exception as e:
            logger.error(f"Error calculating commission for order {order_id}: {e}")
            return {"status": "error", "message": str(e)}
    
    async def _get_order_details(self, order_id: str) -> Optional[Dict[str, Any]]:
        """Get order details from database"""
        try:
            query = """
                SELECT 
                    order_id,
                    merchant_id,
                    agent_id,
                    total,
                    currency,
                    payment_status,
                    created_at
                FROM orders
                WHERE order_id = :order_id
            """
            order = await self.database.fetch_one(query, {"order_id": order_id})
            return dict(order) if order else None
        except Exception as e:
            logger.error(f"Error fetching order {order_id}: {e}")
            return None
    
    async def _check_existing_commission(self, order_id: str) -> bool:
        """Check if commission already calculated for this order"""
        try:
            query = """
                SELECT id FROM revenue_matching_logs
                WHERE order_id = :order_id
                LIMIT 1
            """
            result = await self.database.fetch_one(query, {"order_id": order_id})
            return result is not None
        except Exception as e:
            logger.error(f"Error checking existing commission: {e}")
            return False
    
    async def _log_revenue_matching(
        self,
        order_id: str,
        order: Dict[str, Any],
        match_result: Dict[str, Any],
        commission_amount: Decimal
    ):
        """Log revenue matching to database"""
        try:
            # Insert into revenue_matching_logs
            query = """
                INSERT INTO revenue_matching_logs (
                    agent_id,
                    merchant_id,
                    order_id,
                    order_amount,
                    currency,
                    final_rate,
                    agent_commission,
                    match_type,
                    match_source,
                    merchant_offer_id,
                    agent_expectation_id,
                    notes,
                    created_at
                ) VALUES (
                    :agent_id,
                    :merchant_id,
                    :order_id,
                    :order_amount,
                    :currency,
                    :final_rate,
                    :agent_commission,
                    :match_type,
                    :match_source,
                    :merchant_offer_id,
                    :agent_expectation_id,
                    :notes,
                    NOW()
                )
            """
            
            await self.database.execute(query, {
                "agent_id": order['agent_id'],
                "merchant_id": order['merchant_id'],
                "order_id": order_id,
                "order_amount": float(order.get('total', 0)),
                "currency": order.get('currency', 'USD'),
                "final_rate": float(match_result.get('actual_rate', 0)),
                "agent_commission": float(commission_amount),
                "match_type": match_result.get('match_type', 'platform_default'),
                "match_source": match_result.get('match_source', 'system'),
                "merchant_offer_id": match_result.get('merchant_offer_id'),
                "agent_expectation_id": match_result.get('agent_expectation_id'),
                "notes": f"Auto-calculated on order completion: {match_result.get('match_type')}"
            })
            
            logger.info(f"Revenue matching logged for order {order_id}")
            
        except Exception as e:
            logger.error(f"Error logging revenue matching: {e}")
            raise
    
    async def _update_order_commission(
        self,
        order_id: str,
        commission_rate: Decimal,
        commission_amount: Decimal
    ):
        """Update order with commission details"""
        try:
            # Add commission info to order metadata
            query = """
                UPDATE orders
                SET metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object(
                    'commission_rate', :commission_rate,
                    'commission_amount', :commission_amount,
                    'commission_calculated_at', :calculated_at
                )
                WHERE order_id = :order_id
            """
            
            await self.database.execute(query, {
                "order_id": order_id,
                "commission_rate": float(commission_rate),
                "commission_amount": float(commission_amount),
                "calculated_at": datetime.utcnow().isoformat()
            })
            
            logger.info(f"Order {order_id} updated with commission details")
            
        except Exception as e:
            logger.error(f"Error updating order commission: {e}")
            # Don't raise - commission is already logged, order update is optional


async def process_order_commission(order_id: str, database: Database) -> Dict[str, Any]:
    """
    Standalone function to process commission for an order
    Can be called from background tasks or payment confirmation
    """
    service = OrderCommissionService(database)
    return await service.calculate_commission_for_order(order_id)


