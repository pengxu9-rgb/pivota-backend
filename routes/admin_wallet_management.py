"""
Admin Wallet Management Routes
Administrator endpoints for managing merchant and agent wallets
"""
import logging
from datetime import datetime
from typing import Optional, List
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from db.database import database

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/wallets",
    tags=["Admin - Wallet Management"]
)


# ========================
# Request/Response Models
# ========================

class MerchantWalletCreate(BaseModel):
    """Create merchant wallet"""
    merchant_id: str = Field(..., description="Merchant ID")
    network: str = Field(..., description="Network (ethereum, bitcoin, polygon, etc.)")
    address: str = Field(..., description="Wallet address")
    custodian: Optional[str] = Field(None, description="Custodian service (optional)")
    custodian_account_id: Optional[str] = Field(None, description="Custodian account ID")


class AgentWalletCreate(BaseModel):
    """Create agent wallet"""
    agent_id: str = Field(..., description="Agent ID")
    network: str = Field(..., description="Network (ethereum, bitcoin, polygon, etc.)")
    address: str = Field(..., description="Wallet address")
    custodian: Optional[str] = Field(None, description="Custodian service (optional)")
    custodian_account_id: Optional[str] = Field(None, description="Custodian account ID")


class WalletBalanceUpdate(BaseModel):
    """Update wallet balance"""
    balance: float = Field(..., description="New balance")
    reason: Optional[str] = Field(None, description="Reason for update")


# The ONLY values merchant_wallets.status and agent_wallets.status can hold.
#
# Not a style choice — db/migrations/022_wallet_infrastructure.sql declares
#   status VARCHAR(20) CHECK (status IN ('pending', 'active', 'inactive'))
# on BOTH tables, so anything else is rejected by the database, not merely
# unusual. These two endpoints used to accept ("active", "suspended", "closed"),
# which meant two of their three documented values could not be written at all,
# while `inactive` — which the column does allow — was refused with a 400.
#
# THERE WERE TWO VALID FIXES and this is the lower-risk one, not the only one.
# Widening the CHECK to admit 'suspended' is a shape this repo already uses:
# db/migrations/108_channel_partners.sql declares a four-value status CHECK
# including 'suspended', mirrored by `_PARTNER_STATUSES` in
# routes/admin_partners.py. Converging on the database was chosen because
# nothing reads a suspended or closed wallet, and shipping an ALTER against a
# live table to support a state no code consumes is the more speculative change.
#
# What makes converging cheap here is that this file already agreed with the
# database everywhere else: the wallet-stats endpoint below counts `active`,
# `pending` and `inactive`, so a wallet set to "suspended" would not have
# appeared in ANY of its three buckets even if the write had succeeded, and
# services/wallet_service.py reads only `status = 'active'`. Neither word appears
# anywhere else in the WALLET lane — they are common elsewhere in the repo, which
# is the point above.
#
# Mapping for anyone who was relying on the old words. "suspended" is `inactive`,
# and that one is exact: every non-active value denies identically, because
# `status = 'active'` is the only read. "closed" is NOT exact — the nearest thing
# is the DELETE endpoint on this router, which removes the row rather than
# marking it terminal, and `wallet_verification_logs.wallet_id` carries no
# foreign key, so its audit rows survive as orphans. If a durable closed state is
# ever wanted, widen the CHECK; do not reach for DELETE.
_WALLET_STATUSES = ("active", "inactive", "pending")


class WalletStatusUpdate(BaseModel):
    """Update wallet status"""
    # Matches the CHECK on merchant_wallets.status / agent_wallets.status in
    # db/migrations/022_wallet_infrastructure.sql. See the note on the validators.
    status: str = Field(..., description="New status (active, inactive, pending)")
    reason: Optional[str] = Field(None, description="Reason for status change")


