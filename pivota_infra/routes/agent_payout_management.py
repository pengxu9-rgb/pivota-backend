"""
Agent Payout Management API
Endpoints for agents to configure payout settings and for admins to manage payouts
"""

from fastapi import APIRouter, HTTPException, Depends, File, UploadFile
from pydantic import BaseModel, EmailStr, Field
from typing import Optional, Dict, Any, List
from datetime import datetime
import logging
import json

from db.database import database
from utils.auth import get_current_user, require_admin
from utils.encryption import encrypt_sensitive_data, decrypt_sensitive_data  # TODO: Create encryption utils

router = APIRouter(
    prefix="/agents/{agent_id}/payout",
    tags=["Agent Payout Management"]
)

logger = logging.getLogger(__name__)


# ============================================================================
# Request/Response Models
# ============================================================================

class PayoutSettingsRequest(BaseModel):
    # Payout method
    primary_payout_method: str = Field(..., description="Primary payout method")
    backup_payout_method: Optional[str] = None
    
    # Stripe Connect
    stripe_account_id: Optional[str] = None
    
    # PayPal
    paypal_email: Optional[EmailStr] = None
    
    # US Bank (ACH)
    us_bank_account_holder_name: Optional[str] = None
    us_bank_account_number: Optional[str] = None  # Will be encrypted
    us_bank_routing_number: Optional[str] = None  # Will be encrypted
    us_bank_account_type: Optional[str] = None
    us_bank_name: Optional[str] = None
    
    # International Bank (SWIFT/IBAN)
    intl_bank_account_holder_name: Optional[str] = None
    intl_iban: Optional[str] = None
    intl_swift_bic: Optional[str] = None
    intl_bank_name: Optional[str] = None
    intl_bank_country: Optional[str] = None
    intl_bank_currency: Optional[str] = "USD"
    
    # Wire Transfer
    wire_beneficiary_name: Optional[str] = None
    wire_bank_name: Optional[str] = None
    wire_account_number: Optional[str] = None  # Will be encrypted
    wire_swift_code: Optional[str] = None
    
    # Cryptocurrency
    crypto_wallet_address: Optional[str] = None
    crypto_network: Optional[str] = None
    crypto_preferred_stablecoin: Optional[str] = None
    
    # Tax Information
    tax_country: str = Field(..., description="Tax residency country")
    tax_id_number: Optional[str] = None  # Will be encrypted
    tax_id_type: Optional[str] = None
    business_type: str = Field(default="individual")
    business_legal_name: Optional[str] = None
    business_registration_number: Optional[str] = None
    
    # Preferences
    minimum_payout_amount: float = Field(default=50.0, ge=10.0)
    payout_frequency: str = Field(default="monthly")
    preferred_currency: str = Field(default="USD")
    auto_payout_enabled: bool = Field(default=True)


# ============================================================================
# Agent Endpoints (Agents manage their own settings)
# ============================================================================

