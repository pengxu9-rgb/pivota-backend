"""
PSP Data Integrity Management
Provides endpoints to check and fix psp_used/psp_id fields
"""
import logging
from datetime import datetime
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from db.database import database
from utils.auth import require_admin

router = APIRouter(prefix="/admin/psp", tags=["PSP Integrity"])
logger = logging.getLogger(__name__)

@router.get("/integrity-check")
async def check_psp_data_integrity(
    current_user: Dict[str, Any] = Depends(require_admin),
):
    """
    Check PSP field data integrity
    Returns detailed report on NULL values, case issues, and mismatches
    """
    try:
        # 1. Check for NULL values
        null_check = await database.fetch_one("""
            SELECT 
                COUNT(*) as total_orders,
                COUNT(CASE WHEN psp_used IS NULL THEN 1 END) as null_psp_used,
                COUNT(CASE WHEN psp_id IS NULL THEN 1 END) as null_psp_id,
                COUNT(CASE WHEN psp_used IS NULL OR psp_id IS NULL THEN 1 END) as incomplete,
                COUNT(CASE WHEN psp_used IS NOT NULL AND psp_id IS NOT NULL THEN 1 END) as complete
            FROM orders
        """)
        
        # 2. Check for case inconsistencies
        case_check = await database.fetch_all("""
            SELECT 
                psp_used,
                LOWER(psp_used) as lowercase_version,
                COUNT(*) as count
            FROM orders
            WHERE psp_used IS NOT NULL 
                AND psp_used != LOWER(psp_used)
            GROUP BY psp_used, LOWER(psp_used)
        """)
        
        # 3. Check for psp_id mismatches (orders with psp_id that doesn't exist in config)
        mismatch_check = await database.fetch_all("""
            SELECT 
                o.order_id,
                o.merchant_id,
                o.psp_used,
                o.psp_id,
                o.created_at
            FROM orders o
            LEFT JOIN merchant_psps mp ON o.psp_id = mp.psp_id
            WHERE o.psp_id IS NOT NULL 
                AND mp.psp_id IS NULL
            ORDER BY o.created_at DESC
            LIMIT 20
        """)
        
        # 4. Check for orders with valid psp_used but missing psp_id
        fixable_orders = await database.fetch_all("""
            SELECT 
                o.order_id,
                o.merchant_id,
                o.psp_used,
                o.psp_id,
                mp.psp_id as correct_psp_id,
                mp.provider
            FROM orders o
            JOIN merchant_psps mp ON o.merchant_id = mp.merchant_id
                AND LOWER(o.psp_used) = LOWER(mp.provider)
                AND mp.status = 'active'
            WHERE o.psp_id IS NULL OR o.psp_id != mp.psp_id
            LIMIT 20
        """)
        
        # 5. PSP provider name distribution
        provider_stats = await database.fetch_all("""
            SELECT 
                LOWER(psp_used) as psp_provider,
                COUNT(*) as order_count,
                COUNT(DISTINCT merchant_id) as merchant_count
            FROM orders
            WHERE psp_used IS NOT NULL
            GROUP BY LOWER(psp_used)
            ORDER BY order_count DESC
        """)
        
        is_healthy = (
            null_check["incomplete"] == 0 
            and len(case_check) == 0 
            and len(mismatch_check) == 0
        )
        
        return {
            "status": "healthy" if is_healthy else "issues_found",
            "checked_at": datetime.utcnow().isoformat(),
            "summary": {
                "total_orders": null_check["total_orders"],
                "complete_orders": null_check["complete"],
                "incomplete_orders": null_check["incomplete"],
                "null_psp_used": null_check["null_psp_used"],
                "null_psp_id": null_check["null_psp_id"],
                "case_issues": len(case_check),
                "psp_id_mismatches": len(mismatch_check),
                "fixable_orders": len(fixable_orders)
            },
            "details": {
                "case_inconsistencies": [
                    {
                        "current": row["psp_used"],
                        "should_be": row["lowercase_version"],
                        "affected_orders": row["count"]
                    }
                    for row in case_check
                ],
                "psp_id_mismatches": [
                    {
                        "order_id": row["order_id"],
                        "merchant_id": row["merchant_id"],
                        "psp_used": row["psp_used"],
                        "invalid_psp_id": row["psp_id"],
                        "created_at": row["created_at"].isoformat() if row["created_at"] else None
                    }
                    for row in mismatch_check
                ],
                "fixable_orders_sample": [
                    {
                        "order_id": row["order_id"],
                        "merchant_id": row["merchant_id"],
                        "psp_used": row["psp_used"],
                        "current_psp_id": row["psp_id"],
                        "correct_psp_id": row["correct_psp_id"],
                        "provider": row["provider"]
                    }
                    for row in fixable_orders
                ],
                "provider_distribution": [
                    {
                        "provider": row["psp_provider"],
                        "order_count": row["order_count"],
                        "merchant_count": row["merchant_count"]
                    }
                    for row in provider_stats
                ]
            },
            "recommendations": _generate_recommendations(null_check, case_check, mismatch_check, fixable_orders)
        }
        
    except Exception as e:
        logger.error(f"Error checking PSP integrity: {e}")
        raise HTTPException(status_code=500, detail=f"Integrity check failed: {str(e)}")