# WHY THE SIX WRITE ENDPOINTS BELOW ALL USE `RETURNING wallet_id`.
#
# `verify_*`, `update_*_status` and `delete_*` each targets exactly one row by
# primary key, and each used to run a bare `database.execute` of its UPDATE or
# DELETE and then return its success shape unconditionally. Called with a
# wallet_id that exists nowhere, all six answered 200: "verified", "updated",
# "deleted" — describing work that never happened. `verify_*` was the worst of
# them, because "verified" is a claim about a wallet's provenance rather than a
# state change, so a caller acting on it believes an address was checked.
#
# THERE IS NO ROWCOUNT TO CHECK INSTEAD, which is the whole reason for the
# RETURNING clause rather than a two-line guard. `databases` 0.7.0 on asyncpg
# implements `execute` as `fetchval`, so an UPDATE or DELETE with no RETURNING
# yields None whether it moved one row or none — measured on PG 15:
#
#     UPDATE ... WHERE <matches a row>    -> None
#     UPDATE ... WHERE <matches nothing>  -> None   (identical, hence the defect)
#
# Reading `RETURNING wallet_id` back through `fetch_one` gives a Record on a hit
# and None on a miss. That is this repo's existing answer to the same question:
# db/product_quality_backfill_jobs.py uses `fetch_one` with `RETURNING` precisely
# to learn whether a row was touched, and documents the same asyncpg behaviour.
#
# NO STATIC GATE CAN SEE THIS DEFECT. tests/test_repo_sql_prepare_postgres.py
# PREPAREs every statement in this file — all 18 of them, these six included —
# but PREPARE is Parse+Describe: it validates TYPES, never execution, and "how
# many rows did that move" is not a question a plan can answer. So the coverage
# is behavioural, in tests/test_wallet_admin_missing_row_postgres.py, which
# executes all six handlers against a real Postgres. Its sibling
# tests/test_wallet_status_vocabulary_postgres.py exists for the same reason.
#
# Keep each statement inline. Passing the SQL into a shared helper would remove
# all six from the PREPARE sweep, which resolves a `database.*` first argument
# only as a literal, a module-level name, or a literal bound once to a
# function-local — and a function PARAMETER counts as a binding, so a helper
# resolves nothing.


# ========================
# Merchant Wallet Management
# ========================

@router.get("/merchant")
async def list_merchant_wallets(
    merchant_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100
):
    """
    List merchant wallets
    
    Admin only - no authentication implemented yet
    """
    query = """
        SELECT wallet_id, merchant_id, network, address,
               custodian, status, verified_at, created_at, updated_at
        FROM merchant_wallets
        WHERE 1=1
    """
    
    params = {}
    
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
        "wallets": [dict(r) for r in results],
        "count": len(results)
    }


@router.post("/merchant")
async def create_merchant_wallet(wallet_data: MerchantWalletCreate):
    """
    Create merchant wallet
    
    Admin only - no authentication implemented yet
    """
    # Validate wallet address
    from services.wallet_service import wallet_service
    is_valid, error_msg = wallet_service.validate_address(wallet_data.network, wallet_data.address)
    
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid wallet address: {error_msg}"
        )
    
    # Check if merchant exists (avoid FK constraint error)
    merchant_check = await database.fetch_one(
        "SELECT merchant_id FROM merchant_onboarding WHERE merchant_id = :merchant_id",
        {"merchant_id": wallet_data.merchant_id}
    )
    
    if not merchant_check:
        # Create minimal merchant record to satisfy FK constraint
        await database.execute(
            """INSERT INTO merchant_onboarding (
                   merchant_id, business_name, store_url, contact_email, status, created_at
               ) VALUES (
                   :merchant_id, :business_name, :store_url, :contact_email, 'pending_verification', NOW()
               ) ON CONFLICT (merchant_id) DO NOTHING""",
            {
                "merchant_id": wallet_data.merchant_id,
                "business_name": f"Test Merchant {wallet_data.merchant_id}",
                "store_url": f"https://test-merchant-{wallet_data.merchant_id}.example.com",
                "contact_email": f"{wallet_data.merchant_id}@test.example.com"
            }
        )
        logger.info(f"Created minimal merchant record for FK: {wallet_data.merchant_id}")
    
    # Check if wallet already exists
    check_query = """
        SELECT wallet_id FROM merchant_wallets
        WHERE merchant_id = :merchant_id AND network = :network AND address = :address
    """
    
    existing = await database.fetch_one(
        check_query,
        {
            "merchant_id": wallet_data.merchant_id,
            "network": wallet_data.network,
            "address": wallet_data.address
        }
    )
    
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Wallet already exists for this merchant"
        )
    
    # Create wallet
    wallet_id = f"mw_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{wallet_data.merchant_id[:8]}"
    
    insert_query = """
        INSERT INTO merchant_wallets (
            wallet_id, merchant_id, network, address,
            custodian, custodian_account_id, status, created_at, updated_at
        ) VALUES (
            :wallet_id, :merchant_id, :network, :address,
            :custodian, :custodian_account_id, 'active', NOW(), NOW()
        )
    """
    
    try:
        await database.execute(
            insert_query,
            {
                "wallet_id": wallet_id,
                "merchant_id": wallet_data.merchant_id,
                "network": wallet_data.network,
                "address": wallet_data.address,
                "custodian": wallet_data.custodian,
                "custodian_account_id": wallet_data.custodian_account_id
            }
        )
        
        logger.info(f"Merchant wallet created: {wallet_id}")
        
        return {
            "wallet_id": wallet_id,
            "merchant_id": wallet_data.merchant_id,
            "network": wallet_data.network,
            "address": wallet_data.address,
            "status": "active",
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        logger.error(f"Failed to create merchant wallet: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create wallet: {str(e)}"
        )


@router.get("/merchant/{wallet_id}")
async def get_merchant_wallet(wallet_id: str):
    """
    Get merchant wallet details
    
    Admin only - no authentication implemented yet
    """
    query = """
        SELECT wallet_id, merchant_id, network, address,
               custodian, status, verified_at, created_at, updated_at
        FROM merchant_wallets
        WHERE wallet_id = :wallet_id
    """
    
    result = await database.fetch_one(query, {"wallet_id": wallet_id})
    
    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Merchant wallet not found"
        )
    
    return dict(result)


