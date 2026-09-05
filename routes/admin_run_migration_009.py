"""Admin endpoint to run migration 009 - Agents Phase 3 Observability"""
from fastapi import APIRouter, Depends, HTTPException
from utils.auth import ADMIN_ROLES, get_current_user
from db.database import database
import logging

router = APIRouter(prefix="/admin/migrations", tags=["Admin Migrations"])
logger = logging.getLogger(__name__)

@router.post("/run-009-agents-phase3")
async def run_migration_009(current_user: dict = Depends(get_current_user)):
    """Execute migration 009: Agent Observability & Governance"""
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin only")
    
    try:
        results = []
        
        # Step 1: Drop and recreate agent_metrics table with correct schema
        await database.execute("DROP TABLE IF EXISTS agent_metrics CASCADE")
        results.append("✅ Dropped old agent_metrics table if exists")
        
        await database.execute("""
            CREATE TABLE agent_metrics (
                id SERIAL PRIMARY KEY,
                agent_id VARCHAR(50) NOT NULL,
                timestamp TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                avg_response_time_ms INTEGER DEFAULT 0,
                success_rate NUMERIC(5, 2) DEFAULT 0,
                error_rate NUMERIC(5, 2) DEFAULT 0,
                queries_per_min INTEGER DEFAULT 0,
                total_queries_count INTEGER DEFAULT 0,
                period_minutes INTEGER DEFAULT 5,
                last_seen_at TIMESTAMP WITH TIME ZONE,
                collected_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                
                FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE
            )
        """)
        results.append("✅ agent_metrics table created")
        
        # Indexes
        await database.execute("CREATE INDEX IF NOT EXISTS idx_agent_metrics_agent_ts ON agent_metrics(agent_id, timestamp DESC)")
        await database.execute("CREATE INDEX IF NOT EXISTS idx_agent_metrics_last_seen ON agent_metrics(agent_id, last_seen_at DESC)")
        await database.execute("CREATE INDEX IF NOT EXISTS idx_agent_metrics_collected ON agent_metrics(collected_at DESC)")
        results.append("✅ agent_metrics indexes created")
        
        # Step 2: Create agent_alerts table
        await database.execute("""
            CREATE TABLE IF NOT EXISTS agent_alerts (
                id SERIAL PRIMARY KEY,
                alert_id VARCHAR(50) UNIQUE NOT NULL,
                agent_id VARCHAR(50) NOT NULL,
                alert_type VARCHAR(50) NOT NULL,
                severity VARCHAR(20) NOT NULL,
                message TEXT NOT NULL,
                metadata JSON,
                resolved BOOLEAN DEFAULT false,
                resolved_at TIMESTAMP WITH TIME ZONE,
                resolved_by VARCHAR(100),
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                
                FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE
            )
        """)
        results.append("✅ agent_alerts table created")
        
        await database.execute("CREATE INDEX IF NOT EXISTS idx_agent_alerts_agent ON agent_alerts(agent_id, resolved, created_at DESC)")
        await database.execute("CREATE INDEX IF NOT EXISTS idx_agent_alerts_severity ON agent_alerts(severity, resolved)")
        await database.execute("CREATE INDEX IF NOT EXISTS idx_agent_alerts_type ON agent_alerts(alert_type, resolved)")
        results.append("✅ agent_alerts indexes created")
        
        # Step 3: Create governance_actions_log table
        await database.execute("""
            CREATE TABLE IF NOT EXISTS governance_actions_log (
                id SERIAL PRIMARY KEY,
                action_id VARCHAR(50) UNIQUE NOT NULL,
                agent_id VARCHAR(50) NOT NULL,
                action_type VARCHAR(50) NOT NULL,
                triggered_by VARCHAR(20) NOT NULL,
                executed_by VARCHAR(100),
                action_payload JSON,
                status VARCHAR(20) DEFAULT 'pending',
                reason TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                executed_at TIMESTAMP WITH TIME ZONE,
                
                FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE
            )
        """)
        results.append("✅ governance_actions_log table created")
        
        await database.execute("CREATE INDEX IF NOT EXISTS idx_gov_actions_agent ON governance_actions_log(agent_id, status, created_at DESC)")
        await database.execute("CREATE INDEX IF NOT EXISTS idx_gov_actions_status ON governance_actions_log(status, created_at DESC)")
        await database.execute("CREATE INDEX IF NOT EXISTS idx_gov_actions_type ON governance_actions_log(action_type, status)")
        results.append("✅ governance_actions_log indexes created")
        
        # Verify tables
        check = await database.fetch_one("""
            SELECT 
                (SELECT COUNT(*) FROM agent_metrics) as metrics_count,
                (SELECT COUNT(*) FROM agent_alerts) as alerts_count,
                (SELECT COUNT(*) FROM governance_actions_log) as actions_count
        """)
        
        check_dict = dict(check) if check else {}
        
        return {
            "status": "success",
            "message": "Migration 009 completed successfully",
            "steps": results,
            "verification": {
                "agent_metrics_count": check_dict.get("metrics_count", 0),
                "agent_alerts_count": check_dict.get("alerts_count", 0),
                "governance_actions_count": check_dict.get("actions_count", 0)
            }
        }
    
    except Exception as e:
        logger.error(f"Migration 009 failed: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Migration failed: {str(e)}")

