from fastapi import APIRouter, Depends, HTTPException
from db.database import database
from utils.auth import get_current_user
import logging

router = APIRouter(prefix="/admin/migrate", tags=["admin-migrate"])
logger = logging.getLogger(__name__)

@router.post("/employees-add-password")
async def migrate_employees_add_password(
    current_user: dict = Depends(get_current_user)
):
    """Add password column to employees table"""
    if current_user["role"] not in ["admin", "superadmin"]:
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


