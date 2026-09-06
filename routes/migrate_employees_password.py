from fastapi import APIRouter, Depends, HTTPException
from db.database import database
from utils.auth import ADMIN_ROLES, get_current_user
import logging

router = APIRouter(prefix="/admin/migrate", tags=["admin-migrate"])
logger = logging.getLogger(__name__)

@router.post("/employees-add-password")
async def migrate_employees_add_password(
    current_user: dict = Depends(get_current_user)
):
    """Add password column to employees table"""
    # Was ["admin", "superadmin"] -- "superadmin" is not a role this system can
    # mint (canonical spelling is `super_admin`, see utils.auth.ADMIN_ROLES and
    # routes.auth._validate_role_value), so that arm never matched and a real
    # super_admin was denied. `admin` keeps the access it already had.
    if current_user["role"] not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin access required")
    
    try:
        # Add password column if it doesn't exist
        alter_query = """
            ALTER TABLE employees 
            ADD COLUMN IF NOT EXISTS password VARCHAR(255)
        """
        await database.execute(alter_query)
        
        logger.info("Added password column to employees table")
        
        return {
            "status": "success",
            "message": "Password column added to employees table",
            "note": "Existing employees will have NULL passwords until reset"
        }
        
    except Exception as e:
        logger.error(f"Error migrating employees table: {e}")
        raise HTTPException(status_code=500, detail=str(e))


