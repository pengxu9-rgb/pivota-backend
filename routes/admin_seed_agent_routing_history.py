"""
[Phase 5] Admin endpoint to seed agent routing history for demo
"""

from fastapi import APIRouter, HTTPException, Depends, Path
from datetime import datetime, timedelta
import json

from db.database import database
from utils.auth import ADMIN_ROLES, get_current_user

router = APIRouter(
    prefix="/admin/seed",
    tags=["[Phase 5] Admin Seed Data"]
)


@router.post("/agent-routing-history/{agent_id}")
async def seed_agent_routing_history(
    agent_id: str = Path(..., description="Agent ID to seed history for"),
    current_user: dict = Depends(get_current_user)
):
    """
    Create demo routing history data for an agent
    
    This creates realistic routing logs with:
    - Consensus decisions (agent + merchant agree)
    - Merchant rule decisions (merchant overrides)
    - Conflict scenarios
    - Different PSP selections
    """
    
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin access required")
    
    try:
        # Create varied routing history
        test_logs = [
            # 1. Consensus - Both prefer Stripe
            {
                "merchant_id": "merchant_001",
                "agent_id": agent_id,
                "order_id": f"order_demo_{int(datetime.utcnow().timestamp())}_1",
                "chosen_psp": "stripe",
                "conflict_detected": False,
                "resolution_method": "consensus",
                "resolved_by": "consensus",
                "decision_trace": [
                    {"step": "initial_psps", "psps": ["stripe", "adyen", "paypal"]},
                    {"step": "merchant_rules_applied", "output_psps": ["stripe", "adyen"]},
                    {"step": "agent_rules_applied", "output_psps": ["stripe"]},
                    {"step": "final_selection", "selected": "stripe", "reason": "Both merchant and agent prefer Stripe"}
                ],
                "merchant_rules_applied": {"prefer": ["stripe", "adyen"]},
                "agent_rules_applied": {"weights": {"stripe": 1.0, "adyen": 0.9}},
                "execution_time_ms": 12,
                "hours_ago": 2
            },
            
            # 2. Merchant rule - Agent wanted PayPal, but merchant excluded it
            {
                "merchant_id": "merchant_002",
                "agent_id": agent_id,
                "order_id": f"order_demo_{int(datetime.utcnow().timestamp())}_2",
                "chosen_psp": "adyen",
                "conflict_detected": True,
                "resolution_method": "merchant_priority",
                "resolved_by": "merchant_rule",
                "decision_trace": [
                    {"step": "initial_psps", "psps": ["stripe", "adyen", "paypal"]},
                    {"step": "merchant_rules_applied", "output_psps": ["stripe", "adyen"], "excluded": ["paypal"]},
                    {"action": "conflict_detected", "psp": "paypal", "reason": "Merchant excludes PayPal but agent prefers it"},
                    {"step": "agent_rules_applied", "output_psps": ["adyen"]},
                    {"step": "final_selection", "selected": "adyen", "reason": "Merchant rule priority"}
                ],
                "merchant_rules_applied": {"exclude": ["paypal"], "prefer": ["stripe", "adyen"]},
                "agent_rules_applied": {"weights": {"paypal": 1.0, "adyen": 0.8, "stripe": 0.7}},
                "execution_time_ms": 25,
                "hours_ago": 5
            },
            
            # 3. Agent preference - Merchant allows all, agent chooses PayPal
            {
                "merchant_id": "merchant_003",
                "agent_id": agent_id,
                "order_id": f"order_demo_{int(datetime.utcnow().timestamp())}_3",
                "chosen_psp": "paypal",
                "conflict_detected": False,
                "resolution_method": "consensus",
                "resolved_by": "consensus",
                "decision_trace": [
                    {"step": "initial_psps", "psps": ["stripe", "adyen", "paypal", "square"]},
                    {"step": "merchant_rules_applied", "output_psps": ["stripe", "adyen", "paypal", "square"]},
                    {"step": "agent_rules_applied", "output_psps": ["paypal"]},
                    {"step": "final_selection", "selected": "paypal", "reason": "Agent preference (highest weight)"}
                ],
                "merchant_rules_applied": {},
                "agent_rules_applied": {"weights": {"paypal": 1.0, "stripe": 0.8, "adyen": 0.7}},
                "execution_time_ms": 15,
                "hours_ago": 8
            },
            
            # 4. Consensus on Adyen
            {
                "merchant_id": "merchant_001",
                "agent_id": agent_id,
                "order_id": f"order_demo_{int(datetime.utcnow().timestamp())}_4",
                "chosen_psp": "adyen",
                "conflict_detected": False,
                "resolution_method": "consensus",
                "resolved_by": "consensus",
                "decision_trace": [
                    {"step": "initial_psps", "psps": ["stripe", "adyen", "paypal"]},
                    {"step": "merchant_rules_applied", "output_psps": ["adyen", "stripe"]},
                    {"step": "agent_rules_applied", "output_psps": ["adyen"]},
                    {"step": "final_selection", "selected": "adyen"}
                ],
                "merchant_rules_applied": {"prefer": ["adyen", "stripe"]},
                "agent_rules_applied": {"weights": {"adyen": 1.0, "stripe": 0.85}},
                "execution_time_ms": 10,
                "hours_ago": 12
            },
            
            # 5. Recent consensus on Stripe
            {
                "merchant_id": "merchant_004",
                "agent_id": agent_id,
                "order_id": f"order_demo_{int(datetime.utcnow().timestamp())}_5",
                "chosen_psp": "stripe",
                "conflict_detected": False,
                "resolution_method": "consensus",
                "resolved_by": "consensus",
                "decision_trace": [
                    {"step": "initial_psps", "psps": ["stripe", "adyen"]},
                    {"step": "final_selection", "selected": "stripe"}
                ],
                "merchant_rules_applied": {},
                "agent_rules_applied": {"weights": {"stripe": 1.0}},
                "execution_time_ms": 8,
                "hours_ago": 0.5
            }
        ]
        
        inserted_logs = []
        
        for log_data in test_logs:
            hours_ago = log_data.pop("hours_ago")
            created_at = datetime.utcnow() - timedelta(hours=hours_ago)
            
            query = """
                INSERT INTO routing_logs (
                    merchant_id, agent_id, order_id, 
                    considered_psps, chosen_psp, decision_trace,
                    merchant_rules_applied, agent_rules_applied,
                    conflict_detected, resolution_method, resolved_by,
                    execution_time_ms, created_at
                ) VALUES (
                    :merchant_id, :agent_id, :order_id,
                    :considered_psps, :chosen_psp, :decision_trace,
                    :merchant_rules_applied, :agent_rules_applied,
                    :conflict_detected, :resolution_method, :resolved_by,
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
            
            inserted_logs.append({
                "id": result,
                "psp": log_data["chosen_psp"],
                "resolved_by": log_data["resolved_by"],
                "conflict": log_data["conflict_detected"],
                "hours_ago": hours_ago
            })
        
        # Get summary
        summary = await database.fetch_one(
            """
            SELECT 
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE resolved_by = 'consensus') as consensus,
                COUNT(*) FILTER (WHERE resolved_by = 'merchant_rule') as merchant_rule,
                COUNT(*) FILTER (WHERE resolved_by = 'agent_override') as agent_override,
                COUNT(*) FILTER (WHERE conflict_detected = true) as conflicts
            FROM routing_logs
            WHERE agent_id = :agent_id
            """,
            {"agent_id": agent_id}
        )
        
        return {
            "status": "success",
            "message": f"Created {len(inserted_logs)} routing history records for agent {agent_id}",
            "inserted_logs": inserted_logs,
            "agent_summary": {
                "total_routings": summary["total"],
                "consensus": summary["consensus"],
                "merchant_rule": summary["merchant_rule"],
                "agent_override": summary["agent_override"],
                "conflicts": summary["conflicts"]
            }
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to seed routing history: {str(e)}")


print("[Phase 5] Admin seed agent routing history route initialized")
