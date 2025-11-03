"""
[Phase 5.6] Agent Integration Bridge
Aggregates data from EXISTING tables for Agent Portal visualization
NO NEW ROUTING LOGIC - only reads existing logs
"""

from typing import Dict, List, Any
from datetime import datetime, timedelta
import logging

from databases import Database

logger = logging.getLogger(__name__)


class AgentIntegrationBridge:
    """[Phase 5.6] Aggregates existing data for Agent Portal dashboards"""
    
    def __init__(self, database: Database):
        self.database = database
    
    async def get_integration_overview(self, agent_id: str) -> Dict[str, Any]:
        """
        Aggregate from EXISTING tables:
        - routing_logs
        - protocol_events  
        - agent_protocols
        - agent_revenue_logs
        """
        
        # Routing stats (from existing routing_logs)
        routing_stats = await self.database.fetch_one(
            """
            SELECT 
                COUNT(*) as total_routings,
                COUNT(DISTINCT merchant_id) as unique_merchants,
                COUNT(*) FILTER (WHERE conflict_detected) as conflicts
            FROM routing_logs
            WHERE agent_id = :agent_id
            AND created_at > NOW() - INTERVAL '30 days'
            """,
            {"agent_id": agent_id}
        )
        
        # Protocol status (from existing agent_protocols)
        protocols = await self.database.fetch_all(
            """
            SELECT protocol_name, status, version, last_tested_at
            FROM agent_protocols
            WHERE agent_id = :agent_id
            """,
            {"agent_id": agent_id}
        )
        
        # Revenue stats (from existing agent_revenue_logs)
        revenue = await self.database.fetch_one(
            """
            SELECT 
                SUM(agent_earned_amount) as total_earned,
                COUNT(*) as transactions
            FROM agent_revenue_logs
            WHERE agent_id = :agent_id
            AND created_at > NOW() - INTERVAL '30 days'
            """,
            {"agent_id": agent_id}
        )
        
        return {
            "agent_id": agent_id,
            "routing": dict(routing_stats) if routing_stats else {},
            "protocols": [dict(p) for p in protocols],
            "revenue": dict(revenue) if revenue else {},
            "last_updated": datetime.utcnow().isoformat()
        }
    
    async def log_integration_action(
        self,
        agent_id: str,
        action_type: str,
        target_entity: str,
        status: str,
        details: Dict[str, Any]
    ) -> int:
        """Log integration action to NEW agent_integration_logs table"""
        
        result = await self.database.execute(
            """
            INSERT INTO agent_integration_logs (
                agent_id, action_type, target_entity, status,
                request_data, response_data, created_at
            ) VALUES (
                :agent_id, :action_type, :target, :status,
                :request, :response, NOW()
            )
            RETURNING id
            """,
            {
                "agent_id": agent_id,
                "action_type": action_type,
                "target": target_entity,
                "status": status,
                "request": json.dumps(details.get("request", {})),
                "response": json.dumps(details.get("response", {}))
            }
        )
        
        return result


print("[Phase 5.6] AgentIntegrationBridge loaded - aggregates existing routing/protocol/revenue logs")
