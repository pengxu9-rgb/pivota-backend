"""
Admin endpoint to cleanup all test data
Keeps only specified production merchants and agents
"""
from fastapi import APIRouter, Depends, HTTPException
from utils.auth import require_admin_or_key
from pydantic import BaseModel, Field
from typing import List
from db.database import database
import logging
from datetime import datetime

logger = logging.getLogger(__name__)
# AUTHENTICATION. Every route on this router was reachable with NO credentials
# of any kind: no Depends, no header check, no role check. The guard is applied
# at the ROUTER, not per-handler, so a route added here later inherits it
# instead of having to remember it -- which is how this file got here.
# require_admin_or_key accepts an X-ADMIN-KEY header or an admin/super_admin
# JWT and fails closed (401) when neither is present.
#
# POST /admin/cleanup/all-test-data was an anonymous bulk DELETE.
router = APIRouter(prefix="/admin/cleanup", tags=["admin-cleanup-all"], dependencies=[Depends(require_admin_or_key)])

class CleanupAllRequest(BaseModel):
    confirm: str = Field(..., description="Type: CLEANUP ALL TEST DATA")
    keep_merchants: List[str] = Field(default_factory=list, description="Merchant IDs to keep")
    keep_agents: List[str] = Field(default_factory=list, description="Agent IDs to keep")
    backup: bool = Field(default=True)

@router.post("/all-test-data")
async def cleanup_all_test_data(payload: CleanupAllRequest):
    """
    Clean up all test data, keeping only specified merchants and agents
    
    Removes:
    - Test merchants (except those in keep_merchants)
    - Test agents (except those in keep_agents)
    - Their associated orders, products, stores, PSPs
    - Agent usage logs
    
    Creates backup tables before deletion
    """
    if payload.confirm != "CLEANUP ALL TEST DATA":
        raise HTTPException(status_code=400, detail="Confirmation phrase mismatch")
    
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    results = {
        "timestamp": ts,
        "backups": [],
        "deleted": {},
        "kept": {
            "merchants": payload.keep_merchants,
            "agents": payload.keep_agents
        }
    }
    
    try:
        # Step 1: Create backups
        if payload.backup:
            backup_tables = [
                "merchant_onboarding",
                "merchant_stores",
                "merchant_psps",
                "agents",
                "orders",
                "products_cache",
                "agent_usage_logs",
                "agent_merchants"
            ]
            
            for table in backup_tables:
                try:
                    backup_name = f"backup_{table}_{ts}"
                    await database.execute(f"CREATE TABLE {backup_name} AS SELECT * FROM {table}")
                    results["backups"].append(backup_name)
                    logger.info(f"Created backup: {backup_name}")
                except Exception as e:
                    logger.warning(f"Failed to backup {table}: {e}")
        
        # Step 2: Delete merchants (except keep list)
        if payload.keep_merchants:
            placeholders = ','.join([f"'{m}'" for m in payload.keep_merchants])
            merchant_where = f"WHERE merchant_id NOT IN ({placeholders})"
        else:
            merchant_where = ""  # Delete all
        
        # Delete merchant-related data
        deleted_merchants = await database.execute(
            f"DELETE FROM merchant_stores {merchant_where}"
        )
        deleted_psps = await database.execute(
            f"DELETE FROM merchant_psps {merchant_where}"
        )
        deleted_cache = await database.execute(
            f"DELETE FROM products_cache {merchant_where}"
        )
        deleted_orders = await database.execute(
            f"DELETE FROM orders {merchant_where}"
        )
        deleted_merchant_records = await database.execute(
            f"DELETE FROM merchant_onboarding {merchant_where}"
        )
        
        results["deleted"]["merchants"] = deleted_merchant_records or 0
        results["deleted"]["merchant_stores"] = deleted_merchants or 0
        results["deleted"]["merchant_psps"] = deleted_psps or 0
        results["deleted"]["products_cache"] = deleted_cache or 0
        results["deleted"]["orders"] = deleted_orders or 0
        
        # Step 3: Delete agents (except keep list)
        if payload.keep_agents:
            agent_placeholders = ','.join([f"'{a}'" for a in payload.keep_agents])
            agent_where = f"WHERE agent_id NOT IN ({agent_placeholders})"
        else:
            agent_where = ""  # Delete all
        
        deleted_agent_logs = await database.execute(
            f"DELETE FROM agent_usage_logs {agent_where}"
        )
        deleted_agent_merchants = await database.execute(
            f"DELETE FROM agent_merchants {agent_where}"
        )
        deleted_agents = await database.execute(
            f"DELETE FROM agents {agent_where}"
        )
        
        results["deleted"]["agents"] = deleted_agents or 0
        results["deleted"]["agent_usage_logs"] = deleted_agent_logs or 0
        results["deleted"]["agent_merchants"] = deleted_agent_merchants or 0
        
        logger.info(f"Cleanup completed: {results}")
        
        return {
            "success": True,
            "message": "Test data cleanup completed",
            "results": results
        }
        
    except Exception as e:
        logger.error(f"Cleanup failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/preview-cleanup")
async def preview_cleanup(
    keep_merchants: str = "",
    keep_agents: str = ""
):
    """
    Preview what would be deleted (dry run)
    
    Query params:
    - keep_merchants: comma-separated merchant IDs
    - keep_agents: comma-separated agent IDs
    """
    try:
        keep_merchant_list = [m.strip() for m in keep_merchants.split(',') if m.strip()]
        keep_agent_list = [a.strip() for a in keep_agents.split(',') if a.strip()]
        
        # Count what would be deleted
        if keep_merchant_list:
            placeholders = ','.join([f"'{m}'" for m in keep_merchant_list])
            merchant_where = f"WHERE merchant_id NOT IN ({placeholders})"
        else:
            merchant_where = ""
        
        merchants_to_delete = await database.fetch_one(
            f"SELECT COUNT(*) as count FROM merchant_onboarding {merchant_where}"
        )
        
        if keep_agent_list:
            agent_placeholders = ','.join([f"'{a}'" for a in keep_agent_list])
            agent_where = f"WHERE agent_id NOT IN ({agent_placeholders})"
        else:
            agent_where = ""
        
        agents_to_delete = await database.fetch_one(
            f"SELECT COUNT(*) as count FROM agents {agent_where}"
        )
        
        return {
            "would_delete": {
                "merchants": merchants_to_delete["count"] if merchants_to_delete else 0,
                "agents": agents_to_delete["count"] if agents_to_delete else 0
            },
            "would_keep": {
                "merchants": keep_merchant_list,
                "agents": keep_agent_list
            },
            "note": "This is a preview. Use POST /admin/cleanup/all-test-data to execute."
        }
        
    except Exception as e:
        logger.error(f"Preview failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))



