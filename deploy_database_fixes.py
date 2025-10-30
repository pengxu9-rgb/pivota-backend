#!/usr/bin/env python3
"""
Deploy database fixes to production
"""
import asyncpg
import asyncio
import os
from datetime import datetime

async def apply_fixes():
    """Apply database fixes to production"""
    
    # Get database URL from environment
    DATABASE_URL = os.environ.get('DATABASE_URL')
    if not DATABASE_URL:
        print("❌ DATABASE_URL not set")
        return
    
    # Connect to database
    conn = await asyncpg.connect(DATABASE_URL)
    
    try:
        print("🔧 Applying database fixes...")
        
        # 1. Add missing total_gmv column to agents table
        print("✅ Adding total_gmv column to agents table...")
        await conn.execute("""
            ALTER TABLE agents ADD COLUMN IF NOT EXISTS total_gmv NUMERIC(12,2) DEFAULT 0
        """)
        
        # 2. Add other missing columns to agents table
        print("✅ Adding other missing columns to agents table...")
        await conn.execute("""
            ALTER TABLE agents ADD COLUMN IF NOT EXISTS total_requests INTEGER DEFAULT 0
        """)
        await conn.execute("""
            ALTER TABLE agents ADD COLUMN IF NOT EXISTS total_orders INTEGER DEFAULT 0
        """)
        await conn.execute("""
            ALTER TABLE agents ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMP WITH TIME ZONE
        """)
        
        # 3. Fix request_id constraint
        print("✅ Fixing request_id constraint...")
        
        # Drop existing constraint
        await conn.execute("""
            ALTER TABLE agent_usage_logs DROP CONSTRAINT IF EXISTS agent_usage_logs_request_id_key
        """)
        
        # Clean up empty request_ids
        result = await conn.execute("""
            UPDATE agent_usage_logs SET request_id = NULL WHERE request_id = ''
        """)
        print(f"   - Updated {result.split()[-1]} empty request_ids")
        
        # Allow NULLs
        await conn.execute("""
            ALTER TABLE agent_usage_logs ALTER COLUMN request_id DROP NOT NULL
        """)
        
        # Re-add unique constraint
        await conn.execute("""
            ALTER TABLE agent_usage_logs ADD CONSTRAINT agent_usage_logs_request_id_key UNIQUE (request_id)
        """)
        
        # 4. Create indexes
        print("✅ Creating indexes...")
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_agent_usage_logs_agent_id_timestamp 
            ON agent_usage_logs(agent_id, timestamp DESC)
        """)
        
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_agents_agent_id 
            ON agents(agent_id)
        """)
        
        # 5. Verify fixes
        print("\n📊 Verifying fixes...")
        
        # Check agents columns
        rows = await conn.fetch("""
            SELECT column_name, data_type, is_nullable 
            FROM information_schema.columns 
            WHERE table_name = 'agents' 
            AND column_name IN ('total_gmv', 'total_requests', 'total_orders', 'last_used_at')
            ORDER BY column_name
        """)
        
        print("\nAgents table columns:")
        for row in rows:
            print(f"   - {row['column_name']}: {row['data_type']} (nullable: {row['is_nullable']})")
        
        # Check constraint
        constraint = await conn.fetchrow("""
            SELECT constraint_name, constraint_type 
            FROM information_schema.table_constraints 
            WHERE table_name = 'agent_usage_logs' 
            AND constraint_name = 'agent_usage_logs_request_id_key'
        """)
        
        if constraint:
            print(f"\nConstraint verified: {constraint['constraint_name']} ({constraint['constraint_type']})")
        
        print("\n✅ All fixes applied successfully!")
        
    except Exception as e:
        print(f"\n❌ Error applying fixes: {e}")
        raise
    finally:
        await conn.close()

if __name__ == "__main__":
    print(f"🚀 Starting database fixes at {datetime.now()}")
    asyncio.run(apply_fixes())
