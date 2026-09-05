"""
[Phase 4++] Admin endpoint to seed test routing logs
"""

from fastapi import APIRouter, HTTPException, Depends
from datetime import datetime, timedelta
import json

from db.database import database
from utils.auth import ADMIN_ROLES, get_current_user

router = APIRouter(
    prefix="/admin/seed",
    tags=["[Phase 4++] Admin Seed Data"]
)


@router.post("/routing-logs")
async def seed_routing_logs(current_user: dict = Depends(get_current_user)):
    """Create test routing logs for demo purposes"""
    
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin access required")
    
    try:
        # Test data
        test_logs = [
            {
                "merchant_id": "merchant_high_risk_001",
                "agent_id": "agent_ee38f2b3645a2ec2",
                "order_id": f"test_order_{int(datetime.utcnow().timestamp())}",
                "chosen_psp": "stripe",
                "conflict_detected": False,
                "resolution_method": "consensus",
                "decision_trace": [
                    {"step": "initial_psps", "psps": ["stripe", "adyen", "paypal"]},
                    {"step": "merchant_rules_applied", "output_psps": ["stripe"]},
                    {"step": "agent_rules_applied", "output_psps": ["stripe"]}
                ],
                "merchant_rules_applied": {"excluded": ["paypal", "square"], "required": ["stripe"]},
                "agent_rules_applied": {"preferred": ["stripe", "adyen", "paypal"]},
                "execution_time_ms": 15,
                "hours_ago": 2
            },
            {
                "merchant_id": "merchant_cost_sensitive_002",
                "agent_id": "agent_ee38f2b3645a2ec2",
                "order_id": f"test_order_{int(datetime.utcnow().timestamp()) + 1}",
                "chosen_psp": "stripe",
                "conflict_detected": True,
                "resolution_method": "merchant_priority",
                "decision_trace": [
                    {"step": "initial_psps", "psps": ["stripe", "adyen", "paypal"]},
                    {"step": "merchant_rules_applied", "output_psps": ["stripe", "paypal"]},
                    {"action": "conflict_detected", "psp": "adyen", "reason": "Merchant excludes, Agent prefers"}
                ],
                "merchant_rules_applied": {"excluded": ["adyen"], "preferred": ["paypal", "stripe"]},
                "agent_rules_applied": {"preferred": ["stripe", "adyen"], "weights": {"stripe": 1.0, "adyen": 0.85}},
                "execution_time_ms": 23,
                "hours_ago": 1
            },
            {
                "merchant_id": "merchant_high_risk_001",
                "agent_id": "agent_ee38f2b3645a2ec2",
                "order_id": f"test_order_{int(datetime.utcnow().timestamp()) + 2}",
                "chosen_psp": "stripe",
                "conflict_detected": False,
                "resolution_method": "consensus",
                "decision_trace": [
                    {"step": "initial_psps", "psps": ["stripe", "adyen"]},
                    {"step": "final_selection", "selected": "stripe"}
                ],
                "merchant_rules_applied": {"required": ["stripe"]},
                "agent_rules_applied": {"preferred": ["stripe"]},
                "execution_time_ms": 12,
                "hours_ago": 0.5
            },
            {
                "merchant_id": "merchant_cost_sensitive_002",
                "agent_id": "agent_ee38f2b3645a2ec2",
                "order_id": f"test_order_{int(datetime.utcnow().timestamp()) + 3}",
                "chosen_psp": "paypal",
                "conflict_detected": False,
                "resolution_method": "consensus",
                "decision_trace": [
                    {"step": "initial_psps", "psps": ["stripe", "paypal"]},
                    {"step": "agent_preference", "selected": "paypal"}
                ],
                "merchant_rules_applied": {"preferred": ["paypal", "stripe"]},
                "agent_rules_applied": {"preferred": ["paypal", "stripe"]},
                "execution_time_ms": 18,
                "hours_ago": 0.2
            },
            {
                "merchant_id": "merchant_high_risk_001",
                "agent_id": "agent_ee38f2b3645a2ec2",
                "order_id": f"test_order_{int(datetime.utcnow().timestamp()) + 4}",
                "chosen_psp": "stripe",
                "conflict_detected": False,
                "resolution_method": "consensus",
                "decision_trace": [
                    {"step": "merchant_required", "required": ["stripe"]},
                    {"step": "agent_agrees", "selected": "stripe"}
                ],
                "merchant_rules_applied": {"required": ["stripe"]},
                "agent_rules_applied": {"weights": {"stripe": 1.0}},
                "execution_time_ms": 10,
                "hours_ago": 3
            }
        ]
        
        inserted = []
        
        for log_data in test_logs:
            hours_ago = log_data.pop("hours_ago")
            created_at = datetime.utcnow() - timedelta(hours=hours_ago)
            
            query = """
                INSERT INTO routing_logs (
                    merchant_id, agent_id, order_id, 
                    considered_psps, chosen_psp, decision_trace,
                    merchant_rules_applied, agent_rules_applied,
                    conflict_detected, resolution_method,
                    execution_time_ms, created_at
                ) VALUES (
                    :merchant_id, :agent_id, :order_id,
                    :considered_psps, :chosen_psp, :decision_trace,
                    :merchant_rules_applied, :agent_rules_applied,
                    :conflict_detected, :resolution_method,
                    :execution_time_ms, :created_at
                )
                RETURNING id
            """
            
            result = await database.execute(query, {
                **log_data,
                "considered_psps": json.dumps(["stripe", "adyen", "paypal"]),
                "decision_trace": json.dumps(log_data["decision_trace"]),
                "merchant_rules_applied": json.dumps(log_data["merchant_rules_applied"]),
                "agent_rules_applied": json.dumps(log_data["agent_rules_applied"]),
                "created_at": created_at
            })
            
            inserted.append({
                "id": result,
                "psp": log_data["chosen_psp"],
                "conflict": log_data["conflict_detected"],
                "resolution": log_data["resolution_method"]
            })
        
        # Get statistics
        stats = await database.fetch_one("""
            SELECT 
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE conflict_detected = true) as conflicts
            FROM routing_logs
        """)
        
        psp_dist = await database.fetch_all("""
            SELECT chosen_psp, COUNT(*) as count
            FROM routing_logs
            GROUP BY chosen_psp
            ORDER BY count DESC
        """)
        
        return {
            "status": "success",
            "message": f"Created {len(inserted)} test routing logs",
            "inserted_logs": inserted,
            "database_stats": {
                "total_logs": stats["total"],
                "total_conflicts": stats["conflicts"],
                "psp_distribution": [
                    {"psp": row["chosen_psp"], "count": row["count"]}
                    for row in psp_dist
                ]
            }
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to seed routing logs: {str(e)}")


print("[Phase 4++] Admin seed routing logs route initialized")
