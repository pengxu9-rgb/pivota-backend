"""
Debug agents table schema and data
"""
from fastapi import APIRouter
from db.database import database

router = APIRouter(prefix="/admin/debug", tags=["debug"])

@router.get("/agents-table-schema")
async def check_agents_table_schema():
    """Check agents table column structure"""
    try:
        schema_query = """
            SELECT column_name, data_type, character_maximum_length, is_nullable
            FROM information_schema.columns
            WHERE table_name = 'agents'
            ORDER BY ordinal_position
        """
        columns = await database.fetch_all(schema_query)
        
        return {
            "status": "success",
            "table": "agents",
            "columns": [
                {
                    "name": col["column_name"],
                    "type": col["data_type"],
                    "max_length": col["character_maximum_length"],
                    "nullable": col["is_nullable"]
                }
                for col in columns
            ],
            "total_columns": len(columns)
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}

@router.get("/agents-table-data")
async def check_agents_table_data():
    """Check all agent records"""
    try:
        agents_query = "SELECT * FROM agents ORDER BY created_at DESC LIMIT 10"
        agents = await database.fetch_all(agents_query)
        
        return {
            "status": "success",
            "agents": [dict(agent) for agent in agents],
            "count": len(agents)
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}

@router.get("/test-agent-lookup")
async def test_agent_lookup():
    """Test looking up agent@test.com by API key"""
    try:
        # First get the agent record
        agent_query = "SELECT * FROM agents WHERE email = 'agent@test.com' LIMIT 1"
        agent = await database.fetch_one(agent_query)
        
        if not agent:
            return {"status": "not_found", "message": "agent@test.com not in agents table"}
        
        agent_dict = dict(agent)
        api_key = agent_dict.get("api_key")
        
        # Now test lookup by API key
        lookup_query = "SELECT * FROM agents WHERE api_key = :api_key LIMIT 1"
        lookup_result = await database.fetch_one(lookup_query, {"api_key": api_key})
        
        return {
            "status": "success",
            "agent_record": {
                "agent_id": agent_dict.get("agent_id"),
                "email": agent_dict.get("email"),
                "name": agent_dict.get("name"),
                "agent_name": agent_dict.get("agent_name"),
                "api_key_prefix": api_key[:15] if api_key else None,
                "api_key_length": len(api_key) if api_key else 0,
                "has_api_key_hash": "api_key_hash" in agent_dict,
                "is_active": agent_dict.get("is_active"),
                "status": agent_dict.get("status"),
            },
            "lookup_by_key_works": lookup_result is not None,
            "all_fields": list(agent_dict.keys())
        }
    except Exception as e:
        import traceback
        return {
            "status": "error",
            "error": str(e),
            "traceback": traceback.format_exc()
        }


