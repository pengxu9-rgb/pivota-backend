"""
Ultra-simple POST test router
"""

from fastapi import APIRouter

# Create ultra-simple router
simple_post_router = APIRouter(prefix="/simple", tags=["simple"])

@simple_post_router.get("/")
async def simple_get():
    """Ultra-simple GET"""
    return {"method": "GET", "status": "ok"}

@simple_post_router.post("/")
async def simple_post():
    """Ultra-simple POST"""
    return {"method": "POST", "status": "ok"}

@simple_post_router.options("/")
async def simple_options():
    """Ultra-simple OPTIONS for CORS"""
    return {"method": "OPTIONS", "status": "ok"}
