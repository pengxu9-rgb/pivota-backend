"""
Admin route to run migration 010 - Payment Routing & Protocol Support
"""
from fastapi import APIRouter, HTTPException, Depends
from typing import Dict, Any
import os

from ..database import database
from ..auth import verify_employee_token

router = APIRouter(prefix="/admin/migrations", tags=["Admin Migrations"])


@router.post("/run/010", response_model=Dict[str, Any])
async def run_migration_010(
    token_data: dict = Depends(verify_employee_token)
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
    if token_data.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    
    try:
        # Read migration file
        migration_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "db/migrations/010_payment_routing.sql"
        )
        
        with open(migration_path, 'r') as f:
            migration_sql = f.read()
        
        # Split into individual statements (separated by semicolons)
        statements = [
            stmt.strip() 
            for stmt in migration_sql.split(';') 
            if stmt.strip() and not stmt.strip().startswith('--')
        ]
        
        results = []
        
        # Execute each statement
        for i, statement in enumerate(statements):
            try:
                # Skip empty statements
                if not statement or statement.isspace():
                    continue
                
                # Add semicolon back
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
    token_data: dict = Depends(verify_employee_token)
):
    """
    Rollback migration 010 - removes payment routing and protocol tables
    WARNING: This will delete all payment routing data!
    """
    # Verify admin access
    if token_data.get("role") != "admin":
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
