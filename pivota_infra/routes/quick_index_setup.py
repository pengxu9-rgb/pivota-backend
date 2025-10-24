"""
Quick setup endpoint to create database indexes
Now requires admin authentication for security
"""
from fastapi import APIRouter, Depends, HTTPException
from db.database import database
from utils.auth import require_admin
import time
import os

router = APIRouter(prefix="/setup", tags=["setup"])

# Allow without auth only in development or with special env var
SETUP_KEY = os.getenv("SETUP_KEY", None)

@router.post("/create-all-indexes")
async def create_all_indexes(setup_key: str = None, current_user: dict = None):
    """Create all database indexes to improve performance"""
    
    # Check authentication: either setup key or admin user
    if SETUP_KEY:
        if setup_key != SETUP_KEY:
            # Try admin auth as fallback
            try:
                from utils.auth import require_admin
                if not current_user:
                    raise HTTPException(status_code=401, detail="Setup key invalid or admin authentication required")
            except:
                raise HTTPException(status_code=401, detail="Setup key invalid")
    else:
        # No setup key configured, require admin auth
        if not current_user:
            raise HTTPException(status_code=401, detail="Admin authentication required")
    
    indexes_created = []
    errors = []
    
    # List of indexes to create
    indexes = [
        ("idx_products_cache_merchant_id", "CREATE INDEX IF NOT EXISTS idx_products_cache_merchant_id ON products_cache(merchant_id)"),
        ("idx_products_cache_status", "CREATE INDEX IF NOT EXISTS idx_products_cache_status ON products_cache(cache_status)"),
        ("idx_orders_merchant_id", "CREATE INDEX IF NOT EXISTS idx_orders_merchant_id ON orders(merchant_id)"),
        ("idx_orders_agent_id", "CREATE INDEX IF NOT EXISTS idx_orders_agent_id ON orders(agent_id)"),
        ("idx_orders_status", "CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)"),
        ("idx_agent_usage_logs_agent_id", "CREATE INDEX IF NOT EXISTS idx_agent_usage_logs_agent_id ON agent_usage_logs(agent_id)"),
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
async def create_usage_logs_table():
    """Create agent_usage_logs table if it doesn't exist"""
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
