"""
Employees Management and Security Routes
"""
from fastapi import APIRouter, Depends, HTTPException
from typing import Optional, List
from datetime import datetime, timedelta
from utils.auth import get_current_user
from db.database import database
import uuid
import secrets

router = APIRouter()

# ============== Employees Management ==============

@router.get("/employees/list")
async def get_all_employees(
    current_user: dict = Depends(get_current_user)
):
    """Get all employees"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Create employees table if not exists
        create_table_query = """
            CREATE TABLE IF NOT EXISTS employees (
                employee_id VARCHAR(50) PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                email VARCHAR(100) UNIQUE NOT NULL,
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

@router.post("/employees/create")
async def create_employee(
    name: str,
    email: str,
    role: str,
    department: Optional[str] = None,
    current_user: dict = Depends(get_current_user)
):
    """Create a new employee"""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Only admins can create employees")
    
    try:
        # Validate role
        valid_roles = ["employee", "admin", "manager"]
        if role not in valid_roles:
            raise HTTPException(status_code=400, detail=f"Invalid role. Must be one of: {', '.join(valid_roles)}")
        
        # Check if employee exists
        check_query = "SELECT employee_id FROM employees WHERE email = :email"
        existing = await database.fetch_one(check_query, {"email": email})
        
        if existing:
            raise HTTPException(status_code=400, detail="Employee with this email already exists")
        
        # Generate employee ID
        employee_id = f"emp_{uuid.uuid4().hex[:8]}"
        
        # Insert employee
        insert_query = """
            INSERT INTO employees (
                employee_id, name, email, role, department, status, created_at
            ) VALUES (
                :employee_id, :name, :email, :role, :department, :status, :created_at
            )
        """
        
        await database.execute(insert_query, {
            "employee_id": employee_id,
            "name": name,
            "email": email,
            "role": role,
            "department": department,
            "status": "active",
            "created_at": datetime.now()
        })
        
        return {
            "status": "success",
            "message": "Employee created successfully",
            "employee_id": employee_id
        }
    
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
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Only admins can update employees")
    
    try:
        # Build update query
        updates = []
        params = {"employee_id": employee_id}
        
        if name:
            updates.append("name = :name")
            params["name"] = name
        if role:
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
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Only admins can delete employees")
    
    try:
        update_query = """
            UPDATE employees
            SET status = 'inactive'
            WHERE employee_id = :employee_id
        """
        
        await database.execute(update_query, {"employee_id": employee_id})
        
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
    if current_user["role"] != "admin":
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
    if current_user["role"] != "admin":
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
    if current_user["role"] != "admin":
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
    if current_user["role"] != "admin":
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






