"""
Admin endpoints to reset employee passwords and list employees.

Both routes were previously unauthenticated ("no auth required - for emergency
access"). That made this a fourth anonymous privilege-escalation lane, worse
than the `/auth/signup` one it sat next to: `reset-password` INSERTs an
`employees` row with a hardcoded `role: "admin"` and `status: "active"` and a
caller-chosen password, and `POST /auth/signin` then mints a `role: "admin"`
JWT from that row. Its `ON CONFLICT (email) DO UPDATE` also let an anonymous
caller take over ANY existing employee account and re-activate a disabled one,
and `/list` handed out the roster to choose a target from.

Both now require `require_admin_or_key`, which keeps the break-glass path
alive: an operator can still authenticate with the `X-ADMIN-KEY` header when
no admin can log in. That dependency fails closed -- with no key configured
the header branch cannot match, so it falls through to the JWT branch and
then to 401.
"""
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from db.database import database
from utils.auth import require_admin_or_key
import hashlib
import logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/employees", tags=["admin-employee-mgmt"])

class ResetPasswordRequest(BaseModel):
    email: str
    new_password: str = "Admin123!"

@router.post("/reset-password")
async def reset_employee_password(
    request: ResetPasswordRequest,
    _admin: Dict[str, Any] = Depends(require_admin_or_key),
):
    """
    Reset an employee password, or create the employee if absent.

    Requires an admin JWT or the `X-ADMIN-KEY` break-glass header.
    """
    try:
        # Hash password using employee salt
        salt = "pivota_employee_salt_v1"
        hashed_password = hashlib.sha256(f"{request.new_password}{salt}".encode()).hexdigest()
        
        logger.info(f"Resetting password for {request.email}")
        
        # Check if employee exists
        employee = await database.fetch_one(
            "SELECT employee_id, name, email, status FROM employees WHERE email = :email",
            {"email": request.email}
        )
        
        if not employee:
            # Create employee if doesn't exist
            logger.info(f"Employee not found, creating new record for {request.email}")
            
            await database.execute(
                """INSERT INTO employees 
                   (employee_id, name, email, password, role, department, status, created_at)
                   VALUES (:employee_id, :name, :email, :password, :role, :department, :status, NOW())
                   ON CONFLICT (email) DO UPDATE SET
                       password = :password,
                       status = 'active'
                """,
                {
                    "employee_id": f"emp_{request.email.split('@')[0]}",
                    "name": "Pivota Admin",
                    "email": request.email,
                    "password": hashed_password,
                    "role": "admin",
                    "department": "Operations",
                    "status": "active"
                }
            )
            
            return {
                "success": True,
                "message": f"Employee account created for {request.email}",
                "email": request.email,
                "password": request.new_password,
                "note": "Account is now active and ready to use"
            }
        else:
            # Update existing employee
            logger.info(f"Updating password for existing employee: {employee['name']}")
            
            await database.execute(
                """UPDATE employees 
                   SET password = :password, 
                       status = 'active'
                   WHERE email = :email""",
                {"password": hashed_password, "email": request.email}
            )
            
            return {
                "success": True,
                "message": f"Password reset for {employee['name']}",
                "email": request.email,
                "employee_id": employee["employee_id"],
                "password": request.new_password,
                "status": "active"
            }
            
    except Exception as e:
        logger.error(f"Error resetting password: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/list")
async def list_all_employees(_admin: Dict[str, Any] = Depends(require_admin_or_key)):
    """List all employees. Requires an admin JWT or the `X-ADMIN-KEY` header."""
    try:
        employees = await database.fetch_all(
            """SELECT employee_id, name, email, role, department, status, created_at 
               FROM employees 
               ORDER BY created_at DESC"""
        )
        
        return {
            "total": len(employees),
            "employees": [
                {
                    "employee_id": e["employee_id"],
                    "name": e["name"],
                    "email": e["email"],
                    "role": e["role"],
                    "department": e["department"],
                    "status": e["status"],
                    "created_at": e["created_at"].isoformat() if e["created_at"] else None
                }
                for e in employees
            ]
        }
    except Exception as e:
        return {"error": str(e), "employees": []}


