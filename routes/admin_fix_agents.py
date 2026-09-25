"""Admin endpoint to fix agent data (name/email)"""
from fastapi import APIRouter, Depends, HTTPException, status
from db.database import database
from utils.auth import ADMIN_ROLES, get_current_user
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
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can execute this fix"
        )
    
    try:
        # First, count how many agents need fixing
        count_query = """
            SELECT COUNT(*) as count 
            FROM agents 
            WHERE name IS NULL OR name = '' 
               OR email IS NULL OR email = ''
        """
        result = await database.fetch_one(count_query)
        needs_fix = result['count']
        
        if needs_fix == 0:
            return {
                "success": True,
                "message": "No agents need fixing",
                "updated": 0
            }
        
        # Update agents with missing name/email
        update_query = """
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
                            LOWER(REPLACE(COALESCE(
                                company,
                                use_case,
                                CONCAT('agent_', SUBSTRING(agent_id, 1, 8))
                            ), ' ', '_')),
                            '@example.com'
                        )
                    ELSE email
                END
            WHERE name IS NULL OR name = '' 
               OR email IS NULL OR email = ''
            RETURNING agent_id, name, email, company, use_case
        """
        
        # Execute update and get results
        updated_agents = await database.fetch_all(update_query)
        
        # Format results
        results = []
        for agent in updated_agents:
            results.append({
                "agent_id": agent['agent_id'],
                "new_name": agent['name'],
                "new_email": agent['email'],
                "company": agent['company'],
                "use_case": agent['use_case']
            })
        
        return {
            "success": True,
            "message": f"Successfully fixed {len(results)} agents",
            "updated": len(results),
            "sample_results": results[:5]  # Show first 5 as sample
        }
        
    except Exception as e:
        logger.error(f"Error fixing agents data: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fix agents data: {str(e)}"
        )

@router.get("/agents-status")
async def check_agents_status(current_user: dict = Depends(get_current_user)):
    """
    Check how many agents need fixing
    """
    # Check if user is admin
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can check agent status"
        )
    
    try:
        # Count agents needing fix
        stats_query = """
            SELECT 
                COUNT(*) as total,
                COUNT(CASE WHEN name IS NULL OR name = '' THEN 1 END) as missing_name,
                COUNT(CASE WHEN email IS NULL OR email = '' THEN 1 END) as missing_email
            FROM agents
        """
        
        stats = await database.fetch_one(stats_query)
        
        # Get sample of agents needing fix
        sample_query = """
            SELECT agent_id, name, email, company, use_case, created_at
            FROM agents
            WHERE name IS NULL OR name = '' 
               OR email IS NULL OR email = ''
            LIMIT 5
        """
        
        sample_agents = await database.fetch_all(sample_query)
        
        formatted_samples = []
        for agent in sample_agents:
            formatted_samples.append({
                "agent_id": agent['agent_id'],
                "name": agent['name'],
                "email": agent['email'],
                "company": agent['company'],
                "use_case": agent['use_case'],
                "created_at": agent['created_at'].isoformat() if agent['created_at'] else None
            })
        
        return {
            "total_agents": stats['total'],
            "needs_fix": {
                "missing_name": stats['missing_name'],
                "missing_email": stats['missing_email'],
                "any_missing": stats['missing_name'] + stats['missing_email']
            },
            "sample_agents_needing_fix": formatted_samples
        }
        
    except Exception as e:
        logger.error(f"Error checking agents status: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to check agents status: {str(e)}"
        )
from db.database import database
# NOTE: this module's whole body appears TWICE; `router` is rebound here, so
# main.py mounts THIS copy's routes and the ones above are dead. ADMIN_ROLES
# is therefore imported on both sides -- the live guards below use it, and
# relying on the dead copy's import to bind it would turn the obvious
# de-duplication cleanup into a NameError on every admin route here.
from utils.auth import ADMIN_ROLES, get_current_user
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
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can execute this fix"
        )
    
    try:
        # First, count how many agents need fixing
        count_query = """
            SELECT COUNT(*) as count 
            FROM agents 
            WHERE name IS NULL OR name = '' 
               OR email IS NULL OR email = ''
        """
        result = await database.fetch_one(count_query)
        needs_fix = result['count']
        
        if needs_fix == 0:
            return {
                "success": True,
                "message": "No agents need fixing",
                "updated": 0
            }
        
        # Update agents with missing name/email
        update_query = """
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
                            LOWER(REPLACE(COALESCE(
                                company,
                                use_case,
                                CONCAT('agent_', SUBSTRING(agent_id, 1, 8))
                            ), ' ', '_')),
                            '@example.com'
                        )
                    ELSE email
                END
            WHERE name IS NULL OR name = '' 
               OR email IS NULL OR email = ''
            RETURNING agent_id, name, email, company, use_case
        """
        
        # Execute update and get results
        updated_agents = await database.fetch_all(update_query)
        
        # Format results
        results = []
        for agent in updated_agents:
            results.append({
                "agent_id": agent['agent_id'],
                "new_name": agent['name'],
                "new_email": agent['email'],
                "company": agent['company'],
                "use_case": agent['use_case']
            })
        
        return {
            "success": True,
            "message": f"Successfully fixed {len(results)} agents",
            "updated": len(results),
            "sample_results": results[:5]  # Show first 5 as sample
        }
        
    except Exception as e:
        logger.error(f"Error fixing agents data: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fix agents data: {str(e)}"
        )

@router.get("/agents-status")
async def check_agents_status(current_user: dict = Depends(get_current_user)):
    """
    Check how many agents need fixing
    """
    # Check if user is admin
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can check agent status"
        )
    
    try:
        # Count agents needing fix
        stats_query = """
            SELECT 
                COUNT(*) as total,
                COUNT(CASE WHEN name IS NULL OR name = '' THEN 1 END) as missing_name,
                COUNT(CASE WHEN email IS NULL OR email = '' THEN 1 END) as missing_email
            FROM agents
        """
        
        stats = await database.fetch_one(stats_query)
        
        # Get sample of agents needing fix
        sample_query = """
            SELECT agent_id, name, email, company, use_case, created_at
            FROM agents
            WHERE name IS NULL OR name = '' 
               OR email IS NULL OR email = ''
            LIMIT 5
        """
        
        sample_agents = await database.fetch_all(sample_query)
        
        formatted_samples = []
        for agent in sample_agents:
            formatted_samples.append({
                "agent_id": agent['agent_id'],
                "name": agent['name'],
                "email": agent['email'],
                "company": agent['company'],
                "use_case": agent['use_case'],
                "created_at": agent['created_at'].isoformat() if agent['created_at'] else None
            })
        
        return {
            "total_agents": stats['total'],
            "needs_fix": {
                "missing_name": stats['missing_name'],
                "missing_email": stats['missing_email'],
                "any_missing": stats['missing_name'] + stats['missing_email']
            },
            "sample_agents_needing_fix": formatted_samples
        }
        
    except Exception as e:
        logger.error(f"Error checking agents status: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to check agents status: {str(e)}"
        )