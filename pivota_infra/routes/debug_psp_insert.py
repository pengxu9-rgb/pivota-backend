"""
临时调试端点：直接插入 PSP 配置
仅用于调试！
"""
from fastapi import APIRouter, Depends
from db.database import database
from datetime import datetime
import uuid

router = APIRouter(prefix="/debug", tags=["debug"])

@router.post("/insert-adyen")
async def debug_insert_adyen(
    api_key: str,
    account_id: str = "TestMerchant"
):
    """临时端点：直接插入 Adyen 配置"""
    
    psp_id = f"psp_adyen_{uuid.uuid4().hex[:8]}"
    merchant_id = "merch_208139f7600dbf42"
    
    async with database.transaction():
        await database.execute(
            """
            INSERT INTO merchant_psps 
            (psp_id, merchant_id, provider, name, api_key, account_id, capabilities, status, connected_at)
            VALUES (:psp_id, :merchant_id, :provider, :name, :api_key, :account_id, :capabilities, :status, :connected_at)
            """,
            {
                "psp_id": psp_id,
                "merchant_id": merchant_id,
                "provider": "adyen",
                "name": "Adyen Account",
                "api_key": api_key,
                "account_id": account_id,
                "capabilities": "payments,refunds,payouts",
                "status": "active",
                "connected_at": datetime.now()
            }
        )
    
    return {
        "status": "success",
        "message": "Adyen PSP inserted",
        "psp_id": psp_id,
        "merchant_id": merchant_id
    }

@router.get("/check-psps")
async def debug_check_psps():
    """查看数据库中的所有 PSP"""
    
    rows = await database.fetch_all(
        """
        SELECT psp_id, merchant_id, provider,
               LENGTH(api_key) as api_key_len,
               account_id,
               CASE WHEN secret_key IS NOT NULL THEN LENGTH(secret_key) ELSE 0 END as secret_len,
               status, connected_at
        FROM merchant_psps 
        WHERE merchant_id = 'merch_208139f7600dbf42'
        ORDER BY connected_at DESC
        """
    )
    
    return {
        "status": "success",
        "merchant_id": "merch_208139f7600dbf42",
        "psps": [
            {
                "psp_id": r["psp_id"],
                "provider": r["provider"],
                "api_key_len": r["api_key_len"],
                "account_id": r["account_id"],
                "secret_len": r["secret_len"],
                "status": r["status"],
                "connected_at": str(r["connected_at"])
            }
            for r in rows
        ]
    }

