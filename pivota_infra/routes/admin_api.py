"""
Admin API Routes
Provides endpoints for the admin dashboard
"""

from fastapi import APIRouter, Depends, HTTPException, status
from typing import Dict, List, Any
from routes.auth_routes import verify_jwt_token, require_admin
from datetime import datetime

router = APIRouter(prefix="/admin", tags=["admin"])

# Mock data for now - replace with actual database queries later
MOCK_PSPS = {
    "stripe": {
        "id": "stripe",
        "name": "Stripe",
        "enabled": True,
        "status": "active",
        "last_test": datetime.now().isoformat(),
        "success_rate": 99.5
    },
    "paypal": {
        "id": "paypal",
        "name": "PayPal",
        "enabled": True,
        "status": "active",
        "last_test": datetime.now().isoformat(),
        "success_rate": 98.2
    }
}

@router.get("/dashboard")
async def get_dashboard(current_user: dict = Depends(require_admin)):
    """Get dashboard overview"""
    return {
        "status": "success",
        "data": {
            "total_transactions": 1250,
            "total_volume": 125000.50,
            "active_psps": len([p for p in MOCK_PSPS.values() if p["enabled"]]),
            "pending_approvals": 0
        }
    }

@router.get("/psp/status")
async def get_psp_status(current_user: dict = Depends(require_admin)):
    """Get PSP status"""
    return {
        "status": "success",
        "psp": MOCK_PSPS
    }

@router.get("/psp/list")
async def get_psp_list(current_user: dict = Depends(require_admin)):
    """Get list of PSPs"""
    return {
        "status": "success",
        "psps": list(MOCK_PSPS.values())
    }

@router.post("/psp/{psp_id}/test")
async def test_psp(psp_id: str, current_user: dict = Depends(require_admin)):
    """Test PSP connection"""
    if psp_id not in MOCK_PSPS:
        raise HTTPException(status_code=404, detail="PSP not found")
    
    return {
        "status": "success",
        "message": f"PSP {psp_id} test successful",
        "test_result": {
            "latency_ms": 45,
            "status": "healthy"
        }
    }

@router.post("/psp/{psp_id}/toggle")
async def toggle_psp(psp_id: str, enable: bool, current_user: dict = Depends(require_admin)):
    """Enable/disable PSP"""
    if psp_id not in MOCK_PSPS:
        raise HTTPException(status_code=404, detail="PSP not found")
    
    MOCK_PSPS[psp_id]["enabled"] = enable
    return {
        "status": "success",
        "message": f"PSP {psp_id} {'enabled' if enable else 'disabled'}"
    }

@router.get("/routing/rules")
async def get_routing_rules(current_user: dict = Depends(require_admin)):
    """Get routing rules"""
    return {
        "status": "success",
        "rules": [
            {
                "id": "rule1",
                "name": "High Value to Stripe",
                "rule_type": "amount_based",
                "conditions": {"min_amount": 1000},
                "target_psp": "stripe",
                "priority": 1,
                "enabled": True
            }
        ]
    }

@router.get("/merchants/kyb/status")
async def get_merchant_kyb(current_user: dict = Depends(require_admin)):
    """Get merchant KYB status"""
    return {
        "status": "success",
        "merchants": {}
    }

@router.get("/logs")
async def get_system_logs(
    limit: int = 50,
    hours: int = 24,
    current_user: dict = Depends(require_admin)
):
    """Get system logs"""
    return {
        "status": "success",
        "logs": [
            {
                "id": "log1",
                "timestamp": datetime.now().isoformat(),
                "level": "info",
                "message": "System started",
                "source": "system"
            }
        ]
    }

@router.get("/dev/api-keys")
async def get_api_keys(current_user: dict = Depends(require_admin)):
    """Get API keys"""
    return {
        "status": "success",
        "api_keys": []
    }

@router.get("/analytics/overview")
async def get_analytics(days: int = 30, current_user: dict = Depends(require_admin)):
    """Get analytics overview"""
    return {
        "status": "success",
        "total_transactions": 1250,
        "total_volume": 125000.50,
        "success_rate": 99.2,
        "average_transaction_value": 100.00,
        "top_psps": [
            {"name": "Stripe", "volume": 75000.30, "count": 750},
            {"name": "PayPal", "volume": 50000.20, "count": 500}
        ],
        "daily_stats": []
    }

