from fastapi import APIRouter, Depends, HTTPException
from db.database import database
from routes.auth_routes import require_admin
import logging

router = APIRouter(prefix="/admin/migrations", tags=["Admin Migrations"])
logger = logging.getLogger(__name__)

ALTER_SQL = [
    """
    ALTER TABLE orders 
    ADD COLUMN IF NOT EXISTS payment_intent_id VARCHAR(255),
    ADD COLUMN IF NOT EXISTS client_secret TEXT;
    """,
    """
    ALTER TABLE agents
    ADD COLUMN IF NOT EXISTS total_requests INTEGER DEFAULT 0;
    """
]

@router.post("/apply-psp-fixes")
async def apply_psp_schema_fixes(current_user: dict = Depends(require_admin)):
    try:
        async with database.transaction():
            for stmt in ALTER_SQL:
                await database.execute(stmt)
        return {"status": "success", "message": "PSP columns ensured"}
    except Exception as e:
        logger.error(f"Migration failed: {e}")
        raise HTTPException(status_code=500, detail=f"Migration failed: {str(e)}")
