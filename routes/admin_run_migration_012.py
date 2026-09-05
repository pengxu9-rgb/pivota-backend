"""
[Phase 5] Admin endpoint to run migrations 012a and 012b
"""

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import List, Dict, Any
import os
import logging

from db.database import database
from utils.auth import ADMIN_ROLES, get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/migrations",
    tags=["[Phase 5] Admin Migrations"]
)


class MigrationResponse(BaseModel):
    status: str
    message: str
    steps: List[str]
    verification: Dict[str, Any]


@router.post("/run-012a-agent-revenue", response_model=MigrationResponse)
async def run_migration_012a(current_user: dict = Depends(get_current_user)):
    """
    [Phase 5] Execute migration 012a: Agent Revenue Schema
    
    Creates:
    - agent_revenue_policies table
    - agent_revenue_logs table
    - agent_revenue_summary view
    - Default policies for existing agents
    """
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin access required")
    
    steps = []
    
    try:
        migration_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "db/migrations/012a_agent_revenue.sql"
        )
        
        with open(migration_path, 'r') as f:
            migration_sql = f.read()
        
        steps.append("✅ Migration file loaded")
        
        # Parse and execute statements
        statements = []
        current_statement = []
        in_do_block = False
        
        for line in migration_sql.split('\n'):
            stripped = line.strip()
            if not stripped or stripped.startswith('--'):
                continue
            
            if 'DO $$' in line:
                in_do_block = True
            elif in_do_block and '$$;' in line:
                current_statement.append(line)
                statements.append('\n'.join(current_statement))
                current_statement = []
                in_do_block = False
                continue
            
            if line.strip():
                current_statement.append(line)
            
            if ';' in line and not in_do_block and not stripped.startswith('--'):
                if current_statement:
                    statements.append('\n'.join(current_statement))
                    current_statement = []
        
        if current_statement:
            statements.append('\n'.join(current_statement))
        
        # Execute statements
        for statement in statements:
            clean = statement.strip()
            if not clean:
                continue
                
            try:
                await database.execute(clean)
                
                if 'CREATE TABLE' in clean:
                    if 'agent_revenue_policies' in clean:
                        steps.append("✅ agent_revenue_policies table created")
                    elif 'agent_revenue_logs' in clean:
                        steps.append("✅ agent_revenue_logs table created")
                elif 'CREATE INDEX' in clean:
                    steps.append("✅ Index created")
                elif 'CREATE OR REPLACE VIEW' in clean:
                    steps.append("✅ agent_revenue_summary view created")
                elif 'INSERT INTO agent_revenue_policies' in clean:
                    steps.append("✅ Default revenue policies created")
                elif 'DO $$' in clean:
                    steps.append("✅ Verification completed")
                    
            except Exception as e:
                steps.append(f"❌ Error: {str(e)}")
                logger.error(f"[Phase 5] Migration 012a error: {e}")
        
        # Verification
        verification = {}
        
        tables = await database.fetch_all(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_name IN ('agent_revenue_policies', 'agent_revenue_logs')
            """
        )
        verification["tables_created"] = [t["table_name"] for t in tables]
        
        policy_count = await database.fetch_one(
            "SELECT COUNT(*) as count FROM agent_revenue_policies"
        )
        verification["revenue_policies_count"] = policy_count["count"]
        
        return MigrationResponse(
            status="success",
            message="Migration 012a completed successfully",
            steps=steps,
            verification=verification
        )
        
    except Exception as e:
        logger.error(f"[Phase 5] Migration 012a failed: {e}")
        return MigrationResponse(
            status="error",
            message=f"Migration failed: {str(e)}",
            steps=steps,
            verification={}
        )


@router.post("/run-012b-routing-extensions", response_model=MigrationResponse)
async def run_migration_012b(current_user: dict = Depends(get_current_user)):
    """
    [Phase 5] Execute migration 012b: Routing Extensions
    
    Adds columns:
    - routing_logs.resolved_by
    - routing_logs.revenue_calculated
    - agents.revenue_sharing_enabled
    """
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin access required")
    
    steps = []
    
    try:
        migration_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "db/migrations/012b_routing_extensions.sql"
        )
        
        with open(migration_path, 'r') as f:
            migration_sql = f.read()
        
        steps.append("✅ Migration file loaded")
        
        # Execute (same parsing logic)
        statements = []
        current_statement = []
        in_do_block = False
        
        for line in migration_sql.split('\n'):
            stripped = line.strip()
            if not stripped or stripped.startswith('--'):
                continue
            
            if 'DO $$' in line or 'CREATE OR REPLACE FUNCTION' in line or 'CREATE TRIGGER' in line:
                in_do_block = True
            elif in_do_block and ('$$;' in line or 'END;' in line or ';' in line and 'TRIGGER' in current_statement[0] if current_statement else False):
                current_statement.append(line)
                statements.append('\n'.join(current_statement))
                current_statement = []
                in_do_block = False
                continue
            
            if line.strip():
                current_statement.append(line)
            
            if ';' in line and not in_do_block:
                if current_statement:
                    statements.append('\n'.join(current_statement))
                    current_statement = []
        
        for statement in statements:
            clean = statement.strip()
            if not clean:
                continue
                
            try:
                await database.execute(clean)
                
                if 'ALTER TABLE routing_logs' in clean and 'resolved_by' in clean:
                    steps.append("✅ routing_logs.resolved_by column added")
                elif 'ALTER TABLE routing_logs' in clean and 'revenue_calculated' in clean:
                    steps.append("✅ routing_logs.revenue_calculated column added")
                elif 'ALTER TABLE agents' in clean:
                    steps.append("✅ agents.revenue_sharing_enabled column added")
                elif 'CREATE INDEX' in clean:
                    steps.append("✅ Index created")
                elif 'CREATE OR REPLACE FUNCTION' in clean:
                    steps.append("✅ Trigger function created")
                elif 'CREATE TRIGGER' in clean:
                    steps.append("✅ Auto-resolve trigger created")
                elif 'DO $$' in clean:
                    steps.append("✅ Verification completed")
                    
            except Exception as e:
                steps.append(f"❌ Error: {str(e)}")
        
        # Verification
        verification = {}
        
        columns = await database.fetch_all(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'routing_logs' AND column_name IN ('resolved_by', 'revenue_calculated')
            """
        )
        verification["routing_logs_columns"] = [c["column_name"] for c in columns]
        
        agent_col = await database.fetch_one(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'agents' AND column_name = 'revenue_sharing_enabled'
            ) as exists
            """
        )
        verification["agent_revenue_column_exists"] = agent_col["exists"]
        
        return MigrationResponse(
            status="success",
            message="Migration 012b completed successfully",
            steps=steps,
            verification=verification
        )
        
    except Exception as e:
        return MigrationResponse(
            status="error",
            message=f"Migration failed: {str(e)}",
            steps=steps,
            verification={}
        )


print("[Phase 5] Admin migration 012 routes initialized")
