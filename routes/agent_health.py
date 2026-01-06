"""Agent health check endpoint"""
from fastapi import APIRouter, Request
from datetime import datetime
import os

router = APIRouter(prefix="/agent", tags=["agent-health"])

@router.get("/health")
async def health_check(request: Request):
    """Simple health check for agent portal"""
    checkout_secret = (os.getenv("CHECKOUT_TOKEN_SECRET") or os.getenv("AGENT_CHECKOUT_TOKEN_SECRET") or "").strip()
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "service": "agent-api",
        "version": "1.0.0",
        # Debug-friendly but non-sensitive: helps verify hosted checkout prerequisites.
        "checkout_token_secret_configured": bool(checkout_secret),
    }

