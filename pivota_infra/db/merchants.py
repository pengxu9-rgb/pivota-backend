"""
Merchant database tables and operations
"""

from sqlalchemy import Table, Column, Integer, String, DateTime, Float, Text, Boolean, ForeignKey
from sqlalchemy.sql import func
from db.database import metadata, database
from typing import Dict, List, Any, Optional
from datetime import datetime

# Merchants table
merchants = Table(
    "merchants",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("business_name", String(255), nullable=False),
    Column("legal_name", String(255), nullable=False),
    Column("platform", String(50), nullable=False),  # shopify, wix, custom
    Column("store_url", String(500)),
    Column("contact_email", String(255), nullable=False),
    Column("contact_phone", String(50)),
    Column("business_type", String(100)),
    Column("country", String(10)),
    Column("expected_monthly_volume", Float, default=0),
    Column("description", Text),
    Column("status", String(50), default="pending"),  # pending, approved, rejected, active
    Column("verification_status", String(50), default="pending"),  # pending, verified, rejected
    Column("volume_processed", Float, default=0),
    Column("created_at", DateTime, server_default=func.now()),
    Column("updated_at", DateTime, server_default=func.now(), onupdate=func.now()),
    Column("approved_by", String, nullable=True),  # UUID from Supabase
    Column("approved_at", DateTime, nullable=True),
)

# KYB Documents table
kyb_documents = Table(
    "kyb_documents",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("merchant_id", Integer, ForeignKey("merchants.id"), nullable=False),
    Column("document_type", String(100), nullable=False),  # business_license, tax_id, bank_statement, etc.
    Column("file_name", String(500), nullable=False),
    Column("file_path", String(1000), nullable=False),
    Column("file_size", Integer),
    Column("uploaded_at", DateTime, server_default=func.now()),
    Column("verified", Boolean, default=False),
    Column("verified_at", DateTime, nullable=True),
    Column("verified_by", Integer, nullable=True),
)

# Merchant operations
async def create_merchant(merchant_data: Dict[str, Any]) -> int:
    """Create a new merchant application"""
    query = merchants.insert().values(**merchant_data)
    merchant_id = await database.execute(query)
    return merchant_id

async def get_merchant(merchant_id: int) -> Optional[Dict[str, Any]]:
    """Get merchant by ID"""
    query = merchants.select().where(merchants.c.id == merchant_id)
    result = await database.fetch_one(query)
    return dict(result) if result else None

async def get_all_merchants(status: Optional[str] = None) -> List[Dict[str, Any]]:
    """Get all merchants, optionally filtered by status"""
    query = merchants.select()
    if status:
        query = query.where(merchants.c.status == status)
    query = query.order_by(merchants.c.created_at.desc())
    results = await database.fetch_all(query)
    return [dict(row) for row in results]

async def update_merchant_status(merchant_id: int, status: str, admin_id: str) -> bool:
    """Update merchant status (approve/reject)"""
    update_data = {
        "status": status,
        "approved_by": admin_id,  # UUID string from Supabase
        "approved_at": datetime.now()
    }
    if status == "approved":
        update_data["verification_status"] = "verified"
    
    query = merchants.update().where(merchants.c.id == merchant_id).values(**update_data)
    await database.execute(query)
    return True

async def add_kyb_document(merchant_id: int, doc_data: Dict[str, Any]) -> int:
    """Add a KYB document for a merchant"""
    doc_data["merchant_id"] = merchant_id
    query = kyb_documents.insert().values(**doc_data)
    doc_id = await database.execute(query)
    return doc_id

async def get_merchant_documents(merchant_id: int) -> List[Dict[str, Any]]:
    """Get all KYB documents for a merchant"""
    query = kyb_documents.select().where(kyb_documents.c.merchant_id == merchant_id)
    results = await database.fetch_all(query)
    return [dict(row) for row in results]

async def verify_document(doc_id: int, admin_id: int) -> bool:
    """Mark a document as verified"""
    query = kyb_documents.update().where(kyb_documents.c.id == doc_id).values(
        verified=True,
        verified_at=datetime.now(),
        verified_by=admin_id
    )
    await database.execute(query)
    return True

