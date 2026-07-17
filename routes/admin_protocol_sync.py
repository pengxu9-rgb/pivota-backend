"""
Admin Protocol Sync Routes
Administrator endpoints for syncing protocol data and managing protocol configurations
"""
import json
import logging
from datetime import datetime
from typing import Optional, List
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from db.database import database

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/protocol-sync",
    tags=["Admin - Protocol Sync"]
)


# ========================
# Request/Response Models
# ========================

class ExchangeRateUpdate(BaseModel):
    """Exchange rate update"""
    from_currency: str = Field(..., description="Source currency")
    to_currency: str = Field(..., description="Target currency")
    rate: float = Field(..., description="Exchange rate", gt=0)


class BulkExchangeRateUpdate(BaseModel):
    """Bulk exchange rate update"""
    rates: List[ExchangeRateUpdate] = Field(..., description="List of exchange rates")


class ExchangeRateSnapshotCreate(BaseModel):
    """Exchange rate snapshot creation"""
    base_currency: str = Field(..., description="Base currency (e.g., USD)")
    rates: dict = Field(..., description="Currency rates as dict (e.g., {'EUR': 0.85})")
    provider: Optional[str] = Field("manual", description="Rate provider")


class ConsentStats(BaseModel):
    """Consent statistics"""
    total: int = Field(..., description="Total consents")
    active: int = Field(..., description="Active consents")
    expired: int = Field(..., description="Expired consents")
    revoked: int = Field(..., description="Revoked consents")


class TransactionStats(BaseModel):
    """Transaction statistics"""
    total: int = Field(..., description="Total transactions")
    pending: int = Field(..., description="Pending transactions")
    completed: int = Field(..., description="Completed transactions")
    failed: int = Field(..., description="Failed transactions")


# ========================
# Exchange Rate Management
# ========================

@router.get("/exchange-rates")
async def list_exchange_rates(
    base_currency: Optional[str] = None
):
    """
    List all exchange rate snapshots
    
    Admin only - no authentication implemented yet
    """
    query = """
        SELECT id, snapshot_id, base_currency, rates, provider, created_at, expires_at
        FROM x402_exchange_rates
        WHERE 1=1
    """
    
    params = {}
    
    if base_currency:
        query += " AND base_currency = :base_currency"
        params["base_currency"] = base_currency.upper()
    
    query += " ORDER BY created_at DESC LIMIT 50"
    
    results = await database.fetch_all(query, params)
    
    return {
        "snapshots": [dict(r) for r in results],
        "count": len(results)
    }


@router.post("/exchange-rates")
async def create_exchange_rate_snapshot(
    snapshot_data: ExchangeRateSnapshotCreate
):
    """
    Create exchange rate snapshot
    
    Admin only - no authentication implemented yet
    
    Example:
    {
        "base_currency": "USD",
        "rates": {"EUR": 0.85, "GBP": 0.73, "JPY": 110.5},
        "provider": "manual"
    }
    """
    snapshot_id = f"snap_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    
    insert_query = """
        INSERT INTO x402_exchange_rates (
            snapshot_id, base_currency, rates, provider, created_at, expires_at
        ) VALUES (
            :snapshot_id, :base_currency, :rates, :provider, NOW(), NOW() + INTERVAL '1 day'
        )
    """
    
    await database.execute(
        insert_query,
        {
            "snapshot_id": snapshot_id,
            "base_currency": snapshot_data.base_currency.upper(),
            "rates": json.dumps(snapshot_data.rates),
            "provider": snapshot_data.provider
        }
    )
    
    logger.info(f"Exchange rate snapshot created: {snapshot_id} ({snapshot_data.base_currency})")
    
    return {
        "status": "created",
        "snapshot_id": snapshot_id,
        "base_currency": snapshot_data.base_currency.upper(),
        "rates": snapshot_data.rates,
        "timestamp": datetime.utcnow().isoformat()
    }


@router.post("/exchange-rates/bulk")
async def bulk_update_exchange_rates(bulk_update: BulkExchangeRateUpdate):
    """
    Bulk update exchange rates
    
    Admin only - no authentication implemented yet
    """
    updated_count = 0
    
    for rate_update in bulk_update.rates:
        insert_query = """
            INSERT INTO x402_exchange_rates (from_currency, to_currency, rate, updated_at)
            VALUES (:from_currency, :to_currency, :rate, NOW())
            ON CONFLICT (from_currency, to_currency)
            DO UPDATE SET rate = :rate, updated_at = NOW()
        """
        
        await database.execute(
            insert_query,
            {
                "from_currency": rate_update.from_currency.upper(),
                "to_currency": rate_update.to_currency.upper(),
                "rate": rate_update.rate
            }
        )
        
        updated_count += 1
    
    logger.info(f"Bulk exchange rate update: {updated_count} rates updated")
    
    return {
        "status": "success",
        "updated": updated_count,
        "timestamp": datetime.utcnow().isoformat()
    }