@router.post("/auto-heal")
async def auto_heal_psp_data(
    dry_run: bool = True,
    current_user: Dict[str, Any] = Depends(require_admin),
):
    """
    Automatically fix PSP field issues
    
    Args:
        dry_run: If True, only report what would be fixed without making changes
    """
    try:
        fixes_applied = {
            "dry_run": dry_run,
            "timestamp": datetime.utcnow().isoformat(),
            "fixes": {}
        }
        
        # 1. Fix case inconsistencies
        if dry_run:
            case_count = await database.fetch_one("""
                SELECT COUNT(*) as count
                FROM orders
                WHERE psp_used IS NOT NULL AND psp_used != LOWER(psp_used)
            """)
            fixes_applied["fixes"]["case_normalization"] = {
                "would_fix": case_count["count"],
                "executed": False
            }
        else:
            case_result = await database.execute("""
                UPDATE orders
                SET psp_used = LOWER(psp_used)
                WHERE psp_used != LOWER(psp_used)
            """)
            fixes_applied["fixes"]["case_normalization"] = {
                "fixed": case_result,
                "executed": True
            }
        
        # 2. Fill missing psp_id based on psp_used
        if dry_run:
            id_count = await database.fetch_one("""
                SELECT COUNT(*) as count
                FROM orders o
                JOIN merchant_psps mp ON o.merchant_id = mp.merchant_id
                    AND LOWER(o.psp_used) = LOWER(mp.provider)
                    AND mp.status = 'active'
                WHERE o.psp_id IS NULL
            """)
            fixes_applied["fixes"]["fill_psp_id"] = {
                "would_fix": id_count["count"],
                "executed": False
            }
        else:
            id_result = await database.execute("""
                UPDATE orders o
                SET psp_id = mp.psp_id
                FROM merchant_psps mp
                WHERE o.merchant_id = mp.merchant_id
                    AND o.psp_id IS NULL
                    AND o.psp_used IS NOT NULL
                    AND LOWER(o.psp_used) = LOWER(mp.provider)
                    AND mp.status = 'active'
            """)
            fixes_applied["fixes"]["fill_psp_id"] = {
                "fixed": id_result,
                "executed": True
            }
        
        # 3. Fill missing psp_used based on psp_id
        if dry_run:
            used_count = await database.fetch_one("""
                SELECT COUNT(*) as count
                FROM orders o
                JOIN merchant_psps mp ON o.psp_id = mp.psp_id
                WHERE o.psp_used IS NULL
            """)
            fixes_applied["fixes"]["fill_psp_used"] = {
                "would_fix": used_count["count"],
                "executed": False
            }
        else:
            used_result = await database.execute("""
                UPDATE orders o
                SET psp_used = LOWER(mp.provider)
                FROM merchant_psps mp
                WHERE o.psp_id = mp.psp_id
                    AND o.psp_used IS NULL
            """)
            fixes_applied["fixes"]["fill_psp_used"] = {
                "fixed": used_result,
                "executed": True
            }
        
        # 4. Fix mismatched psp_id (where psp_used is correct but psp_id is wrong)
        if dry_run:
            mismatch_count = await database.fetch_one("""
                SELECT COUNT(*) as count
                FROM orders o
                JOIN merchant_psps mp ON o.merchant_id = mp.merchant_id
                    AND LOWER(o.psp_used) = LOWER(mp.provider)
                    AND mp.status = 'active'
                WHERE o.psp_id IS NOT NULL
                    AND o.psp_id != mp.psp_id
            """)
            fixes_applied["fixes"]["fix_psp_id_mismatch"] = {
                "would_fix": mismatch_count["count"],
                "executed": False
            }
        else:
            mismatch_result = await database.execute("""
                UPDATE orders o
                SET psp_id = mp.psp_id
                FROM merchant_psps mp
                WHERE o.merchant_id = mp.merchant_id
                    AND LOWER(o.psp_used) = LOWER(mp.provider)
                    AND mp.status = 'active'
                    AND o.psp_id IS NOT NULL
                    AND o.psp_id != mp.psp_id
            """)
            fixes_applied["fixes"]["fix_psp_id_mismatch"] = {
                "fixed": mismatch_result,
                "executed": True
            }
        
        total_fixes = sum(
            f.get("fixed", f.get("would_fix", 0)) 
            for f in fixes_applied["fixes"].values()
        )
        
        fixes_applied["summary"] = {
            "total_fixes": total_fixes,
            "status": "completed" if not dry_run else "dry_run_only"
        }
        
        return fixes_applied
        
    except Exception as e:
        logger.error(f"Error in auto-heal: {e}")
        raise HTTPException(status_code=500, detail=f"Auto-heal failed: {str(e)}")


