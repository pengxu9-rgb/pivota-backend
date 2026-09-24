"""Admin endpoint to fix agent metrics display"""
from fastapi import APIRouter, Depends, HTTPException, status
from db.database import database
from utils.auth import ADMIN_ROLES, get_current_user
import logging
from typing import Dict, Any

# This module's whole body used to appear twice, the second copy rebinding `router`; the dead
# first copy is gone, so this is the one main.py mounts.
router = APIRouter(prefix="/admin/fix", tags=["Admin Fix"])
logger = logging.getLogger(__name__)

# RETIRED (501). It dropped and re-created the agent_metrics_24h view (nothing reads it), then
# ran UPDATE agents SET request_count = total_requests, success_rate = <all-time 2xx usage logs
# / total_requests * 100>. agents has no request_count column (db/agents.py), so that UPDATE
# fails on prod's table on every call, after the view has already been swapped, and the
# success_rate rewrite cannot run there. Dropping only request_count would arm it: a rewrite of
# success_rate for every agent with total_requests > 0, from two different populations (it can
# pass 100, and at 1000 overflows Numeric(5,2)).
@router.post("/agent-metrics")
async def fix_agent_metrics(current_user: dict = Depends(get_current_user)):
    """Retired: answers 501 and writes nothing."""
    # Check if user is admin
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can execute this fix"
        )
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="The agent metrics fix is retired; agents.total_requests is the request counter",
    )

@router.get("/agent-metrics-status")
async def check_agent_metrics_status(current_user: dict = Depends(get_current_user)):
    """
    Check the current status of agent metrics
    """
    # Check if user is admin
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can check metrics status"
        )

    try:
        # Check if view exists
        view_check = """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.views
                WHERE table_name = 'agent_metrics_24h'
            ) as view_exists
        """

        view_result = await database.fetch_one(view_check)

        # agents has no request_count column, so there is no request_count / total_requests
        # discrepancy to report (this used to 500 on every call); total_requests is the counter.
        agents_check = """
            SELECT
                COUNT(*) as total_agents,
                SUM(total_requests) as total_all_requests
            FROM agents
        """

        agents_row = await database.fetch_one(agents_check)

        # Check usage logs
        usage_check = """
            SELECT
                COUNT(DISTINCT agent_id) as agents_with_logs,
                COUNT(*) as total_logs,
                MIN(timestamp) as earliest_log,
                MAX(timestamp) as latest_log
            FROM agent_usage_logs
        """

        usage = await database.fetch_one(usage_check)

        return {
            "view_exists": view_result["view_exists"],
            "agents": {
                "total": agents_row["total_agents"],
                "total_requests_sum": agents_row["total_all_requests"],
            },
            "usage_logs": {
                "agents_with_logs": usage["agents_with_logs"],
                "total_logs": usage["total_logs"],
                "earliest": usage["earliest_log"].isoformat() if usage["earliest_log"] else None,
                "latest": usage["latest_log"].isoformat() if usage["latest_log"] else None
            },
        }

    except Exception as e:
        logger.error(f"Error checking metrics status: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to check metrics status: {str(e)}"
        )
