"""
Merchant - View Agent Bank Details
Allows merchants to view agent bank details if agent has enabled sharing
"""

from fastapi import APIRouter, Depends, HTTPException, Path
from typing import Optional
from db.database import database
from utils.auth import get_current_user
import logging

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/merchants/{merchant_id}/agents",
    tags=["Merchant Agent Bank"]
)

@router.get("/{agent_id}/bank-details")
async def get_agent_bank_details(
    merchant_id: str,
    agent_id: str,
    current_user: dict = Depends(get_current_user)
):
    """
    Get agent's bank details for payout purposes
    Only returns data if agent has enabled sharing (allow_share_with_merchants = true)
    """
    try:
        # Get agent info
        agent = await database.fetch_one(
            "SELECT agent_id, name, email FROM agents WHERE agent_id = :agent_id",
            {"agent_id": agent_id}
        )
        
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        
        # Get bank details - only if sharing is enabled
        bank_details = await database.fetch_one(
            """
            SELECT 
                id,
                method,
                currency,
                account_holder_name,
                iban_preview,
                account_number_last4,
                bank_name,
                bank_country,
                verify_status,
                allow_share_with_merchants,
                updated_at
            FROM agent_beneficiaries
            WHERE agent_id = :agent_id
            ORDER BY 
                CASE WHEN allow_share_with_merchants THEN 0 ELSE 1 END,
                verified_at DESC NULLS LAST,
                created_at DESC
            LIMIT 1
            """,
            {"agent_id": agent_id}
        )
        
        if not bank_details:
            return {
                "status": "not_configured",
                "message": "Agent has not set up bank account yet",
                "agent": {
                    "agent_id": agent["agent_id"],
                    "name": agent["name"],
                    "email": agent["email"]
                },
                "bank_details": None,
                "sharing_enabled": False
            }
        
        # Check if sharing is enabled
        sharing_enabled = bank_details["allow_share_with_merchants"]
        
        if not sharing_enabled:
            return {
                "status": "not_shared",
                "message": "Agent has not enabled bank details sharing with merchants",
                "agent": {
                    "agent_id": agent["agent_id"],
                    "name": agent["name"],
                    "email": agent["email"]
                },
                "bank_details": {
                    "configured": True,
                    "method": bank_details["method"],
                    "currency": bank_details["currency"],
                    "verify_status": bank_details["verify_status"]
                },
                "sharing_enabled": False,
                "instructions": f"Please contact {agent['email']} directly to obtain bank account details for payment."
            }
        
        # Sharing is enabled - return bank details
        return {
            "status": "success",
            "message": "Bank details available",
            "agent": {
                "agent_id": agent["agent_id"],
                "name": agent["name"],
                "email": agent["email"]
            },
            "bank_details": {
                "method": bank_details["method"],
                "currency": bank_details["currency"],
                "account_holder_name": bank_details["account_holder_name"],
                "iban_preview": bank_details["iban_preview"],
                "account_number_last4": bank_details["account_number_last4"],
                "bank_name": bank_details["bank_name"],
                "bank_country": bank_details["bank_country"],
                "verify_status": bank_details["verify_status"],
                "last_updated": bank_details["updated_at"].isoformat() if bank_details["updated_at"] else None
            },
            "sharing_enabled": True
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get agent bank details: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve bank details")
