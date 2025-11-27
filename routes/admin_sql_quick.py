"""Quick SQL execution for admin fixes (now disabled for safety)"""
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
import logging
from db.database import database
from sqlalchemy import text

router = APIRouter(prefix="/admin/sql", tags=["admin-sql"])
logger = logging.getLogger(__name__)

DISABLED_MSG = "admin/sql/execute has been disabled for production safety"

class SQLRequest(BaseModel):
    sql: str
    confirm: bool = False

@router.post("/execute")
async def execute_sql(request: SQLRequest):
    """
    Disabled endpoint; returns 404 to prevent arbitrary SQL execution.
    """
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=DISABLED_MSG
    )

