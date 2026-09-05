"""Admin endpoint to run migration 008 - Agents Phase 2"""
from fastapi import APIRouter, Depends, HTTPException
from utils.auth import ADMIN_ROLES, get_current_user
from db.database import database
import logging

router = APIRouter(prefix="/admin/migrations", tags=["Admin Migrations"])
logger = logging.getLogger(__name__)

@router.post("/run-008-agents-phase2")
async def run_migration_008(current_user: dict = Depends(get_current_user)):
    """Execute migration 008: Agents Advanced Schema"""
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin only")
    
    try:
        results = []
        
        # Step 1: Create agent_api_keys table
        await database.execute("""
            CREATE TABLE IF NOT EXISTS agent_api_keys (
                id SERIAL PRIMARY KEY,
                agent_id VARCHAR(50) NOT NULL,
                key_id VARCHAR(50) UNIQUE NOT NULL,
                key_hash VARCHAR(255) NOT NULL,
                key_prefix VARCHAR(20) NOT NULL,
                scopes JSON DEFAULT '["orders:read", "products:read"]'::json,
                ip_whitelist JSON DEFAULT '[]'::json,
                is_active BOOLEAN DEFAULT true,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                expires_at TIMESTAMP WITH TIME ZONE,
                last_used_at TIMESTAMP WITH TIME ZONE,
                last_rotated_at TIMESTAMP WITH TIME ZONE,
                created_by VARCHAR(100),
                
                FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE
            )
        """)
        results.append("✅ agent_api_keys table created")
        
        # Create indexes
        await database.execute("""
            CREATE INDEX IF NOT EXISTS idx_agent_api_keys_agent_id ON agent_api_keys(agent_id)
        """)
        await database.execute("""
            CREATE INDEX IF NOT EXISTS idx_agent_api_keys_is_active ON agent_api_keys(is_active)
        """)
        await database.execute("""
            CREATE INDEX IF NOT EXISTS idx_agent_api_keys_key_id ON agent_api_keys(key_id)
        """)
        results.append("✅ agent_api_keys indexes created")
        
        # Step 2: Create agent_protocols table
        await database.execute("""
            CREATE TABLE IF NOT EXISTS agent_protocols (
                id SERIAL PRIMARY KEY,
                agent_id VARCHAR(50) NOT NULL,
                protocol_name VARCHAR(50) NOT NULL,
                version VARCHAR(20),
                status VARCHAR(20) DEFAULT 'active',
                last_verified_at TIMESTAMP WITH TIME ZONE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                
                FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE,
                UNIQUE(agent_id, protocol_name, version)
            )
        """)
        results.append("✅ agent_protocols table created")
        
        await database.execute("""
            CREATE INDEX IF NOT EXISTS idx_agent_protocols_agent_id ON agent_protocols(agent_id)
        """)
        await database.execute("""
            CREATE INDEX IF NOT EXISTS idx_agent_protocols_status ON agent_protocols(status)
        """)
        results.append("✅ agent_protocols indexes created")
        
        # Step 3: Create agent_performance_stats table
        await database.execute("""
            CREATE TABLE IF NOT EXISTS agent_performance_stats (
                id SERIAL PRIMARY KEY,
                agent_id VARCHAR(50) NOT NULL,
                period_start TIMESTAMP WITH TIME ZONE NOT NULL,
                period_end TIMESTAMP WITH TIME ZONE NOT NULL,
                total_requests INTEGER DEFAULT 0,
                success_count INTEGER DEFAULT 0,
                fail_count INTEGER DEFAULT 0,
                success_rate NUMERIC(5, 2) DEFAULT 0,
                avg_latency_ms INTEGER DEFAULT 0,
                total_gmv NUMERIC(12, 2) DEFAULT 0,
                total_orders INTEGER DEFAULT 0,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                
                FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE,
                UNIQUE(agent_id, period_start)
            )
        """)
        results.append("✅ agent_performance_stats table created")
        
        await database.execute("""
            CREATE INDEX IF NOT EXISTS idx_agent_perf_stats_agent_id ON agent_performance_stats(agent_id)
        """)
        await database.execute("""
            CREATE INDEX IF NOT EXISTS idx_agent_perf_stats_period ON agent_performance_stats(period_start DESC)
        """)
        results.append("✅ agent_performance_stats indexes created")
        
        # Step 4: Migrate existing api_key to agent_api_keys
        migrate_result = await database.execute("""
            INSERT INTO agent_api_keys (
                agent_id, 
                key_id, 
                key_hash, 
                key_prefix,
                scopes,
                is_active,
                created_at
            )
            SELECT 
                agent_id,
                CONCAT('key_', SUBSTRING(MD5(RANDOM()::TEXT), 1, 12)) as key_id,
                MD5(api_key) as key_hash,
                SUBSTRING(api_key, 1, 12) || '...' as key_prefix,
                '["orders:read", "products:read", "orders:write"]'::json as scopes,
                true as is_active,
                created_at
            FROM agents
            WHERE api_key IS NOT NULL
                AND NOT EXISTS (
                    SELECT 1 FROM agent_api_keys WHERE agent_api_keys.agent_id = agents.agent_id
                )
            ON CONFLICT (key_id) DO NOTHING
        """)
        results.append(f"✅ Migrated existing API keys: {migrate_result} records")
        
        # Step 5: Add default REST protocol
        protocol_result = await database.execute("""
            INSERT INTO agent_protocols (agent_id, protocol_name, version, status, last_verified_at)
            SELECT 
                agent_id,
                'REST' as protocol_name,
                '1.0' as version,
                'active' as status,
                NOW() as last_verified_at
            FROM agents
            WHERE NOT EXISTS (
                SELECT 1 FROM agent_protocols 
                WHERE agent_protocols.agent_id = agents.agent_id 
                    AND protocol_name = 'REST'
            )
            ON CONFLICT (agent_id, protocol_name, version) DO NOTHING
        """)
        results.append(f"✅ Added default REST protocol: {protocol_result} records")
        
        # Verify tables
        check = await database.fetch_one("""
            SELECT 
                (SELECT COUNT(*) FROM agent_api_keys) as api_keys_count,
                (SELECT COUNT(*) FROM agent_protocols) as protocols_count,
                (SELECT COUNT(*) FROM agent_performance_stats) as stats_count
        """)
        
        check_dict = dict(check) if check else {}
        
        return {
            "status": "success",
            "message": "Migration 008 completed successfully",
            "steps": results,
            "verification": {
                "agent_api_keys_count": check_dict.get("api_keys_count", 0),
                "agent_protocols_count": check_dict.get("protocols_count", 0),
                "agent_performance_stats_count": check_dict.get("stats_count", 0)
            }
        }
    
    except Exception as e:
        logger.error(f"Migration 008 failed: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Migration failed: {str(e)}")

