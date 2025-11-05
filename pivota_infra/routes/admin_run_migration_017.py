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

@router.post("/run/017")
async def run_migration_017(current_user: dict = Depends(require_admin)):
    """
    Run migration 017 to create agent payout tables for Phase 6.1
    
    Creates:
    - agent_payout_settings: Comprehensive agent payout information
    - payout_transactions: Track individual payout executions
    - payout_method_availability: Track which methods are available per country
    - agent_payout_history: Audit log of all payout attempts
    """
    
    try:
        # Read migration file
        migration_path = os.path.join(
            os.path.dirname(__file__),
            "../db/migrations/017_agent_payout_comprehensive.sql"
        )
        
        with open(migration_path, 'r') as f:
            migration_sql = f.read()
        
        # Split migration into individual statements
        # Remove comments and split by semicolons
        statements = []
        current_statement = []
        in_dollar_quote = False
        dollar_tag = None
        
        for line in migration_sql.split('\n'):
            stripped = line.strip()
            
            # Skip empty lines and comments
            if not stripped or stripped.startswith('--'):
                continue
            
            # Check for dollar-quoted strings (for DO blocks, etc)
            if '$$' in line:
                if not in_dollar_quote:
                    in_dollar_quote = True
                    dollar_tag = line.split('$$')[0].strip()
                else:
                    in_dollar_quote = False
            
            current_statement.append(line)
            
            # End of statement if we hit a semicolon outside of dollar quotes
            if ';' in line and not in_dollar_quote:
                stmt = '\n'.join(current_statement)
                if stmt.strip():
                    statements.append(stmt)
                current_statement = []
        
        # Execute each statement
        logger.info(f"Running migration 017 with {len(statements)} statements...")
        executed_count = 0
        
        for i, statement in enumerate(statements, 1):
            try:
                logger.info(f"Executing statement {i}/{len(statements)}...")
                await database.execute(text(statement))
                executed_count += 1
            except Exception as e:
                # Some statements may fail if already executed (like CREATE TABLE IF NOT EXISTS)
                logger.warning(f"Statement {i} warning: {e}")
                # Continue with next statement
        
        logger.info(f"Migration 017 completed: {executed_count}/{len(statements)} statements executed successfully")
        
        return {
            "success": True,
            "message": f"Migration 017 executed: {executed_count}/{len(statements)} statements",
            "migration": "017_agent_payout_comprehensive.sql",
            "total_statements": len(statements),
            "executed_statements": executed_count
        }
        
    except Exception as e:
        logger.error(f"Migration 017 failed: {e}", exc_info=True)
        return {
            "success": False,
            "error": str(e)
        }

