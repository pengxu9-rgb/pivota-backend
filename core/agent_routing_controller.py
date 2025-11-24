"""
[Phase 5] Agent Routing Controller
Revenue-aware routing coordinator that integrates routing and revenue tracking
"""

from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime
from decimal import Decimal
import logging
import json

from databases import Database

logger = logging.getLogger(__name__)


class AgentRevenueService:
    """Service for managing agent revenue calculations and policies"""
    
    def __init__(self, database: Database):
        self.database = database
    
    async def get_revenue_policy(
        self, 
        agent_id: str, 
        merchant_id: Optional[str] = None,
        amount: Optional[Decimal] = None,
        currency: str = "USD"
    ) -> Optional[Dict[str, Any]]:
        """
        Get applicable revenue policy for agent/merchant combination
        
        Priority:
        1. Specific merchant + currency policy
        2. Default policy for currency
        3. None (no revenue sharing)
        """
        # Try merchant-specific policy first
        if merchant_id:
            query = """
                SELECT * FROM agent_revenue_policies
                WHERE agent_id = :agent_id
                AND merchant_id = :merchant_id
                AND currency = :currency
                AND is_active = true
                AND (active_period_start IS NULL OR active_period_start <= NOW())
                AND (active_period_end IS NULL OR active_period_end >= NOW())
            """
            
            policy = await self.database.fetch_one(query, {
                "agent_id": agent_id,
                "merchant_id": merchant_id,
                "currency": currency
            })
            
            if policy:
                # Check amount range if specified
                if amount is not None:
                    if policy['min_transaction_amount'] and amount < policy['min_transaction_amount']:
                        return None
                    if policy['max_transaction_amount'] and amount > policy['max_transaction_amount']:
                        return None
                
                return dict(policy)
        
        # Fall back to default policy (merchant_id = NULL)
        query = """
            SELECT * FROM agent_revenue_policies
            WHERE agent_id = :agent_id
            AND merchant_id IS NULL
            AND currency = :currency
            AND is_active = true
            AND (active_period_start IS NULL OR active_period_start <= NOW())
            AND (active_period_end IS NULL OR active_period_end >= NOW())
        """
        
        policy = await self.database.fetch_one(query, {
            "agent_id": agent_id,
            "currency": currency
        })
        
        if policy:
            if amount is not None:
                if policy['min_transaction_amount'] and amount < policy['min_transaction_amount']:
                    return None
                if policy['max_transaction_amount'] and amount > policy['max_transaction_amount']:
                    return None
            
            return dict(policy)
        
        return None
    
    async def calculate_split(
        self,
        amount: Decimal,
        split_ratio: Decimal
    ) -> Decimal:
        """Calculate agent's earned amount"""
        earned = amount * split_ratio
        return earned.quantize(Decimal('0.01'))  # Round to 2 decimal places


