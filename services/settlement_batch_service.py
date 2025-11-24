"""
Settlement Batch Processing Service
Creates settlements for agents based on accumulated commissions
"""

from typing import Dict, List, Optional, Any
from decimal import Decimal
from datetime import datetime, timedelta
import logging

from databases import Database

logger = logging.getLogger(__name__)


class SettlementBatchService:
    """
    Processes agent settlements in batches
    
    Workflow:
    1. Identify agents with unsettled commissions
    2. Calculate total earnings for the period
    3. Create settlement records
    4. Mark commissions as settled
    """
    
    def __init__(self, database: Database):
        self.database = database
    
    async def process_monthly_settlements(
        self,
        period_end: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """
        Process monthly settlements for all agents
        
        Args:
            period_end: End of settlement period (default: now)
            
        Returns:
            Summary of settlements created
        """
        if not period_end:
            period_end = datetime.utcnow()
        
        # Period is the month ending at period_end
        period_start = period_end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        
        logger.info(f"Processing monthly settlements for period {period_start} to {period_end}")
        
        try:
            # Get all agents with unsettled commissions
            agents = await self._get_agents_with_commissions(period_start, period_end)
            
            settlements_created = []
            total_amount = Decimal('0')
            
            for agent_data in agents:
                agent_id = agent_data['agent_id']
                
                # Calculate settlement for this agent
                settlement = await self._create_agent_settlement(
                    agent_id=agent_id,
                    period_start=period_start,
                    period_end=period_end
                )
                
                if settlement:
                    settlements_created.append(settlement)
                    total_amount += Decimal(str(settlement['amount']))
            
            logger.info(
                f"Created {len(settlements_created)} settlements "
                f"totaling ${total_amount}"
            )
            
            return {
                "status": "success",
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
                "settlements_created": len(settlements_created),
                "total_amount": float(total_amount),
                "settlements": settlements_created
            }
            
        except Exception as e:
            logger.error(f"Error processing monthly settlements: {e}")
            return {
                "status": "error",
                "message": str(e)
            }
    
    async def _get_agents_with_commissions(
        self,
        period_start: datetime,
        period_end: datetime
    ) -> List[Dict[str, Any]]:
        """Get list of agents with commissions in the period"""
        try:
            query = """
                SELECT DISTINCT 
                    agent_id,
                    COUNT(*) as transaction_count,
                    SUM(agent_commission) as total_commission
                FROM revenue_matching_logs
                WHERE created_at >= :period_start
                AND created_at < :period_end
                AND agent_id IS NOT NULL
                GROUP BY agent_id
                HAVING SUM(agent_commission) > 0
                ORDER BY agent_id
            """
            
            agents = await self.database.fetch_all(query, {
                "period_start": period_start,
                "period_end": period_end
            })
            
            return [dict(agent) for agent in agents]
            
        except Exception as e:
            logger.error(f"Error fetching agents with commissions: {e}")
            return []
    
    async def _create_agent_settlement(
        self,
        agent_id: str,
        period_start: datetime,
        period_end: datetime
    ) -> Optional[Dict[str, Any]]:
        """Create settlement record for an agent"""
        try:
            # Check if settlement already exists for this period
            existing = await self._check_existing_settlement(
                agent_id, period_start, period_end
            )
            if existing:
                logger.info(f"Settlement already exists for agent {agent_id} in this period")
                return None
            
            # Calculate total commission and transaction count
            summary = await self._get_agent_commission_summary(
                agent_id, period_start, period_end
            )
            
            if not summary or summary['total_commission'] <= 0:
                logger.info(f"No commissions to settle for agent {agent_id}")
                return None
            
            # Get merchant breakdown
            merchant_breakdown = await self._get_merchant_breakdown(
                agent_id, period_start, period_end
            )
            
            # Create settlement record
            settlement_id = await self._insert_settlement(
                agent_id=agent_id,
                period_start=period_start,
                period_end=period_end,
                amount=summary['total_commission'],
                transaction_count=summary['transaction_count'],
                merchant_breakdown=merchant_breakdown
            )
            
            logger.info(
                f"Created settlement {settlement_id} for agent {agent_id}: "
                f"${summary['total_commission']} from {summary['transaction_count']} transactions"
            )
            
            return {
                "settlement_id": settlement_id,
                "agent_id": agent_id,
                "amount": float(summary['total_commission']),
                "transaction_count": summary['transaction_count'],
                "merchant_count": len(merchant_breakdown),
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat()
            }
            
        except Exception as e:
            logger.error(f"Error creating settlement for agent {agent_id}: {e}")
            return None
    
    async def _check_existing_settlement(
        self,
        agent_id: str,
        period_start: datetime,
        period_end: datetime
    ) -> bool:
        """Check if settlement already exists for this period"""
        try:
            query = """
                SELECT id FROM agent_settlements
                WHERE agent_id = :agent_id
                AND settlement_period_start = :period_start
                AND settlement_period_end = :period_end
                LIMIT 1
            """
            
            result = await self.database.fetch_one(query, {
                "agent_id": agent_id,
                "period_start": period_start,
                "period_end": period_end
            })
            
            return result is not None
            
        except Exception as e:
            logger.error(f"Error checking existing settlement: {e}")
            return False
    
    async def _get_agent_commission_summary(
        self,
        agent_id: str,
        period_start: datetime,
        period_end: datetime
    ) -> Optional[Dict[str, Any]]:
        """Get commission summary for an agent"""
        try:
            query = """
                SELECT 
                    COUNT(*) as transaction_count,
                    SUM(agent_commission) as total_commission,
                    SUM(order_amount) as total_order_value
                FROM revenue_matching_logs
                WHERE agent_id = :agent_id
                AND created_at >= :period_start
                AND created_at < :period_end
            """
            
            result = await self.database.fetch_one(query, {
                "agent_id": agent_id,
                "period_start": period_start,
                "period_end": period_end
            })
            
            if result:
                return {
                    "transaction_count": result['transaction_count'],
                    "total_commission": Decimal(str(result['total_commission'] or 0)),
                    "total_order_value": Decimal(str(result['total_order_value'] or 0))
                }
            
            return None
            
        except Exception as e:
            logger.error(f"Error getting commission summary: {e}")
            return None
    
    async def _get_merchant_breakdown(
        self,
        agent_id: str,
        period_start: datetime,
        period_end: datetime
    ) -> List[Dict[str, Any]]:
        """Get commission breakdown by merchant"""
        try:
            query = """
                SELECT 
                    merchant_id,
                    COUNT(*) as transaction_count,
                    SUM(agent_commission) as commission_amount,
                    SUM(order_amount) as order_value
                FROM revenue_matching_logs
                WHERE agent_id = :agent_id
                AND created_at >= :period_start
                AND created_at < :period_end
                GROUP BY merchant_id
                ORDER BY commission_amount DESC
            """
            
            results = await self.database.fetch_all(query, {
                "agent_id": agent_id,
                "period_start": period_start,
                "period_end": period_end
            })
            
            return [
                {
                    "merchant_id": r['merchant_id'],
                    "transaction_count": r['transaction_count'],
                    "commission_amount": float(r['commission_amount']),
                    "order_value": float(r['order_value'])
                }
                for r in results
            ]
            
        except Exception as e:
            logger.error(f"Error getting merchant breakdown: {e}")
            return []
    
    async def _insert_settlement(
        self,
        agent_id: str,
        period_start: datetime,
        period_end: datetime,
        amount: Decimal,
        transaction_count: int,
        merchant_breakdown: List[Dict[str, Any]]
    ) -> str:
        """Insert settlement record into database"""
        try:
            # Generate settlement_id
            import uuid
            settlement_id = f"settle_{uuid.uuid4().hex[:16]}"
            
            # Prepare calculation details
            calculation_details = {
                "merchant_breakdown": merchant_breakdown,
                "total_commission": float(amount),
                "transaction_count": transaction_count
            }
            
            query = """
                INSERT INTO agent_settlements (
                    settlement_id,
                    agent_id,
                    settlement_period_start,
                    settlement_period_end,
                    settlement_amount,
                    total_transactions,
                    status,
                    calculation_details,
                    created_at,
                    updated_at
                ) VALUES (
                    :settlement_id,
                    :agent_id,
                    :period_start,
                    :period_end,
                    :amount,
                    :transaction_count,
                    :status,
                    :calculation_details,
                    NOW(),
                    NOW()
                )
                RETURNING settlement_id
            """
            
            import json
            result = await self.database.fetch_one(query, {
                "settlement_id": settlement_id,
                "agent_id": agent_id,
                "period_start": period_start,
                "period_end": period_end,
                "amount": float(amount),
                "transaction_count": transaction_count,
                "status": "pending",
                "calculation_details": json.dumps(calculation_details)
            })
            
            return result['settlement_id'] if result else None
            
        except Exception as e:
            logger.error(f"Error inserting settlement: {e}")
            raise


async def run_monthly_settlement_batch(database: Database) -> Dict[str, Any]:
    """
    Standalone function to run monthly settlement batch
    Can be called from admin endpoints or scheduled tasks
    """
    service = SettlementBatchService(database)
    return await service.process_monthly_settlements()

