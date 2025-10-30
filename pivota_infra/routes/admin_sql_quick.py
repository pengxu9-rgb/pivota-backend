"""Quick SQL execution for admin fixes"""
from fastapi import APIRouter
from pydantic import BaseModel
import logging
from db.database import database
from sqlalchemy import text

router = APIRouter(prefix="/admin/sql", tags=["admin-sql"])
logger = logging.getLogger(__name__)

class SQLRequest(BaseModel):
    sql: str
    confirm: bool = False

@router.post("/execute")
async def execute_sql(request: SQLRequest):
    if not request.confirm:
        return {"error": "Must confirm"}
    try:
        result = await database.execute(text(request.sql))
        return {"success": True, "affected_rows": result if result else 0}
    except Exception as e:
        return {"success": False, "error": str(e)}

