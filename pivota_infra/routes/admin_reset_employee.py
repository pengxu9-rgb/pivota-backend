"""
Admin endpoint to reset employee passwords
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from db.database import database
import hashlib
import logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/employees", tags=["admin-employee-mgmt"])

class ResetPasswordRequest(BaseModel):
    email: str
    new_password: str = "Admin123!"

@router.post("/reset-password")
async def reset_employee_password(request: ResetPasswordRequest):
    """
    Reset employee password (no auth required - for emergency access)
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
                   (employee_id, name, email, password, role, department, status, created_at, updated_at)
                   VALUES (:employee_id, :name, :email, :password, :role, :department, :status, NOW(), NOW())
                   ON CONFLICT (email) DO UPDATE SET
                       password = :password,
                       status = 'active',
                       updated_at = NOW()
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
                       status = 'active',
                       updated_at = NOW()
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
async def list_all_employees():
    """List all employees (no auth for debugging)"""
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
