"""
Admin endpoint to fix agent protocols - replace REST v1.0 with Phase 4 protocols
"""
from fastapi import APIRouter, Depends, HTTPException
from utils.auth import ADMIN_ROLES, get_current_user
from db.database import database

router = APIRouter(prefix="/admin/agents", tags=["Admin Agent Protocols"])

@router.post("/fix-protocols")
async def fix_agent_protocols(
    agent_id: str = None,
    current_user: dict = Depends(get_current_user)
):
    """
    Fix agent protocols by:
    1. Removing old REST v1.0 protocol
    2. Adding Phase 4 protocols (AP2, ACP)
    3. Optionally enabling X-402 (beta)
    """
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin only")
    
    try:
        results = []
        
        # If agent_id specified, fix only that agent, otherwise fix all
        if agent_id:
            agents = [{"agent_id": agent_id}]
        else:
            agents = await database.fetch_all("SELECT agent_id FROM agents")
        
        for agent in agents:
            aid = agent["agent_id"]
            
            # 1. Disable old REST v1.0 protocol (keep for history, just mark inactive)
            rest_disabled = await database.execute(
                """
                UPDATE agent_protocols 
                SET status = 'deprecated'
                WHERE agent_id = :agent_id 
                AND protocol_name = 'REST' 
                AND version = '1.0'
                """,
                {"agent_id": aid}
            )
            
            if rest_disabled:
                results.append(f"✅ {aid}: Deprecated REST v1.0")
            
            # 2. Add AP2 v2.0
            await database.execute(
                """
                INSERT INTO agent_protocols (agent_id, protocol_name, version, status, last_verified_at)
                VALUES (:agent_id, 'AP2', '2.0', 'active', NOW())
                ON CONFLICT (agent_id, protocol_name, version) 
                DO UPDATE SET status = 'active', last_verified_at = NOW()
                """,
                {"agent_id": aid}
            )
            results.append(f"✅ {aid}: Added/Updated AP2 v2.0")
            
            # 3. Add ACP v1.0
            await database.execute(
                """
                INSERT INTO agent_protocols (agent_id, protocol_name, version, status, last_verified_at)
                VALUES (:agent_id, 'ACP', '1.0', 'active', NOW())
                ON CONFLICT (agent_id, protocol_name, version) 
                DO UPDATE SET status = 'active', last_verified_at = NOW()
                """,
                {"agent_id": aid}
            )
            results.append(f"✅ {aid}: Added/Updated ACP v1.0")
            
            # 4. Add X-402 v3.1 (as active, not beta)
            await database.execute(
                """
                INSERT INTO agent_protocols (agent_id, protocol_name, version, status, last_verified_at)
                VALUES (:agent_id, 'X-402', '3.1', 'active', NOW())
                ON CONFLICT (agent_id, protocol_name, version) 
                DO UPDATE SET status = 'active', last_verified_at = NOW()
                """,
                {"agent_id": aid}
            )
            results.append(f"✅ {aid}: Added/Updated X-402 v3.1")
        
        # Get final count
        final_protocols = await database.fetch_all(
            """
            SELECT agent_id, protocol_name, version, status
            FROM agent_protocols
            WHERE status = 'active'
            ORDER BY agent_id, protocol_name
            """
        )
        
        protocols_by_agent = {}
        for p in final_protocols:
            agent = p["agent_id"]
            if agent not in protocols_by_agent:
                protocols_by_agent[agent] = []
            protocols_by_agent[agent].append(f"{p['protocol_name']} v{p['version']}")
        
        return {
            "status": "success",
            "message": "Agent protocols updated",
            "agents_updated": len(agents),
            "steps": results[:20],  # Show first 20 to avoid huge response
            "summary": {
                "total_active_protocols": len(final_protocols),
                "agents_with_protocols": len(protocols_by_agent),
                "sample_agent_protocols": {
                    k: v for k, v in list(protocols_by_agent.items())[:3]
                }
            }
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fix protocols: {str(e)}")


@router.get("/protocols-status")
async def get_protocols_status(
    current_user: dict = Depends(get_current_user)
):
    """
    Get current status of all agent protocols
    """
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin only")
    
    # Count by protocol and status
    protocol_stats = await database.fetch_all(
        """
        SELECT 
            protocol_name,
            version,
            status,
            COUNT(DISTINCT agent_id) as agent_count
        FROM agent_protocols
        GROUP BY protocol_name, version, status
        ORDER BY protocol_name, version
        """
    )
    
    return {
        "protocol_statistics": [
            {
                "protocol": f"{p['protocol_name']} v{p['version']}",
                "status": p['status'],
                "agent_count": p['agent_count']
            }
            for p in protocol_stats
        ]
    }