@router.delete("/exchange-rates/{from_currency}/{to_currency}")
async def delete_exchange_rate(from_currency: str, to_currency: str):
    """
    Delete exchange rate
    
    Admin only - no authentication implemented yet
    """
    delete_query = """
        DELETE FROM x402_exchange_rates
        WHERE from_currency = :from_currency AND to_currency = :to_currency
    """
    
    await database.execute(
        delete_query,
        {
            "from_currency": from_currency.upper(),
            "to_currency": to_currency.upper()
        }
    )
    
    logger.info(f"Exchange rate deleted: {from_currency}/{to_currency}")
    
    return {
        "status": "deleted",
        "from_currency": from_currency.upper(),
        "to_currency": to_currency.upper()
    }


# ========================
# Consent Management
# ========================

@router.get("/consents")
async def list_consents(
    agent_id: Optional[str] = None,
    status: Optional[str] = None
):
    """
    List agent consents
    
    Admin only - no authentication implemented yet
    """
    query = """
        SELECT consent_id, agent_id, scope, status, granted_at, expires_at, revoked_at
        FROM agent_consents
        WHERE 1=1
    """
    
    params = {}
    
    if agent_id:
        query += " AND agent_id = :agent_id"
        params["agent_id"] = agent_id
    
    if status:
        query += " AND status = :status"
        params["status"] = status
    
    query += " ORDER BY granted_at DESC LIMIT 100"
    
    results = await database.fetch_all(query, params)
    
    return {
        "consents": [dict(r) for r in results],
        "count": len(results)
    }


@router.get("/consents/stats", response_model=ConsentStats)
async def get_consent_stats():
    """
    Get consent statistics
    
    Admin only - no authentication implemented yet
    """
    query = """
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) as active,
            SUM(CASE WHEN status = 'expired' THEN 1 ELSE 0 END) as expired,
            SUM(CASE WHEN status = 'revoked' THEN 1 ELSE 0 END) as revoked
        FROM agent_consents
    """
    
    result = await database.fetch_one(query)
    
    if not result:
        return ConsentStats(total=0, active=0, expired=0, revoked=0)
    
    return ConsentStats(
        total=result["total"] or 0,
        active=result["active"] or 0,
        expired=result["expired"] or 0,
        revoked=result["revoked"] or 0
    )


@router.post("/consents/{consent_id}/revoke")
async def admin_revoke_consent(consent_id: str):
    """
    Revoke consent (admin override)
    
    Admin only - no authentication implemented yet
    """
    update_query = """
        UPDATE agent_consents
        SET status = 'revoked',
            revoked_at = NOW()
        WHERE consent_id = :consent_id
    """
    
    result = await database.execute(update_query, {"consent_id": consent_id})
    
    logger.info(f"Admin revoked consent: {consent_id}")
    
    return {
        "status": "revoked",
        "consent_id": consent_id,
        "timestamp": datetime.utcnow().isoformat()
    }


@router.delete("/consents/cleanup")
async def cleanup_expired_consents():
    """
    Cleanup expired consents
    
    Admin only - no authentication implemented yet
    """
    delete_query = """
        DELETE FROM agent_consents
        WHERE status = 'expired'
          AND expires_at < NOW() - INTERVAL '30 days'
    """
    
    result = await database.execute(delete_query)
    
    logger.info("Expired consents cleanup completed")
    
    return {
        "status": "success",
        "message": "Expired consents cleaned up",
        "timestamp": datetime.utcnow().isoformat()
    }


# ========================
# Transaction Management
# ========================

@router.get("/transactions")
async def list_transactions(
    agent_id: Optional[str] = None,
    merchant_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100
):
    """
    List AP2 transactions
    
    Admin only - no authentication implemented yet
    """
    query = """
        SELECT transaction_id, agent_id, merchant_id, amount, currency,
               status, created_at, confirmed_at
        FROM x402_transactions
        WHERE 1=1
    """
    
    params = {}
    
    if agent_id:
        query += " AND agent_id = :agent_id"
        params["agent_id"] = agent_id
    
    if merchant_id:
        query += " AND merchant_id = :merchant_id"
        params["merchant_id"] = merchant_id
    
    if status:
        query += " AND status = :status"
        params["status"] = status
    
    query += " ORDER BY created_at DESC LIMIT :limit"
    params["limit"] = limit
    
    results = await database.fetch_all(query, params)
    
    return {
        "transactions": [dict(r) for r in results],
        "count": len(results)
    }


