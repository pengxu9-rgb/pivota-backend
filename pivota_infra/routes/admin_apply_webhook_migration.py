"""
Admin endpoint to apply webhook_events migration
"""

from fastapi import APIRouter, HTTPException
from db.database import database
import logging

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/admin/migrations/apply-webhook-events")
async def apply_webhook_events_migration():
    """
    Apply webhook_events table migration
    
    This endpoint creates the webhook_events table and all necessary indexes.
    Safe to run multiple times (uses IF NOT EXISTS).
    """
    try:
        # Create table
        create_table_sql = """
        CREATE TABLE IF NOT EXISTS webhook_events (
            id SERIAL PRIMARY KEY,
            
            -- Event identification
            event_id VARCHAR(255) UNIQUE NOT NULL,
            event_type VARCHAR(100) NOT NULL,
            psp_type VARCHAR(50) NOT NULL,
            
            -- Order linkage
            order_id VARCHAR(50),
            reference VARCHAR(255),
            
            -- Payload and headers
            payload JSONB NOT NULL,
            headers JSONB,
            
            -- Processing status
            status VARCHAR(50) NOT NULL DEFAULT 'pending',
            processed_at TIMESTAMP WITH TIME ZONE,
            error_message TEXT,
            
            -- Signature verification
            signature_verified BOOLEAN DEFAULT FALSE,
            signature_header VARCHAR(500),
            
            -- Retry tracking
            retry_count INTEGER DEFAULT 0,
            last_retry_at TIMESTAMP WITH TIME ZONE,
            
            -- Timestamps
            received_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        );
        """
        
        await database.execute(create_table_sql)
        logger.info("✅ webhook_events table created")
        
        # Create indexes
        indexes = [
            "CREATE INDEX IF NOT EXISTS idx_webhook_events_event_id ON webhook_events(event_id);",
            "CREATE INDEX IF NOT EXISTS idx_webhook_events_order_id ON webhook_events(order_id);",
            "CREATE INDEX IF NOT EXISTS idx_webhook_events_status ON webhook_events(status);",
            "CREATE INDEX IF NOT EXISTS idx_webhook_events_psp_type ON webhook_events(psp_type);",
            "CREATE INDEX IF NOT EXISTS idx_webhook_events_event_type ON webhook_events(event_type);",
            "CREATE INDEX IF NOT EXISTS idx_webhook_events_received_at ON webhook_events(received_at DESC);",
            "CREATE INDEX IF NOT EXISTS idx_webhook_events_idempotency ON webhook_events(event_id, order_id, status);"
        ]
        
        for idx_sql in indexes:
            await database.execute(idx_sql)
        
        logger.info(f"✅ {len(indexes)} indexes created")
        
        # Create trigger function
        trigger_function_sql = """
        CREATE OR REPLACE FUNCTION update_webhook_events_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
        
        await database.execute(trigger_function_sql)
        logger.info("✅ Trigger function created")
        
        # Create trigger
        trigger_sql = """
        DROP TRIGGER IF EXISTS trigger_update_webhook_events_updated_at ON webhook_events;
        CREATE TRIGGER trigger_update_webhook_events_updated_at
            BEFORE UPDATE ON webhook_events
            FOR EACH ROW
            EXECUTE FUNCTION update_webhook_events_updated_at();
        """
        
        await database.execute(trigger_sql)
        logger.info("✅ Trigger created")
        
        # Add comment
        comment_sql = """
        COMMENT ON TABLE webhook_events IS 'Stores all incoming webhook events for idempotency, auditing, and debugging';
        """
        
        await database.execute(comment_sql)
        
        # Verify table
        verify_sql = "SELECT COUNT(*) as count FROM webhook_events;"
        result = await database.fetch_one(verify_sql)
        
        logger.info("✅ Migration 024 applied successfully!")
        
        return {
            "status": "success",
            "message": "Webhook events migration applied successfully",
            "migration": "024_webhook_events",
            "details": {
                "table": "webhook_events",
                "indexes": len(indexes),
                "triggers": 1,
                "current_row_count": result["count"]
            }
        }
    
    except Exception as e:
        logger.error(f"Migration failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Migration failed: {str(e)}"
        )


@router.get("/admin/migrations/verify-webhook-events")
async def verify_webhook_events_migration():
    """
    Verify that webhook_events table exists and is properly configured
    """
    try:
        # Check if table exists
        check_table_sql = """
        SELECT EXISTS (
            SELECT FROM information_schema.tables 
            WHERE table_name = 'webhook_events'
        );
        """
        
        table_exists = await database.fetch_val(check_table_sql)
        
        if not table_exists:
            return {
                "status": "not_applied",
                "message": "webhook_events table does not exist",
                "table_exists": False
            }
        
        # Check row count
        count_sql = "SELECT COUNT(*) as count FROM webhook_events;"
        result = await database.fetch_one(count_sql)
        
        # Check indexes
        indexes_sql = """
        SELECT indexname 
        FROM pg_indexes 
        WHERE tablename = 'webhook_events'
        ORDER BY indexname;
        """
        
        indexes = await database.fetch_all(indexes_sql)
        index_names = [idx["indexname"] for idx in indexes]
        
        return {
            "status": "applied",
            "message": "webhook_events table exists and is configured",
            "table_exists": True,
            "row_count": result["count"],
            "indexes": index_names,
            "index_count": len(index_names)
        }
    
    except Exception as e:
        logger.error(f"Verification failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Verification failed: {str(e)}"
        )

