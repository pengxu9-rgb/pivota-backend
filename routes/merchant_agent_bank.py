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
        # Get agent info (only id; avoid missing column issues on name/email)
        agent = await database.fetch_one(
            "SELECT agent_id FROM agents WHERE agent_id = :agent_id",
            {"agent_id": agent_id}
        )
        agent = dict(agent) if agent else {}
        
        # Get bank details - only if sharing is enabled
        bank_details = await database.fetch_one(
            """
            SELECT 
                id,
                method,
                currency,
                account_holder_name,
                iban,
                swift_bic,
                iban_preview,
                account_number,
                routing_number,
                account_number_last4,
                bank_name,
                bank_country,
                verify_status,
                allow_share_with_merchants,
                updated_at,
                verified_at
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
        bank_details = dict(bank_details) if bank_details else None
        
        if not bank_details:
            return {
                "status": "not_configured",
                "message": "Agent has not set up bank account yet",
                "agent": {
                    "agent_id": agent_id,
                    "name": (agent or {}).get("name"),
                    "email": (agent or {}).get("email")
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
                    "agent_id": agent_id,
                    "name": agent.get("name"),
                    "email": agent.get("email")
                },
                "bank_details": {
                    "configured": True,
                    "method": bank_details["method"],
                    "currency": bank_details["currency"],
                    "verify_status": bank_details["verify_status"]
                },
                "sharing_enabled": False,
                "instructions": f"Please contact the agent directly to obtain bank account details for payment."
            }
        
        # Sharing is enabled - return FULL bank details
        bank_info = {
            "method": bank_details["method"],
            "currency": bank_details["currency"],
            "account_holder_name": bank_details["account_holder_name"],
            "bank_name": bank_details["bank_name"],
            "bank_country": bank_details["bank_country"],
            "verify_status": bank_details["verify_status"],
            "verified_at": bank_details["verified_at"].isoformat() if bank_details["verified_at"] else None,
            "last_updated": bank_details["updated_at"].isoformat() if bank_details["updated_at"] else None,
            # Include sensitive fields for merchant payout CSV/export
            "iban": bank_details.get("iban"),
            "iban_preview": bank_details.get("iban_preview"),
            "swift_bic": bank_details.get("swift_bic"),
            "account_number": bank_details.get("account_number"),
            "routing_number": bank_details.get("routing_number"),
            "account_number_last4": bank_details.get("account_number_last4"),
        }
        
        # Include full account details based on method
        if bank_details["method"] == "bank_wire" and bank_details["iban"]:
            bank_info["iban"] = bank_details["iban"]  # Full IBAN
            bank_info["swift_bic"] = bank_details["swift_bic"]
            bank_info["iban_preview"] = bank_details["iban_preview"]  # Keep preview for backward compatibility
        elif bank_details["method"] in ["ach", "wire"] and bank_details["account_number"]:
            bank_info["account_number"] = bank_details["account_number"]  # Full account number
            bank_info["routing_number"] = bank_details["routing_number"]
        else:
            # Fallback to preview versions
            bank_info["iban_preview"] = bank_details.get("iban_preview") or bank_details.get("account_number_last4")
            bank_info["account_number_last4"] = bank_details.get("account_number_last4")
        
        return {
            "status": "success",
            "message": "Bank details available",
            "agent": {
                "agent_id": agent_id,
                # Prefer stored agent name/email; fall back to account holder name for better UI
                "name": agent.get("name") or bank_details.get("account_holder_name"),
                "email": agent.get("email")
            },
            "bank_details": bank_info,
            "sharing_enabled": True
        }
        
    except HTTPException:
        raise
    except Exception as e:
        # Graceful fallback: do not break merchant portal if bank table/data missing
        logger.error(f"Failed to get agent bank details: {e}", exc_info=True)
        return {
            "status": "error",
            "message": f"Bank details unavailable: {str(e)}",
            "agent": {"agent_id": agent_id},
            "bank_details": None,
            "sharing_enabled": False
        }
