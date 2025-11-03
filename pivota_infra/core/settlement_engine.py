"""
[Phase 5.6] Agent Settlement Engine
Calculates settlements using EXISTING RevenueShareService (Phase 5.5)
"""

from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
from decimal import Decimal
import logging
import json

from databases import Database
from services.revenue_share_service import RevenueShareService  # REUSE Phase 5.5

logger = logging.getLogger(__name__)


class AgentSettlementEngine:
    """
    [Phase 5.6] Settlement calculation engine
    
    REUSES:
    - RevenueShareService for commission matching
    - revenue_matching_logs table for transaction data
    - agent_revenue_logs table for actual earnings
    """
    
    def __init__(self, revenue_service: RevenueShareService, database: Database):
        self.revenue_service = revenue_service  # REUSE existing service
        self.database = database
        
    async def calculate_settlement(
        self,
        agent_id: str,
        period_start: datetime,
        period_end: datetime,
        merchant_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Calculate settlement for agent in given period
        
        Uses EXISTING revenue_matching_logs to aggregate earnings
        """
        try:
            # Query existing revenue_matching_logs (Phase 5.5 table)
            query = """
                SELECT 
                    COUNT(*) as transaction_count,
                    SUM(rml.actual_commission_rate * arl.transaction_amount) as total_commission,
                    AVG(rml.actual_commission_rate) as avg_rate,
                    array_agg(DISTINCT arl.merchant_id) as merchants
                FROM revenue_matching_logs rml
                JOIN agent_revenue_logs arl ON arl.matching_log_id = rml.id
                WHERE rml.agent_id = :agent_id
                AND rml.matched_at BETWEEN :period_start AND :period_end
            """
            
            params = {
                "agent_id": agent_id,
                "period_start": period_start,
                "period_end": period_end
            }
            
            if merchant_id:
                query += " AND rml.merchant_id = :merchant_id"
                params["merchant_id"] = merchant_id
            
            result = await self.database.fetch_one(query, params)
            
            settlement_amount = Decimal(str(result["total_commission"] or 0))
            
            # Generate settlement ID
            settlement_id = f"settle_{agent_id[6:14]}_{period_end.strftime('%Y%m%d')}"
            
            # Create settlement record
            insert_query = """
                INSERT INTO agent_settlements (
                    settlement_id, agent_id, merchant_id,
                    settlement_period_start, settlement_period_end,
                    total_transactions, settlement_amount,
                    commission_rate_applied, status, calculation_details,
                    created_at
                ) VALUES (
                    :settlement_id, :agent_id, :merchant_id,
                    :period_start, :period_end,
                    :transactions, :amount,
                    :avg_rate, 'pending', :details,
                    NOW()
                )
                RETURNING id
            """
            
            settlement_record_id = await self.database.execute(insert_query, {
                "settlement_id": settlement_id,
                "agent_id": agent_id,
                "merchant_id": merchant_id,
                "period_start": period_start,
                "period_end": period_end,
                "transactions": result["transaction_count"] or 0,
                "amount": float(settlement_amount),
                "avg_rate": float(result["avg_rate"] or 0),
                "details": json.dumps({
                    "calculation_date": datetime.utcnow().isoformat(),
                    "merchants": result["merchants"] or [],
                    "source": "revenue_matching_logs"
                })
            })
            
            logger.info(
                f"[Phase 5.6] Settlement calculated: id={settlement_id}, "
                f"agent={agent_id}, amount={settlement_amount}"
            )
            
            return {
                "settlement_id": settlement_id,
                "record_id": settlement_record_id,
                "agent_id": agent_id,
                "period": {"start": period_start, "end": period_end},
                "total_transactions": result["transaction_count"] or 0,
                "settlement_amount": float(settlement_amount),
                "avg_commission_rate": float(result["avg_rate"] or 0),
                "status": "pending"
            }
            
        except Exception as e:
            logger.error(f"[Phase 5.6] Settlement calculation failed: {e}")
            raise
    
    async def get_pending_settlements(self, agent_id: str) -> List[Dict[str, Any]]:
        """Get all pending settlements for agent"""
        
        results = await self.database.fetch_all(
            """
            SELECT * FROM agent_settlements
            WHERE agent_id = :agent_id AND status = 'pending'
            ORDER BY created_at DESC
            """,
            {"agent_id": agent_id}
        )
        
        return [dict(r) for r in results]
    
    async def trigger_payout(self, settlement_id: str, payout_method: str = "bank_transfer") -> bool:
        """Mark settlement for payout processing"""
        
        try:
            await self.database.execute(
                """
                UPDATE agent_settlements
                SET status = 'processing',
                    payout_method = :method,
                    updated_at = NOW()
                WHERE settlement_id = :settlement_id
                """,
                {"settlement_id": settlement_id, "method": payout_method}
            )
            
            logger.info(f"[Phase 5.6] Payout triggered: {settlement_id}")
            return True
            
        except Exception as e:
            logger.error(f"[Phase 5.6] Payout trigger failed: {e}")
            return False


# [Phase 5.6] Test module
if __name__ == "__main__":
    print("[Phase 5.6] AgentSettlementEngine module loaded")
    print("Reuses: RevenueShareService, revenue_matching_logs, agent_revenue_logs")
    print("Features: Calculate settlements, trigger payouts, track status")
[Phase 5.6] Agent Settlement Engine
Calculates settlements using EXISTING RevenueShareService (Phase 5.5)
"""

from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
from decimal import Decimal
import logging
import json

from databases import Database
from services.revenue_share_service import RevenueShareService  # REUSE Phase 5.5

logger = logging.getLogger(__name__)


class AgentSettlementEngine:
    """
    [Phase 5.6] Settlement calculation engine
    
    REUSES:
    - RevenueShareService for commission matching
    - revenue_matching_logs table for transaction data
    - agent_revenue_logs table for actual earnings
    """
    
    def __init__(self, revenue_service: RevenueShareService, database: Database):
        self.revenue_service = revenue_service  # REUSE existing service
        self.database = database
        
    async def calculate_settlement(
        self,
        agent_id: str,
        period_start: datetime,
        period_end: datetime,
        merchant_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Calculate settlement for agent in given period
        
        Uses EXISTING revenue_matching_logs to aggregate earnings
        """
        try:
            # Query existing revenue_matching_logs (Phase 5.5 table)
            query = """
                SELECT 
                    COUNT(*) as transaction_count,
                    SUM(rml.actual_commission_rate * arl.transaction_amount) as total_commission,
                    AVG(rml.actual_commission_rate) as avg_rate,
                    array_agg(DISTINCT arl.merchant_id) as merchants
                FROM revenue_matching_logs rml
                JOIN agent_revenue_logs arl ON arl.matching_log_id = rml.id
                WHERE rml.agent_id = :agent_id
                AND rml.matched_at BETWEEN :period_start AND :period_end
            """
            
            params = {
                "agent_id": agent_id,
                "period_start": period_start,
                "period_end": period_end
            }
            
            if merchant_id:
                query += " AND rml.merchant_id = :merchant_id"
                params["merchant_id"] = merchant_id
            
            result = await self.database.fetch_one(query, params)
            
            settlement_amount = Decimal(str(result["total_commission"] or 0))
            
            # Generate settlement ID
            settlement_id = f"settle_{agent_id[6:14]}_{period_end.strftime('%Y%m%d')}"
            
            # Create settlement record
            insert_query = """
                INSERT INTO agent_settlements (
                    settlement_id, agent_id, merchant_id,
                    settlement_period_start, settlement_period_end,
                    total_transactions, settlement_amount,
                    commission_rate_applied, status, calculation_details,
                    created_at
                ) VALUES (
                    :settlement_id, :agent_id, :merchant_id,
                    :period_start, :period_end,
                    :transactions, :amount,
                    :avg_rate, 'pending', :details,
                    NOW()
                )
                RETURNING id
            """
            
            settlement_record_id = await self.database.execute(insert_query, {
                "settlement_id": settlement_id,
                "agent_id": agent_id,
                "merchant_id": merchant_id,
                "period_start": period_start,
                "period_end": period_end,
                "transactions": result["transaction_count"] or 0,
                "amount": float(settlement_amount),
                "avg_rate": float(result["avg_rate"] or 0),
                "details": json.dumps({
                    "calculation_date": datetime.utcnow().isoformat(),
                    "merchants": result["merchants"] or [],
                    "source": "revenue_matching_logs"
                })
            })
            
            logger.info(
                f"[Phase 5.6] Settlement calculated: id={settlement_id}, "
                f"agent={agent_id}, amount={settlement_amount}"
            )
            
            return {
                "settlement_id": settlement_id,
                "record_id": settlement_record_id,
                "agent_id": agent_id,
                "period": {"start": period_start, "end": period_end},
                "total_transactions": result["transaction_count"] or 0,
                "settlement_amount": float(settlement_amount),
                "avg_commission_rate": float(result["avg_rate"] or 0),
                "status": "pending"
            }
            
        except Exception as e:
            logger.error(f"[Phase 5.6] Settlement calculation failed: {e}")
            raise
    
    async def get_pending_settlements(self, agent_id: str) -> List[Dict[str, Any]]:
        """Get all pending settlements for agent"""
        
        results = await self.database.fetch_all(
            """
            SELECT * FROM agent_settlements
            WHERE agent_id = :agent_id AND status = 'pending'
            ORDER BY created_at DESC
            """,
            {"agent_id": agent_id}
        )
        
        return [dict(r) for r in results]
    
    async def trigger_payout(self, settlement_id: str, payout_method: str = "bank_transfer") -> bool:
        """Mark settlement for payout processing"""
        
        try:
            await self.database.execute(
                """
                UPDATE agent_settlements
                SET status = 'processing',
                    payout_method = :method,
                    updated_at = NOW()
                WHERE settlement_id = :settlement_id
                """,
                {"settlement_id": settlement_id, "method": payout_method}
            )
            
            logger.info(f"[Phase 5.6] Payout triggered: {settlement_id}")
            return True
            
        except Exception as e:
            logger.error(f"[Phase 5.6] Payout trigger failed: {e}")
            return False


# [Phase 5.6] Test module
if __name__ == "__main__":
    print("[Phase 5.6] AgentSettlementEngine module loaded")
    print("Reuses: RevenueShareService, revenue_matching_logs, agent_revenue_logs")
    print("Features: Calculate settlements, trigger payouts, track status")