class AgentRoutingController:
    """
    [Phase 5] Revenue-aware routing coordinator
    
    Integrates DualRoutingEngine with revenue tracking and agent earnings calculation
    """
    
    def __init__(self, routing_engine, revenue_service: AgentRevenueService):
        """
        Initialize controller
        
        Args:
            routing_engine: DualRoutingEngine instance
            revenue_service: AgentRevenueService instance
        """
        self.routing_engine = routing_engine
        self.revenue_service = revenue_service
        logger.info("[Phase 5] AgentRoutingController initialized")
    
    async def route_with_revenue(
        self,
        agent_id: str,
        merchant_id: str,
        tx_context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Route payment with revenue calculation
        
        Workflow:
        1. Call routing_engine.route_payment() for PSP selection
        2. Check if agent has revenue sharing enabled
        3. Calculate revenue split if applicable
        4. Log to agent_revenue_logs
        5. Return combined result
        
        Args:
            agent_id: Agent ID
            merchant_id: Merchant ID
            tx_context: Transaction context (amount, currency, etc.)
            
        Returns:
            Routing result with revenue information
        """
        try:
            # Extract transaction details
            amount = Decimal(str(tx_context.get('amount', 0)))
            currency = tx_context.get('currency', 'USD')
            tx_id = tx_context.get('tx_id') or tx_context.get('order_id')
            
            logger.info(
                f"[Phase 5] route_with_revenue: agent={agent_id}, merchant={merchant_id}, "
                f"amount={amount} {currency}, tx_id={tx_id}"
            )
            
            # Step 1: Route payment using existing engine
            routing_context = {
                'agent_id': agent_id,
                'merchant_id': merchant_id,
                'amount': float(amount),
                'currency': currency,
                'tx_id': tx_id
            }
            
            routing_result = self.routing_engine.route_payment(routing_context)
            
            # Step 2: Check revenue sharing eligibility
            revenue_policy = await self.revenue_service.get_revenue_policy(
                agent_id=agent_id,
                merchant_id=merchant_id,
                amount=amount,
                currency=currency
            )
            
            # Step 3: Calculate revenue split if enabled
            revenue_info = None
            if revenue_policy:
                split_ratio = Decimal(str(revenue_policy['split_ratio']))
                agent_earned = await self.revenue_service.calculate_split(amount, split_ratio)
                
                revenue_info = {
                    'policy_id': revenue_policy['id'],
                    'split_ratio': float(split_ratio),
                    'agent_earned_amount': float(agent_earned),
                    'transaction_amount': float(amount),
                    'currency': currency,
                    'settlement_status': 'pending'
                }
                
                logger.info(
                    f"[Phase 5] Revenue calculated: agent earns {agent_earned} {currency} "
                    f"({float(split_ratio)*100}% of {amount})"
                )
            
            # Step 4: Prepare combined result
            result = {
                **routing_result,
                'revenue_info': revenue_info,
                'revenue_sharing_enabled': revenue_policy is not None,
                'tx_id': tx_id,
                'agent_id': agent_id,
                'merchant_id': merchant_id
            }
            
            return result
            
        except Exception as e:
            logger.error(f"[Phase 5] route_with_revenue failed: {e}")
            raise
    
    async def apply_revenue_split(
        self,
        tx_id: str,
        routing_result: Dict[str, Any],
        amount: Decimal,
        routing_log_id: Optional[int] = None
    ) -> Optional[int]:
        """
        Apply and log revenue split to database
        
        Args:
            tx_id: Transaction ID
            routing_result: Result from route_with_revenue()
            amount: Transaction amount
            routing_log_id: Optional routing log ID for linkage
            
        Returns:
            Revenue log ID if created, None otherwise
        """
        try:
            revenue_info = routing_result.get('revenue_info')
            if not revenue_info:
                logger.info(f"[Phase 5] No revenue policy for tx {tx_id}, skipping")
                return None
            
            # Insert into agent_revenue_logs
            query = """
                INSERT INTO agent_revenue_logs (
                    tx_id, routing_log_id, agent_id, merchant_id,
                    psp_used, transaction_amount, agent_earned_amount,
                    split_ratio_applied, currency, settlement_status,
                    metadata, created_at
                ) VALUES (
                    :tx_id, :routing_log_id, :agent_id, :merchant_id,
                    :psp_used, :transaction_amount, :agent_earned_amount,
                    :split_ratio, :currency, :settlement_status,
                    :metadata, NOW()
                )
                RETURNING id
            """
            
            result = await self.revenue_service.database.execute(query, {
                "tx_id": tx_id,
                "routing_log_id": routing_log_id,
                "agent_id": routing_result.get('agent_id'),
                "merchant_id": routing_result.get('merchant_id'),
                "psp_used": routing_result.get('selected_psp'),
                "transaction_amount": float(amount),
                "agent_earned_amount": revenue_info['agent_earned_amount'],
                "split_ratio": revenue_info['split_ratio'],
                "currency": revenue_info['currency'],
                "settlement_status": 'pending',
                "metadata": json.dumps({
                    'policy_id': revenue_info.get('policy_id'),
                    'routing_resolution': routing_result.get('resolution_method')
                })
            })
            
            logger.info(f"[Phase 5] Revenue logged: id={result}, tx={tx_id}, earned={revenue_info['agent_earned_amount']}")
            
            return result
            
        except Exception as e:
            logger.error(f"[Phase 5] Failed to apply revenue split: {e}")
            return None
    
    async def record_agent_decision(
        self,
        tx_id: str,
        decision_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Enhanced logging with revenue information
        
        Args:
            tx_id: Transaction ID
            decision_data: Decision details including routing and revenue
            
        Returns:
            Complete record with log IDs
        """
        try:
            # This is a placeholder for comprehensive logging
            # Actual implementation would coordinate between:
            # - routing_logs (via PaymentRoutingService)
            # - agent_revenue_logs (via this controller)
            
            logger.info(
                f"[Phase 5] Recording agent decision: tx={tx_id}, "
                f"psp={decision_data.get('selected_psp')}, "
                f"revenue={decision_data.get('revenue_info') is not None}"
            )
            
            return {
                "tx_id": tx_id,
                "recorded_at": datetime.utcnow().isoformat(),
                **decision_data
            }
            
        except Exception as e:
            logger.error(f"[Phase 5] Failed to record agent decision: {e}")
            raise


# [Phase 5] Convenience function to create controller with services
async def create_agent_routing_controller(database: Database) -> AgentRoutingController:
    """
    Factory function to create AgentRoutingController with dependencies
    
    Args:
        database: Database instance
        
    Returns:
        Configured AgentRoutingController
    """
    from core.routing_engine import DualRoutingEngine
    
    # Create revenue service
    revenue_service = AgentRevenueService(database)
    
    # Create routing engine (placeholder - actual instantiation needs policies)
    routing_engine = DualRoutingEngine(
        merchant_rules={},
        agent_rules={},
        available_psps=[],
        agent_whitelisted=False
    )
    
    # Create controller
    controller = AgentRoutingController(routing_engine, revenue_service)
    
    return controller


# [Phase 5] Test if module loads correctly
if __name__ == "__main__":
    print("[Phase 5] AgentRoutingController module loaded successfully")
    print("  - AgentRevenueService: Revenue policy lookup and calculation")
    print("  - AgentRoutingController: Coordinated routing with revenue tracking")
    print("  - Integration point: Payment orchestrator")