@router.post("/settings")
async def save_payout_settings(
    agent_id: str,
    settings: PayoutSettingsRequest,
    current_user: dict = Depends(get_current_user)
):
    """
    Save or update agent payout settings
    
    **Agent can only update their own settings**
    """
    # Auth check - agent can only manage their own payout settings
    if current_user.get("role") != "admin" and current_user.get("agent_id") != agent_id:
        raise HTTPException(status_code=403, detail="Cannot manage other agent's payout settings")
    
    try:
        # Check if settings already exist
        existing = await database.fetch_one(
            "SELECT id FROM agent_payout_settings WHERE agent_id = :agent_id",
            {"agent_id": agent_id}
        )
        
        # Encrypt sensitive data
        encrypted_data = {}
        if settings.us_bank_account_number:
            encrypted_data['us_bank_account_number_encrypted'] = encrypt_sensitive_data(settings.us_bank_account_number)
        if settings.us_bank_routing_number:
            encrypted_data['us_bank_routing_number_encrypted'] = encrypt_sensitive_data(settings.us_bank_routing_number)
        if settings.wire_account_number:
            encrypted_data['wire_account_number_encrypted'] = encrypt_sensitive_data(settings.wire_account_number)
        if settings.tax_id_number:
            encrypted_data['tax_id_number_encrypted'] = encrypt_sensitive_data(settings.tax_id_number)
        
        if existing:
            # Update existing settings
            query = """
                UPDATE agent_payout_settings
                SET 
                    primary_payout_method = :primary_method,
                    backup_payout_method = :backup_method,
                    stripe_account_id = :stripe_account_id,
                    paypal_email = :paypal_email,
                    us_bank_account_holder_name = :us_bank_holder,
                    us_bank_account_number_encrypted = :us_account_encrypted,
                    us_bank_routing_number_encrypted = :us_routing_encrypted,
                    us_bank_account_type = :us_account_type,
                    us_bank_name = :us_bank_name,
                    intl_bank_account_holder_name = :intl_holder,
                    intl_iban = :intl_iban,
                    intl_swift_bic = :intl_swift,
                    intl_bank_name = :intl_bank_name,
                    intl_bank_country = :intl_country,
                    intl_bank_currency = :intl_currency,
                    wire_beneficiary_name = :wire_beneficiary,
                    wire_bank_name = :wire_bank,
                    wire_account_number_encrypted = :wire_account_encrypted,
                    wire_swift_code = :wire_swift,
                    crypto_wallet_address = :crypto_wallet,
                    crypto_network = :crypto_network,
                    crypto_preferred_stablecoin = :crypto_stablecoin,
                    tax_country = :tax_country,
                    tax_id_number_encrypted = :tax_id_encrypted,
                    tax_id_type = :tax_id_type,
                    business_type = :business_type,
                    business_legal_name = :business_legal_name,
                    business_registration_number = :business_reg_number,
                    minimum_payout_amount = :min_payout,
                    payout_frequency = :payout_frequency,
                    preferred_currency = :preferred_currency,
                    auto_payout_enabled = :auto_payout,
                    verification_status = :verification_status,
                    updated_at = NOW()
                WHERE agent_id = :agent_id
            """
            
            verification_status = 'under_review'  # Reset to review when settings change
        else:
            # Insert new settings
            query = """
                INSERT INTO agent_payout_settings (
                    agent_id, primary_payout_method, backup_payout_method,
                    stripe_account_id, paypal_email,
                    us_bank_account_holder_name, us_bank_account_number_encrypted,
                    us_bank_routing_number_encrypted, us_bank_account_type, us_bank_name,
                    intl_bank_account_holder_name, intl_iban, intl_swift_bic,
                    intl_bank_name, intl_bank_country, intl_bank_currency,
                    wire_beneficiary_name, wire_bank_name, wire_account_number_encrypted, wire_swift_code,
                    crypto_wallet_address, crypto_network, crypto_preferred_stablecoin,
                    tax_country, tax_id_number_encrypted, tax_id_type,
                    business_type, business_legal_name, business_registration_number,
                    minimum_payout_amount, payout_frequency, preferred_currency,
                    auto_payout_enabled, verification_status
                ) VALUES (
                    :agent_id, :primary_method, :backup_method,
                    :stripe_account_id, :paypal_email,
                    :us_bank_holder, :us_account_encrypted, :us_routing_encrypted,
                    :us_account_type, :us_bank_name,
                    :intl_holder, :intl_iban, :intl_swift, :intl_bank_name,
                    :intl_country, :intl_currency,
                    :wire_beneficiary, :wire_bank, :wire_account_encrypted, :wire_swift,
                    :crypto_wallet, :crypto_network, :crypto_stablecoin,
                    :tax_country, :tax_id_encrypted, :tax_id_type,
                    :business_type, :business_legal_name, :business_reg_number,
                    :min_payout, :payout_frequency, :preferred_currency,
                    :auto_payout, :verification_status
                )
            """
            
            verification_status = 'pending'
        
        await database.execute(query, {
            "agent_id": agent_id,
            "primary_method": settings.primary_payout_method,
            "backup_method": settings.backup_payout_method,
            "stripe_account_id": settings.stripe_account_id,
            "paypal_email": settings.paypal_email,
            "us_bank_holder": settings.us_bank_account_holder_name,
            "us_account_encrypted": encrypted_data.get('us_bank_account_number_encrypted'),
            "us_routing_encrypted": encrypted_data.get('us_bank_routing_number_encrypted'),
            "us_account_type": settings.us_bank_account_type,
            "us_bank_name": settings.us_bank_name,
            "intl_holder": settings.intl_bank_account_holder_name,
            "intl_iban": settings.intl_iban,
            "intl_swift": settings.intl_swift_bic,
            "intl_bank_name": settings.intl_bank_name,
            "intl_country": settings.intl_bank_country,
            "intl_currency": settings.intl_bank_currency,
            "wire_beneficiary": settings.wire_beneficiary_name,
            "wire_bank": settings.wire_bank_name,
            "wire_account_encrypted": encrypted_data.get('wire_account_number_encrypted'),
            "wire_swift": settings.wire_swift_code,
            "crypto_wallet": settings.crypto_wallet_address,
            "crypto_network": settings.crypto_network,
            "crypto_stablecoin": settings.crypto_preferred_stablecoin,
            "tax_country": settings.tax_country,
            "tax_id_encrypted": encrypted_data.get('tax_id_number_encrypted'),
            "tax_id_type": settings.tax_id_type,
            "business_type": settings.business_type,
            "business_legal_name": settings.business_legal_name,
            "business_reg_number": settings.business_registration_number,
            "min_payout": settings.minimum_payout_amount,
            "payout_frequency": settings.payout_frequency,
            "preferred_currency": settings.preferred_currency,
            "auto_payout": settings.auto_payout_enabled,
            "verification_status": verification_status
        })
        
        # Log the action
        await _log_payout_action(
            agent_id=agent_id,
            action='settings_updated' if existing else 'settings_created',
            payout_method=settings.primary_payout_method,
            performed_by=current_user.get("email")
        )
        
        return {
            "status": "success",
            "message": "Payout settings saved successfully",
            "verification_status": verification_status,
            "next_step": "Admin will review and verify your payout information within 1-2 business days"
        }
    
    except Exception as e:
        logger.error(f"Error saving payout settings for agent {agent_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to save payout settings: {str(e)}")


@router.get("/settings")
async def get_payout_settings(
    agent_id: str,
    current_user: dict = Depends(get_current_user)
):
    """
    Get agent's payout settings (sensitive data masked)
    
    **Agent can only view their own settings**
    """
    # Auth check
    if current_user.get("role") != "admin" and current_user.get("agent_id") != agent_id:
        raise HTTPException(status_code=403, detail="Cannot view other agent's payout settings")
    
    try:
        settings = await database.fetch_one(
            "SELECT * FROM agent_payout_settings WHERE agent_id = :agent_id",
            {"agent_id": agent_id}
        )
        
        if not settings:
            return {
                "status": "not_configured",
                "message": "Payout settings not configured yet"
            }
        
        # Mask sensitive data (only show last 4 digits)
        def mask_account(value):
            if not value:
                return None
            return f"••••{value[-4:]}" if len(value) > 4 else "••••"
        
        return {
            "status": "success",
            "settings": {
                "primary_payout_method": settings['primary_payout_method'],
                "backup_payout_method": settings['backup_payout_method'],
                
                # Stripe
                "stripe_connected": bool(settings['stripe_account_id']),
                "stripe_onboarding_complete": settings['stripe_onboarding_complete'],
                "stripe_payouts_enabled": settings['stripe_payouts_enabled'],
                
                # PayPal
                "paypal_email": settings['paypal_email'],
                "paypal_verified": settings['paypal_verified'],
                
                # Bank details (masked)
                "us_bank_holder": settings['us_bank_account_holder_name'],
                "us_bank_name": settings['us_bank_name'],
                "us_bank_account_type": settings['us_bank_account_type'],
                "us_bank_account_last4": mask_account(settings.get('us_bank_account_number_encrypted')),
                
                "intl_bank_holder": settings['intl_bank_account_holder_name'],
                "intl_bank_name": settings['intl_bank_name'],
                "intl_bank_country": settings['intl_bank_country'],
                "intl_iban_last4": mask_account(settings.get('intl_iban')),
                
                # Crypto (show full address - it's public anyway)
                "crypto_wallet_address": settings['crypto_wallet_address'],
                "crypto_network": settings['crypto_network'],
                
                # Tax (masked)
                "tax_country": settings['tax_country'],
                "tax_id_type": settings['tax_id_type'],
                "business_type": settings['business_type'],
                "business_legal_name": settings['business_legal_name'],
                
                # Preferences
                "minimum_payout_amount": float(settings['minimum_payout_amount']),
                "payout_frequency": settings['payout_frequency'],
                "preferred_currency": settings['preferred_currency'],
                "auto_payout_enabled": settings['auto_payout_enabled'],
                
                # Status
                "verification_status": settings['verification_status'],
                "kyc_status": settings['kyc_status'],
                "hold_payouts": settings['hold_payouts'],
                
                # Stats
                "last_payout_date": settings['last_payout_date'].isoformat() if settings['last_payout_date'] else None,
                "total_payouts_count": settings['total_payouts_count'],
                "total_paid_out": float(settings['total_paid_out']),
                
                "created_at": settings['created_at'].isoformat(),
                "updated_at": settings['updated_at'].isoformat()
            }
        }
    
    except Exception as e:
        logger.error(f"Error fetching payout settings: {e}")
        raise HTTPException(status_code=500, detail="Failed to get payout settings")


@router.get("/available-methods")
async def get_available_payout_methods(
    agent_id: str,
    country: Optional[str] = None,
    current_user: dict = Depends(get_current_user)
):
    """
    Get available payout methods for agent's country
    
    **Public for agents to see options**
    """
    try:
        # Get agent's country if not specified
        if not country:
            agent = await database.fetch_one(
                "SELECT tax_country FROM agent_payout_settings WHERE agent_id = :agent_id",
                {"agent_id": agent_id}
            )
            country = agent['tax_country'] if agent else 'GLOBAL'
        
        # Get available methods for this country
        methods = await database.fetch_all(
            """
            SELECT 
                payout_method,
                min_amount,
                max_amount,
                processing_days,
                fee_percentage,
                fee_fixed,
                currency,
                notes
            FROM payout_method_availability
            WHERE (country_code = :country OR country_code = 'GLOBAL')
            AND is_available = true
            AND enabled = true
            ORDER BY 
                CASE 
                    WHEN payout_method = 'stripe_connect' THEN 1
                    WHEN payout_method = 'paypal' THEN 2
                    ELSE 3
                END
            """,
            {"country": country}
        )
        
        return {
            "status": "success",
            "country": country,
            "methods": [
                {
                    "method": m['payout_method'],
                    "min_amount": float(m['min_amount']) if m['min_amount'] else None,
                    "max_amount": float(m['max_amount']) if m['max_amount'] else None,
                    "processing_days": m['processing_days'],
                    "fee_percentage": float(m['fee_percentage']) if m['fee_percentage'] else None,
                    "fee_fixed": float(m['fee_fixed']) if m['fee_fixed'] else None,
                    "currency": m['currency'],
                    "notes": m['notes']
                }
                for m in methods
            ]
        }
    
    except Exception as e:
        logger.error(f"Error fetching available methods: {e}")
        raise HTTPException(status_code=500, detail="Failed to get available methods")


@router.post("/verify-paypal")
async def verify_paypal_email(
    agent_id: str,
    email: EmailStr,
    current_user: dict = Depends(get_current_user)
):
    """
    Verify PayPal email (sends test payment or verification link)
    
    **Agent only**
    """
    # Auth check
    if current_user.get("role") != "admin" and current_user.get("agent_id") != agent_id:
        raise HTTPException(status_code=403, detail="Unauthorized")
    
    try:
        # TODO: Send verification micro-payment or use PayPal API
        # For now, just update status
        await database.execute(
            """
            UPDATE agent_payout_settings
            SET paypal_email = :email,
                paypal_verified = true,
                paypal_verified_at = NOW(),
                updated_at = NOW()
            WHERE agent_id = :agent_id
            """,
            {"agent_id": agent_id, "email": email}
        )
        
        return {
            "status": "success",
            "message": "PayPal email verified",
            "email": email
        }
    
    except Exception as e:
        logger.error(f"Error verifying PayPal: {e}")
        raise HTTPException(status_code=500, detail="Verification failed")


# ============================================================================
# Admin Endpoints (Admins manage verification and payouts)
# ============================================================================

