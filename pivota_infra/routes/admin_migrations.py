from fastapi import APIRouter, Depends, HTTPException
from db.database import database
from routes.auth_routes import require_admin
import logging

router = APIRouter(prefix="/admin/migrations", tags=["Admin Migrations"])
logger = logging.getLogger(__name__)

ALTER_SQL = [
    """
    ALTER TABLE orders 
    ADD COLUMN IF NOT EXISTS payment_intent_id VARCHAR(255),
    ADD COLUMN IF NOT EXISTS client_secret TEXT,
    ADD COLUMN IF NOT EXISTS fulfillment_status VARCHAR(50) DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS shopify_order_id VARCHAR(255),
    ADD COLUMN IF NOT EXISTS payment_method_id VARCHAR(255),
    ADD COLUMN IF NOT EXISTS tracking_number VARCHAR(255),
    ADD COLUMN IF NOT EXISTS tracking_url TEXT,
    ADD COLUMN IF NOT EXISTS carrier VARCHAR(100),
    ADD COLUMN IF NOT EXISTS shipped_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS paid_at TIMESTAMP;
    """,
    """
    ALTER TABLE agents
    ADD COLUMN IF NOT EXISTS total_requests INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS total_orders INTEGER DEFAULT 0;
    """
]

@router.post("/apply-psp-fixes")
async def apply_psp_schema_fixes(current_user: dict = Depends(require_admin)):
    try:
        async with database.transaction():
            for stmt in ALTER_SQL:
                await database.execute(stmt)
        return {"status": "success", "message": "PSP columns ensured"}
    except Exception as e:
        logger.error(f"Migration failed: {e}")
        raise HTTPException(status_code=500, detail=f"Migration failed: {str(e)}")

@router.post("/cleanup-duplicate-psps/{merchant_id}")
async def cleanup_duplicate_psps(merchant_id: str, current_user: dict = Depends(require_admin)):
    """Clean up duplicate PSP configs, keep only the most recent for each provider"""
    try:
        # Get all PSPs
        psps = await database.fetch_all(
            """
            SELECT psp_id, provider, connected_at,
                   LENGTH(api_key) as key_len,
                   account_id
            FROM merchant_psps
            WHERE merchant_id = :merchant_id
            ORDER BY provider, connected_at DESC
            """,
            {"merchant_id": merchant_id}
        )
        
        by_provider = {}
        for psp in psps:
            provider = psp["provider"]
            if provider not in by_provider:
                by_provider[provider] = []
            by_provider[provider].append(psp)
        
        # Delete all except the most recent for each provider
        deleted_count = 0
        kept_psps = []
        
        async with database.transaction():
            for provider, configs in by_provider.items():
                if len(configs) > 1:
                    # Keep first (most recent), delete rest
                    kept_psps.append(configs[0])
                    to_delete = [cfg['psp_id'] for cfg in configs[1:]]
                    
                    await database.execute(
                        """
                        DELETE FROM merchant_psps
                        WHERE psp_id = ANY(:psp_ids)
                        """,
                        {"psp_ids": to_delete}
                    )
                    deleted_count += len(to_delete)
                else:
                    kept_psps.append(configs[0])
        
        return {
            "status": "success",
            "deleted": deleted_count,
            "kept": len(kept_psps),
            "psps": [{"provider": p["provider"], "psp_id": p["psp_id"], "key_len": p["key_len"], "account_id": p["account_id"]} for p in kept_psps]
        }
        
    except Exception as e:
        logger.error(f"Cleanup failed: {e}")
        raise HTTPException(status_code=500, detail=f"Cleanup failed: {str(e)}")
