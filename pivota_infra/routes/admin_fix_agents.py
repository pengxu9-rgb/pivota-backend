"""Admin endpoint to fix agent data (name/email)"""
from fastapi import APIRouter, Depends, HTTPException, status
from database.connection import get_db_connection
from auth import get_current_user
import logging
from typing import Dict, Any

router = APIRouter(prefix="/admin/fix", tags=["Admin Fix"])
logger = logging.getLogger(__name__)

@router.post("/agents-data")
async def fix_agents_data(current_user: dict = Depends(get_current_user)):
    """
    Fix agents with null name/email by populating from company/use_case/agent_id
    """
    # Check if user is admin
    if current_user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can execute this fix"
        )
    
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # First, count how many agents need fixing
        cur.execute("""
            SELECT COUNT(*) 
            FROM agents 
            WHERE name IS NULL OR name = '' 
               OR email IS NULL OR email = ''
        """)
        needs_fix = cur.fetchone()[0]
        
        if needs_fix == 0:
            return {
                "success": True,
                "message": "No agents need fixing",
                "updated": 0
            }
        
        # Update agents with missing name/email
        cur.execute("""
            UPDATE agents 
            SET 
                name = CASE 
                    WHEN name IS NULL OR name = '' THEN 
                        COALESCE(
                            company,
                            use_case,
                            CONCAT('Agent_', SUBSTRING(agent_id, 1, 8))
                        )
                    ELSE name
                END,
                email = CASE 
                    WHEN email IS NULL OR email = '' THEN 
                        CONCAT(
                            LOWER(COALESCE(
                                company,
                                use_case,
                                CONCAT('agent_', SUBSTRING(agent_id, 1, 8))
                            )),
                            '@example.com'
                        )
                    ELSE email
                END
            WHERE name IS NULL OR name = '' 
               OR email IS NULL OR email = ''
            RETURNING agent_id, name, email, company, use_case
        """)
        
        updated_agents = cur.fetchall()
        conn.commit()
        
        # Format results
        results = []
        for agent in updated_agents:
            results.append({
                "agent_id": agent[0],
                "new_name": agent[1],
                "new_email": agent[2],
                "company": agent[3],
                "use_case": agent[4]
            })
        
        return {
            "success": True,
            "message": f"Successfully fixed {len(results)} agents",
            "updated": len(results),
            "sample_results": results[:5]  # Show first 5 as sample
        }
        
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error fixing agents data: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fix agents data: {str(e)}"
        )
    finally:
        if conn:
            cur.close()
            conn.close()

@router.get("/agents-status")
async def check_agents_status(current_user: dict = Depends(get_current_user)):
    """
    Check how many agents need fixing
    """
    # Check if user is admin
    if current_user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can check agent status"
        )
    
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Count agents needing fix
        cur.execute("""
            SELECT 
                COUNT(*) as total,
                COUNT(CASE WHEN name IS NULL OR name = '' THEN 1 END) as missing_name,
                COUNT(CASE WHEN email IS NULL OR email = '' THEN 1 END) as missing_email
            FROM agents
        """)
        
        stats = cur.fetchone()
        
        # Get sample of agents needing fix
        cur.execute("""
            SELECT agent_id, name, email, company, use_case, created_at
            FROM agents
            WHERE name IS NULL OR name = '' 
               OR email IS NULL OR email = ''
            LIMIT 5
        """)
        
        sample_agents = []
        for agent in cur.fetchall():
            sample_agents.append({
                "agent_id": agent[0],
                "name": agent[1],
                "email": agent[2],
                "company": agent[3],
                "use_case": agent[4],
                "created_at": agent[5].isoformat() if agent[5] else None
            })
        
        return {
            "total_agents": stats[0],
            "needs_fix": {
                "missing_name": stats[1],
                "missing_email": stats[2],
                "any_missing": stats[1] + stats[2]
            },
            "sample_agents_needing_fix": sample_agents
        }
        
    except Exception as e:
        logger.error(f"Error checking agents status: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to check agents status: {str(e)}"
        )
    finally:
        if conn:
            cur.close()
            conn.close()