@router.patch("/merchant/{wallet_id}/verify")
async def verify_merchant_wallet(wallet_id: str):
    """
    Verify merchant wallet (mark as verified)
    
    Admin only - no authentication implemented yet
    """
    update_query = """
        UPDATE merchant_wallets
        SET status = 'active',
            verified_at = NOW(),
            updated_at = NOW()
        WHERE wallet_id = :wallet_id
        RETURNING wallet_id
    """
    
    verified = await database.fetch_one(update_query, {"wallet_id": wallet_id})
    
    if not verified:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Merchant wallet not found"
        )
    
    logger.info(f"Merchant wallet verified: {wallet_id}")
    
    return {
        "status": "verified",
        "wallet_id": wallet_id,
        "timestamp": datetime.utcnow().isoformat()
    }


@router.patch("/merchant/{wallet_id}/status")
async def update_merchant_wallet_status(
    wallet_id: str,
    status_update: WalletStatusUpdate
):
    """
    Update merchant wallet status
    
    Admin only - no authentication implemented yet
    """
    if status_update.status not in _WALLET_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid status. Must be one of: {', '.join(sorted(_WALLET_STATUSES))}"
        )
    
    update_query = """
        UPDATE merchant_wallets
        SET status = :status,
            updated_at = NOW()
        WHERE wallet_id = :wallet_id
        RETURNING wallet_id
    """
    
    updated = await database.fetch_one(
        update_query,
        {
            "wallet_id": wallet_id,
            "status": status_update.status
        }
    )
    
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Merchant wallet not found"
        )
    
    logger.info(
        f"Merchant wallet status updated: {wallet_id} = {status_update.status} "
        f"(reason: {status_update.reason})"
    )
    
    return {
        "status": "updated",
        "wallet_id": wallet_id,
        "new_status": status_update.status,
        "timestamp": datetime.utcnow().isoformat()
    }


@router.delete("/merchant/{wallet_id}")
async def delete_merchant_wallet(wallet_id: str):
    """
    Delete merchant wallet
    
    Admin only - no authentication implemented yet
    """
    delete_query = """
        DELETE FROM merchant_wallets
        WHERE wallet_id = :wallet_id
        RETURNING wallet_id
    """
    
    deleted = await database.fetch_one(delete_query, {"wallet_id": wallet_id})
    
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Merchant wallet not found"
        )
    
    logger.info(f"Merchant wallet deleted: {wallet_id}")
    
    return {
        "status": "deleted",
        "wallet_id": wallet_id,
        "timestamp": datetime.utcnow().isoformat()
    }


# ========================
# Agent Wallet Management
# ========================

