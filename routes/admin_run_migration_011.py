"""
[Phase 4++] Admin endpoint to run migration 011
Run Phase 4++ database migration via API
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
    tags=["[Phase 4++] Admin Migrations"]
)


class MigrationResponse(BaseModel):
    status: str
    message: str
    steps: List[str]
    verification: Dict[str, Any]


@router.post("/run-011-dual-routing", response_model=MigrationResponse)
async def run_migration_011(current_user: dict = Depends(get_current_user)):
    """
    [Phase 4++] Execute migration 011: Dual-side routing and AP2 adapter
    
    This migration adds:
    - routing_policies table for merchant/agent rules
    - routing_logs table for decision tracking
    - ap2_transactions table for AP2 protocol logging
    - routing_override_enabled column to agents table
    - detect_routing_conflicts function
    - routing_conflict_summary view
    """
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin access required")
    
    steps = []
    
    try:
        # Read migration file
        migration_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "db/migrations/011_dual_routing.sql"
        )
        
        with open(migration_path, 'r') as f:
            migration_sql = f.read()
        
        steps.append("✅ Migration file loaded")
        
        # Split into individual statements, preserving DO $$ blocks
        statements = []
        current_statement = []
        in_do_block = False
        
        for line in migration_sql.split('\n'):
            stripped = line.strip()
            
            # Skip empty lines and comments
            if not stripped or stripped.startswith('--'):
                continue
            
            # Track DO $$ blocks
            if 'DO $$' in line or 'DO $' in line:
                in_do_block = True
            elif in_do_block and ('END $$' in line or '$$;' in line):
                current_statement.append(line)
                statements.append('\n'.join(current_statement))
                current_statement = []
                in_do_block = False
                continue
            
            # Add line to current statement
            if line.strip():
                current_statement.append(line)
            
            # End of statement (semicolon outside DO block)
            if ';' in line and not in_do_block and not stripped.startswith('--'):
                if current_statement:
                    statements.append('\n'.join(current_statement))
                    current_statement = []
        
        # Add any remaining statement
        if current_statement:
            statements.append('\n'.join(current_statement))
        
        # Execute each statement
        for i, statement in enumerate(statements):
            try:
                # Clean up the statement
                clean_statement = statement.strip()
                if not clean_statement:
                    continue
                
                await database.execute(clean_statement)
                
                # Log specific operations
                if 'CREATE TABLE' in clean_statement:
                    if 'routing_policies' in clean_statement:
                        steps.append("✅ routing_policies table created")
                    elif 'routing_logs' in clean_statement:
                        steps.append("✅ routing_logs table created")
                    elif 'ap2_transactions' in clean_statement:
                        steps.append("✅ ap2_transactions table created")
                elif 'CREATE INDEX' in clean_statement:
                    steps.append("✅ Index created")
                elif 'ALTER TABLE agents' in clean_statement:
                    steps.append("✅ Added routing_override_enabled column to agents")
                elif 'CREATE OR REPLACE FUNCTION' in clean_statement:
                    steps.append("✅ detect_routing_conflicts function created")
                elif 'CREATE OR REPLACE VIEW' in clean_statement:
                    steps.append("✅ routing_conflict_summary view created")
                elif 'INSERT INTO routing_policies' in clean_statement:
                    steps.append("✅ Default routing policies created for existing agents")
                elif 'DO $$' in clean_statement:
                    steps.append("✅ Migration verification completed")
                    
            except Exception as e:
                steps.append(f"❌ Statement {i+1}: {str(e)}")
                logger.error(f"[Phase 4++] Failed to execute statement {i+1}: {e}")
        
        # Verify migration
        verification = {}
        
        # Check tables exist
        tables = await database.fetch_all(
            """
            SELECT table_name 
            FROM information_schema.tables 
            WHERE table_schema = 'public' 
            AND table_name IN ('routing_policies', 'routing_logs', 'ap2_transactions')
            """
        )
        verification["tables_created"] = [t["table_name"] for t in tables]
        
        # Check routing policies count
        policy_count = await database.fetch_one(
            "SELECT COUNT(*) as count FROM routing_policies"
        )
        verification["routing_policies_count"] = policy_count["count"]
        
        # Check if function exists
        function_exists = await database.fetch_one(
            """
            SELECT EXISTS (
                SELECT 1 FROM pg_proc 
                WHERE proname = 'detect_routing_conflicts'
            ) as exists
            """
        )
        verification["conflict_function_exists"] = function_exists["exists"]
        
        # Check if view exists
        view_exists = await database.fetch_one(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.views
                WHERE table_name = 'routing_conflict_summary'
            ) as exists
            """
        )
        verification["conflict_view_exists"] = view_exists["exists"]
        
        # Check if column was added
        column_exists = await database.fetch_one(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'agents'
                AND column_name = 'routing_override_enabled'
            ) as exists
            """
        )
        verification["agent_override_column_exists"] = column_exists["exists"]
        
        return MigrationResponse(
            status="success",
            message="Migration 011 completed successfully",
            steps=steps,
            verification=verification
        )
        
    except Exception as e:
        logger.error(f"[Phase 4++] Migration 011 failed: {e}")
        return MigrationResponse(
            status="error",
            message=f"Migration failed: {str(e)}",
            steps=steps,
            verification={}
        )


# [Phase 4++] Admin migration route initialized
print("[Phase 4++] Admin migration 011 route initialized")