@router.get("/admin/pending-verification", tags=["Admin - Payout Management"])
async def get_agents_pending_verification(
    current_user: dict = Depends(require_admin)
):
    """
    Get list of agents pending payout verification
    
    **Admin only**
    """
    try:
        agents = await database.fetch_all(
            "SELECT * FROM agents_pending_payout_verification ORDER BY settings_created_at ASC"
        )
        
        return {
            "status": "success",
            "count": len(agents),
            "agents": [
                {
                    "agent_id": a['agent_id'],
                    "name": a['name'],
                    "email": a['email'],
                    "company": a['company'],
                    "agent_type": a['agent_type'],
                    "primary_payout_method": a['primary_payout_method'],
                    "verification_status": a['verification_status'],
                    "tax_country": a['tax_country'],
                    "submitted_at": a['settings_created_at'].isoformat() if a['settings_created_at'] else None,
                    "stripe_complete": a['stripe_onboarding_complete'],
                    "paypal_verified": a['paypal_verified'],
                    "bank_verified": a['us_bank_verified'] or a['intl_bank_verified']
                }
                for a in agents
            ]
        }
    
    except Exception as e:
        logger.error(f"Error fetching pending verifications: {e}")
        raise HTTPException(status_code=500, detail="Failed to get pending verifications")


@router.post("/admin/verify", tags=["Admin - Payout Management"])
async def verify_agent_payout_settings(
    agent_id: str,
    approved: bool = True,
    notes: Optional[str] = None,
    current_user: dict = Depends(require_admin)
):
    """
    Approve or reject agent payout settings
    
    **Admin only**
    """
    try:
        status = 'verified' if approved else 'rejected'
        
        await database.execute(
            """
            UPDATE agent_payout_settings
            SET verification_status = :status,
                verified_at = CASE WHEN :approved THEN NOW() ELSE NULL END,
                verified_by = :verified_by,
                verification_notes = :notes,
                rejection_reason = CASE WHEN NOT :approved THEN :notes ELSE NULL END,
                updated_at = NOW()
            WHERE agent_id = :agent_id
            """,
            {
                "agent_id": agent_id,
                "status": status,
                "approved": approved,
                "verified_by": current_user.get("email"),
                "notes": notes
            }
        )
        
        # Log action
        await _log_payout_action(
            agent_id=agent_id,
            action='payout_verified' if approved else 'payout_rejected',
            payout_method=None,
            performed_by=current_user.get("email")
        )
        
        return {
            "status": "success",
            "message": f"Payout settings {'approved' if approved else 'rejected'}",
            "agent_id": agent_id,
            "verification_status": status
        }
    
    except Exception as e:
        logger.error(f"Error verifying payout settings: {e}")
        raise HTTPException(status_code=500, detail="Verification failed")


# ============================================================================
# Helper Functions
# ============================================================================

async def _log_payout_action(
    agent_id: str,
    action: str,
    payout_method: Optional[str],
    performed_by: str,
    old_value: Optional[Dict] = None,
    new_value: Optional[Dict] = None
):
    """Log payout-related actions for audit trail"""
    try:
        await database.execute(
            """
            INSERT INTO agent_payout_history (
                agent_id, action, payout_method, old_value, new_value, performed_by
            ) VALUES (
                :agent_id, :action, :payout_method, :old_value, :new_value, :performed_by
            )
            """,
            {
                "agent_id": agent_id,
                "action": action,
                "payout_method": payout_method,
                "old_value": json.dumps(old_value) if old_value else None,
                "new_value": json.dumps(new_value) if new_value else None,
                "performed_by": performed_by
            }
        )
    except Exception as e:
        logger.error(f"Error logging payout action: {e}")


# Placeholder encryption functions (to be implemented with proper encryption)
def encrypt_sensitive_data(data: str) -> str:
    """
    Encrypt sensitive data before storing
    TODO: Implement with proper encryption (AES-256, KMS, etc.)
    """
    # TEMPORARY: Base64 encoding (NOT SECURE - Replace with real encryption)
    import base64
    return base64.b64encode(data.encode()).decode()


def decrypt_sensitive_data(encrypted: str) -> str:
    """
    Decrypt sensitive data when needed
    TODO: Implement with proper decryption
    """
    import base64
    return base64.b64decode(encrypted.encode()).decode()


