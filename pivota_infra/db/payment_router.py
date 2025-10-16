"""
Payment Router Database - Links merchants to PSPs
Enables unified /payment/execute endpoint with automatic PSP routing
"""

from sqlalchemy import Table, Column, Integer, String, DateTime, Boolean, Float, ForeignKey, JSON
from sqlalchemy.sql import func
from pivota_infra.db.database import metadata, database
from typing import Dict, List, Any, Optional
import json

# Payment routing configuration
payment_router_config = Table(
    "payment_router_config",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("merchant_id", String(50), index=True, unique=True),  # From merchant_onboarding
    Column("psp_type", String(50), nullable=False),  # stripe, adyen, shoppay
    Column("psp_credentials", JSON, nullable=False),  # PSP credentials as JSON
    Column("routing_priority", Integer, default=1),  # For multi-PSP routing later
    Column("enabled", Boolean, default=True),
    Column("created_at", DateTime, server_default=func.now()),
    Column("updated_at", DateTime, server_default=func.now(), onupdate=func.now()),
)

# ============================================================================
# PAYMENT ROUTER OPERATIONS
# ============================================================================

async def register_merchant_psp_route(
    merchant_id: str,
    psp_type: str,
    psp_credentials: str
) -> bool:
    """
    Register a merchant's PSP routing configuration
    Called after merchant completes PSP setup
    """
    # Check if route already exists
    query = payment_router_config.select().where(
        payment_router_config.c.merchant_id == merchant_id
    )
    existing = await database.fetch_one(query)
    
    if existing:
        # Update existing route
        update_query = payment_router_config.update().where(
            payment_router_config.c.merchant_id == merchant_id
        ).values(
            psp_type=psp_type,
            psp_credentials=psp_credentials,
            enabled=True
        )
        await database.execute(update_query)
    else:
        # Insert new route
        insert_query = payment_router_config.insert().values(
            merchant_id=merchant_id,
            psp_type=psp_type,
            psp_credentials=psp_credentials,
            enabled=True
        )
        await database.execute(insert_query)
    
    return True

async def get_merchant_psp_route(merchant_id: str) -> Optional[Dict[str, Any]]:
    """Get PSP routing config for a merchant"""
    query = payment_router_config.select().where(
        payment_router_config.c.merchant_id == merchant_id
    )
    result = await database.fetch_one(query)
    return dict(result) if result else None

async def disable_merchant_route(merchant_id: str) -> bool:
    """Disable PSP routing for a merchant"""
    query = payment_router_config.update().where(
        payment_router_config.c.merchant_id == merchant_id
    ).values(enabled=False)
    await database.execute(query)
    return True

async def get_all_active_routes() -> List[Dict[str, Any]]:
    """Get all active merchant PSP routes"""
    query = payment_router_config.select().where(
        payment_router_config.c.enabled == True
    )
    results = await database.fetch_all(query)
    return [dict(row) for row in results]