@router.get("/check-008-status")
async def check_migration_008_status(current_user: dict = Depends(get_current_user)):
    """Check if migration 008 has been run"""
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin only")
    
    try:
        # Check if tables exist
        tables_check = await database.fetch_one("""
            SELECT 
                EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'agent_api_keys') as has_api_keys,
                EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'agent_protocols') as has_protocols,
                EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'agent_performance_stats') as has_stats
        """)
        
        check_dict = dict(tables_check) if tables_check else {}
        
        all_exist = (
            check_dict.get("has_api_keys") and 
            check_dict.get("has_protocols") and 
            check_dict.get("has_stats")
        )
        
        counts = {}
        if all_exist:
            counts_result = await database.fetch_one("""
                SELECT 
                    (SELECT COUNT(*) FROM agent_api_keys) as api_keys,
                    (SELECT COUNT(*) FROM agent_protocols) as protocols,
                    (SELECT COUNT(*) FROM agent_performance_stats) as stats
            """)
            counts = dict(counts_result) if counts_result else {}
        
        return {
            "migration_completed": all_exist,
            "tables_exist": {
                "agent_api_keys": check_dict.get("has_api_keys", False),
                "agent_protocols": check_dict.get("has_protocols", False),
                "agent_performance_stats": check_dict.get("has_stats", False)
            },
            "record_counts": counts if all_exist else None,
            "recommendation": "Run POST /admin/migrations/run-008-agents-phase2" if not all_exist else "Migration already complete"
        }
    
    except Exception as e:
        logger.error(f"Check migration 008 status failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


from fastapi import APIRouter, Depends, HTTPException
from utils.auth import get_current_user
from db.database import database
import logging

router = APIRouter(prefix="/admin/migrations", tags=["Admin Migrations"])
logger = logging.getLogger(__name__)

@router.post("/run-008-agents-phase2")
async def run_migration_008(current_user: dict = Depends(get_current_user)):
    """Execute migration 008: Agents Advanced Schema"""
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin only")
    
    try:
        results = []
        
        # Step 1: Create agent_api_keys table
        await database.execute("""
            CREATE TABLE IF NOT EXISTS agent_api_keys (
                id SERIAL PRIMARY KEY,
                agent_id VARCHAR(50) NOT NULL,
                key_id VARCHAR(50) UNIQUE NOT NULL,
                key_hash VARCHAR(255) NOT NULL,
                key_prefix VARCHAR(20) NOT NULL,
                scopes JSON DEFAULT '["orders:read", "products:read"]'::json,
                ip_whitelist JSON DEFAULT '[]'::json,
                is_active BOOLEAN DEFAULT true,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                expires_at TIMESTAMP WITH TIME ZONE,
                last_used_at TIMESTAMP WITH TIME ZONE,
                last_rotated_at TIMESTAMP WITH TIME ZONE,
                created_by VARCHAR(100),
                
                FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE
            )
        """)
        results.append("✅ agent_api_keys table created")
        
        # Create indexes
        await database.execute("""
            CREATE INDEX IF NOT EXISTS idx_agent_api_keys_agent_id ON agent_api_keys(agent_id)
        """)
        await database.execute("""
            CREATE INDEX IF NOT EXISTS idx_agent_api_keys_is_active ON agent_api_keys(is_active)
        """)
        await database.execute("""
            CREATE INDEX IF NOT EXISTS idx_agent_api_keys_key_id ON agent_api_keys(key_id)
        """)
        results.append("✅ agent_api_keys indexes created")
        
        # Step 2: Create agent_protocols table
        await database.execute("""
            CREATE TABLE IF NOT EXISTS agent_protocols (
                id SERIAL PRIMARY KEY,
                agent_id VARCHAR(50) NOT NULL,
                protocol_name VARCHAR(50) NOT NULL,
                version VARCHAR(20),
                status VARCHAR(20) DEFAULT 'active',
                last_verified_at TIMESTAMP WITH TIME ZONE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                
                FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE,
                UNIQUE(agent_id, protocol_name, version)
            )
        """)
        results.append("✅ agent_protocols table created")
        
        await database.execute("""
            CREATE INDEX IF NOT EXISTS idx_agent_protocols_agent_id ON agent_protocols(agent_id)
        """)
        await database.execute("""
            CREATE INDEX IF NOT EXISTS idx_agent_protocols_status ON agent_protocols(status)
        """)
        results.append("✅ agent_protocols indexes created")
        
        # Step 3: Create agent_performance_stats table
        await database.execute("""
            CREATE TABLE IF NOT EXISTS agent_performance_stats (
                id SERIAL PRIMARY KEY,
                agent_id VARCHAR(50) NOT NULL,
                period_start TIMESTAMP WITH TIME ZONE NOT NULL,
                period_end TIMESTAMP WITH TIME ZONE NOT NULL,
                total_requests INTEGER DEFAULT 0,
                success_count INTEGER DEFAULT 0,
                fail_count INTEGER DEFAULT 0,
                success_rate NUMERIC(5, 2) DEFAULT 0,
                avg_latency_ms INTEGER DEFAULT 0,
                total_gmv NUMERIC(12, 2) DEFAULT 0,
                total_orders INTEGER DEFAULT 0,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                
                FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE,
                UNIQUE(agent_id, period_start)
            )
        """)
        results.append("✅ agent_performance_stats table created")
        
        await database.execute("""
            CREATE INDEX IF NOT EXISTS idx_agent_perf_stats_agent_id ON agent_performance_stats(agent_id)
        """)
        await database.execute("""
            CREATE INDEX IF NOT EXISTS idx_agent_perf_stats_period ON agent_performance_stats(period_start DESC)
        """)
        results.append("✅ agent_performance_stats indexes created")
        
        # Step 4: Migrate existing api_key to agent_api_keys
        migrate_result = await database.execute("""
            INSERT INTO agent_api_keys (
                agent_id, 
                key_id, 
                key_hash, 
                key_prefix,
                scopes,
                is_active,
                created_at
            )
            SELECT 
                agent_id,
                CONCAT('key_', SUBSTRING(MD5(RANDOM()::TEXT), 1, 12)) as key_id,
                MD5(api_key) as key_hash,
                SUBSTRING(api_key, 1, 12) || '...' as key_prefix,
                '["orders:read", "products:read", "orders:write"]'::json as scopes,
                true as is_active,
                created_at
            FROM agents
            WHERE api_key IS NOT NULL
                AND NOT EXISTS (
                    SELECT 1 FROM agent_api_keys WHERE agent_api_keys.agent_id = agents.agent_id
                )
            ON CONFLICT (key_id) DO NOTHING
        """)
        results.append(f"✅ Migrated existing API keys: {migrate_result} records")
        
        # Step 5: Add default REST protocol
        protocol_result = await database.execute("""
            INSERT INTO agent_protocols (agent_id, protocol_name, version, status, last_verified_at)
            SELECT 
                agent_id,
                'REST' as protocol_name,
                '1.0' as version,
                'active' as status,
                NOW() as last_verified_at
            FROM agents
            WHERE NOT EXISTS (
                SELECT 1 FROM agent_protocols 
                WHERE agent_protocols.agent_id = agents.agent_id 
                    AND protocol_name = 'REST'
            )
            ON CONFLICT (agent_id, protocol_name, version) DO NOTHING
        """)
        results.append(f"✅ Added default REST protocol: {protocol_result} records")
        
        # Verify tables
        check = await database.fetch_one("""
            SELECT 
                (SELECT COUNT(*) FROM agent_api_keys) as api_keys_count,
                (SELECT COUNT(*) FROM agent_protocols) as protocols_count,
                (SELECT COUNT(*) FROM agent_performance_stats) as stats_count
        """)
        
        check_dict = dict(check) if check else {}
        
        return {
            "status": "success",
            "message": "Migration 008 completed successfully",
            "steps": results,
            "verification": {
                "agent_api_keys_count": check_dict.get("api_keys_count", 0),
                "agent_protocols_count": check_dict.get("protocols_count", 0),
                "agent_performance_stats_count": check_dict.get("stats_count", 0)
            }
        }
    
    except Exception as e:
        logger.error(f"Migration 008 failed: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Migration failed: {str(e)}")

@router.get("/check-008-status")
async def check_migration_008_status(current_user: dict = Depends(get_current_user)):
    """Check if migration 008 has been run"""
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin only")
    
    try:
        # Check if tables exist
        tables_check = await database.fetch_one("""
            SELECT 
                EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'agent_api_keys') as has_api_keys,
                EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'agent_protocols') as has_protocols,
                EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'agent_performance_stats') as has_stats
        """)
        
        check_dict = dict(tables_check) if tables_check else {}
        
        all_exist = (
            check_dict.get("has_api_keys") and 
            check_dict.get("has_protocols") and 
            check_dict.get("has_stats")
        )
        
        counts = {}
        if all_exist:
            counts_result = await database.fetch_one("""
                SELECT 
                    (SELECT COUNT(*) FROM agent_api_keys) as api_keys,
                    (SELECT COUNT(*) FROM agent_protocols) as protocols,
                    (SELECT COUNT(*) FROM agent_performance_stats) as stats
            """)
            counts = dict(counts_result) if counts_result else {}
        
        return {
            "migration_completed": all_exist,
            "tables_exist": {
                "agent_api_keys": check_dict.get("has_api_keys", False),
                "agent_protocols": check_dict.get("has_protocols", False),
                "agent_performance_stats": check_dict.get("has_stats", False)
            },
            "record_counts": counts if all_exist else None,
            "recommendation": "Run POST /admin/migrations/run-008-agents-phase2" if not all_exist else "Migration already complete"
        }
    
    except Exception as e:
        logger.error(f"Check migration 008 status failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


