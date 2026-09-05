"""
Admin Migration Runner
Allows running database migrations via API endpoint
"""
import logging
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException
from utils.auth import require_admin_or_key
from db.database import database

# AUTHENTICATION. Every route on this router was reachable with NO credentials
# of any kind: no Depends, no header check, no role check. The guard is applied
# at the ROUTER, not per-handler, so a route added here later inherits it
# instead of having to remember it -- which is how this file got here.
# require_admin_or_key accepts an X-ADMIN-KEY header or an admin/super_admin
# JWT and fails closed (401) when neither is present.
#
# POST /admin/migrations/run/006-psp-constraints ran DDL anonymously.
router = APIRouter(prefix="/admin/migrations", tags=["Admin Migrations"], dependencies=[Depends(require_admin_or_key)])
logger = logging.getLogger(__name__)

@router.post("/run/006-psp-constraints")
async def run_migration_006_psp_constraints(dry_run: bool = True):
    """
    Run migration 006: PSP Fields Constraints and Indexes
    
    Args:
        dry_run: If True, only validate and preview changes without applying
    
    This migration:
    1. Normalizes psp_used to lowercase
    2. Fills missing psp_id/psp_used
    3. Adds CHECK constraints
    4. Creates performance indexes
    5. Creates monitoring views
    """
    try:
        results = {
            "migration": "006_psp_fields_constraints",
            "dry_run": dry_run,
            "steps": []
        }
        
        # Step 1: Data cleanup - normalize case
        if dry_run:
            case_preview = await database.fetch_one("""
                SELECT COUNT(*) as count
                FROM orders
                WHERE psp_used IS NOT NULL AND psp_used != LOWER(psp_used)
            """)
            results["steps"].append({
                "step": "1_normalize_case",
                "action": "preview",
                "would_update": case_preview["count"],
                "query": "UPDATE orders SET psp_used = LOWER(psp_used)"
            })
        else:
            case_result = await database.execute("""
                UPDATE orders
                SET psp_used = LOWER(psp_used)
                WHERE psp_used IS NOT NULL AND psp_used != LOWER(psp_used)
            """)
            results["steps"].append({
                "step": "1_normalize_case",
                "action": "executed",
                "updated": case_result
            })
        
        # Step 2: Fill missing psp_id
        if dry_run:
            fill_id_preview = await database.fetch_one("""
                SELECT COUNT(*) as count
                FROM orders o
                JOIN merchant_psps mp ON o.merchant_id = mp.merchant_id
                    AND LOWER(o.psp_used) = LOWER(mp.provider)
                    AND mp.status = 'active'
                WHERE o.psp_id IS NULL
            """)
            results["steps"].append({
                "step": "2_fill_psp_id",
                "action": "preview",
                "would_update": fill_id_preview["count"],
                "query": "UPDATE orders SET psp_id = mp.psp_id FROM merchant_psps mp"
            })
        else:
            fill_id_result = await database.execute("""
                UPDATE orders o
                SET psp_id = mp.psp_id
                FROM merchant_psps mp
                WHERE o.merchant_id = mp.merchant_id
                    AND o.psp_id IS NULL
                    AND o.psp_used IS NOT NULL
                    AND LOWER(o.psp_used) = LOWER(mp.provider)
                    AND mp.status = 'active'
            """)
            results["steps"].append({
                "step": "2_fill_psp_id",
                "action": "executed",
                "updated": fill_id_result
            })
        
        # Step 3: Fill missing psp_used
        if dry_run:
            fill_used_preview = await database.fetch_one("""
                SELECT COUNT(*) as count
                FROM orders o
                JOIN merchant_psps mp ON o.psp_id = mp.psp_id
                WHERE o.psp_used IS NULL
            """)
            results["steps"].append({
                "step": "3_fill_psp_used",
                "action": "preview",
                "would_update": fill_used_preview["count"],
                "query": "UPDATE orders SET psp_used = LOWER(mp.provider) FROM merchant_psps mp"
            })
        else:
            fill_used_result = await database.execute("""
                UPDATE orders o
                SET psp_used = LOWER(mp.provider)
                FROM merchant_psps mp
                WHERE o.psp_id = mp.psp_id
                    AND o.psp_used IS NULL
            """)
            results["steps"].append({
                "step": "3_fill_psp_used",
                "action": "executed",
                "updated": fill_used_result
            })
        
        # Step 4: Add constraints (only if not dry_run)
        if not dry_run:
            try:
                # Check lowercase constraint
                await database.execute("""
                    ALTER TABLE orders 
                        DROP CONSTRAINT IF EXISTS check_psp_used_lowercase
                """)
                await database.execute("""
                    ALTER TABLE orders 
                        ADD CONSTRAINT check_psp_used_lowercase 
                        CHECK (psp_used IS NULL OR psp_used = LOWER(psp_used))
                """)
                results["steps"].append({
                    "step": "4a_add_lowercase_constraint",
                    "action": "executed",
                    "status": "success"
                })
            except Exception as e:
                results["steps"].append({
                    "step": "4a_add_lowercase_constraint",
                    "action": "failed",
                    "error": str(e)
                })
            
            try:
                # Valid provider constraint
                await database.execute("""
                    ALTER TABLE orders 
                        DROP CONSTRAINT IF EXISTS check_psp_used_valid_provider
                """)
                await database.execute("""
                    ALTER TABLE orders 
                        ADD CONSTRAINT check_psp_used_valid_provider 
                        CHECK (psp_used IS NULL OR psp_used IN ('stripe', 'adyen', 'checkout', 'paypal', 'braintree'))
                """)
                results["steps"].append({
                    "step": "4b_add_valid_provider_constraint",
                    "action": "executed",
                    "status": "success"
                })
            except Exception as e:
                results["steps"].append({
                    "step": "4b_add_valid_provider_constraint",
                    "action": "failed",
                    "error": str(e)
                })
        else:
            results["steps"].append({
                "step": "4_add_constraints",
                "action": "skipped",
                "reason": "dry_run mode"
            })
        
        # Step 5: Create indexes (only if not dry_run)
        if not dry_run:
            indexes = [
                ("idx_orders_psp_used", "CREATE INDEX IF NOT EXISTS idx_orders_psp_used ON orders(psp_used)"),
                ("idx_orders_psp_id", "CREATE INDEX IF NOT EXISTS idx_orders_psp_id ON orders(psp_id)"),
                ("idx_orders_merchant_psp_id", "CREATE INDEX IF NOT EXISTS idx_orders_merchant_psp_id ON orders(merchant_id, psp_id)"),
                ("idx_orders_merchant_psp_used", "CREATE INDEX IF NOT EXISTS idx_orders_merchant_psp_used ON orders(merchant_id, psp_used)"),
                ("idx_orders_psp_created_at", "CREATE INDEX IF NOT EXISTS idx_orders_psp_created_at ON orders(psp_id, created_at DESC)"),
                ("idx_orders_psp_payment_status", "CREATE INDEX IF NOT EXISTS idx_orders_psp_payment_status ON orders(psp_used, payment_status)")
            ]
            
            for idx_name, idx_sql in indexes:
                try:
                    await database.execute(idx_sql)
                    results["steps"].append({
                        "step": f"5_create_index_{idx_name}",
                        "action": "executed",
                        "status": "success"
                    })
                except Exception as e:
                    results["steps"].append({
                        "step": f"5_create_index_{idx_name}",
                        "action": "failed",
                        "error": str(e)
                    })
        else:
            results["steps"].append({
                "step": "5_create_indexes",
                "action": "skipped",
                "reason": "dry_run mode",
                "indexes": [
                    "idx_orders_psp_used",
                    "idx_orders_psp_id",
                    "idx_orders_merchant_psp_id",
                    "idx_orders_merchant_psp_used",
                    "idx_orders_psp_created_at",
                    "idx_orders_psp_payment_status"
                ]
            })
        
        # Step 6: Create views (only if not dry_run)
        if not dry_run:
            try:
                await database.execute("""
                    CREATE OR REPLACE VIEW psp_data_quality AS
                    SELECT 
                        COUNT(*) as total_orders,
                        COUNT(CASE WHEN psp_used IS NULL THEN 1 END) as null_psp_used,
                        COUNT(CASE WHEN psp_id IS NULL THEN 1 END) as null_psp_id,
                        COUNT(CASE WHEN psp_used IS NULL OR psp_id IS NULL THEN 1 END) as incomplete_orders,
                        COUNT(CASE WHEN psp_used IS NOT NULL AND psp_id IS NOT NULL THEN 1 END) as complete_orders,
                        ROUND(100.0 * COUNT(CASE WHEN psp_used IS NOT NULL AND psp_id IS NOT NULL THEN 1 END) / NULLIF(COUNT(*), 0), 2) as completion_rate
                    FROM orders
                """)
                results["steps"].append({
                    "step": "6a_create_quality_view",
                    "action": "executed",
                    "status": "success"
                })
            except Exception as e:
                results["steps"].append({
                    "step": "6a_create_quality_view",
                    "action": "failed",
                    "error": str(e)
                })
            
            try:
                await database.execute("""
                    CREATE OR REPLACE VIEW psp_usage_stats AS
                    SELECT 
                        LOWER(psp_used) as psp_provider,
                        COUNT(*) as order_count,
                        COUNT(DISTINCT merchant_id) as merchant_count,
                        COUNT(CASE WHEN payment_status = 'paid' THEN 1 END) as successful_orders,
                        COALESCE(SUM(CASE WHEN payment_status = 'paid' THEN total ELSE 0 END), 0) as total_volume,
                        ROUND(100.0 * COUNT(CASE WHEN payment_status = 'paid' THEN 1 END) / NULLIF(COUNT(*), 0), 2) as success_rate
                    FROM orders
                    WHERE psp_used IS NOT NULL
                    GROUP BY LOWER(psp_used)
                    ORDER BY order_count DESC
                """)
                results["steps"].append({
                    "step": "6b_create_usage_view",
                    "action": "executed",
                    "status": "success"
                })
            except Exception as e:
                results["steps"].append({
                    "step": "6b_create_usage_view",
                    "action": "failed",
                    "error": str(e)
                })
        else:
            results["steps"].append({
                "step": "6_create_views",
                "action": "skipped",
                "reason": "dry_run mode",
                "views": ["psp_data_quality", "psp_usage_stats"]
            })
        
        # Summary
        total_steps = len(results["steps"])
        executed = len([s for s in results["steps"] if s.get("action") == "executed"])
        failed = len([s for s in results["steps"] if s.get("action") == "failed"])
        
        results["summary"] = {
            "total_steps": total_steps,
            "executed": executed,
            "failed": failed,
            "status": "completed" if failed == 0 else "completed_with_errors" if executed > 0 else "failed"
        }
        
        return results
        
    except Exception as e:
        logger.error(f"Migration failed: {e}")
        raise HTTPException(status_code=500, detail=f"Migration failed: {str(e)}")


