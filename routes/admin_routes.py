import logging
import time
import uuid
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, HTTPException, Depends, Query, BackgroundTasks
from pydantic import BaseModel
from enum import Enum
from datetime import datetime, timedelta

from utils.auth import verify_jwt_token, check_permission
from realtime.metrics_store import get_metrics_store

logger = logging.getLogger("admin_routes")
router = APIRouter(prefix="/admin", tags=["admin"])

# Enums for admin system
class PSPStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    MAINTENANCE = "maintenance"
    ERROR = "error"

class RoutingRuleType(str, Enum):
    GEOGRAPHIC = "geographic"
    CURRENCY = "currency"
    RISK_LEVEL = "risk_level"
    AMOUNT = "amount"
    MERCHANT = "merchant"

class KYBStatus(str, Enum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    APPROVED = "approved"
    REJECTED = "rejected"
    PENDING_DOCUMENTS = "pending_documents"

# Pydantic models
class PSPConfigRequest(BaseModel):
    psp_type: str
    api_key: str
    webhook_secret: str
    merchant_account: Optional[str] = None
    enabled: bool = True
    sandbox_mode: bool = True

class RoutingRuleRequest(BaseModel):
    name: str
    rule_type: RoutingRuleType
    conditions: Dict[str, Any]
    target_psp: str
    priority: int = 100
    enabled: bool = True

class MerchantKYBRequest(BaseModel):
    merchant_id: str
    kyb_status: KYBStatus
    documents: Optional[List[str]] = None
    notes: Optional[str] = None

# In-memory storage for admin system
admin_store = {
    "psp_configs": {},
    "routing_rules": [],
    "merchant_kyb": {},
    "system_logs": [],
    "api_keys": {},
    "webhook_endpoints": {},
    "sandbox_mode": True
}

def log_admin_action(action: str, details: Dict[str, Any], admin_user: str):
    """Log admin actions for audit trail"""
    log_entry = {
        "id": str(uuid.uuid4()),
        "action": action,
        "details": details,
        "admin_user": admin_user,
        "timestamp": datetime.utcnow().isoformat(),
        "timestamp_unix": time.time()
    }
    admin_store["system_logs"].append(log_entry)
    logger.info(f"Admin action: {action} by {admin_user}")

# Dashboard Overview
@router.get("/dashboard")
async def get_admin_dashboard(
    credentials: dict = Depends(verify_jwt_token)
):
    """Get admin dashboard overview - requires admin role"""
    check_permission(credentials, "admin")
    
    # Get system metrics
    metrics_store = get_metrics_store()
    system_metrics = metrics_store.get_snapshot() if metrics_store else {}
    
    # Calculate PSP health
    active_psps = len([psp for psp in admin_store["psp_configs"].values() if psp.get("enabled", False)])
    total_psps = len(admin_store["psp_configs"])
    
    # Calculate routing efficiency
    total_rules = len(admin_store["routing_rules"])
    active_rules = len([rule for rule in admin_store["routing_rules"] if rule.get("enabled", False)])
    
    # Calculate KYB status
    total_merchants = len(admin_store["merchant_kyb"])
    approved_kyb = len([kyb for kyb in admin_store["merchant_kyb"].values() if kyb.get("status") == KYBStatus.APPROVED])
    
    return {
        "system_health": {
            "status": "operational",
            "uptime": "99.9%",
            "last_updated": datetime.utcnow().isoformat()
        },
        "psp_management": {
            "total_psps": total_psps,
            "active_psps": active_psps,
            "health_score": (active_psps / total_psps * 100) if total_psps > 0 else 0
        },
        "routing_rules": {
            "total_rules": total_rules,
            "active_rules": active_rules,
            "efficiency": (active_rules / total_rules * 100) if total_rules > 0 else 0
        },
        "merchant_kyb": {
            "total_merchants": total_merchants,
            "approved_kyb": approved_kyb,
            "approval_rate": (approved_kyb / total_merchants * 100) if total_merchants > 0 else 0
        },
        "transaction_metrics": {
            "total_transactions": system_metrics.get("summary", {}).get("total", 0),
            "success_rate": system_metrics.get("summary", {}).get("success_rate", 0),
            "avg_latency": system_metrics.get("summary", {}).get("avg_latency", 0)
        },
        "alerts": [
            {
                "type": "warning",
                "message": f"{total_psps - active_psps} PSPs inactive",
                "action": "Review PSP configurations"
            } if total_psps - active_psps > 0 else None,
            {
                "type": "info",
                "message": f"{total_merchants - approved_kyb} merchants pending KYB",
                "action": "Process KYB applications"
            } if total_merchants - approved_kyb > 0 else None
        ]
    }

# PSP Management
@router.post("/psp/add")
async def add_psp_config(
    config: PSPConfigRequest,
    credentials: dict = Depends(verify_jwt_token)
):
    """Add new PSP configuration - requires admin role"""
    check_permission(credentials, "admin")
    
    psp_id = f"PSP_{config.psp_type.upper()}_{int(time.time())}"
    
    psp_config = {
        "id": psp_id,
        "psp_type": config.psp_type,
        "api_key": config.api_key,
        "webhook_secret": config.webhook_secret,
        "merchant_account": config.merchant_account,
        "enabled": config.enabled,
        "sandbox_mode": config.sandbox_mode,
        "status": PSPStatus.ACTIVE if config.enabled else PSPStatus.INACTIVE,
        "created_at": datetime.utcnow().isoformat(),
        "created_by": credentials.get("sub"),
        "last_tested": None,
        "test_results": {}
    }
    
    admin_store["psp_configs"][psp_id] = psp_config
    
    log_admin_action("psp_added", {
        "psp_id": psp_id,
        "psp_type": config.psp_type,
        "enabled": config.enabled
    }, credentials.get("sub"))
    
    return {
        "status": "success",
        "message": f"PSP {config.psp_type} configuration added",
        "psp_id": psp_id,
        "next_steps": [
            "Test PSP connectivity",
            "Configure webhook endpoints",
            "Set up routing rules",
            "Monitor performance"
        ]
    }

@router.get("/psp/list")
async def list_psp_configs(
    credentials: dict = Depends(verify_jwt_token)
):
    """List all PSP configurations - requires admin role"""
    check_permission(credentials, "admin")
    
    psp_list = []
    for psp_id, config in admin_store["psp_configs"].items():
        psp_list.append({
            "id": psp_id,
            "psp_type": config["psp_type"],
            "status": config["status"],
            "enabled": config["enabled"],
            "sandbox_mode": config["sandbox_mode"],
            "created_at": config["created_at"],
            "last_tested": config.get("last_tested"),
            "test_success": config.get("test_results", {}).get("success", False)
        })
    
    return {
        "psps": psp_list,
        "total": len(psp_list),
        "active": len([p for p in psp_list if p["enabled"]]),
        "inactive": len([p for p in psp_list if not p["enabled"]])
    }

@router.post("/psp/{psp_id}/test")
async def test_psp_connection(
    psp_id: str,
    credentials: dict = Depends(verify_jwt_token)
):
    """Test PSP connection - requires admin role"""
    check_permission(credentials, "admin")
    
    if psp_id not in admin_store["psp_configs"]:
        raise HTTPException(status_code=404, detail="PSP configuration not found")
    
    psp_config = admin_store["psp_configs"][psp_id]
    
    # Simulate PSP test
    test_results = {
        "success": True,
        "latency_ms": 150,
        "error_rate": 0.0,
        "last_test": datetime.utcnow().isoformat(),
        "details": {
            "api_connectivity": "OK",
            "webhook_validation": "OK",
            "merchant_account": "OK" if psp_config.get("merchant_account") else "N/A"
        }
    }
    
    # Update PSP config with test results
    admin_store["psp_configs"][psp_id]["test_results"] = test_results
    admin_store["psp_configs"][psp_id]["last_tested"] = datetime.utcnow().isoformat()
    
    log_admin_action("psp_tested", {
        "psp_id": psp_id,
        "success": test_results["success"],
        "latency": test_results["latency_ms"]
    }, credentials.get("sub"))
    
    return {
        "status": "success",
        "message": f"PSP {psp_id} test completed",
        "test_results": test_results
    }

# Routing Rules Management
@router.post("/routing/rules/add")
async def add_routing_rule(
    rule: RoutingRuleRequest,
    credentials: dict = Depends(verify_jwt_token)
):
    """Add new routing rule - requires admin role"""
    check_permission(credentials, "admin")
    
    rule_id = f"RULE_{int(time.time())}"
    
    routing_rule = {
        "id": rule_id,
        "name": rule.name,
        "rule_type": rule.rule_type,
        "conditions": rule.conditions,
        "target_psp": rule.target_psp,
        "priority": rule.priority,
        "enabled": rule.enabled,
        "created_at": datetime.utcnow().isoformat(),
        "created_by": credentials.get("sub"),
        "last_used": None,
        "usage_count": 0
    }
    
    admin_store["routing_rules"].append(routing_rule)
    
    log_admin_action("routing_rule_added", {
        "rule_id": rule_id,
        "rule_name": rule.name,
        "rule_type": rule.rule_type,
        "target_psp": rule.target_psp
    }, credentials.get("sub"))
    
    return {
        "status": "success",
        "message": f"Routing rule '{rule.name}' added",
        "rule_id": rule_id,
        "next_steps": [
            "Test routing logic",
            "Monitor rule performance",
            "Adjust priority if needed"
        ]
    }

@router.get("/routing/rules")
async def list_routing_rules(
    credentials: dict = Depends(verify_jwt_token)
):
    """List all routing rules - requires admin role"""
    check_permission(credentials, "admin")
    
    return {
        "rules": admin_store["routing_rules"],
        "total": len(admin_store["routing_rules"]),
        "active": len([r for r in admin_store["routing_rules"] if r.get("enabled", False)])
    }

@router.post("/routing/rules/{rule_id}/toggle")
async def toggle_routing_rule(
    rule_id: str,
    enabled: bool,
    credentials: dict = Depends(verify_jwt_token)
):
    """Toggle routing rule - requires admin role"""
    check_permission(credentials, "admin")
    
    rule = next((r for r in admin_store["routing_rules"] if r["id"] == rule_id), None)
    if not rule:
        raise HTTPException(status_code=404, detail="Routing rule not found")
    
    rule["enabled"] = enabled
    rule["updated_at"] = datetime.utcnow().isoformat()
    rule["updated_by"] = credentials.get("sub")
    
    log_admin_action("routing_rule_toggled", {
        "rule_id": rule_id,
        "enabled": enabled
    }, credentials.get("sub"))
    
    return {
        "status": "success",
        "message": f"Routing rule {rule_id} {'enabled' if enabled else 'disabled'}",
        "rule": rule
    }

# Merchant KYB Management
@router.post("/merchants/kyb/update")
async def update_merchant_kyb(
    kyb_request: MerchantKYBRequest,
    credentials: dict = Depends(verify_jwt_token)
):
    """Update merchant KYB status - requires admin role"""
    check_permission(credentials, "admin")
    
    merchant_id = kyb_request.merchant_id
    
    kyb_data = {
        "merchant_id": merchant_id,
        "status": kyb_request.kyb_status,
        "documents": kyb_request.documents or [],
        "notes": kyb_request.notes,
        "updated_at": datetime.utcnow().isoformat(),
        "updated_by": credentials.get("sub"),
        "review_history": []
    }
    
    # Add to review history if updating existing
    if merchant_id in admin_store["merchant_kyb"]:
        existing = admin_store["merchant_kyb"][merchant_id]
        kyb_data["review_history"] = existing.get("review_history", [])
        kyb_data["review_history"].append({
            "status": existing.get("status"),
            "updated_at": existing.get("updated_at"),
            "updated_by": existing.get("updated_by")
        })
    
    admin_store["merchant_kyb"][merchant_id] = kyb_data
    
    log_admin_action("merchant_kyb_updated", {
        "merchant_id": merchant_id,
        "status": kyb_request.kyb_status,
        "documents_count": len(kyb_request.documents or [])
    }, credentials.get("sub"))
    
    return {
        "status": "success",
        "message": f"Merchant {merchant_id} KYB status updated to {kyb_request.kyb_status}",
        "kyb_data": kyb_data
    }

@router.get("/merchants/kyb/status")
async def get_merchant_kyb_status(
    credentials: dict = Depends(verify_jwt_token)
):
    """Get merchant KYB status overview - requires admin role"""
    check_permission(credentials, "admin")
    
    kyb_status = {}
    for status in KYBStatus:
        kyb_status[status.value] = len([
            kyb for kyb in admin_store["merchant_kyb"].values() 
            if kyb.get("status") == status
        ])
    
    return {
        "kyb_status": kyb_status,
        "total_merchants": len(admin_store["merchant_kyb"]),
        "pending_review": kyb_status.get("in_progress", 0) + kyb_status.get("pending_documents", 0)
    }

# System Logs
@router.get("/logs")
async def get_system_logs(
    credentials: dict = Depends(verify_jwt_token),
    limit: int = Query(50, description="Number of log entries to return"),
    action_type: Optional[str] = Query(None, description="Filter by action type")
):
    """Get system logs - requires admin role"""
    check_permission(credentials, "admin")
    
    logs = admin_store["system_logs"]
    
    if action_type:
        logs = [log for log in logs if action_type in log.get("action", "")]
    
    # Sort by timestamp (newest first)
    logs.sort(key=lambda x: x["timestamp_unix"], reverse=True)
    
    return {
        "logs": logs[:limit],
        "total_logs": len(admin_store["system_logs"]),
        "filtered_count": len(logs)
    }

# Developer Tools
@router.post("/dev/api-keys/generate")
async def generate_api_key(
    name: str,
    permissions: List[str],
    credentials: dict = Depends(verify_jwt_token)
):
    """Generate new API key - requires admin role"""
    check_permission(credentials, "admin")
    
    api_key = f"pk_live_{uuid.uuid4().hex[:24]}"
    key_id = f"KEY_{int(time.time())}"
    
    key_data = {
        "id": key_id,
        "name": name,
        "api_key": api_key,
        "permissions": permissions,
        "created_at": datetime.utcnow().isoformat(),
        "created_by": credentials.get("sub"),
        "last_used": None,
        "usage_count": 0,
        "enabled": True
    }
    
    admin_store["api_keys"][key_id] = key_data
    
    log_admin_action("api_key_generated", {
        "key_id": key_id,
        "name": name,
        "permissions": permissions
    }, credentials.get("sub"))
    
    return {
        "status": "success",
        "message": "API key generated successfully",
        "key_id": key_id,
        "api_key": api_key,
        "permissions": permissions
    }

@router.get("/dev/api-keys")
async def list_api_keys(
    credentials: dict = Depends(verify_jwt_token)
):
    """List API keys - requires admin role"""
    check_permission(credentials, "admin")
    
    keys = []
    for key_id, key_data in admin_store["api_keys"].items():
        keys.append({
            "id": key_id,
            "name": key_data["name"],
            "permissions": key_data["permissions"],
            "created_at": key_data["created_at"],
            "last_used": key_data.get("last_used"),
            "usage_count": key_data.get("usage_count", 0),
            "enabled": key_data.get("enabled", True)
        })
    
    return {
        "api_keys": keys,
        "total": len(keys),
        "active": len([k for k in keys if k.get("enabled", False)])
    }

@router.post("/dev/sandbox/toggle")
async def toggle_sandbox_mode(
    enabled: bool,
    credentials: dict = Depends(verify_jwt_token)
):
    """Toggle sandbox mode - requires admin role"""
    check_permission(credentials, "admin")
    
    admin_store["sandbox_mode"] = enabled
    
    log_admin_action("sandbox_mode_toggled", {
        "enabled": enabled
    }, credentials.get("sub"))
    
    return {
        "status": "success",
        "message": f"Sandbox mode {'enabled' if enabled else 'disabled'}",
        "sandbox_mode": enabled
    }

# Analytics
@router.get("/analytics/overview")
async def get_analytics_overview(
    credentials: dict = Depends(verify_jwt_token),
    days: int = Query(30, description="Number of days to analyze")
):
    """Get analytics overview - requires admin role"""
    check_permission(credentials, "admin")
    
    # Get metrics from the main system
    metrics_store = get_metrics_store()
    system_metrics = metrics_store.get_snapshot() if metrics_store else {}
    
    # Calculate PSP performance
    psp_performance = {}
    for psp_id, config in admin_store["psp_configs"].items():
        if config.get("enabled", False):
            psp_performance[config["psp_type"]] = {
                "status": config.get("status", "unknown"),
                "last_tested": config.get("last_tested"),
                "test_success": config.get("test_results", {}).get("success", False)
            }
    
    # Calculate routing rule usage
    rule_usage = {}
    for rule in admin_store["routing_rules"]:
        if rule.get("enabled", False):
            rule_usage[rule["name"]] = {
                "usage_count": rule.get("usage_count", 0),
                "last_used": rule.get("last_used"),
                "target_psp": rule.get("target_psp")
            }
    
    return {
        "period_days": days,
        "system_metrics": system_metrics,
        "psp_performance": psp_performance,
        "routing_usage": rule_usage,
        "kyb_metrics": {
            "total_merchants": len(admin_store["merchant_kyb"]),
            "approved_rate": len([
                kyb for kyb in admin_store["merchant_kyb"].values() 
                if kyb.get("status") == KYBStatus.APPROVED
            ]) / len(admin_store["merchant_kyb"]) * 100 if admin_store["merchant_kyb"] else 0
        },
        "admin_actions": {
            "total_logs": len(admin_store["system_logs"]),
            "recent_actions": len([
                log for log in admin_store["system_logs"]
                if log["timestamp_unix"] > time.time() - (days * 86400)
            ])
        }
    }
