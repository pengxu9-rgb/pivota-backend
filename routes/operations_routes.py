import logging
import time
import uuid
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel
from enum import Enum
from datetime import datetime, timedelta

from utils.auth import verify_jwt_token, check_permission
from realtime.metrics_store import get_metrics_store

logger = logging.getLogger("operations_routes")
router = APIRouter(prefix="/api/operations", tags=["operations"])

# Enums for status tracking
class OnboardingStatus(str, Enum):
    PENDING = "pending"
    UNDER_REVIEW = "under_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DEACTIVATED = "deactivated"

class EntityType(str, Enum):
    AGENT = "agent"
    MERCHANT = "merchant"

class VerificationStatus(str, Enum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    VERIFIED = "verified"
    FAILED = "failed"

# Pydantic models
class AgentOnboardingRequest(BaseModel):
    agent_name: str
    contact_email: str
    company_name: Optional[str] = None
    description: Optional[str] = None
    expected_volume: Optional[int] = None
    preferred_psps: Optional[List[str]] = None

class MerchantOnboardingRequest(BaseModel):
    merchant_name: str
    contact_email: str
    store_url: str
    platform: str  # "shopify", "wix", "woocommerce", etc.
    api_credentials: Optional[Dict[str, str]] = None
    description: Optional[str] = None
    expected_volume: Optional[int] = None

class OnboardingUpdate(BaseModel):
    status: OnboardingStatus
    notes: Optional[str] = None
    assigned_to: Optional[str] = None

class VerificationRequest(BaseModel):
    entity_id: str
    entity_type: EntityType
    verification_type: str  # "api_test", "payment_test", "store_verification", etc.

# In-memory storage (in production, use a database)
operations_store = {
    "agents": {},
    "merchants": {},
    "onboarding_queue": [],
    "verification_tasks": {},
    "operations_log": []
}

def log_operation(operation: str, details: Dict[str, Any], operator: str):
    """Log operations for audit trail"""
    log_entry = {
        "id": str(uuid.uuid4()),
        "operation": operation,
        "details": details,
        "operator": operator,
        "timestamp": datetime.utcnow().isoformat(),
        "timestamp_unix": time.time()
    }
    operations_store["operations_log"].append(log_entry)
    logger.info(f"Operations log: {operation} by {operator}")

# Agent Management Endpoints
@router.post("/agents/onboard")
async def onboard_agent(
    request: AgentOnboardingRequest,
    credentials: dict = Depends(verify_jwt_token)
):
    """Onboard a new agent - requires operator or admin role"""
    check_permission(credentials, "operator")
    
    agent_id = f"AGENT_{int(time.time())}"
    agent_data = {
        "id": agent_id,
        "name": request.agent_name,
        "email": request.contact_email,
        "company": request.company_name,
        "description": request.description,
        "expected_volume": request.expected_volume,
        "preferred_psps": request.preferred_psps or [],
        "status": OnboardingStatus.PENDING,
        "created_at": datetime.utcnow().isoformat(),
        "created_by": credentials.get("sub"),
        "verification_status": VerificationStatus.NOT_STARTED,
        "api_key": None,
        "webhook_url": None,
        "settings": {
            "auto_approve_orders": False,
            "max_order_amount": 10000,
            "allowed_currencies": ["USD", "EUR", "GBP"],
            "notifications_enabled": True
        }
    }
    
    operations_store["agents"][agent_id] = agent_data
    operations_store["onboarding_queue"].append({
        "entity_id": agent_id,
        "entity_type": EntityType.AGENT,
        "priority": "normal",
        "created_at": time.time()
    })
    
    log_operation("agent_onboarded", {
        "agent_id": agent_id,
        "agent_name": request.agent_name,
        "email": request.contact_email
    }, credentials.get("sub"))
    
    return {
        "status": "success",
        "message": "Agent onboarding request created",
        "agent_id": agent_id,
        "next_steps": [
            "Review agent information",
            "Test API connectivity",
            "Set up payment routing",
            "Send welcome email"
        ]
    }

@router.post("/merchants/onboard")
async def onboard_merchant(
    request: MerchantOnboardingRequest,
    credentials: dict = Depends(verify_jwt_token)
):
    """Onboard a new merchant - requires operator or admin role"""
    check_permission(credentials, "operator")
    
    merchant_id = f"MERCH_{int(time.time())}"
    merchant_data = {
        "id": merchant_id,
        "name": request.merchant_name,
        "email": request.contact_email,
        "store_url": request.store_url,
        "platform": request.platform,
        "api_credentials": request.api_credentials or {},
        "description": request.description,
        "expected_volume": request.expected_volume,
        "status": OnboardingStatus.PENDING,
        "created_at": datetime.utcnow().isoformat(),
        "created_by": credentials.get("sub"),
        "verification_status": VerificationStatus.NOT_STARTED,
        "webhook_secret": None,
        "settings": {
            "auto_fulfill_orders": True,
            "inventory_sync": True,
            "currency_preference": "USD",
            "notification_webhooks": True
        }
    }
    
    operations_store["merchants"][merchant_id] = merchant_data
    operations_store["onboarding_queue"].append({
        "entity_id": merchant_id,
        "entity_type": EntityType.MERCHANT,
        "priority": "normal",
        "created_at": time.time()
    })
    
    log_operation("merchant_onboarded", {
        "merchant_id": merchant_id,
        "merchant_name": request.merchant_name,
        "store_url": request.store_url,
        "platform": request.platform
    }, credentials.get("sub"))
    
    return {
        "status": "success",
        "message": "Merchant onboarding request created",
        "merchant_id": merchant_id,
        "next_steps": [
            "Verify store connection",
            "Test order creation",
            "Configure payment methods",
            "Set up webhooks",
            "Send welcome package"
        ]
    }

# Onboarding Queue Management
@router.get("/onboarding-queue")
async def get_onboarding_queue(
    credentials: dict = Depends(verify_jwt_token),
    limit: int = Query(20, description="Number of items to return")
):
    """Get the onboarding queue - requires operator or admin role"""
    check_permission(credentials, "operator")
    
    queue_items = []
    for item in operations_store["onboarding_queue"][:limit]:
        entity_id = item["entity_id"]
        entity_type = item["entity_type"]
        
        if entity_type == EntityType.AGENT:
            entity_data = operations_store["agents"].get(entity_id, {})
        else:
            entity_data = operations_store["merchants"].get(entity_id, {})
        
        queue_items.append({
            **item,
            "entity_data": entity_data,
            "days_in_queue": (time.time() - item["created_at"]) / 86400
        })
    
    return {
        "queue": queue_items,
        "total_pending": len(operations_store["onboarding_queue"]),
        "summary": {
            "agents_pending": len([a for a in operations_store["agents"].values() 
                                 if a["status"] == OnboardingStatus.PENDING]),
            "merchants_pending": len([m for m in operations_store["merchants"].values() 
                                    if m["status"] == OnboardingStatus.PENDING])
        }
    }

@router.put("/onboarding/{entity_id}/status")
async def update_onboarding_status(
    entity_id: str,
    update: OnboardingUpdate,
    credentials: dict = Depends(verify_jwt_token)
):
    """Update onboarding status - requires operator or admin role"""
    check_permission(credentials, "operator")
    
    # Find entity in agents or merchants
    entity_data = None
    entity_type = None
    
    if entity_id in operations_store["agents"]:
        entity_data = operations_store["agents"][entity_id]
        entity_type = EntityType.AGENT
    elif entity_id in operations_store["merchants"]:
        entity_data = operations_store["merchants"][entity_id]
        entity_type = EntityType.MERCHANT
    else:
        raise HTTPException(status_code=404, detail="Entity not found")
    
    # Update status
    old_status = entity_data["status"]
    entity_data["status"] = update.status
    entity_data["updated_at"] = datetime.utcnow().isoformat()
    entity_data["updated_by"] = credentials.get("sub")
    
    if update.notes:
        entity_data["notes"] = update.notes
    
    if update.assigned_to:
        entity_data["assigned_to"] = update.assigned_to
    
    # Remove from queue if approved
    if update.status in [OnboardingStatus.APPROVED, OnboardingStatus.ACTIVE]:
        operations_store["onboarding_queue"] = [
            item for item in operations_store["onboarding_queue"] 
            if item["entity_id"] != entity_id
        ]
    
    log_operation("onboarding_status_updated", {
        "entity_id": entity_id,
        "entity_type": entity_type.value,
        "old_status": old_status,
        "new_status": update.status,
        "notes": update.notes
    }, credentials.get("sub"))
    
    return {
        "status": "success",
        "message": f"{entity_type.value.title()} status updated to {update.status}",
        "entity_id": entity_id,
        "new_status": update.status
    }

# Verification System
@router.post("/verify")
async def start_verification(
    request: VerificationRequest,
    credentials: dict = Depends(verify_jwt_token)
):
    """Start verification process - requires operator or admin role"""
    check_permission(credentials, "operator")
    
    verification_id = f"VERIFY_{int(time.time())}"
    
    # Determine entity data
    if request.entity_type == EntityType.AGENT:
        entity_data = operations_store["agents"].get(request.entity_id)
    else:
        entity_data = operations_store["merchants"].get(request.entity_id)
    
    if not entity_data:
        raise HTTPException(status_code=404, detail="Entity not found")
    
    verification_task = {
        "id": verification_id,
        "entity_id": request.entity_id,
        "entity_type": request.entity_type,
        "verification_type": request.verification_type,
        "status": VerificationStatus.IN_PROGRESS,
        "started_at": datetime.utcnow().isoformat(),
        "started_by": credentials.get("sub"),
        "results": {},
        "notes": ""
    }
    
    operations_store["verification_tasks"][verification_id] = verification_task
    
    # Update entity verification status
    entity_data["verification_status"] = VerificationStatus.IN_PROGRESS
    
    log_operation("verification_started", {
        "verification_id": verification_id,
        "entity_id": request.entity_id,
        "verification_type": request.verification_type
    }, credentials.get("sub"))
    
    return {
        "status": "success",
        "message": "Verification process started",
        "verification_id": verification_id,
        "estimated_duration": "5-10 minutes"
    }

@router.get("/verification-tasks")
async def get_verification_tasks(
    credentials: dict = Depends(verify_jwt_token),
    status: Optional[VerificationStatus] = Query(None)
):
    """Get verification tasks - requires operator or admin role"""
    check_permission(credentials, "operator")
    
    tasks = list(operations_store["verification_tasks"].values())
    
    if status:
        tasks = [t for t in tasks if t["status"] == status]
    
    return {
        "tasks": tasks,
        "summary": {
            "total": len(operations_store["verification_tasks"]),
            "in_progress": len([t for t in tasks if t["status"] == VerificationStatus.IN_PROGRESS]),
            "completed": len([t for t in tasks if t["status"] == VerificationStatus.VERIFIED]),
            "failed": len([t for t in tasks if t["status"] == VerificationStatus.FAILED])
        }
    }

# Analytics and Reporting
@router.get("/analytics")
async def get_operations_analytics(
    credentials: dict = Depends(verify_jwt_token),
    days: int = Query(30, description="Number of days to analyze")
):
    """Get operations analytics - requires operator or admin role"""
    check_permission(credentials, "operator")
    
    cutoff_time = time.time() - (days * 86400)
    
    # Analyze recent operations
    recent_logs = [
        log for log in operations_store["operations_log"]
        if log["timestamp_unix"] > cutoff_time
    ]
    
    # Count operations by type
    operation_counts = {}
    for log in recent_logs:
        op_type = log["operation"]
        operation_counts[op_type] = operation_counts.get(op_type, 0) + 1
    
    # Analyze onboarding success rate
    total_onboardings = len([log for log in recent_logs if "onboarded" in log["operation"]])
    
    # Get current status distribution
    agent_statuses = {}
    merchant_statuses = {}
    
    for agent in operations_store["agents"].values():
        status = agent["status"]
        agent_statuses[status] = agent_statuses.get(status, 0) + 1
    
    for merchant in operations_store["merchants"].values():
        status = merchant["status"]
        merchant_statuses[status] = merchant_statuses.get(status, 0) + 1
    
    return {
        "period_days": days,
        "operations_summary": {
            "total_operations": len(recent_logs),
            "operation_breakdown": operation_counts,
            "most_active_operator": max(
                [log["operator"] for log in recent_logs], 
                key=[log["operator"] for log in recent_logs].count
            ) if recent_logs else None
        },
        "onboarding_metrics": {
            "total_agents": len(operations_store["agents"]),
            "total_merchants": len(operations_store["merchants"]),
            "pending_onboardings": len(operations_store["onboarding_queue"]),
            "agent_status_distribution": agent_statuses,
            "merchant_status_distribution": merchant_statuses
        },
        "verification_metrics": {
            "total_verifications": len(operations_store["verification_tasks"]),
            "pending_verifications": len([
                t for t in operations_store["verification_tasks"].values()
                if t["status"] == VerificationStatus.IN_PROGRESS
            ])
        }
    }

# Client Communication Tools
@router.post("/send-welcome-email")
async def send_welcome_email(
    entity_id: str,
    entity_type: EntityType,
    credentials: dict = Depends(verify_jwt_token)
):
    """Send welcome email to new client - requires operator or admin role"""
    check_permission(credentials, "operator")
    
    # Get entity data
    if entity_type == EntityType.AGENT:
        entity_data = operations_store["agents"].get(entity_id)
    else:
        entity_data = operations_store["merchants"].get(entity_id)
    
    if not entity_data:
        raise HTTPException(status_code=404, detail="Entity not found")
    
    # In a real implementation, this would integrate with email service
    email_content = {
        "to": entity_data["email"],
        "subject": f"Welcome to Pivota - {entity_type.value.title()} Onboarding",
        "template": f"{entity_type.value}_welcome",
        "variables": {
            "name": entity_data["name"],
            "entity_id": entity_id,
            "onboarding_status": entity_data["status"],
            "next_steps": [
                "Complete API integration",
                "Test payment processing",
                "Configure webhooks",
                "Review documentation"
            ]
        }
    }
    
    log_operation("welcome_email_sent", {
        "entity_id": entity_id,
        "entity_type": entity_type.value,
        "email": entity_data["email"]
    }, credentials.get("sub"))
    
    return {
        "status": "success",
        "message": f"Welcome email sent to {entity_data['email']}",
        "email_content": email_content
    }

@router.get("/operations-log")
async def get_operations_log(
    credentials: dict = Depends(verify_jwt_token),
    limit: int = Query(50, description="Number of log entries to return"),
    operation_type: Optional[str] = Query(None, description="Filter by operation type")
):
    """Get operations audit log - requires operator or admin role"""
    check_permission(credentials, "operator")
    
    logs = operations_store["operations_log"]
    
    if operation_type:
        logs = [log for log in logs if operation_type in log["operation"]]
    
    # Sort by timestamp (newest first)
    logs.sort(key=lambda x: x["timestamp_unix"], reverse=True)
    
    return {
        "logs": logs[:limit],
        "total_logs": len(operations_store["operations_log"]),
        "filtered_count": len(logs)
    }

# Dashboard Summary
@router.get("/dashboard-summary")
async def get_dashboard_summary(
    credentials: dict = Depends(verify_jwt_token)
):
    """Get operations dashboard summary - requires operator or admin role"""
    check_permission(credentials, "operator")
    
    # Get metrics from the main system
    metrics_store = get_metrics_store()
    system_metrics = metrics_store.get_snapshot() if metrics_store else {}
    
    # Calculate onboarding metrics
    pending_agents = len([a for a in operations_store["agents"].values() 
                         if a["status"] == OnboardingStatus.PENDING])
    pending_merchants = len([m for m in operations_store["merchants"].values() 
                           if m["status"] == OnboardingStatus.PENDING])
    
    active_agents = len([a for a in operations_store["agents"].values() 
                        if a["status"] == OnboardingStatus.ACTIVE])
    active_merchants = len([m for m in operations_store["merchants"].values() 
                          if m["status"] == OnboardingStatus.ACTIVE])
    
    # Recent activity
    recent_logs = operations_store["operations_log"][-10:]
    
    return {
        "onboarding": {
            "pending_agents": pending_agents,
            "pending_merchants": pending_merchants,
            "active_agents": active_agents,
            "active_merchants": active_merchants,
            "queue_length": len(operations_store["onboarding_queue"])
        },
        "verifications": {
            "in_progress": len([
                t for t in operations_store["verification_tasks"].values()
                if t["status"] == VerificationStatus.IN_PROGRESS
            ]),
            "pending_review": len([
                t for t in operations_store["verification_tasks"].values()
                if t["status"] == VerificationStatus.NOT_STARTED
            ])
        },
        "system_health": {
            "total_transactions": system_metrics.get("summary", {}).get("total", 0),
            "success_rate": system_metrics.get("summary", {}).get("success_rate", 0),
            "active_psps": len(system_metrics.get("psp_usage", [])),
            "system_status": "operational"
        },
        "recent_activity": recent_logs,
        "alerts": [
            {
                "type": "warning",
                "message": f"{pending_agents} agents pending approval",
                "action": "Review onboarding queue"
            } if pending_agents > 5 else None,
            {
                "type": "info",
                "message": f"{active_merchants} merchants actively processing orders",
                "action": "Monitor merchant performance"
            } if active_merchants > 0 else None
        ]
    }
