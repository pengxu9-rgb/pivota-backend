"""
Performance optimization endpoints for debugging and improving API speed
"""
from fastapi import APIRouter, Depends
from db.database import database
from utils.auth import get_current_user, require_admin
import time
import asyncio
from typing import Dict, Any

router = APIRouter(prefix="/admin/performance", tags=["performance"])

@router.get("/test-db-connection")
async def test_db_connection(current_user: dict = Depends(require_admin)):
    """Test database connection speed"""
    results = {}
    
    # Test 1: Simple query
    start = time.time()
    try:
        await database.fetch_one("SELECT 1")
        results["simple_query"] = {
            "status": "success",
            "time_ms": round((time.time() - start) * 1000, 2)
        }
    except Exception as e:
        results["simple_query"] = {
            "status": "error",
            "error": str(e)
        }
    
    # Test 2: Count query
    start = time.time()
    try:
        count = await database.fetch_val("SELECT COUNT(*) FROM merchant_onboarding")
        results["count_query"] = {
            "status": "success",
            "count": count,
            "time_ms": round((time.time() - start) * 1000, 2)
        }
    except Exception as e:
        results["count_query"] = {
            "status": "error",
            "error": str(e)
        }
    
    # Test 3: Complex JOIN query (like product search)
    start = time.time()
    try:
        products = await database.fetch_all("""
            SELECT p.*, m.business_name
            FROM products_cache p
            JOIN merchant_onboarding m ON p.merchant_id = m.merchant_id
            WHERE m.status = 'active'
            LIMIT 10
        """)
        results["join_query"] = {
            "status": "success",
            "rows": len(products),
            "time_ms": round((time.time() - start) * 1000, 2)
        }
    except Exception as e:
        results["join_query"] = {
            "status": "error",
            "error": str(e)
        }
    
    # Test 4: Multiple parallel queries
    start = time.time()
    try:
        tasks = [
            database.fetch_one("SELECT COUNT(*) as c FROM agents"),
            database.fetch_one("SELECT COUNT(*) as c FROM merchant_onboarding"),
            database.fetch_one("SELECT COUNT(*) as c FROM orders"),
        ]
        results_parallel = await asyncio.gather(*tasks)
        results["parallel_queries"] = {
            "status": "success",
            "agents": results_parallel[0]["c"] if results_parallel[0] else 0,
            "merchants": results_parallel[1]["c"] if results_parallel[1] else 0,
            "orders": results_parallel[2]["c"] if results_parallel[2] else 0,
            "time_ms": round((time.time() - start) * 1000, 2)
        }
    except Exception as e:
        results["parallel_queries"] = {
            "status": "error",
            "error": str(e)
        }
    
    return {
        "status": "success",
        "tests": results,
        "recommendations": _get_recommendations(results)
    }

def _get_recommendations(results: Dict[str, Any]) -> list:
    """Generate performance recommendations based on test results"""
    recommendations = []
    
    # Check simple query time
    if results.get("simple_query", {}).get("time_ms", 0) > 100:
        recommendations.append({
            "issue": "High database latency",
            "suggestion": "Consider upgrading Railway plan or moving database closer to server"
        })
    
    # Check JOIN query time
    if results.get("join_query", {}).get("time_ms", 0) > 500:
        recommendations.append({
            "issue": "Slow JOIN queries",
            "suggestion": "Add database indexes on foreign key columns (merchant_id, agent_id)"
        })
    
    # Check parallel query performance
    if results.get("parallel_queries", {}).get("time_ms", 0) > 1000:
        recommendations.append({
            "issue": "Poor connection pooling",
            "suggestion": "Increase database connection pool size"
        })
    
    if not recommendations:
        recommendations.append({
            "status": "good",
            "message": "Database performance is acceptable"
        })
    
    return recommendations

@router.post("/create-indexes")
async def create_indexes(current_user: dict = Depends(require_admin)):
    """Create database indexes to improve query performance"""
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
        "indexes_created": indexes_created,
        "errors": errors,
        "recommendation": "Indexes created. API queries should be faster now."
    }

@router.get("/check-slow-queries")
async def check_slow_queries(current_user: dict = Depends(require_admin)):
    """Check for slow queries in the database"""
    try:
        # PostgreSQL query to find slow queries
        slow_queries = await database.fetch_all("""
            SELECT 
                query,
                calls,
                mean_exec_time as avg_ms,
                max_exec_time as max_ms,
                total_exec_time as total_ms
            FROM pg_stat_statements
            WHERE mean_exec_time > 100
            ORDER BY mean_exec_time DESC
            LIMIT 10
        """)
        
        return {
            "status": "success",
            "slow_queries": [dict(q) for q in slow_queries] if slow_queries else [],
            "note": "Queries with average execution time > 100ms"
        }
    except Exception as e:
        # pg_stat_statements might not be enabled
        return {
            "status": "info",
            "message": "pg_stat_statements extension not available",
            "suggestion": "Enable pg_stat_statements in PostgreSQL to track slow queries"
        }

@router.get("/connection-pool-status")
async def get_connection_pool_status(current_user: dict = Depends(require_admin)):
    """Check database connection pool status"""
    try:
        # Get connection pool info
        pool_info = {
            "min_size": getattr(database, "_pool_min_size", 10),
            "max_size": getattr(database, "_pool_max_size", 20),
            "current_size": getattr(database._pool, "size", "unknown") if hasattr(database, "_pool") else "unknown",
            "free_connections": getattr(database._pool, "freesize", "unknown") if hasattr(database, "_pool") else "unknown",
        }
        
        # Test concurrent connections
        start = time.time()
        tasks = [database.fetch_one("SELECT 1") for _ in range(10)]
        await asyncio.gather(*tasks)
        concurrent_time = round((time.time() - start) * 1000, 2)
        
        return {
            "status": "success",
            "pool_info": pool_info,
            "concurrent_test": {
                "queries": 10,
                "time_ms": concurrent_time,
                "avg_per_query": round(concurrent_time / 10, 2)
            },
            "recommendation": "Increase pool size if avg_per_query > 100ms"
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }
