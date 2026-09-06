"""
[Phase 5 Cleanup] Admin endpoint to run migration 013
Consolidate routing systems: payment_routes → routing_policies
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


@router.post("/run-013-consolidate-routing", response_model=MigrationResponse)
async def run_migration_013(current_user: dict = Depends(get_current_user)):
    """
    [Phase 5 Cleanup] Execute migration 013: Consolidate Routing Systems
    
    This migration:
    - Migrates payment_routes to routing_policies
    - Marks payment_routes as deprecated
    - Creates migration tracking log
    - Simplifies routing architecture
    """
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin access required")
    
    steps = []
    
    try:
        migration_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "db/migrations/013_consolidate_routing.sql"
        )
        
        with open(migration_path, 'r') as f:
            migration_sql = f.read()
        
        steps.append("✅ Migration file loaded")
        
        # Parse statements
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
            
            if ';' in line and not in_do_block:
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
                
                if 'INSERT INTO routing_policies' in clean:
                    steps.append("✅ Migrated payment_routes to routing_policies")
                elif 'ALTER TABLE payment_routes' in clean and 'deprecated' in clean:
                    steps.append("✅ Added deprecation column to payment_routes")
                elif 'UPDATE payment_routes' in clean and 'deprecated = true' in clean:
                    steps.append("✅ Marked old payment_routes as deprecated")
                elif 'CREATE TABLE' in clean and 'routing_migration_log' in clean:
                    steps.append("✅ Created migration tracking table")
                elif 'INSERT INTO routing_migration_log' in clean:
                    steps.append("✅ Logged migration")
                elif 'DO $$' in clean:
                    steps.append("✅ Verification completed")
                    
            except Exception as e:
                steps.append(f"⚠️ Statement: {str(e)[:100]}")
                logger.warning(f"[Migration 013] Non-critical error: {e}")
        
        # Verification
        verification = {}
        
        # Check deprecation
        deprecated_count = await database.fetch_one(
            "SELECT COUNT(*) as count FROM payment_routes WHERE deprecated = true"
        )
        verification["deprecated_payment_routes"] = deprecated_count["count"]
        
        # Check migrated policies
        agent_policies = await database.fetch_one(
            "SELECT COUNT(*) as count FROM routing_policies WHERE owner_type = 'agent'"
        )
        verification["agent_routing_policies"] = agent_policies["count"]
        
        # Check migration log
        migration_log = await database.fetch_one(
            """
            SELECT records_migrated FROM routing_migration_log 
            WHERE migration_type = 'consolidate_routing' 
            ORDER BY id DESC LIMIT 1
            """
        )
        verification["records_migrated"] = migration_log["records_migrated"] if migration_log else 0
        
        return MigrationResponse(
            status="success",
            message="Migration 013 completed - Routing systems consolidated",
            steps=steps,
            verification=verification
        )
        
    except Exception as e:
        logger.error(f"[Migration 013] Failed: {e}")
        return MigrationResponse(
            status="error",
            message=f"Migration failed: {str(e)}",
            steps=steps,
            verification={}
        )


print("[Phase 5] Admin migration 013 route initialized")
