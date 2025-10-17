"""
Merchant Onboarding Database - Phase 2
Handles merchant registration, KYC verification, PSP setup, and API key issuance
"""

from sqlalchemy import Table, Column, Integer, String, DateTime, Boolean, Text, JSON, Float
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
    Column("store_url", String(500), nullable=False, index=True),  # Required for KYB and MCP
    Column("website", String(500)),  # Optional, for additional info
    Column("region", String(50)),  # e.g., US, EU, APAC
    Column("contact_email", String(255), nullable=False),
    Column("contact_phone", String(50)),
    Column("auto_approved", Boolean, default=False),  # 是否自动批准
    Column("approval_confidence", Float, default=0.0),  # 自动批准置信度
    Column("full_kyb_deadline", DateTime, nullable=True),  # 完整KYB截止日期（7天）
    Column("status", String(50), default="pending_verification"),  # pending_verification, approved, rejected
    Column("psp_connected", Boolean, default=False),
    Column("psp_type", String(50), nullable=True),  # stripe, adyen, shoppay
    Column("psp_sandbox_key", Text, nullable=True),  # Encrypted API key
    # MCP 集成标记
    Column("mcp_connected", Boolean, default=False),
    Column("mcp_platform", String(50), nullable=True),  # shopify/wix/woocommerce
    Column("mcp_shop_domain", String(255), nullable=True),
    Column("mcp_access_token", Text, nullable=True),
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
    # Generate unique merchant_id
    merchant_id = f"merch_{secrets.token_hex(8)}"
    merchant_data["merchant_id"] = merchant_id
    merchant_data["status"] = "pending_verification"
    
    query = merchant_onboarding.insert().values(**merchant_data)
    await database.execute(query)
    return merchant_id

async def get_merchant_onboarding(merchant_id: str) -> Optional[Dict[str, Any]]:
    """Get merchant onboarding record by ID"""
    query = merchant_onboarding.select().where(merchant_onboarding.c.merchant_id == merchant_id)
    result = await database.fetch_one(query)
    return dict(result) if result else None

async def update_kyc_status(merchant_id: str, status: str, reason: Optional[str] = None, rejection_reason: Optional[str] = None) -> bool:
    """Update KYC verification status. 
    When approving after rejection, pass rejection_reason=None to clear it."""
    update_data = {
        "status": status,
        "updated_at": datetime.now()
    }
    if status == "approved":
        update_data["verified_at"] = datetime.now()
        # Clear rejection reason on approval unless explicitly provided
        if rejection_reason is None and reason is None:
            update_data["rejection_reason"] = None
    if reason:
        update_data["rejection_reason"] = reason
    elif rejection_reason is not None:
        update_data["rejection_reason"] = rejection_reason
        
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

async def add_kyc_document(merchant_id: str, document: Dict[str, Any]) -> bool:
    """Append a single KYB document metadata to the onboarding record."""
    # Fetch existing
    query = merchant_onboarding.select().where(merchant_onboarding.c.merchant_id == merchant_id)
    record = await database.fetch_one(query)
    if not record:
        return False
    data = dict(record)
    docs = data.get("kyc_documents") or []
    if isinstance(docs, dict):
        # Normalize old structure
        docs = [docs]
    docs.append(document)
    upd = merchant_onboarding.update().where(
        merchant_onboarding.c.merchant_id == merchant_id
    ).values(kyc_documents=docs, updated_at=datetime.now())
    await database.execute(upd)
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

async def get_all_merchant_onboardings(status: Optional[str] = None, include_deleted: bool = False) -> List[Dict[str, Any]]:
    """Get all merchant onboarding records, optionally filtered by status.
    Excludes deleted by default unless include_deleted=True.
    """
    query = merchant_onboarding.select()
    if status:
        query = query.where(merchant_onboarding.c.status == status)
    if not include_deleted:
        query = query.where(merchant_onboarding.c.status != "deleted")
    query = query.order_by(merchant_onboarding.c.created_at.desc())
    results = await database.fetch_all(query)
    return [dict(row) for row in results]

async def soft_delete_merchant_onboarding(merchant_id: str) -> bool:
    """Soft delete onboarding merchant by setting status='deleted'"""
    query = merchant_onboarding.update().where(
        merchant_onboarding.c.merchant_id == merchant_id
    ).values(status="deleted", updated_at=datetime.now())
    await database.execute(query)
    return True

async def hard_delete_merchant_onboarding(merchant_id: str) -> bool:
    """Hard delete onboarding merchant (permanent)"""
    delete_q = merchant_onboarding.delete().where(
        merchant_onboarding.c.merchant_id == merchant_id
    )
    await database.execute(delete_q)
    return True

