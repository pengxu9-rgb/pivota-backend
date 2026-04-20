"""PSP telemetry endpoints."""
from fastapi import APIRouter, Depends, HTTPException
from db.database import database
from utils.auth import get_current_user

router = APIRouter()

PAYMENT_TELEMETRY_NOT_REPORTED = "Payment telemetry not reported"

@router.get("/merchant/psps/{psp_id}/metrics")
async def get_psp_metrics(
    psp_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get measured telemetry for a specific PSP when available."""
    if current_user["role"] not in ["merchant", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Get PSP info
        psp_query = "SELECT provider FROM merchant_psps WHERE psp_id = :psp_id"
        psp = await database.fetch_one(psp_query, {"psp_id": psp_id})
        
        if not psp:
            return {
                "payment_telemetry_reported": False,
                "message": PAYMENT_TELEMETRY_NOT_REPORTED
            }
        
        provider = psp["provider"]

        return {
            "provider": provider,
            "payment_telemetry_reported": False,
            "message": PAYMENT_TELEMETRY_NOT_REPORTED
        }
    except Exception as e:
        return {
            "payment_telemetry_reported": False,
            "message": PAYMENT_TELEMETRY_NOT_REPORTED,
            "error": str(e)
        }

@router.get("/merchant/psps/metrics/all")
async def get_all_psp_metrics(current_user: dict = Depends(get_current_user)):
    """Get measured telemetry for all PSPs of this merchant when available."""
    if current_user["role"] not in ["merchant", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    merchant_id = current_user.get("merchant_id", "merch_6b90dc9838d5fd9c")
    
    try:
        # Get all PSPs
        psps_query = "SELECT psp_id, provider FROM merchant_psps WHERE merchant_id = :merchant_id"
        psps = await database.fetch_all(psps_query, {"merchant_id": merchant_id})
        
        metrics = {}
        for psp in psps:
            psp_id = psp["psp_id"]
            provider = psp["provider"]

            metrics[psp_id] = {
                "provider": provider,
                "payment_telemetry_reported": False,
                "message": PAYMENT_TELEMETRY_NOT_REPORTED
            }
        
        return {
            "status": "success",
            "data": metrics
        }
    except Exception as e:
        return {
            "status": "error",
            "message": str(e)
        }
