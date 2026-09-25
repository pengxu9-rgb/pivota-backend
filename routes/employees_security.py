"""
Employees Management and Security Routes
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, timedelta
from textwrap import dedent
import logging
from db.auth_identity import upsert_membership
from utils.auth import ADMIN_ROLES, EMPLOYEE_STAFF_ROLES, get_current_user, hash_password as hash_user_password

# All seven guards below were `current_user["role"] != "admin"`, which locked
# `super_admin` -- the portal's most privileged role -- out of employee
# management entirely. ADMIN_ROLES corrects that omission and NOTHING ELSE.
#
# Deliberately NOT EMPLOYEE_STAFF_ROLES: these endpoints create, update and
# deactivate employees and read the security audit log, the API-key list and
# the security settings. A plain `employee` reaching them could grant itself a
# colleague's access or delete one; `outsourced` is a contractor. Both stay
# refused. Widening this family is a scope decision for whoever needs it, with
# its own review -- not a side effect of fixing a role-name omission.
from db.database import database
from config.settings import settings
import uuid
import secrets
import hashlib
import string
import random

router = APIRouter()
logger = logging.getLogger("employees_security")

MANAGED_EMPLOYEE_ROLES = ["employee", "admin", "super_admin", "outsourced"]

# Helper functions for password management
def generate_temp_password(length: int = 12) -> str:
    """Generate a secure temporary password"""
    chars = string.ascii_letters + string.digits + "!@#$%"
    return ''.join(random.choice(chars) for _ in range(length))

def hash_password(password: str) -> str:
    """Hash password using SHA256"""
    salt = "pivota_employee_salt_v1"
    return hashlib.sha256(f"{password}{salt}".encode()).hexdigest()

def _send_employee_welcome_email(email: str, name: str, temp_password: str) -> str:
    """
    Best-effort email sender for newly created employee accounts.
    Returns: "sent" or "failed".
    """
    from_email = getattr(settings, "from_email", "noreply@pivota.ai")
    subject = "Your Pivota employee account"
    text_content = (
        f"Hi {name},\n\n"
        "Your Pivota employee account has been created.\n\n"
        f"Email: {email}\n"
        f"Temporary password: {temp_password}\n\n"
        "Please sign in and change your password on first login.\n"
        "If you did not expect this email, please contact your administrator."
    )
    html_content = dedent(
        f"""
        <p>Hi {name},</p>
        <p>Your Pivota employee account has been created.</p>
        <p><strong>Email:</strong> {email}<br/>
        <strong>Temporary password:</strong> {temp_password}</p>
        <p>Please sign in and change your password on first login.</p>
        <p>If you did not expect this email, please contact your administrator.</p>
        """
    ).strip()

    try:
        from utils.email_sender import send_email, mask_email

        res = send_email(
            to_email=email,
            subject=subject,
            text_body=text_content,
            html_body=html_content,
            from_email=from_email,
            from_name="Pivota",
            tags={"type": "employee_welcome"},
        )
        if not getattr(res, "ok", False):
            logger.warning(
                "[Employees] Welcome email delivery failed provider=%s error=%s to=%s",
                getattr(res, "provider", None),
                getattr(res, "error", None),
                mask_email(email),
            )
            return "failed"
        logger.info("[Employees] Welcome email sent to %s", mask_email(email))
        return "sent"
    except Exception as exc:
        logger.warning("[Employees] Welcome email send raised error=%s", type(exc).__name__)
        return "failed"

# ============== Employees Management ==============

@router.get("/employees/list")
async def get_all_employees(
    current_user: dict = Depends(get_current_user)
):
    """Get all employees"""
    if current_user["role"] not in EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Create employees table if not exists
        create_table_query = """
            CREATE TABLE IF NOT EXISTS employees (
                employee_id VARCHAR(50) PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                email VARCHAR(100) UNIQUE NOT NULL,
                password VARCHAR(255),
                role VARCHAR(50) NOT NULL,
                department VARCHAR(100),
                status VARCHAR(20) DEFAULT 'active',
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                last_login TIMESTAMP WITH TIME ZONE
            )
        """
        await database.execute(create_table_query)
        
        # Get employees
        employees_query = """
            SELECT * FROM employees
            ORDER BY created_at DESC
        """
        employees = await database.fetch_all(employees_query)
        
        # If no employees exist, return demo employees
        if not employees:
            return {
                "status": "success",
                "employees": [
                    {
                        "employee_id": "emp_001",
                        "name": "Admin User",
                        "email": "employee@pivota.com",
                        "role": "admin",
                        "department": "Operations",
                        "status": "active",
                        "created_at": datetime.now().isoformat(),
                        "last_login": datetime.now().isoformat()
                    }
                ]
            }
        
        return {
            "status": "success",
            "employees": [
                {
                    "employee_id": e["employee_id"],
                    "name": e["name"],
                    "email": e["email"],
                    "role": e["role"],
                    "department": e["department"],
                    "status": e["status"],
                    "created_at": e["created_at"].isoformat() if e["created_at"] else None,
                    "last_login": e["last_login"].isoformat() if e["last_login"] else None
                }
                for e in employees
            ]
        }
    
    except Exception as e:
        print(f"Error getting employees: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get employees: {str(e)}")

class CreateEmployeeRequest(BaseModel):
    name: str
    email: str
    role: str
    department: Optional[str] = None

@router.post("/employees/create")
async def create_employee(
    request: CreateEmployeeRequest,
    current_user: dict = Depends(get_current_user)
):
    """Create a new employee"""
    if current_user["role"] not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Only admins can create employees")
    
    try:
        normalized_email = (request.email or "").strip().lower()
        # Validate role
        if request.role not in MANAGED_EMPLOYEE_ROLES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid role. Must be one of: {', '.join(MANAGED_EMPLOYEE_ROLES)}"
            )
        
        # Check if employee exists
        check_query = "SELECT employee_id FROM employees WHERE LOWER(email) = LOWER(:email)"
        existing = await database.fetch_one(check_query, {"email": normalized_email})
        
        if existing:
            raise HTTPException(status_code=400, detail="Employee with this email already exists")
        
        # Generate employee ID and temporary password
        employee_id = f"emp_{uuid.uuid4().hex[:8]}"
        temp_password = generate_temp_password()
        hashed_password = hash_password(temp_password)
        user_password_hash = hash_user_password(temp_password)
        
        # Insert employee with password
        insert_query = """
            INSERT INTO employees (
                employee_id, name, email, password, role, department, status, created_at
            ) VALUES (
                :employee_id, :name, :email, :password, :role, :department, :status, :created_at
            )
        """
        
        await database.execute(insert_query, {
            "employee_id": employee_id,
            "name": request.name,
            "email": normalized_email,
            "password": hashed_password,
            "role": request.role,
            "department": request.department,
            "status": "active",
            "created_at": datetime.now()
        })

        # Best-effort: sync employee into users table for auth login
        try:
            users_insert = """
                INSERT INTO users (
                    email, password_hash, full_name, role, active, merchant_id
                ) VALUES (
                    :email, :password_hash, :full_name, :role, :active, NULL
                )
                ON CONFLICT (email) DO UPDATE SET
                    password_hash = EXCLUDED.password_hash,
                    full_name = EXCLUDED.full_name,
                    role = EXCLUDED.role,
                    active = EXCLUDED.active,
                    merchant_id = NULL
            """
            await database.execute(users_insert, {
                "email": normalized_email,
                "password_hash": user_password_hash,
                "full_name": request.name,
                "role": request.role,
                "active": True,
            })
        except Exception as exc:
            logger.error("[Employees] Failed to sync users table: %s", exc)

        try:
            await upsert_membership(
                email=normalized_email,
                membership_type="employee",
                role=request.role,
                entity_id=employee_id,
                status="active",
                permissions=[],
                full_name=request.name,
                password_hash=user_password_hash,
                credential_source="employee_create",
                source="employees_create",
            )
        except Exception as exc:
            logger.error("[Employees] Failed to sync auth membership: %s", exc)

        email_status = _send_employee_welcome_email(
            normalized_email,
            request.name,
            temp_password,
        )

        response = {
            "status": "success",
            "message": "Employee created successfully",
            "employee_id": employee_id,
            "email_status": email_status,
        }

        if email_status == "sent":
            response["note"] = (
                "Temporary password sent to the employee via email. "
                "They should change it on first login."
            )
        else:
            response["temporary_password"] = temp_password
            response["note"] = (
                "Email delivery was not confirmed. "
                "Please share this temporary password with the employee. "
                "They should change it on first login."
            )

        return response
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create employee: {str(e)}")

@router.put("/employees/{employee_id}")
async def update_employee(
    employee_id: str,
    name: Optional[str] = None,
    role: Optional[str] = None,
    department: Optional[str] = None,
    status: Optional[str] = None,
    current_user: dict = Depends(get_current_user)
):
    """Update employee information"""
    if current_user["role"] not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Only admins can update employees")
    
    try:
        # Build update query
        updates = []
        params = {"employee_id": employee_id}
        
        if name:
            updates.append("name = :name")
            params["name"] = name
        if role:
            if role not in MANAGED_EMPLOYEE_ROLES:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid role. Must be one of: {', '.join(MANAGED_EMPLOYEE_ROLES)}"
                )
            updates.append("role = :role")
            params["role"] = role
        if department:
            updates.append("department = :department")
            params["department"] = department
        if status:
            updates.append("status = :status")
            params["status"] = status
        
        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")
        
        update_query = f"""
            UPDATE employees
            SET {', '.join(updates)}
            WHERE employee_id = :employee_id
        """
        
        await database.execute(update_query, params)

        # Best-effort: keep canonical users table in sync for /api/auth/login.
        try:
            employee_row = await database.fetch_one(
                "SELECT employee_id, name, email, role, status FROM employees WHERE employee_id = :employee_id",
                {"employee_id": employee_id},
            )
            if not employee_row:
                employee_row = await database.fetch_one(
                    "SELECT email FROM employees WHERE employee_id = :employee_id",
                    {"employee_id": employee_id},
                )
            if employee_row:
                employee_row = dict(employee_row)
                employee_email = employee_row["email"]
                user_updates = []
                user_params = {"email": employee_email}

                if name:
                    user_updates.append("full_name = :full_name")
                    user_params["full_name"] = name
                if role:
                    user_updates.append("role = :role")
                    user_params["role"] = role
                if status:
                    user_updates.append("active = :active")
                    user_params["active"] = status == "active"

                if user_updates:
                    await database.execute(
                        f"UPDATE users SET {', '.join(user_updates)} WHERE email = :email",
                        user_params,
                    )
                if all(employee_row.get(key) for key in ("employee_id", "role")):
                    await upsert_membership(
                        email=employee_email,
                        membership_type="employee",
                        role=employee_row["role"],
                        entity_id=employee_row["employee_id"],
                        status=employee_row.get("status") or "active",
                        permissions=[],
                        full_name=employee_row.get("name"),
                        source="employees_update",
                    )
                else:
                    logger.info("[Employees] Skipping canonical membership update; employee row missing role/id")
        except Exception as exc:
            logger.error("[Employees] Failed to sync users table on update: %s", exc)

        return {
            "status": "success",
            "message": "Employee updated successfully"
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update employee: {str(e)}")

@router.delete("/employees/{employee_id}")
async def delete_employee(
    employee_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Deactivate an employee"""
    if current_user["role"] not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Only admins can delete employees")
    
    try:
        update_query = """
            UPDATE employees
            SET status = 'inactive'
            WHERE employee_id = :employee_id
        """
        
        await database.execute(update_query, {"employee_id": employee_id})

        # Best-effort: deactivate corresponding users account for /api/auth/login.
        try:
            employee_row = await database.fetch_one(
                "SELECT employee_id, name, email, role, status FROM employees WHERE employee_id = :employee_id",
                {"employee_id": employee_id},
            )
            if not employee_row:
                employee_row = await database.fetch_one(
                    "SELECT email FROM employees WHERE employee_id = :employee_id",
                    {"employee_id": employee_id},
                )
            if employee_row:
                employee_row = dict(employee_row)
                await database.execute(
                    "UPDATE users SET active = :active WHERE email = :email",
                    {"active": False, "email": employee_row["email"]},
                )
                if all(employee_row.get(key) for key in ("employee_id", "role")):
                    await upsert_membership(
                        email=employee_row["email"],
                        membership_type="employee",
                        role=employee_row["role"],
                        entity_id=employee_row["employee_id"],
                        status="inactive",
                        permissions=[],
                        full_name=employee_row.get("name"),
                        source="employees_delete",
                    )
                else:
                    logger.info("[Employees] Skipping canonical membership deactivate; employee row missing role/id")
        except Exception as exc:
            logger.error("[Employees] Failed to sync users table on delete: %s", exc)

        return {
            "status": "success",
            "message": "Employee deactivated successfully"
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to deactivate employee: {str(e)}")

# ============== Security ==============

@router.get("/security/audit-logs")
async def get_audit_logs(
    limit: int = 50,
    current_user: dict = Depends(get_current_user)
):
    """Get security audit logs"""
    if current_user["role"] not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Only admins can view audit logs")
    
    try:
        # Create audit_logs table if not exists
        create_table_query = """
            CREATE TABLE IF NOT EXISTS audit_logs (
                log_id VARCHAR(50) PRIMARY KEY,
                user_id VARCHAR(50),
                user_email VARCHAR(100),
                action VARCHAR(100) NOT NULL,
                resource VARCHAR(100),
                ip_address VARCHAR(50),
                status VARCHAR(20),
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        """
        await database.execute(create_table_query)
        
        logs_query = """
            SELECT * FROM audit_logs
            ORDER BY created_at DESC
            LIMIT :limit
        """
        logs = await database.fetch_all(logs_query, {"limit": limit})
        
        # If no logs, return demo logs
        if not logs:
            demo_logs = [
                {
                    "log_id": f"log_{i}",
                    "user_email": "employee@pivota.com",
                    "action": action,
                    "resource": "merchant_onboarding",
                    "ip_address": "192.168.1.1",
                    "status": "success",
                    "created_at": (datetime.now() - timedelta(hours=i)).isoformat()
                }
                for i, action in enumerate(["login", "view_merchant", "update_kyb", "create_merchant", "logout"])
            ]
            return {
                "status": "success",
                "logs": demo_logs
            }
        
        return {
            "status": "success",
            "logs": [
                {
                    "log_id": log["log_id"],
                    "user_email": log["user_email"],
                    "action": log["action"],
                    "resource": log["resource"],
                    "ip_address": log["ip_address"],
                    "status": log["status"],
                    "created_at": log["created_at"].isoformat() if log["created_at"] else None
                }
                for log in logs
            ]
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get audit logs: {str(e)}")

@router.get("/security/api-keys")
async def get_api_keys(
    current_user: dict = Depends(get_current_user)
):
    """Get API keys"""
    if current_user["role"] not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Only admins can view API keys")
    
    try:
        # Get all agents' API keys
        agents_query = """
            SELECT agent_id, name, email, api_key, created_at, last_active
            FROM agents
            WHERE status = 'active'
        """
        agents = await database.fetch_all(agents_query)
        
        return {
            "status": "success",
            "api_keys": [
                {
                    "key_id": agent["agent_id"],
                    "name": f"{agent['name']} (Agent)",
                    "email": agent["email"],
                    "key": agent["api_key"][:20] + "..." if agent["api_key"] else None,
                    "created_at": agent["created_at"].isoformat() if agent["created_at"] else None,
                    "last_used": agent["last_active"].isoformat() if agent["last_active"] else None
                }
                for agent in agents
            ]
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get API keys: {str(e)}")

@router.get("/security/settings")
async def get_security_settings(
    current_user: dict = Depends(get_current_user)
):
    """Get security settings"""
    if current_user["role"] not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Only admins can view security settings")
    
    return {
        "status": "success",
        "settings": {
            "two_factor_enabled": False,
            "password_policy": {
                "min_length": 8,
                "require_uppercase": True,
                "require_numbers": True,
                "require_special_chars": True
            },
            "session_timeout": 3600,  # seconds
            "max_login_attempts": 5,
            "ip_whitelist_enabled": False,
            "audit_log_retention": 90  # days
        }
    }

@router.put("/security/settings")
async def update_security_settings(
    two_factor_enabled: Optional[bool] = None,
    session_timeout: Optional[int] = None,
    max_login_attempts: Optional[int] = None,
    current_user: dict = Depends(get_current_user)
):
    """Update security settings"""
    if current_user["role"] not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Only admins can update security settings")
    
    # In production, this would update settings in database
    return {
        "status": "success",
        "message": "Security settings updated successfully",
        "updated_settings": {
            "two_factor_enabled": two_factor_enabled,
            "session_timeout": session_timeout,
            "max_login_attempts": max_login_attempts
        }
    }
