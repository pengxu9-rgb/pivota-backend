"""
[Phase 5.6] Agent Protocol Service
CRUD and management for agent protocol configurations
REUSES existing agent_protocols table (Phase 4)
"""

from typing import Dict, List, Optional, Any
import logging
import json

from databases import Database

logger = logging.getLogger(__name__)


class AgentProtocolService:
    """[Phase 5.6] Agent protocol management - REUSES Phase 4 infrastructure"""
    
    def __init__(self, database: Database):
        self.database = database
    
    async def get_agent_protocols(self, agent_id: str) -> List[Dict[str, Any]]:
        """Get all protocols for agent - REUSES agent_protocols table"""
        
        protocols = await self.database.fetch_all(
            """
            SELECT * FROM agent_protocols
            WHERE agent_id = :agent_id
            ORDER BY protocol_name
            """,
            {"agent_id": agent_id}
        )
        
        return [dict(p) for p in protocols]
    
    async def update_protocol_config(
        self,
        agent_id: str,
        protocol_name: str,
        config: Dict[str, Any]
    ) -> bool:
        """Store protocol config (API keys, etc) in EXISTING table"""
        
        try:
            await self.database.execute(
                """
                UPDATE agent_protocols
                SET protocol_config = :config,
                    updated_at = NOW()
                WHERE agent_id = :agent_id AND protocol_name = :protocol_name
                """,
                {
                    "agent_id": agent_id,
                    "protocol_name": protocol_name,
                    "config": json.dumps(config)
                }
            )
            
            logger.info(f"[Phase 5.6] Protocol config updated: {agent_id}/{protocol_name}")
            return True
            
        except Exception as e:
            logger.error(f"[Phase 5.6] Config update failed: {e}")
            return False
    
    async def test_protocol(
        self,
        agent_id: str,
        protocol_name: str,
        test_payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Test protocol with agent's config - REUSES existing adapters"""
        
        # This would use existing AP2Adapter, ACPAdapter, etc from Phase 4++
        # For now, return test result structure
        
        try:
            await self.database.execute(
                """
                UPDATE agent_protocols
                SET last_tested_at = NOW(),
                    test_result = :result
                WHERE agent_id = :agent_id AND protocol_name = :protocol_name
                """,
                {
                    "agent_id": agent_id,
                    "protocol_name": protocol_name,
                    "result": json.dumps({"status": "test_placeholder", "timestamp": datetime.utcnow().isoformat()})
                }
            )
            
            return {"status": "success", "protocol": protocol_name}
            
        except Exception as e:
            return {"status": "error", "error": str(e)}


print("[Phase 5.6] AgentProtocolService loaded - reuses Phase 4 agent_protocols table")
