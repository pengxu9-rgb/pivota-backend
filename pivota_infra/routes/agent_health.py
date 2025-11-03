"""Agent health check endpoint"""
from fastapi import APIRouter, Request
from datetime import datetime

router = APIRouter(prefix="/agent", tags=["agent-health"])

@router.get("/health")
async def health_check(request: Request):
    """Simple health check for agent portal"""
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "service": "agent-api",
        "version": "1.0.0"
    }


