"""
Merchant Integrations Database Schema
Handles PSP and E-commerce platform integrations
"""
import logging
from datetime import datetime
from typing import Optional, Dict, Any
from db.database import database

logger = logging.getLogger(__name__)


async def create_merchant_integrations_tables():
    """Create merchant integrations related tables"""
    try:
        # Main integrations table
        await database.execute("""
            CREATE TABLE IF NOT EXISTS merchant_integrations (
                id SERIAL PRIMARY KEY,
                merchant_id VARCHAR(50) NOT NULL REFERENCES merchant_onboarding(merchant_id),
                integration_type VARCHAR(20) NOT NULL CHECK (integration_type IN ('psp', 'platform')),
                provider VARCHAR(50) NOT NULL,
                display_name VARCHAR(100) NOT NULL,
                status VARCHAR(20) DEFAULT 'pending' CHECK (status IN ('pending', 'active', 'inactive', 'error')),
                is_primary BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_tested_at TIMESTAMP,
                last_test_result JSONB,
                metadata JSONB DEFAULT '{}',
                UNIQUE(merchant_id, integration_type, provider, display_name)
            )
        """)
        logger.info("✅ Created merchant_integrations table")
        
        # Encrypted credentials storage
        await database.execute("""
            CREATE TABLE IF NOT EXISTS merchant_integration_secrets (
                id SERIAL PRIMARY KEY,
                integration_id INTEGER NOT NULL REFERENCES merchant_integrations(id) ON DELETE CASCADE,
                encrypted_credentials TEXT NOT NULL,
                encryption_key_version INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                rotated_at TIMESTAMP,
                expires_at TIMESTAMP,
                UNIQUE(integration_id)
            )
        """)
        logger.info("✅ Created merchant_integration_secrets table")
        
        # Integration settings
        await database.execute("""
            CREATE TABLE IF NOT EXISTS merchant_integration_settings (
                id SERIAL PRIMARY KEY,
                integration_id INTEGER NOT NULL REFERENCES merchant_integrations(id) ON DELETE CASCADE,
                settings JSONB DEFAULT '{}',
                webhook_url TEXT,
                webhook_secret TEXT,
                api_version VARCHAR(20),
                environment VARCHAR(20) DEFAULT 'production' CHECK (environment IN ('production', 'sandbox', 'test')),
                rate_limit INTEGER DEFAULT 100,
                timeout_seconds INTEGER DEFAULT 30,
                retry_count INTEGER DEFAULT 3,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(integration_id)
            )
        """)
        logger.info("✅ Created merchant_integration_settings table")
        
        # Webhook registrations
        await database.execute("""
            CREATE TABLE IF NOT EXISTS merchant_integration_webhooks (
                id SERIAL PRIMARY KEY,
                integration_id INTEGER NOT NULL REFERENCES merchant_integrations(id) ON DELETE CASCADE,
                event_type VARCHAR(100) NOT NULL,
                external_webhook_id VARCHAR(255),
                endpoint_url TEXT NOT NULL,
                secret VARCHAR(255),
                status VARCHAR(20) DEFAULT 'active' CHECK (status IN ('active', 'inactive', 'error')),
                last_received_at TIMESTAMP,
                failure_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        logger.info("✅ Created merchant_integration_webhooks table")
        
        # Routing rules
        await database.execute("""
            CREATE TABLE IF NOT EXISTS merchant_routing_rules (
                id SERIAL PRIMARY KEY,
                merchant_id VARCHAR(50) NOT NULL REFERENCES merchant_onboarding(merchant_id),
                rule_name VARCHAR(100) NOT NULL,
                rule_type VARCHAR(20) NOT NULL CHECK (rule_type IN ('psp', 'platform')),
                priority INTEGER DEFAULT 100,
                conditions JSONB NOT NULL,
                integration_id INTEGER NOT NULL REFERENCES merchant_integrations(id),
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(merchant_id, rule_name)
            )
        """)
        logger.info("✅ Created merchant_routing_rules table")
        
        # Integration logs for debugging
        await database.execute("""
            CREATE TABLE IF NOT EXISTS merchant_integration_logs (
                id SERIAL PRIMARY KEY,
                integration_id INTEGER NOT NULL REFERENCES merchant_integrations(id) ON DELETE CASCADE,
                log_type VARCHAR(20) NOT NULL CHECK (log_type IN ('api_call', 'webhook', 'error', 'config_change')),
                request_data JSONB,
                response_data JSONB,
                status_code INTEGER,
                error_message TEXT,
                duration_ms INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        logger.info("✅ Created merchant_integration_logs table")
        
        # Create indexes
        await database.execute("""
            CREATE INDEX IF NOT EXISTS idx_merchant_integrations_merchant_id 
            ON merchant_integrations(merchant_id);
            
            CREATE INDEX IF NOT EXISTS idx_merchant_integrations_status 
            ON merchant_integrations(status);
            
            CREATE INDEX IF NOT EXISTS idx_merchant_integration_logs_integration_id 
            ON merchant_integration_logs(integration_id);
            
            CREATE INDEX IF NOT EXISTS idx_merchant_integration_logs_created_at 
            ON merchant_integration_logs(created_at);
            
            CREATE INDEX IF NOT EXISTS idx_merchant_routing_rules_merchant_id 
            ON merchant_routing_rules(merchant_id);
        """)
        logger.info("✅ Created indexes for merchant integrations tables")
        
        return True
        
    except Exception as e:
        logger.error(f"❌ Failed to create merchant integrations tables: {str(e)}")
        return False


async def get_merchant_integrations(merchant_id: str, integration_type: Optional[str] = None) -> list:
    """Get all integrations for a merchant"""
    try:
        query = """
            SELECT 
                mi.*,
                mis.settings,
                mis.webhook_url,
                mis.api_version,
                mis.environment,
                COUNT(miw.id) as webhook_count,
                COUNT(CASE WHEN miw.status = 'active' THEN 1 END) as active_webhook_count
            FROM merchant_integrations mi
            LEFT JOIN merchant_integration_settings mis ON mi.id = mis.integration_id
            LEFT JOIN merchant_integration_webhooks miw ON mi.id = miw.integration_id
            WHERE mi.merchant_id = :merchant_id
        """
        
        params = {"merchant_id": merchant_id}
        
        if integration_type:
            query += " AND mi.integration_type = :integration_type"
            params["integration_type"] = integration_type
        
        query += """
            GROUP BY mi.id, mis.integration_id, mis.settings, mis.webhook_url, 
                     mis.api_version, mis.environment
            ORDER BY mi.is_primary DESC, mi.created_at DESC
        """
        
        return await database.fetch_all(query, params)
        
    except Exception as e:
        logger.error(f"Error fetching merchant integrations: {str(e)}")
        return []


async def create_merchant_integration(
    merchant_id: str,
    integration_type: str,
    provider: str,
    display_name: str,
    credentials: Dict[str, Any],
    settings: Optional[Dict[str, Any]] = None
) -> Optional[int]:
    """Create a new merchant integration"""
    try:
        # Start transaction
        async with database.transaction():
            # Create main integration record
            integration_id = await database.fetch_val("""
                INSERT INTO merchant_integrations 
                (merchant_id, integration_type, provider, display_name, status)
                VALUES (:merchant_id, :integration_type, :provider, :display_name, 'pending')
                RETURNING id
            """, {
                "merchant_id": merchant_id,
                "integration_type": integration_type,
                "provider": provider,
                "display_name": display_name
            })
            
            # Store encrypted credentials
            from utils.encryption import encrypt_data
            encrypted_creds = encrypt_data(credentials)
            
            await database.execute("""
                INSERT INTO merchant_integration_secrets
                (integration_id, encrypted_credentials)
                VALUES (:integration_id, :encrypted_credentials)
            """, {
                "integration_id": integration_id,
                "encrypted_credentials": encrypted_creds
            })
            
            # Store settings
            await database.execute("""
                INSERT INTO merchant_integration_settings
                (integration_id, settings)
                VALUES (:integration_id, :settings)
            """, {
                "integration_id": integration_id,
                "settings": settings or {}
            })
            
            logger.info(f"✅ Created integration {integration_id} for merchant {merchant_id}")
            return integration_id
            
    except Exception as e:
        logger.error(f"Error creating merchant integration: {str(e)}")
        return None


async def update_integration_status(
    integration_id: int,
    status: str,
    test_result: Optional[Dict[str, Any]] = None
) -> bool:
    """Update integration status"""
    try:
        await database.execute("""
            UPDATE merchant_integrations
            SET status = :status,
                last_tested_at = CURRENT_TIMESTAMP,
                last_test_result = :test_result,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = :integration_id
        """, {
            "integration_id": integration_id,
            "status": status,
            "test_result": test_result
        })
        return True
    except Exception as e:
        logger.error(f"Error updating integration status: {str(e)}")
        return False


async def log_integration_activity(
    integration_id: int,
    log_type: str,
    request_data: Optional[Dict[str, Any]] = None,
    response_data: Optional[Dict[str, Any]] = None,
    status_code: Optional[int] = None,
    error_message: Optional[str] = None,
    duration_ms: Optional[int] = None
) -> bool:
    """Log integration activity for debugging"""
    try:
        await database.execute("""
            INSERT INTO merchant_integration_logs
            (integration_id, log_type, request_data, response_data, 
             status_code, error_message, duration_ms)
            VALUES (:integration_id, :log_type, :request_data, :response_data,
                    :status_code, :error_message, :duration_ms)
        """, {
            "integration_id": integration_id,
            "log_type": log_type,
            "request_data": request_data,
            "response_data": response_data,
            "status_code": status_code,
            "error_message": error_message,
            "duration_ms": duration_ms
        })
        return True
    except Exception as e:
        logger.error(f"Error logging integration activity: {str(e)}")
        return False