@router.get("/agent")
async def list_agent_wallets(
    agent_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100
):
    """
    List agent wallets
    
    Admin only - no authentication implemented yet
    """
    query = """
        SELECT wallet_id, agent_id, network, address,
               custodian, status, verified_at, created_at, updated_at
        FROM agent_wallets
        WHERE 1=1
    """
    
    params = {}
    
    if agent_id:
        query += " AND agent_id = :agent_id"
        params["agent_id"] = agent_id
    
    if status:
        query += " AND status = :status"
        params["status"] = status
    
    query += " ORDER BY created_at DESC LIMIT :limit"
    params["limit"] = limit
    
    results = await database.fetch_all(query, params)
    
    return {
        "wallets": [dict(r) for r in results],
        "count": len(results)
    }


@router.post("/agent")
async def create_agent_wallet(wallet_data: AgentWalletCreate):
    """
    Create agent wallet
    
    Admin only - no authentication implemented yet
    """
    # Validate wallet address
    from services.wallet_service import wallet_service
    is_valid, error_msg = wallet_service.validate_address(wallet_data.network, wallet_data.address)
    
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid wallet address: {error_msg}"
        )
    
    # Check if agent exists (avoid FK constraint error)
    agent_check = await database.fetch_one(
        "SELECT agent_id FROM agents WHERE agent_id = :agent_id",
        {"agent_id": wallet_data.agent_id}
    )
    
    if not agent_check:
        # Create minimal agent record to satisfy FK constraint
        import secrets
        api_key = f"test_key_{secrets.token_hex(16)}"
        api_key_hash = f"hash_{secrets.token_hex(32)}"
        
        await database.execute(
            """INSERT INTO agents (
                   agent_id, agent_name, agent_type, api_key, api_key_hash,
                   is_active, created_at
               ) VALUES (
                   :agent_id, :agent_name, :agent_type, :api_key, :api_key_hash,
                   true, NOW()
               ) ON CONFLICT (agent_id) DO NOTHING""",
            {
                "agent_id": wallet_data.agent_id,
                "agent_name": f"Test Agent {wallet_data.agent_id}",
                "agent_type": "basic",
                "api_key": api_key,
                "api_key_hash": api_key_hash
            }
        )
        logger.info(f"Created minimal agent record for FK: {wallet_data.agent_id}")
    
    # Check if wallet already exists
    check_query = """
        SELECT wallet_id FROM agent_wallets
        WHERE agent_id = :agent_id AND network = :network AND address = :address
    """
    
    existing = await database.fetch_one(
        check_query,
        {
            "agent_id": wallet_data.agent_id,
            "network": wallet_data.network,
            "address": wallet_data.address
        }
    )
    
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Wallet already exists for this agent"
        )
    
    # Create wallet
    wallet_id = f"aw_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{wallet_data.agent_id[:8]}"
    
    insert_query = """
        INSERT INTO agent_wallets (
            wallet_id, agent_id, network, address,
            custodian, custodian_account_id, status, created_at, updated_at
        ) VALUES (
            :wallet_id, :agent_id, :network, :address,
            :custodian, :custodian_account_id, 'active', NOW(), NOW()
        )
    """
    
    try:
        await database.execute(
            insert_query,
            {
                "wallet_id": wallet_id,
                "agent_id": wallet_data.agent_id,
                "network": wallet_data.network,
                "address": wallet_data.address,
                "custodian": wallet_data.custodian,
                "custodian_account_id": wallet_data.custodian_account_id
            }
        )
        
        logger.info(f"Agent wallet created: {wallet_id}")
        
        return {
            "wallet_id": wallet_id,
            "agent_id": wallet_data.agent_id,
            "network": wallet_data.network,
            "address": wallet_data.address,
            "status": "active",
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        logger.error(f"Failed to create agent wallet: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create wallet: {str(e)}"
        )


@router.get("/agent/{wallet_id}")
async def get_agent_wallet(wallet_id: str):
    """
    Get agent wallet details
    
    Admin only - no authentication implemented yet
    """
    query = """
        SELECT wallet_id, agent_id, network, address,
               custodian, status, verified_at, created_at, updated_at
        FROM agent_wallets
        WHERE wallet_id = :wallet_id
    """
    
    result = await database.fetch_one(query, {"wallet_id": wallet_id})
    
    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent wallet not found"
        )
    
    return dict(result)