@router.get("/status/006-psp-constraints")
async def check_migration_006_status():
    """
    Check if migration 006 has been applied
    """
    try:
        status = {
            "migration": "006_psp_fields_constraints",
            "checks": {}
        }
        
        # Check if constraints exist
        constraints = await database.fetch_all("""
            SELECT constraint_name 
            FROM information_schema.table_constraints 
            WHERE table_name = 'orders' 
                AND constraint_name IN ('check_psp_used_lowercase', 'check_psp_used_valid_provider')
        """)
        status["checks"]["constraints"] = {
            "found": [c["constraint_name"] for c in constraints],
            "expected": ["check_psp_used_lowercase", "check_psp_used_valid_provider"],
            "applied": len(constraints) >= 2
        }
        
        # Check if indexes exist
        indexes = await database.fetch_all("""
            SELECT indexname 
            FROM pg_indexes 
            WHERE tablename = 'orders' 
                AND indexname LIKE 'idx_orders_psp%'
        """)
        status["checks"]["indexes"] = {
            "found": [i["indexname"] for i in indexes],
            "count": len(indexes),
            "applied": len(indexes) >= 4
        }
        
        # Check if views exist
        views = await database.fetch_all("""
            SELECT table_name 
            FROM information_schema.views 
            WHERE table_name IN ('psp_data_quality', 'psp_usage_stats')
        """)
        status["checks"]["views"] = {
            "found": [v["table_name"] for v in views],
            "expected": ["psp_data_quality", "psp_usage_stats"],
            "applied": len(views) >= 2
        }
        
        # Check data quality
        quality = await database.fetch_one("""
            SELECT 
                COUNT(*) as total,
                COUNT(CASE WHEN psp_used IS NULL OR psp_id IS NULL THEN 1 END) as incomplete
            FROM orders
        """)
        status["checks"]["data_quality"] = {
            "total_orders": quality["total"],
            "incomplete_orders": quality["incomplete"],
            "health": "good" if quality["incomplete"] == 0 else "needs_attention"
        }
        
        # Overall status
        all_applied = (
            status["checks"]["constraints"]["applied"] and
            status["checks"]["indexes"]["applied"] and
            status["checks"]["views"]["applied"]
        )
        status["status"] = "applied" if all_applied else "not_applied"
        status["recommendation"] = (
            "Migration already applied" if all_applied 
            else "Run: POST /admin/migrations/run/006-psp-constraints?dry_run=false"
        )
        
        return status
        
    except Exception as e:
        logger.error(f"Status check failed: {e}")
        raise HTTPException(status_code=500, detail=f"Status check failed: {str(e)}")


