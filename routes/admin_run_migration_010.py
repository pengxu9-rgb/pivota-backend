"""
Admin route to run migration 010 - Payment Routing & Protocol Support
"""
from fastapi import APIRouter, HTTPException, Depends
from typing import Dict, Any
import os

from db.database import database
from utils.auth import ADMIN_ROLES, get_current_employee

router = APIRouter(prefix="/admin/migrations", tags=["Admin Migrations"])


@router.post("/run/010", response_model=Dict[str, Any])
async def run_migration_010(
    current_user: dict = Depends(get_current_employee)
):
    """
    Run migration 010 to create payment routing and protocol tables for Phase 4
    
    Creates:
    - payment_routes: Routing configuration with PSP priorities
    - payment_attempts: Payment attempt logging with failover tracking
    - protocol_definitions: AP2, ACP, X-402 protocol specifications
    - protocol_events: Protocol-specific event logging
    - psp_performance_metrics: Aggregated PSP performance data
    """
    # Verify admin access
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin access required")
    
    try:
        # Read migration file
        migration_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "db/migrations/010_payment_routing.sql"
        )
        
        with open(migration_path, 'r') as f:
            migration_sql = f.read()
        
        # Split into individual statements, preserving DO $$ blocks
        # DO $$ blocks should not be split by internal semicolons
        statements = []
        current_statement = []
        in_do_block = False
        
        for line in migration_sql.split('\n'):
            stripped = line.strip()
            
            # Skip comment-only lines
            if stripped.startswith('--'):
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
        
        results = []
        
        # Execute each statement
        for i, statement in enumerate(statements):
            try:
                # Skip empty statements
                if not statement or statement.isspace():
                    continue
                
                # Add semicolon back if not already present
                if not statement.rstrip().endswith(';'):
                    statement = statement + ';'
                
                # Execute statement
                await database.execute(statement)
                
                # Identify what was created
                if 'CREATE TABLE' in statement:
                    if 'payment_routes' in statement:
                        results.append("✅ payment_routes table created")
                    elif 'payment_attempts' in statement:
                        results.append("✅ payment_attempts table created")
                    elif 'protocol_definitions' in statement:
                        results.append("✅ protocol_definitions table created")
                    elif 'protocol_events' in statement:
                        results.append("✅ protocol_events table created")
                    elif 'psp_performance_metrics' in statement:
                        results.append("✅ psp_performance_metrics table created")
                elif 'CREATE INDEX' in statement:
                    results.append("✅ Index created")
                elif 'INSERT INTO protocol_definitions' in statement:
                    results.append("✅ Default protocols inserted (AP2, ACP, X-402)")
                elif 'INSERT INTO payment_routes' in statement:
                    results.append("✅ Default routes created for existing agents")
                elif 'INSERT INTO agent_protocols' in statement:
                    results.append("✅ Protocol support added to existing agents")
                elif 'COMMENT ON' in statement:
                    # Skip comment statements in results
                    continue
                
            except Exception as e:
                # Log error but continue
                error_msg = str(e)
                if "already exists" in error_msg:
                    results.append(f"⚠️  Statement {i+1}: Already exists (skipped)")
                else:
                    results.append(f"❌ Statement {i+1}: {error_msg}")
        
        # Verify tables were created
        verification = {}
        
        # Check payment_routes
        routes_count = await database.fetch_one(
            "SELECT COUNT(*) as count FROM payment_routes"
        )
        verification["payment_routes_count"] = dict(routes_count)["count"] if routes_count else 0
        
        # Check protocol_definitions
        protocols = await database.fetch_all(
            "SELECT protocol_name, version FROM protocol_definitions"
        )
        verification["protocols"] = [
            f"{p['protocol_name']} v{p['version']}" for p in protocols
        ]
        
        # Check psp_performance_metrics table exists
        table_check = await database.fetch_one(
            """
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_name = 'psp_performance_metrics'
            )
            """
        )
        verification["psp_metrics_table_exists"] = dict(table_check)["exists"] if table_check else False
        
        return {
            "status": "success",
            "message": "Migration 010 completed successfully",
            "steps": results,
            "verification": verification
        }
        
    except FileNotFoundError:
        raise HTTPException(
            status_code=500,
            detail="Migration file 010_payment_routing.sql not found"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Migration failed: {str(e)}"
        )


@router.delete("/rollback/010", response_model=Dict[str, Any])
async def rollback_migration_010(
    current_user: dict = Depends(get_current_employee)
):
    """
    Rollback migration 010 - removes payment routing and protocol tables
    WARNING: This will delete all payment routing data!
    """
    # Verify admin access
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin access required")
    
    try:
        # Drop tables in reverse order (due to foreign keys)
        tables_to_drop = [
            "protocol_events",
            "payment_attempts", 
            "payment_routes",
            "psp_performance_metrics",
            "protocol_definitions"
        ]
        
        results = []
        
        for table in tables_to_drop:
            try:
                await database.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
                results.append(f"✅ Dropped table: {table}")
            except Exception as e:
                results.append(f"❌ Failed to drop {table}: {str(e)}")
        
        # Remove Phase 4 protocols from agent_protocols
        try:
            await database.execute(
                """
                DELETE FROM agent_protocols 
                WHERE protocol_name IN ('AP2', 'ACP', 'X-402')
                """
            )
            results.append("✅ Removed Phase 4 protocols from agents")
        except Exception as e:
            results.append(f"❌ Failed to clean agent_protocols: {str(e)}")
        
        return {
            "status": "success",
            "message": "Migration 010 rolled back",
            "steps": results
        }
        
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Rollback failed: {str(e)}"
        )

"""
from fastapi import APIRouter, HTTPException, Depends
from typing import Dict, Any
import os

from db.database import database
# NOTE: this module's whole body appears TWICE; `router` is rebound here, so
# main.py mounts THIS copy's routes and the ones above are dead. ADMIN_ROLES
# is therefore imported on both sides -- the live guards below use it, and
# relying on the dead copy's import to bind it would turn the obvious
# de-duplication cleanup into a NameError on every admin route here.
from utils.auth import ADMIN_ROLES, get_current_employee

router = APIRouter(prefix="/admin/migrations", tags=["Admin Migrations"])


@router.post("/run/010", response_model=Dict[str, Any])
async def run_migration_010(
    current_user: dict = Depends(get_current_employee)
):
    """
