"""
Merchant Onboarding Database - Phase 2
Handles merchant registration, KYC verification, PSP setup, and API key issuance
"""

from sqlalchemy import Table, Column, Integer, String, DateTime, Boolean, Text, JSON
from sqlalchemy.sql import func
from db.database import metadata, database
from typing import Dict, List, Any, Optional
from datetime import datetime
import secrets
import hashlib

# Merchant Onboarding table
merchant_onboarding = Table(
    "merchant_onboarding",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("merchant_id", String(50), unique=True, index=True),  # Auto-generated
    Column("business_name", String(255), nullable=False),
    Column("website", String(500)),
    Column("region", String(50)),  # e.g., US, EU, APAC
    Column("contact_email", String(255), nullable=False),
    Column("contact_phone", String(50)),
    Column("status", String(50), default="pending_verification"),  # pending_verification, approved, rejected
    Column("psp_connected", Boolean, default=False),
    Column("psp_type", String(50), nullable=True),  # stripe, adyen, shoppay
    Column("psp_sandbox_key", Text, nullable=True),  # Encrypted API key
    Column("api_key", String(255), unique=True, nullable=True),  # Merchant's API key for /payment/execute
    Column("api_key_hash", String(255), nullable=True),  # Hashed version for verification
    Column("kyc_documents", JSON, nullable=True),  # JSON blob of uploaded docs
    Column("rejection_reason", Text, nullable=True),
    Column("created_at", DateTime, server_default=func.now()),
    Column("updated_at", DateTime, server_default=func.now(), onupdate=func.now()),
    Column("verified_at", DateTime, nullable=True),
    Column("psp_connected_at", DateTime, nullable=True),
)

# ============================================================================
# MERCHANT ONBOARDING OPERATIONS
# ============================================================================

async def create_merchant_onboarding(merchant_data: Dict[str, Any]) -> str:
    """Create a new merchant onboarding record and return merchant_id"""
    try:
        # Generate unique merchant_id
        merchant_id = f"merch_{secrets.token_hex(8)}"
        merchant_data["merchant_id"] = merchant_id
        merchant_data["status"] = "pending_verification"
        
        query = merchant_onboarding.insert().values(**merchant_data)
        # Ensure clean rollback on failure using an explicit transaction
        async with database.transaction():
            await database.execute(query)
        return merchant_id
    except Exception as e:
        print(f"❌ Error creating merchant onboarding: {e}")
        # Attempt to recover from aborted transaction state
        try:
            await database.disconnect()
            import asyncio as _asyncio
            await _asyncio.sleep(0.5)
            await database.connect()
            print("✅ Database reconnected after create error")
        except Exception as _reconnect_err:
            print(f"⚠️ Database reconnect failed: {_reconnect_err}")
        # Re-raise to let the endpoint handle it
        raise

async def get_merchant_onboarding(merchant_id: str) -> Optional[Dict[str, Any]]:
    """Get merchant onboarding record by ID"""
    query = merchant_onboarding.select().where(merchant_onboarding.c.merchant_id == merchant_id)
    result = await database.fetch_one(query)
    return dict(result) if result else None

async def update_kyc_status(merchant_id: str, status: str, reason: Optional[str] = None) -> bool:
    """Update KYC verification status"""
    update_data = {
        "status": status,
        "updated_at": datetime.now()
    }
    if status == "approved":
        update_data["verified_at"] = datetime.now()
    if reason:
        update_data["rejection_reason"] = reason
        
    query = merchant_onboarding.update().where(
        merchant_onboarding.c.merchant_id == merchant_id
    ).values(**update_data)
    await database.execute(query)
    return True

async def upload_kyc_documents(merchant_id: str, documents: Dict[str, Any]) -> bool:
    """Store KYC documents (simulated as JSON)"""
    query = merchant_onboarding.update().where(
        merchant_onboarding.c.merchant_id == merchant_id
    ).values(kyc_documents=documents, updated_at=datetime.now())
    await database.execute(query)
    return True

async def setup_psp_connection(
    merchant_id: str,
    psp_type: str,
    psp_sandbox_key: str
) -> Dict[str, str]:
    """
    Setup PSP connection and generate merchant API key
    Returns: dict with api_key and merchant_id
    """
    # Generate API key for merchant
    api_key = f"pk_live_{secrets.token_urlsafe(32)}"
    api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    
    # Update merchant record
    update_data = {
        "psp_connected": True,
        "psp_type": psp_type,
        "psp_sandbox_key": psp_sandbox_key,  # TODO: Encrypt this in production
        "api_key": api_key,
        "api_key_hash": api_key_hash,
        "psp_connected_at": datetime.now(),
        "updated_at": datetime.now()
    }
    
    query = merchant_onboarding.update().where(
        merchant_onboarding.c.merchant_id == merchant_id
    ).values(**update_data)
    await database.execute(query)
    
    return {
        "merchant_id": merchant_id,
        "api_key": api_key,
        "psp_type": psp_type
    }

async def verify_api_key(api_key: str) -> Optional[Dict[str, Any]]:
    """Verify merchant API key and return merchant info"""
    query = merchant_onboarding.select().where(merchant_onboarding.c.api_key == api_key)
    result = await database.fetch_one(query)
    return dict(result) if result else None

async def get_merchant_by_api_key(api_key: str) -> Optional[Dict[str, Any]]:
    """Get merchant by API key (alias for verify_api_key)"""
    return await verify_api_key(api_key)

async def get_all_merchant_onboardings(status: Optional[str] = None) -> List[Dict[str, Any]]:
    """Get all merchant onboarding records, optionally filtered by status"""
    query = merchant_onboarding.select()
    if status:
        query = query.where(merchant_onboarding.c.status == status)
    query = query.order_by(merchant_onboarding.c.created_at.desc())
    results = await database.fetch_all(query)
    return [dict(row) for row in results]