@router.get("/transactions/stats", response_model=TransactionStats)
async def get_transaction_stats():
    """
    Get transaction statistics
    
    Admin only - no authentication implemented yet
    """
    query = """
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending,
            SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed
        FROM x402_transactions
    """
    
    result = await database.fetch_one(query)
    
    if not result:
        return TransactionStats(total=0, pending=0, completed=0, failed=0)
    
    return TransactionStats(
        total=result["total"] or 0,
        pending=result["pending"] or 0,
        completed=result["completed"] or 0,
        failed=result["failed"] or 0
    )


@router.post("/transactions/{transaction_id}/cancel")
async def cancel_transaction(transaction_id: str):
    """
    Cancel pending transaction (admin override)
    
    Admin only - no authentication implemented yet
    """
    update_query = """
        UPDATE x402_transactions
        SET status = 'cancelled'
        WHERE transaction_id = :transaction_id
          AND status = 'pending'
    """
    
    result = await database.execute(update_query, {"transaction_id": transaction_id})
    
    logger.info(f"Admin cancelled transaction: {transaction_id}")
    
    return {
        "status": "cancelled",
        "transaction_id": transaction_id,
        "timestamp": datetime.utcnow().isoformat()
    }


# ========================
# Nonce Management
# ========================

@router.get("/nonces")
async def list_nonces(limit: int = 100):
    """
    List used nonces
    
    Admin only - no authentication implemented yet
    """
    # nonce_tracker has no nonce_id column (see migration 021) and nothing
    # writes one — `nonce` is the primary key. Selecting a phantom nonce_id
    # would raise "column does not exist" the moment AP2 is enabled.
    query = """
        SELECT nonce, agent_id, used_at, request_path
        FROM nonce_tracker
        ORDER BY used_at DESC
        LIMIT :limit
    """
    
    results = await database.fetch_all(query, {"limit": limit})
    
    return {
        "nonces": [dict(r) for r in results],
        "count": len(results)
    }


@router.delete("/nonces/cleanup")
async def cleanup_old_nonces(days: int = 7):
    """
    Cleanup old nonces (older than specified days)
    
    Admin only - no authentication implemented yet
    """
    delete_query = """
        DELETE FROM nonce_tracker
        WHERE used_at < NOW() - INTERVAL ':days days'
    """
    
    result = await database.execute(delete_query, {"days": days})
    
    logger.info(f"Old nonces cleanup completed (older than {days} days)")
    
    return {
        "status": "success",
        "message": f"Nonces older than {days} days cleaned up",
        "timestamp": datetime.utcnow().isoformat()
    }


# ========================
# Protocol Health Check
# ========================

@router.get("/health")
async def get_protocol_health():
    """
    Get protocol system health status
    
    Admin only - no authentication implemented yet
    """
    # Check tables exist
    tables_check = await database.fetch_all("""
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name IN (
              'agent_consents',
              'nonce_tracker',
              'merchant_wallets',
              'agent_wallets',
              'x402_transactions',
              'x402_exchange_rates'
          )
    """)
    
    existing_tables = [row["table_name"] for row in tables_check]
    
    # Get statistics
    consent_stats = await get_consent_stats()
    transaction_stats = await get_transaction_stats()
    
    # Count exchange rates
    rate_count = await database.fetch_one(
        "SELECT COUNT(*) as count FROM x402_exchange_rates"
    )
    
    return {
        "status": "healthy",
        "protocol_version": "AP2-0.1",
        "tables": {
            "agent_consents": "agent_consents" in existing_tables,
            "nonce_tracker": "nonce_tracker" in existing_tables,
            "merchant_wallets": "merchant_wallets" in existing_tables,
            "agent_wallets": "agent_wallets" in existing_tables,
            "x402_transactions": "x402_transactions" in existing_tables,
            "x402_exchange_rates": "x402_exchange_rates" in existing_tables
        },
        "statistics": {
            "consents": consent_stats.dict(),
            "transactions": transaction_stats.dict(),
            "exchange_rates": rate_count["count"] if rate_count else 0
        },
        "timestamp": datetime.utcnow().isoformat()
    }

