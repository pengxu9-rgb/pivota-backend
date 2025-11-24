"""
Debug endpoint to ensure commissions table exists with correct schema
"""

from fastapi import APIRouter, Depends
from db.database import database
from utils.auth import get_current_user
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/debug/schema", tags=["Debug"])

@router.post("/ensure-commissions-table")
async def ensure_commissions_table(current_user: dict = Depends(get_current_user)):
    """
    Ensure commissions table exists with correct schema
    Safe to run multiple times
    """
    try:
        # Create table if not exists
        await database.execute("""
            CREATE TABLE IF NOT EXISTS commissions (
                id BIGSERIAL PRIMARY KEY,
                commission_id VARCHAR(50) UNIQUE NOT NULL,
                order_id VARCHAR(100) NOT NULL,
                merchant_id VARCHAR(50) NOT NULL,
                agent_id VARCHAR(50) NOT NULL,
                type VARCHAR(20) NOT NULL CHECK (type IN ('agent', 'platform', 'referral')),
                amount DECIMAL(12,2) NOT NULL,
                rate DECIMAL(5,4) NOT NULL,
                currency VARCHAR(3) DEFAULT 'USD',
                matched BOOLEAN DEFAULT false,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        
        # Create indexes
        await database.execute("""
            CREATE INDEX IF NOT EXISTS idx_commissions_order ON commissions(order_id);
        """)
        await database.execute("""
            CREATE INDEX IF NOT EXISTS idx_commissions_merchant ON commissions(merchant_id);
        """)
        await database.execute("""
            CREATE INDEX IF NOT EXISTS idx_commissions_agent ON commissions(agent_id, type);
        """)
        await database.execute("""
            CREATE INDEX IF NOT EXISTS idx_commissions_created ON commissions(created_at DESC);
        """)
        await database.execute("""
            CREATE INDEX IF NOT EXISTS idx_commissions_type ON commissions(type);
        """)
        
        # Check if table exists
        check = await database.fetch_one("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_name = 'commissions'
            )
        """)
        
        if check and check['exists']:
            # Get row count
            count = await database.fetch_val("SELECT COUNT(*) FROM commissions")
            
            return {
                "status": "success",
                "message": "commissions table exists and is ready",
                "row_count": count
            }
        else:
            return {
                "status": "error",
                "message": "Failed to create commissions table"
            }
            
    except Exception as e:
        logger.error(f"Error ensuring commissions table: {e}")
        return {
            "status": "error",
            "error": str(e)
        }
