"""
Admin SQL execution endpoint for quick fixes
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List
import logging

from db.database import database
from sqlalchemy import text

router = APIRouter(prefix="/admin/sql", tags=["admin-sql"])
logger = logging.getLogger(__name__)


class SQLExecuteRequest(BaseModel):
    sql: str
    confirm: bool = False


@router.post("/execute")
async def execute_sql(request: SQLExecuteRequest):
    """Execute SQL statement - NO AUTH for testing"""
    if not request.confirm:
        return {"error": "Must confirm execution"}
    
    try:
        # Execute the SQL
        result = await database.execute(text(request.sql))
        
        logger.info(f"Executed SQL: {request.sql[:100]}...")
        
        return {
            "success": True,
            "message": "SQL executed successfully",
            "affected_rows": result if result else 0
        }
        
    except Exception as e:
        logger.error(f"Error executing SQL: {str(e)}")
        return {
            "success": False,
            "error": str(e)
        }


@router.post("/query")
async def query_sql(request: SQLExecuteRequest):
    """Query SQL and return results - NO AUTH for testing"""
    try:
        # Execute query
        rows = await database.fetch_all(text(request.sql))
        
        # Convert to list of dicts
        result = [dict(row) for row in rows]
        
        logger.info(f"Queried SQL: {request.sql[:100]}... returned {len(result)} rows")
        
        return {
            "success": True,
            "rows": result,
            "count": len(result)
        }
        
    except Exception as e:
        logger.error(f"Error querying SQL: {str(e)}")
        return {
            "success": False,
            "error": str(e)
        }
