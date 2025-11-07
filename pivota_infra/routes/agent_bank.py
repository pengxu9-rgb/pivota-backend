"""
Agent Bank Account Management Routes
Allows agents to manage their bank account information
Phase 6 - Payouts & Banking
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, validator
from typing import Optional, List
from datetime import datetime
import logging
import re

from db.database import database
from db.beneficiary_repo import BeneficiaryRepo
from utils.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents/{agent_id}/bank", tags=["agent-bank"])

# Request/Response Models
class BankDetailsRequest(BaseModel):
    method: Optional[str] = Field("bank_wire", description="Payment method: bank_wire, ach, sepa, etc.")
    currency: Optional[str] = Field("USD", description="Currency code (ISO 4217)")
    account_holder_name: Optional[str] = Field(None, max_length=255)
    
    # International fields
    iban: Optional[str] = Field(None, max_length=34)
    swift_bic: Optional[str] = Field(None, max_length=11)
    bank_name: Optional[str] = Field(None, max_length=255)
    bank_country: Optional[str] = Field(None, max_length=2, description="ISO 3166-1 alpha-2 country code")
    
    # US domestic fields
    account_number: Optional[str] = Field(None, max_length=34)
    routing_number: Optional[str] = Field(None, max_length=20)
    
    # Permissions
    allow_share_with_merchants: bool = Field(False, description="Allow merchants to see bank details")
    
    @validator('iban')
    def validate_iban(cls, v):
        if v:
            # Remove spaces and convert to uppercase
            v = v.replace(' ', '').upper()
            # Basic IBAN validation (length and format)
            if not re.match(r'^[A-Z]{2}[0-9]{2}[A-Z0-9]+$', v):
                raise ValueError('Invalid IBAN format')
            if len(v) < 15 or len(v) > 34:
                raise ValueError('IBAN must be between 15 and 34 characters')
        return v
    
    @validator('swift_bic')
    def validate_swift(cls, v):
        if v:
            v = v.upper()
            # Basic SWIFT/BIC validation
            if not re.match(r'^[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?$', v):
                raise ValueError('Invalid SWIFT/BIC format')
        return v
    
    @validator('routing_number')
    def validate_routing(cls, v):
        if v and v.isdigit() and len(v) == 9:
            # US routing number checksum validation
            check_sum = 0
            for i in range(0, len(v), 3):
                check_sum += int(v[i]) * 3
                check_sum += int(v[i + 1]) * 7
                check_sum += int(v[i + 2]) * 1
            if check_sum % 10 != 0:
                raise ValueError('Invalid US routing number')
        return v

class BankDetailsResponse(BaseModel):
    id: int
    agent_id: str
    method: str
    currency: str
    account_holder_name: Optional[str]
    
    # Display fields (masked)
    iban_preview: Optional[str]
    account_number_last4: Optional[str]
    
    # Bank info
    bank_name: Optional[str]
    bank_country: Optional[str]
    
    # Status
    verify_status: str
    verified_at: Optional[datetime]
    allow_share_with_merchants: bool
    
    # Timestamps
    created_at: datetime
    updated_at: datetime

@router.get("", response_model=dict)
async def get_bank_details(
    agent_id: str,
    current_user: dict = Depends(get_current_user)
):
    """
    Get agent's bank account details
    Returns masked account information for security
    """
    # Verify agent access
    if current_user.get("role") != "agent":
        raise HTTPException(status_code=403, detail="Only agents can access this endpoint")
    
    user_agent_id = current_user.get("agent_id") or current_user.get("email")
    if user_agent_id != agent_id:
        raise HTTPException(status_code=403, detail="Not authorized to view these bank details")
    
    try:
        repo = BeneficiaryRepo()
        details = await repo.get_default(agent_id)
        
        if not details:
            return {
                "status": "success",
                "bank_details": None,
                "message": "No bank account configured"
            }
        
        # Format response with masked data
        response = BankDetailsResponse(
            id=details["id"],
            agent_id=details["agent_id"],
            method=details["method"],
            currency=details["currency"],
            account_holder_name=details.get("account_holder_name"),
            iban_preview=details.get("iban_preview"),
            account_number_last4=details.get("account_number_last4"),
            bank_name=details.get("bank_name"),
            bank_country=details.get("bank_country"),
            verify_status=details.get("verify_status", "unverified"),
            verified_at=details.get("verified_at"),
            allow_share_with_merchants=details.get("allow_share_with_merchants", False),
            created_at=details["created_at"],
            updated_at=details["updated_at"]
        )
        
        return {
            "status": "success",
            "bank_details": response.dict()
        }
        
    except Exception as e:
        logger.error(f"Error getting bank details for agent {agent_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to get bank details")

@router.put("", response_model=dict)
async def update_bank_details(
    agent_id: str,
    request: BankDetailsRequest,
    current_user: dict = Depends(get_current_user)
):
    """
    Create or update agent's bank account details
    """
    # Verify agent access
    if current_user.get("role") != "agent":
        raise HTTPException(status_code=403, detail="Only agents can access this endpoint")
    
    user_agent_id = current_user.get("agent_id") or current_user.get("email")
    if user_agent_id != agent_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Validate that either IBAN or account number is provided
        if not request.iban and not request.account_number:
            raise HTTPException(
                status_code=400,
                detail="Either IBAN or account number must be provided"
            )
        
        # If US account, routing number is required
        if request.account_number and not request.routing_number:
            raise HTTPException(
                status_code=400,
                detail="Routing number is required for US bank accounts"
            )
        
        # Create or update beneficiary
        repo = BeneficiaryRepo()
        beneficiary_id = await repo.upsert_default(agent_id, request.dict())
        
        logger.info(f"Agent {agent_id} updated bank details (beneficiary_id: {beneficiary_id})")
        
        # Get updated details to return
        updated = await repo.get_default(agent_id)
        
        return {
            "status": "success",
            "beneficiary_id": beneficiary_id,
            "message": "Bank details updated successfully",
            "bank_details": {
                "id": updated["id"],
                "method": updated["method"],
                "currency": updated["currency"],
                "account_holder_name": updated.get("account_holder_name"),
                "iban_preview": updated.get("iban_preview"),
                "account_number_last4": updated.get("account_number_last4"),
                "bank_name": updated.get("bank_name"),
                "bank_country": updated.get("bank_country"),
                "verify_status": updated.get("verify_status", "unverified"),
                "allow_share_with_merchants": updated.get("allow_share_with_merchants", False)
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating bank details: {e}")
        raise HTTPException(status_code=500, detail="Failed to update bank details")

@router.patch("/share", response_model=dict)
async def toggle_share_permission(
    agent_id: str,
    allow: bool = Query(..., description="Whether to allow merchants to see bank details"),
    current_user: dict = Depends(get_current_user)
):
    """
    Toggle permission for merchants to see bank details
    """
    # Verify agent access
    if current_user.get("role") != "agent":
        raise HTTPException(status_code=403, detail="Only agents can access this endpoint")
    
    user_agent_id = current_user.get("agent_id") or current_user.get("email")
    if user_agent_id != agent_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        repo = BeneficiaryRepo()
        
        # Check if bank details exist
        existing = await repo.get_default(agent_id)
        if not existing:
            raise HTTPException(
                status_code=404,
                detail="No bank details found. Please add bank details first."
            )
        
        # Update permission
        await repo.set_share(agent_id, allow)
        
        logger.info(f"Agent {agent_id} set bank share permission to: {allow}")
        
        return {
            "status": "success",
            "allow_share_with_merchants": allow,
            "message": f"Bank details sharing {'enabled' if allow else 'disabled'}"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error toggling share permission: {e}")
        raise HTTPException(status_code=500, detail="Failed to update sharing permission")

@router.delete("", response_model=dict)
async def delete_bank_details(
    agent_id: str,
    current_user: dict = Depends(get_current_user)
):
    """
    Delete agent's bank account details
    """
    # Verify agent access
    if current_user.get("role") != "agent":
        raise HTTPException(status_code=403, detail="Only agents can access this endpoint")
    
    user_agent_id = current_user.get("agent_id") or current_user.get("email")
    if user_agent_id != agent_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        repo = BeneficiaryRepo()
        
        # Get existing details
        existing = await repo.get_default(agent_id)
        if not existing:
            raise HTTPException(status_code=404, detail="No bank details found")
        
        # Check if there are pending payouts
        pending_count = await database.fetch_val(
            query="SELECT COUNT(*) FROM agent_payouts WHERE agent_id = :aid AND status != 'paid'",
            values={"aid": agent_id}
        )
        
        if pending_count > 0:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot delete bank details. You have {pending_count} pending payouts."
            )
        
        # Delete the beneficiary
        await repo.delete(agent_id, existing["id"])
        
        logger.info(f"Agent {agent_id} deleted bank details")
        
        return {
            "status": "success",
            "message": "Bank details deleted successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting bank details: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete bank details")

@router.get("/methods", response_model=dict)
async def get_supported_methods(
    agent_id: str,
    current_user: dict = Depends(get_current_user)
):
    """
    Get list of supported payout methods for the agent's region
    """
    # Verify agent access  
    if current_user.get("role") != "agent":
        raise HTTPException(status_code=403, detail="Only agents can access this endpoint")
    
    user_agent_id = current_user.get("agent_id") or current_user.get("email")
    if user_agent_id != agent_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    # For now, return all supported methods
    # In production, this would check agent's country/region
    methods = [
        {
            "method": "bank_wire",
            "name": "Bank Wire Transfer",
            "currencies": ["USD", "EUR", "GBP", "CAD", "AUD"],
            "processing_time": "1-3 business days",
            "min_amount": 100.00
        },
        {
            "method": "ach",
            "name": "ACH Transfer (US Only)",
            "currencies": ["USD"],
            "processing_time": "2-3 business days",
            "min_amount": 10.00,
            "requirements": ["US bank account", "Routing number"]
        },
        {
            "method": "sepa",
            "name": "SEPA Transfer (EU Only)",
            "currencies": ["EUR"],
            "processing_time": "1-2 business days",
            "min_amount": 10.00,
            "requirements": ["IBAN", "BIC/SWIFT"]
        },
        {
            "method": "paypal",
            "name": "PayPal",
            "currencies": ["USD", "EUR", "GBP"],
            "processing_time": "Instant",
            "min_amount": 1.00,
            "requirements": ["PayPal email"]
        }
    ]
    
    return {
        "status": "success",
        "methods": methods
    }