@router.patch("/agent/{wallet_id}/verify")
async def verify_agent_wallet(wallet_id: str):
    """
    Verify agent wallet (mark as verified)
    
    Admin only - no authentication implemented yet
    """
    update_query = """
        UPDATE agent_wallets
        SET status = 'active',
            verified_at = NOW(),
            updated_at = NOW()
        WHERE wallet_id = :wallet_id
        RETURNING wallet_id
    """
    
    verified = await database.fetch_one(update_query, {"wallet_id": wallet_id})
    
    if not verified:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent wallet not found"
        )
    
    logger.info(f"Agent wallet verified: {wallet_id}")
    
    return {
        "status": "verified",
        "wallet_id": wallet_id,
        "timestamp": datetime.utcnow().isoformat()
    }


@router.patch("/agent/{wallet_id}/status")
async def update_agent_wallet_status(
    wallet_id: str,
    status_update: WalletStatusUpdate
):
    """
    Update agent wallet status
    
    Admin only - no authentication implemented yet
    """
    if status_update.status not in _WALLET_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid status. Must be one of: {', '.join(sorted(_WALLET_STATUSES))}"
        )
    
    update_query = """
        UPDATE agent_wallets
        SET status = :status,
            updated_at = NOW()
        WHERE wallet_id = :wallet_id
        RETURNING wallet_id
    """
    
    updated = await database.fetch_one(
        update_query,
        {
            "wallet_id": wallet_id,
            "status": status_update.status
        }
    )
    
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent wallet not found"
        )
    
    logger.info(
        f"Agent wallet status updated: {wallet_id} = {status_update.status} "
        f"(reason: {status_update.reason})"
    )
    
    return {
        "status": "updated",
        "wallet_id": wallet_id,
        "new_status": status_update.status,
        "timestamp": datetime.utcnow().isoformat()
    }


@router.delete("/agent/{wallet_id}")
async def delete_agent_wallet(wallet_id: str):
    """
    Delete agent wallet
    
    Admin only - no authentication implemented yet
    """
    delete_query = """
        DELETE FROM agent_wallets
        WHERE wallet_id = :wallet_id
        RETURNING wallet_id
    """
    
    deleted = await database.fetch_one(delete_query, {"wallet_id": wallet_id})
    
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent wallet not found"
        )
    
    logger.info(f"Agent wallet deleted: {wallet_id}")
    
    return {
        "status": "deleted",
        "wallet_id": wallet_id,
        "timestamp": datetime.utcnow().isoformat()
    }


# ========================
# Wallet Statistics
# ========================

@router.get("/stats")
async def get_wallet_stats():
    """
    Get wallet statistics
    
    Admin only - no authentication implemented yet
    """
    # Merchant wallet stats
    merchant_stats = await database.fetch_one("""
        SELECT
            COUNT(*) as total,
            COALESCE(SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END), 0) as active,
            COALESCE(SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0) as pending,
            COALESCE(SUM(CASE WHEN status = 'inactive' THEN 1 ELSE 0 END), 0) as inactive
        FROM merchant_wallets
    """)
    
    # Agent wallet stats
    agent_stats = await database.fetch_one("""
        SELECT
            COUNT(*) as total,
            COALESCE(SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END), 0) as active,
            COALESCE(SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0) as pending,
            COALESCE(SUM(CASE WHEN status = 'inactive' THEN 1 ELSE 0 END), 0) as inactive
        FROM agent_wallets
    """)
    
    return {
        "merchant_wallets": dict(merchant_stats) if merchant_stats else {},
        "agent_wallets": dict(agent_stats) if agent_stats else {},
        "timestamp": datetime.utcnow().isoformat()
    }


@router.get("/transactions")
async def get_wallet_transactions(
    wallet_id: Optional[str] = None,
    wallet_type: Optional[str] = None,
    limit: int = 100
):
    """
    Get wallet transactions
    
    Admin only - no authentication implemented yet
    
    Args:
        wallet_id: Specific wallet ID
        wallet_type: Filter by wallet type (merchant or agent)
        limit: Max number of transactions
    """
    query = """
        SELECT transaction_id, agent_id, merchant_id, wallet_address,
               amount, currency, status, created_at
        FROM x402_transactions
        WHERE 1=1
    """
    
    params = {}
    
    if wallet_id:
        # TODO: Join with wallet tables to filter by wallet_id
        pass
    
    query += " ORDER BY created_at DESC LIMIT :limit"
    params["limit"] = limit
    
    results = await database.fetch_all(query, params)
    
    return {
        "transactions": [dict(r) for r in results],
        "count": len(results)
    }

