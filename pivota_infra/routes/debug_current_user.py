from fastapi import APIRouter, Depends
from utils.auth import get_current_user

router = APIRouter(prefix="/debug", tags=["debug"])

@router.get("/whoami")
async def whoami(current_user: dict = Depends(get_current_user)):
    """Debug endpoint to see current user info"""
    return {
        "status": "success",
        "user": current_user,
        "merchant_id": current_user.get("merchant_id"),
        "role": current_user.get("role"),
        "email": current_user.get("email"),
        "note": "This is your current login session info"
    }


