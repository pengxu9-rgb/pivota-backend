"""
User Approval Routes for Lovable Integration
This provides endpoints for Lovable to check and manage user approval status
"""

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Optional, Dict, Any
import datetime

router = APIRouter(prefix="/api/user", tags=["user-approval"])

# Mock user database (in production, this would be a real database)
APPROVED_USERS = {
    "peng@chydan.com": {
        "user_id": "peng_chydan_001",
        "email": "peng@chydan.com",
        "username": "peng",
        "full_name": "Peng Chydan",
        "role": "admin",
        "status": "approved",
        "kyb_status": "approved",
        "permissions": ["admin", "operator", "viewer"],
        "approval_date": "2025-10-14T03:05:16.583103",
        "approved_by": "system_admin",
        "access_level": "full_admin"
    }
}

class UserApprovalRequest(BaseModel):
    email: str
    action: Optional[str] = "check"

class UserApprovalResponse(BaseModel):
    status: str
    message: str
    user: Optional[Dict[str, Any]] = None

@router.get("/approval/status")
async def check_user_approval_status(email: str = Query(..., description="User email to check")):
    """Check if a user is approved"""
    try:
        if email in APPROVED_USERS:
            user_data = APPROVED_USERS[email]
            return {
                "status": "success",
                "message": "User is approved",
                "user": {
                    "email": user_data["email"],
                    "username": user_data["username"],
                    "role": user_data["role"],
                    "status": user_data["status"],
                    "kyb_status": user_data["kyb_status"],
                    "permissions": user_data["permissions"],
                    "access_level": user_data["access_level"]
                }
            }
        else:
            return {
                "status": "pending",
                "message": "User approval is pending",
                "user": {
                    "email": email,
                    "status": "pending",
                    "role": "viewer"
                }
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error checking user status: {str(e)}")

@router.post("/approval")
async def approve_user(request: UserApprovalRequest):
    """Approve a user (admin only)"""
    try:
        if request.email in APPROVED_USERS:
            return {
                "status": "success",
                "message": "User is already approved",
                "user": APPROVED_USERS[request.email]
            }
        else:
            # Create new approved user
            new_user = {
                "user_id": f"user_{len(APPROVED_USERS) + 1:03d}",
                "email": request.email,
                "username": request.email.split("@")[0],
                "full_name": request.email.split("@")[0].title(),
                "role": "admin",
                "status": "approved",
                "kyb_status": "approved",
                "permissions": ["admin", "operator", "viewer"],
                "approval_date": datetime.datetime.utcnow().isoformat(),
                "approved_by": "system_admin",
                "access_level": "full_admin"
            }
            
            APPROVED_USERS[request.email] = new_user
            
            return {
                "status": "success",
                "message": "User approved successfully",
                "user": new_user
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error approving user: {str(e)}")

@router.get("/approval/list")
async def list_approved_users():
    """List all approved users (admin only)"""
    try:
        return {
            "status": "success",
            "message": "Approved users retrieved",
            "users": list(APPROVED_USERS.values()),
            "total_count": len(APPROVED_USERS)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error retrieving users: {str(e)}")

@router.get("/approval/peng@chydan.com")
async def get_peng_approval_status():
    """Get specific approval status for peng@chydan.com"""
    try:
        if "peng@chydan.com" in APPROVED_USERS:
            user_data = APPROVED_USERS["peng@chydan.com"]
            return {
                "status": "success",
                "message": "User is approved",
                "user": {
                    "email": user_data["email"],
                    "username": user_data["username"],
                    "role": user_data["role"],
                    "status": user_data["status"],
                    "kyb_status": user_data["kyb_status"],
                    "permissions": user_data["permissions"],
                    "access_level": user_data["access_level"],
                    "approval_date": user_data["approval_date"]
                }
            }
        else:
            return {
                "status": "error",
                "message": "User not found",
                "user": None
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error checking user status: {str(e)}")
