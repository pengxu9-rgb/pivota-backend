"""
Test routes to debug POST method issues
"""

from fastapi import APIRouter

# Create a separate test router
test_router = APIRouter(prefix="/test", tags=["test"])

@test_router.get("/get-test")
async def test_get():
    """Test GET method"""
    return {"status": "success", "message": "GET works in test router"}

@test_router.post("/post-test")
async def test_post():
    """Test POST method"""
    return {"status": "success", "message": "POST works in test router"}

@test_router.post("/post-test-with-data")
async def test_post_with_data(data: dict):
    """Test POST method with data"""
    return {"status": "success", "message": "POST with data works", "received": data}


