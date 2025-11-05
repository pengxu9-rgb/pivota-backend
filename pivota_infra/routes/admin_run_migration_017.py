"""
Admin endpoint to run migration 017 - Agent Payout Comprehensive
"""

from fastapi import APIRouter, Depends
from utils.auth import require_admin
from db.database import database
from sqlalchemy import text
import logging
import os

router = APIRouter(
    prefix="/admin/migrations",
    tags=["Admin - Migrations"]
)

logger = logging.getLogger(__name__)

@router.post("/017-agent-payout")
async def run_migration_017(current_user: dict = Depends(require_admin)):
    """Run migration 017 to create agent payout tables"""
    
    try:
        # Read migration file
        migration_path = os.path.join(
            os.path.dirname(__file__),
            "../db/migrations/017_agent_payout_comprehensive.sql"
        )
        
        with open(migration_path, 'r') as f:
            migration_sql = f.read()
        
        # Execute migration
        logger.info("Running migration 017...")
        await database.execute(text(migration_sql))
        
        logger.info("Migration 017 completed successfully")
        
        return {
            "success": True,
            "message": "Migration 017 executed successfully",
            "migration": "017_agent_payout_comprehensive.sql"
        }
        
    except Exception as e:
        logger.error(f"Migration 017 failed: {e}", exc_info=True)
        return {
            "success": False,
            "error": str(e)
        }