@router.get("/specification")
async def get_psp_field_specification(
    current_user: Dict[str, Any] = Depends(require_admin),
):
    """
    Returns the PSP field specification and standards
    """
    return {
        "version": "1.0.0",
        "fields": {
            "psp_used": {
                "type": "VARCHAR",
                "required": True,
                "format": "lowercase",
                "description": "PSP provider name",
                "examples": ["stripe", "adyen", "checkout", "paypal", "braintree"],
                "validation": "Must be lowercase, no spaces"
            },
            "psp_id": {
                "type": "VARCHAR",
                "required": True,
                "format": "psp_{provider}_{random_12_chars}",
                "description": "Unique PSP configuration identifier",
                "examples": [
                    "psp_stripe_031421904229",
                    "psp_adyen_8f3a2c1d4e5b",
                    "psp_checkout_7b9c6d2a3f1e"
                ],
                "validation": "Must start with 'psp_', followed by provider and random string"
            }
        },
        "query_standards": {
            "join_condition": """
LEFT JOIN orders o ON o.merchant_id = mp.merchant_id 
    AND o.created_at >= :start_time
    AND (
        (o.psp_id IS NOT NULL AND mp.psp_id IS NOT NULL AND o.psp_id = mp.psp_id)
        OR 
        (o.psp_used IS NOT NULL AND mp.provider IS NOT NULL 
         AND LOWER(o.psp_used) = LOWER(mp.provider))
    )
            """,
            "filter_condition": """
WHERE (
    (o.psp_id IS NOT NULL AND mp.psp_id IS NOT NULL AND o.psp_id = mp.psp_id)
    OR 
    (o.psp_used IS NOT NULL AND mp.provider IS NOT NULL 
     AND LOWER(o.psp_used) = LOWER(mp.provider))
)
            """
        },
        "best_practices": [
            "Always set both psp_used and psp_id when creating orders",
            "Use lowercase for psp_used to avoid case sensitivity issues",
            "Prefer psp_id for exact matching in queries",
            "Use psp_used as fallback for human-readable filtering",
            "Run integrity checks regularly",
            "Enable auto-heal in production with monitoring"
        ]
    }


def _generate_recommendations(null_check, case_check, mismatch_check, fixable_orders):
    """Generate actionable recommendations based on check results"""
    recommendations = []
    
    if null_check["incomplete"] > 0:
        recommendations.append({
            "priority": "HIGH",
            "issue": f"{null_check['incomplete']} orders have incomplete PSP data",
            "action": "Run auto-heal with dry_run=false to fill missing fields",
            "endpoint": "POST /admin/psp/auto-heal?dry_run=false"
        })
    
    if len(case_check) > 0:
        total_case_issues = sum(row["count"] for row in case_check)
        recommendations.append({
            "priority": "MEDIUM",
            "issue": f"{total_case_issues} orders have case inconsistencies in psp_used",
            "action": "Run auto-heal to normalize to lowercase",
            "endpoint": "POST /admin/psp/auto-heal?dry_run=false"
        })
    
    if len(mismatch_check) > 0:
        recommendations.append({
            "priority": "HIGH",
            "issue": f"{len(mismatch_check)} orders have invalid psp_id references",
            "action": "Review mismatches and run auto-heal if safe",
            "endpoint": "POST /admin/psp/auto-heal?dry_run=false"
        })
    
    if len(fixable_orders) > 0:
        recommendations.append({
            "priority": "MEDIUM",
            "issue": f"{len(fixable_orders)} orders have fixable psp_id issues",
            "action": "Run auto-heal to correct psp_id based on merchant config",
            "endpoint": "POST /admin/psp/auto-heal?dry_run=false"
        })
    
    if not recommendations:
        recommendations.append({
            "priority": "INFO",
            "issue": "No issues found",
            "action": "PSP data is healthy. Continue monitoring.",
            "endpoint": "GET /admin/psp/integrity-check"
        })
    
    return recommendations