@router.get("/check-009-status")
async def check_migration_009_status(current_user: dict = Depends(get_current_user)):
    """Check if migration 009 has been run"""
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin only")
    
    try:
        tables_check = await database.fetch_one("""
            SELECT 
                EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'agent_metrics') as has_metrics,
                EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'agent_alerts') as has_alerts,
                EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'governance_actions_log') as has_gov_log
        """)
        
        check_dict = dict(tables_check) if tables_check else {}
        
        all_exist = (
            check_dict.get("has_metrics") and 
            check_dict.get("has_alerts") and 
            check_dict.get("has_gov_log")
        )
        
        counts = {}
        if all_exist:
            counts_result = await database.fetch_one("""
                SELECT 
                    (SELECT COUNT(*) FROM agent_metrics) as metrics,
                    (SELECT COUNT(*) FROM agent_alerts) as alerts,
                    (SELECT COUNT(*) FROM governance_actions_log) as actions
            """)
            counts = dict(counts_result) if counts_result else {}
        
        return {
            "migration_completed": all_exist,
            "tables_exist": {
                "agent_metrics": check_dict.get("has_metrics", False),
                "agent_alerts": check_dict.get("has_alerts", False),
                "governance_actions_log": check_dict.get("has_gov_log", False)
            },
            "record_counts": counts if all_exist else None,
            "recommendation": "Run POST /admin/migrations/run-009-agents-phase3" if not all_exist else "Migration already complete"
        }
    
    except Exception as e:
        logger.error(f"Check migration 009 status failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


from utils.auth import get_current_user
from db.database import database
import logging

router = APIRouter(prefix="/admin/migrations", tags=["Admin Migrations"])
logger = logging.getLogger(__name__)

@router.post("/run-009-agents-phase3")
async def run_migration_009(current_user: dict = Depends(get_current_user)):
    """Execute migration 009: Agent Observability & Governance"""
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin only")
    
    try:
        results = []
        
        # Step 1: Drop and recreate agent_metrics table with correct schema
        await database.execute("DROP TABLE IF EXISTS agent_metrics CASCADE")
        results.append("✅ Dropped old agent_metrics table if exists")
        
        await database.execute("""
            CREATE TABLE agent_metrics (
                id SERIAL PRIMARY KEY,
                agent_id VARCHAR(50) NOT NULL,
                timestamp TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                avg_response_time_ms INTEGER DEFAULT 0,
                success_rate NUMERIC(5, 2) DEFAULT 0,
                error_rate NUMERIC(5, 2) DEFAULT 0,
                queries_per_min INTEGER DEFAULT 0,
                total_queries_count INTEGER DEFAULT 0,
                period_minutes INTEGER DEFAULT 5,
                last_seen_at TIMESTAMP WITH TIME ZONE,
                collected_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                
                FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE
            )
        """)
        results.append("✅ agent_metrics table created")
        
        # Indexes
        await database.execute("CREATE INDEX IF NOT EXISTS idx_agent_metrics_agent_ts ON agent_metrics(agent_id, timestamp DESC)")
        await database.execute("CREATE INDEX IF NOT EXISTS idx_agent_metrics_last_seen ON agent_metrics(agent_id, last_seen_at DESC)")
        await database.execute("CREATE INDEX IF NOT EXISTS idx_agent_metrics_collected ON agent_metrics(collected_at DESC)")
        results.append("✅ agent_metrics indexes created")
        
        # Step 2: Create agent_alerts table
        await database.execute("""
            CREATE TABLE IF NOT EXISTS agent_alerts (
                id SERIAL PRIMARY KEY,
                alert_id VARCHAR(50) UNIQUE NOT NULL,
                agent_id VARCHAR(50) NOT NULL,
                alert_type VARCHAR(50) NOT NULL,
                severity VARCHAR(20) NOT NULL,
                message TEXT NOT NULL,
                metadata JSON,
                resolved BOOLEAN DEFAULT false,
                resolved_at TIMESTAMP WITH TIME ZONE,
                resolved_by VARCHAR(100),
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                
                FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE
            )
        """)
        results.append("✅ agent_alerts table created")
        
        await database.execute("CREATE INDEX IF NOT EXISTS idx_agent_alerts_agent ON agent_alerts(agent_id, resolved, created_at DESC)")
        await database.execute("CREATE INDEX IF NOT EXISTS idx_agent_alerts_severity ON agent_alerts(severity, resolved)")
        await database.execute("CREATE INDEX IF NOT EXISTS idx_agent_alerts_type ON agent_alerts(alert_type, resolved)")
        results.append("✅ agent_alerts indexes created")
        
        # Step 3: Create governance_actions_log table
        await database.execute("""
            CREATE TABLE IF NOT EXISTS governance_actions_log (
                id SERIAL PRIMARY KEY,
                action_id VARCHAR(50) UNIQUE NOT NULL,
                agent_id VARCHAR(50) NOT NULL,
                action_type VARCHAR(50) NOT NULL,
                triggered_by VARCHAR(20) NOT NULL,
                executed_by VARCHAR(100),
                action_payload JSON,
                status VARCHAR(20) DEFAULT 'pending',
                reason TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                executed_at TIMESTAMP WITH TIME ZONE,
                
                FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE
            )
        """)
        results.append("✅ governance_actions_log table created")
        
        await database.execute("CREATE INDEX IF NOT EXISTS idx_gov_actions_agent ON governance_actions_log(agent_id, status, created_at DESC)")
        await database.execute("CREATE INDEX IF NOT EXISTS idx_gov_actions_status ON governance_actions_log(status, created_at DESC)")
        await database.execute("CREATE INDEX IF NOT EXISTS idx_gov_actions_type ON governance_actions_log(action_type, status)")
        results.append("✅ governance_actions_log indexes created")
        
        # Verify tables
        check = await database.fetch_one("""
            SELECT 
                (SELECT COUNT(*) FROM agent_metrics) as metrics_count,
                (SELECT COUNT(*) FROM agent_alerts) as alerts_count,
                (SELECT COUNT(*) FROM governance_actions_log) as actions_count
        """)
        
        check_dict = dict(check) if check else {}
        
        return {
            "status": "success",
            "message": "Migration 009 completed successfully",
            "steps": results,
            "verification": {
                "agent_metrics_count": check_dict.get("metrics_count", 0),
                "agent_alerts_count": check_dict.get("alerts_count", 0),
                "governance_actions_count": check_dict.get("actions_count", 0)
            }
        }
    
    except Exception as e:
        logger.error(f"Migration 009 failed: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Migration failed: {str(e)}")

@router.get("/check-009-status")
async def check_migration_009_status(current_user: dict = Depends(get_current_user)):
    """Check if migration 009 has been run"""
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin only")
    
    try:
        tables_check = await database.fetch_one("""
            SELECT 
                EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'agent_metrics') as has_metrics,
                EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'agent_alerts') as has_alerts,
                EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'governance_actions_log') as has_gov_log
        """)
        
        check_dict = dict(tables_check) if tables_check else {}
        
        all_exist = (
            check_dict.get("has_metrics") and 
            check_dict.get("has_alerts") and 
            check_dict.get("has_gov_log")
        )
        
        counts = {}
        if all_exist:
            counts_result = await database.fetch_one("""
                SELECT 
                    (SELECT COUNT(*) FROM agent_metrics) as metrics,
                    (SELECT COUNT(*) FROM agent_alerts) as alerts,
                    (SELECT COUNT(*) FROM governance_actions_log) as actions
            """)
            counts = dict(counts_result) if counts_result else {}
        
        return {
            "migration_completed": all_exist,
            "tables_exist": {
                "agent_metrics": check_dict.get("has_metrics", False),
                "agent_alerts": check_dict.get("has_alerts", False),
                "governance_actions_log": check_dict.get("has_gov_log", False)
            },
            "record_counts": counts if all_exist else None,
            "recommendation": "Run POST /admin/migrations/run-009-agents-phase3" if not all_exist else "Migration already complete"
        }
    
    except Exception as e:
        logger.error(f"Check migration 009 status failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

