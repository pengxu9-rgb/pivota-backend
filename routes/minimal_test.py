"""
Minimal test to debug POST method issues
"""

from fastapi import APIRouter
from pydantic import BaseModel

# Create minimal test router
minimal_router = APIRouter(prefix="/minimal", tags=["minimal"])

@minimal_router.get("/")
async def minimal_get():
    """Minimal GET test"""
    return {"status": "success", "method": "GET"}

@minimal_router.post("/")
async def minimal_post():
    """Minimal POST test"""
    return {"status": "success", "method": "POST"}

@minimal_router.post("/data")
async def minimal_post_data():
    """Minimal POST with no body"""
    return {"status": "success", "method": "POST", "data": "none"}

class TestData(BaseModel):
    test: str

@minimal_router.post("/json")
async def minimal_post_json(data: TestData):
    """Minimal POST with JSON body"""
    return {"status": "success", "method": "POST", "received": data.test}
