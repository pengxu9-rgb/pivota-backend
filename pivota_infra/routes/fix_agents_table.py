"""
Fix agents table schema - add missing columns
This endpoint can be called once to update the agents table structure
"""
from fastapi import APIRouter, HTTPException
from db.database import database

router = APIRouter(prefix="/admin/fix", tags=["admin-fix"])

@router.post("/agents-table")
async def fix_agents_table():
    """
    Fix the agents table by dropping and recreating with correct schema
    WARNING: This will delete all existing agents!
    """
    try:
        # Drop the old table
        await database.execute("DROP TABLE IF EXISTS agents CASCADE")
        
        # Create with correct schema
        await database.execute("""
            CREATE TABLE agents (
                agent_id VARCHAR(50) PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                email VARCHAR(255) UNIQUE NOT NULL,
                company VARCHAR(255),
                use_case TEXT,
                api_key VARCHAR(255) UNIQUE,
                status VARCHAR(50) DEFAULT 'active',
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                last_active TIMESTAMP WITH TIME ZONE,
                last_key_rotation TIMESTAMP WITH TIME ZONE,
                deactivated_at TIMESTAMP WITH TIME ZONE,
                request_count INTEGER DEFAULT 0,
                success_rate FLOAT DEFAULT 0,
                rate_limit INTEGER DEFAULT 1000
            )
        """)
        
        return {
            "status": "success",
            "message": "Agents table recreated with correct schema"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fix agents table: {str(e)}")





