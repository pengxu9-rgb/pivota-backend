"""
Quick setup endpoints that create database indexes and tables.

BOTH ROUTES EXECUTE DDL AND BOTH REQUIRE ADMIN AUTHENTICATION.

The previous version claimed in this very docstring that it "now requires admin
authentication", and did not. `create_all_indexes` declared its user parameter
with no dependency attached, so FastAPI bound it as an ordinary request-BODY
field: the caller supplied it, any truthy value satisfied the check, and
`require_admin` was imported twice and never called. `create_usage_logs_table`
had no parameters and no check at all. Both were reachable unauthenticated from
the public internet and ran DDL against production.

The guard is now a real dependency, so it resolves before the handler and cannot
be satisfied by request content. The optional env-var setup key is gone too: a
route that already demands an authenticated admin does not need a second, weaker
credential, and it was unset in production regardless.

NOTE: neither route has a caller anywhere in this repo. Schema changes belong in
db/migrations/, which is how every other index here is created. These are kept
only because removing a mounted route is a product decision; if nothing calls
them by the next cleanup pass, delete the module.
"""
from fastapi import APIRouter, Depends, HTTPException
from db.database import database
from utils.auth import require_admin
import time

router = APIRouter(prefix="/setup", tags=["setup"])


@router.post("/create-all-indexes")
async def create_all_indexes(current_admin: dict = Depends(require_admin)):
    """Create all database indexes to improve performance. Admin only."""

    indexes_created = []
    errors = []
    
    # List of indexes to create
    indexes = [
        ("idx_products_cache_merchant_id", "CREATE INDEX IF NOT EXISTS idx_products_cache_merchant_id ON products_cache(merchant_id)"),
        ("idx_products_cache_status", "CREATE INDEX IF NOT EXISTS idx_products_cache_status ON products_cache(cache_status)"),
        ("idx_orders_merchant_id", "CREATE INDEX IF NOT EXISTS idx_orders_merchant_id ON orders(merchant_id)"),
        ("idx_orders_merchant_created_at", "CREATE INDEX IF NOT EXISTS idx_orders_merchant_created_at ON orders(merchant_id, created_at DESC)"),
        ("idx_orders_agent_id", "CREATE INDEX IF NOT EXISTS idx_orders_agent_id ON orders(agent_id)"),
        ("idx_orders_agent_created_at", "CREATE INDEX IF NOT EXISTS idx_orders_agent_created_at ON orders(agent_id, created_at DESC)"),
        ("idx_orders_status", "CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)"),
        ("idx_agent_usage_logs_agent_id", "CREATE INDEX IF NOT EXISTS idx_agent_usage_logs_agent_id ON agent_usage_logs(agent_id)"),
        ("idx_agent_usage_logs_agent_timestamp", "CREATE INDEX IF NOT EXISTS idx_agent_usage_logs_agent_timestamp ON agent_usage_logs(agent_id, timestamp DESC)"),
        ("idx_agent_usage_logs_timestamp", "CREATE INDEX IF NOT EXISTS idx_agent_usage_logs_timestamp ON agent_usage_logs(timestamp DESC)"),
        ("idx_merchant_onboarding_status", "CREATE INDEX IF NOT EXISTS idx_merchant_onboarding_status ON merchant_onboarding(status)"),
        ("idx_agents_api_key", "CREATE INDEX IF NOT EXISTS idx_agents_api_key ON agents(api_key)"),
    ]
    
    for index_name, index_sql in indexes:
        try:
            start = time.time()
            await database.execute(index_sql)
            elapsed = round((time.time() - start) * 1000, 2)
            indexes_created.append({
                "index": index_name,
                "status": "created",
                "time_ms": elapsed
            })
        except Exception as e:
            errors.append({
                "index": index_name,
                "error": str(e)
            })
    
    return {
        "status": "success",
        "indexes_created": len(indexes_created),
        "indexes": indexes_created,
        "errors": errors,
        "message": f"Created {len(indexes_created)} indexes. API queries should be faster now!"
    }

@router.post("/create-usage-logs-table")
async def create_usage_logs_table(current_admin: dict = Depends(require_admin)):
    """Create agent_usage_logs table if it doesn't exist. Admin only."""
    try:
        await database.execute("""
            CREATE TABLE IF NOT EXISTS agent_usage_logs (
                id SERIAL PRIMARY KEY,
                agent_id VARCHAR(255) NOT NULL,
                endpoint VARCHAR(500) NOT NULL,
                method VARCHAR(10),
                status_code INTEGER,
                response_time_ms INTEGER,
                timestamp TIMESTAMP DEFAULT NOW(),
                request_id VARCHAR(100)
            )
        """)
        
        # Create indexes
        await database.execute("""
            CREATE INDEX IF NOT EXISTS idx_agent_usage_logs_agent_id 
            ON agent_usage_logs(agent_id)
        """)

        await database.execute("""
            CREATE INDEX IF NOT EXISTS idx_agent_usage_logs_agent_timestamp
            ON agent_usage_logs(agent_id, timestamp DESC)
        """)
        
        await database.execute("""
            CREATE INDEX IF NOT EXISTS idx_agent_usage_logs_timestamp 
            ON agent_usage_logs(timestamp DESC)
        """)
        
        return {
            "status": "success",
            "message": "agent_usage_logs table and indexes created successfully"
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }
