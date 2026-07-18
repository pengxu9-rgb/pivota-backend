"""
Admin Consent Management Routes
Self-service tools for issuing, revoking, and auditing agent consents
"""
import json
import logging
from datetime import datetime, timedelta
from typing import Optional, List
from fastapi import APIRouter, HTTPException, Depends, Request, status
from pydantic import BaseModel, Field
from db.database import database

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/consent-management",
    tags=["Admin - Consent Management"]
)


# ========================
# Request/Response Models
# ========================

class ConsentIssueRequest(BaseModel):
    """Issue consent to an agent"""
    agent_id: str = Field(..., description="Agent ID")
    scope: List[str] = Field(..., description="Permitted actions (e.g., ['read', 'write', 'transaction'])")
    duration_hours: int = Field(24, description="Consent validity in hours")
    spending_limit: Optional[float] = Field(None, description="Maximum spending amount")
    notes: Optional[str] = Field(None, description="Admin notes")


class ConsentRevokeRequest(BaseModel):
    """Revoke consent"""
    reason: str = Field(..., description="Reason for revocation")


class ConsentAuditLog(BaseModel):
    """Consent audit log entry"""
    log_id: str
    consent_id: str
    action: str
    admin_user: str
    details: Optional[dict]
    timestamp: str


# ========================
# Consent Issuance
# ========================

@router.post("/issue")
async def issue_consent(
    request: Request,
    consent_request: ConsentIssueRequest
):
    """
    Issue consent to an agent (admin-initiated)
    
    This bypasses the normal consent flow where agents request consent.
    Useful for testing and emergency access grants.
    
    Requires: Admin authentication
    """
    from services.consent_service import consent_service
    
    # Check if agent exists
    agent = await database.fetch_one(
        "SELECT agent_id, agent_name FROM agents WHERE agent_id = :agent_id",
        {"agent_id": consent_request.agent_id}
    )
    
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent not found: {consent_request.agent_id}"
        )
    
    try:
        # Create consent without signature (admin-initiated)
        consent_data = await consent_service.create_consent(
            agent_id=consent_request.agent_id,
            scope=consent_request.scope,
            duration_hours=consent_request.duration_hours,
            signature=None,  # Admin bypass
            nonce=None,  # Admin bypass
            spending_limit=consent_request.spending_limit,
        )
        
        # Record audit log
        try:
            await database.execute(
                """INSERT INTO consent_audit_logs (
                       consent_id, action, admin_user, agent_id, details, created_at
                   ) VALUES (
                       :consent_id, :action, :admin_user, :agent_id, :details, NOW()
                   )""",
                {
                    "consent_id": consent_data["token"],
                    "action": "issued",
                    "admin_user": getattr(request.state, "admin_user", "admin"),
                    "agent_id": consent_request.agent_id,
                    "details": json.dumps({
                        "scope": consent_request.scope,
                        "duration_hours": consent_request.duration_hours,
                        "spending_limit": consent_request.spending_limit,
                        "notes": consent_request.notes
                    })
                }
            )
        except Exception as audit_error:
            logger.warning(f"Audit log failed (table may not exist): {audit_error}")
        
        logger.info(
            f"✅ Admin issued consent {consent_data['token']} "
            f"to agent {consent_request.agent_id}"
        )
        
        return {
            "status": "issued",
            "consent_token": consent_data["token"],
            "agent_id": consent_request.agent_id,
            "agent_name": agent["agent_name"],
            "scope": consent_data["scope"],
            "expires_at": consent_data["expires_at"],
            "timestamp": datetime.utcnow().isoformat()
        }
        
    except Exception as e:
        logger.error(f"Failed to issue consent: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to issue consent: {str(e)}"
        )


@router.post("/issue-batch")
async def issue_batch_consents(
    request: Request,
    batch_request: List[ConsentIssueRequest]
):
    """
    Issue multiple consents at once
    
    Requires: Admin authentication
    """
    from services.consent_service import consent_service
    
    results = []
    success_count = 0
    failure_count = 0
    
    for consent_req in batch_request:
        try:
            consent_data = await consent_service.create_consent(
                agent_id=consent_req.agent_id,
                scope=consent_req.scope,
                duration_hours=consent_req.duration_hours,
                signature=None,
                nonce=None,
                spending_limit=consent_req.spending_limit,
            )
            
            results.append({
                "agent_id": consent_req.agent_id,
                "status": "success",
                "consent_token": consent_data["token"]
            })
            success_count += 1
            
        except Exception as e:
            results.append({
                "agent_id": consent_req.agent_id,
                "status": "failed",
                "error": str(e)
            })
            failure_count += 1
    
    logger.info(f"Batch consent issuance: {success_count} success, {failure_count} failed")
    
    return {
        "success_count": success_count,
        "failure_count": failure_count,
        "results": results,
        "timestamp": datetime.utcnow().isoformat()
    }


# ========================
# Consent Revocation
# ========================

@router.post("/revoke/{consent_id}")
async def revoke_consent(
    request: Request,
    consent_id: str,
    revoke_request: ConsentRevokeRequest
):
    """
    Revoke a consent token (admin-initiated)
    
    Requires: Admin authentication
    """
    from services.consent_service import consent_service
    
    # Check if consent exists
    consent = await database.fetch_one(
        "SELECT * FROM agent_consents WHERE consent_id = :consent_id",
        {"consent_id": consent_id}
    )
    
    if not consent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Consent not found: {consent_id}"
        )
    
    try:
        # Revoke consent
        await consent_service.revoke_consent(consent_id)
        
        # Record audit log
        await database.execute(
            """INSERT INTO consent_audit_logs (
                   consent_id, action, admin_user, agent_id, details, created_at
               ) VALUES (
                   :consent_id, :action, :admin_user, :agent_id, :details, NOW()
               )""",
            {
                "consent_id": consent_id,
                "action": "revoked",
                "admin_user": getattr(request.state, "admin_user", "admin"),
                "agent_id": consent["agent_id"],
                "details": {"reason": revoke_request.reason}
            }
        )
        
        logger.info(f"✅ Admin revoked consent {consent_id}")
        
        return {
            "status": "revoked",
            "consent_id": consent_id,
            "agent_id": consent["agent_id"],
            "reason": revoke_request.reason,
            "timestamp": datetime.utcnow().isoformat()
        }
        
    except Exception as e:
        logger.error(f"Failed to revoke consent: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to revoke consent: {str(e)}"
        )


@router.post("/revoke-all/{agent_id}")
async def revoke_all_agent_consents(
    request: Request,
    agent_id: str,
    revoke_request: ConsentRevokeRequest
):
    """
    Revoke all active consents for an agent
    
    Requires: Admin authentication
    """
    # Get all active consents
    consents = await database.fetch_all(
        """SELECT consent_id FROM agent_consents 
           WHERE agent_id = :agent_id AND status = 'active'""",
        {"agent_id": agent_id}
    )
    
    revoked_count = 0
    
    for consent in consents:
        await database.execute(
            """UPDATE agent_consents 
               SET status = 'revoked', revoked_at = NOW()
               WHERE consent_id = :consent_id""",
            {"consent_id": consent["consent_id"]}
        )
        
        # Audit log
        await database.execute(
            """INSERT INTO consent_audit_logs (
                   consent_id, action, admin_user, agent_id, details, created_at
               ) VALUES (
                   :consent_id, :action, :admin_user, :agent_id, :details, NOW()
               )""",
            {
                "consent_id": consent["consent_id"],
                "action": "bulk_revoked",
                "admin_user": getattr(request.state, "admin_user", "admin"),
                "agent_id": agent_id,
                "details": {"reason": revoke_request.reason}
            }
        )
        
        revoked_count += 1
    
    logger.info(f"✅ Admin revoked {revoked_count} consents for agent {agent_id}")
    
    return {
        "status": "success",
        "agent_id": agent_id,
        "revoked_count": revoked_count,
        "reason": revoke_request.reason,
        "timestamp": datetime.utcnow().isoformat()
    }


# ========================
# Consent Audit & Monitoring
# ========================

@router.get("/audit")
async def get_consent_audit_logs(
    consent_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    action: Optional[str] = None,
    limit: int = 100
):
    """
    Get consent audit logs
    
    Requires: Admin authentication
    """
    query = """
        SELECT log_id, consent_id, action, admin_user, agent_id, details, created_at
        FROM consent_audit_logs
        WHERE 1=1
    """
    
    params = {}
    
    if consent_id:
        query += " AND consent_id = :consent_id"
        params["consent_id"] = consent_id
    
    if agent_id:
        query += " AND agent_id = :agent_id"
        params["agent_id"] = agent_id
    
    if action:
        query += " AND action = :action"
        params["action"] = action
    
    query += " ORDER BY created_at DESC LIMIT :limit"
    params["limit"] = limit
    
    results = await database.fetch_all(query, params)
    
    return {
        "logs": [dict(r) for r in results],
        "count": len(results)
    }


@router.get("/active")
async def list_active_consents(
    agent_id: Optional[str] = None,
    limit: int = 50
):
    """
    List all active consents
    
    Requires: Admin authentication
    """
    query = """
        SELECT c.consent_id, c.agent_id, a.agent_name, c.scope, 
               c.created_at, c.expires_at, c.status
        FROM agent_consents c
        LEFT JOIN agents a ON c.agent_id = a.agent_id
        WHERE c.status = 'active'
    """
    
    params = {}
    
    if agent_id:
        query += " AND c.agent_id = :agent_id"
        params["agent_id"] = agent_id
    
    query += " ORDER BY c.granted_at DESC LIMIT :limit"
    params["limit"] = limit
    
    results = await database.fetch_all(query, params)
    
    return {
        "consents": [dict(r) for r in results],
        "count": len(results)
    }


@router.get("/expiring")
async def list_expiring_consents(hours: int = 24):
    """
    List consents expiring within specified hours
    
    Requires: Admin authentication
    """
    query = """
        SELECT c.consent_id, c.agent_id, a.agent_name, c.expires_at,
               EXTRACT(EPOCH FROM (c.expires_at - NOW())) / 3600 as hours_remaining
        FROM agent_consents c
        LEFT JOIN agents a ON c.agent_id = a.agent_id
        WHERE c.status = 'active'
          AND c.expires_at < NOW() + (:hours || ' hours')::interval
        ORDER BY c.expires_at ASC
    """
    
    results = await database.fetch_all(query, {"hours": hours})
    
    return {
        "expiring_consents": [dict(r) for r in results],
        "count": len(results),
        "warning_threshold_hours": hours
    }


@router.post("/extend/{consent_id}")
async def extend_consent(
    request: Request,
    consent_id: str,
    additional_hours: int = 24
):
    """
    Extend consent expiration time
    
    Requires: Admin authentication
    """
    # Check consent exists
    consent = await database.fetch_one(
        "SELECT * FROM agent_consents WHERE consent_id = :consent_id",
        {"consent_id": consent_id}
    )
    
    if not consent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Consent not found"
        )
    
    # Extend expiration
    await database.execute(
        """UPDATE agent_consents 
           SET expires_at = expires_at + (:hours || ' hours')::interval
           WHERE consent_id = :consent_id""",
        {"consent_id": consent_id, "hours": additional_hours}
    )
    
    # Audit log
    await database.execute(
        """INSERT INTO consent_audit_logs (
               consent_id, action, admin_user, agent_id, details, created_at
           ) VALUES (
               :consent_id, :action, :admin_user, :agent_id, :details, NOW()
           )""",
        {
            "consent_id": consent_id,
            "action": "extended",
            "admin_user": getattr(request.state, "admin_user", "admin"),
            "agent_id": consent["agent_id"],
            "details": {"additional_hours": additional_hours}
        }
    )
    
    logger.info(f"✅ Admin extended consent {consent_id} by {additional_hours} hours")
    
    return {
        "status": "extended",
        "consent_id": consent_id,
        "additional_hours": additional_hours,
        "timestamp": datetime.utcnow().isoformat()
    }


# ========================
# Consent Analytics
# ========================

@router.get("/analytics")
async def get_consent_analytics():
    """
    Get consent usage analytics
    
    Requires: Admin authentication
    """
    # Overall stats
    overall = await database.fetch_one("""
        SELECT
            COUNT(*) as total,
            COALESCE(SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END), 0) as active,
            COALESCE(SUM(CASE WHEN status = 'expired' THEN 1 ELSE 0 END), 0) as expired,
            COALESCE(SUM(CASE WHEN status = 'revoked' THEN 1 ELSE 0 END), 0) as revoked
        FROM agent_consents
    """)
    
    # By agent
    by_agent = await database.fetch_all("""
        SELECT c.agent_id, a.agent_name,
               COUNT(*) as total_consents,
               SUM(CASE WHEN c.status = 'active' THEN 1 ELSE 0 END) as active_consents
        FROM agent_consents c
        LEFT JOIN agents a ON c.agent_id = a.agent_id
        GROUP BY c.agent_id, a.agent_name
        ORDER BY total_consents DESC
        LIMIT 10
    """)
    
    # Recent activity
    try:
        recent_activity = await database.fetch_all("""
            SELECT action, COUNT(*) as count
            FROM consent_audit_logs
            WHERE created_at > NOW() - INTERVAL '24 hours'
            GROUP BY action
            ORDER BY count DESC
        """)
    except Exception:
        recent_activity = []
    
    return {
        "overall": dict(overall) if overall else {},
        "by_agent": [dict(r) for r in by_agent],
        "recent_activity": [dict(r) for r in recent_activity],
        "timestamp": datetime.utcnow().isoformat()
    }


@router.get("/health")
async def consent_system_health():
    """
    Check consent system health
    
    Public endpoint - no authentication required
    """
    # Check tables exist
    tables = await database.fetch_all("""
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name IN ('agent_consents', 'nonce_tracker', 'consent_audit_logs')
    """)
    
    table_names = [r["table_name"] for r in tables]
    
    # Get stats
    consent_count = await database.fetch_one(
        "SELECT COUNT(*) as count FROM agent_consents"
    )
    
    audit_count = await database.fetch_one(
        "SELECT COUNT(*) as count FROM consent_audit_logs"
    )
    
    return {
        "status": "healthy",
        "tables": {
            "agent_consents": "agent_consents" in table_names,
            "nonce_tracker": "nonce_tracker" in table_names,
            "consent_audit_logs": "consent_audit_logs" in table_names
        },
        "statistics": {
            "total_consents": consent_count["count"] if consent_count else 0,
            "total_audit_logs": audit_count["count"] if audit_count else 0
        },
        "timestamp": datetime.utcnow().isoformat()
    }
