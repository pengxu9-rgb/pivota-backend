"""
Fix agents table schema -- RETIRED (501), see below.
"""
from fastapi import APIRouter, Depends, HTTPException
from utils.auth import require_admin_or_key

# AUTHENTICATION. Every route on this router was reachable with NO credentials
# of any kind: no Depends, no header check, no role check. The guard is applied
# at the ROUTER, not per-handler, so a route added here later inherits it
# instead of having to remember it -- which is how this file got here.
# require_admin_or_key accepts an X-ADMIN-KEY header or an admin/super_admin
# JWT and fails closed (401) when neither is present.
#
# POST /admin/fix/agents-table ran `DROP TABLE IF EXISTS agents CASCADE`
# for an anonymous caller.
router = APIRouter(prefix="/admin/fix", tags=["admin-fix"], dependencies=[Depends(require_admin_or_key)])

# RETIRED (501). For an admin caller it ran `DROP TABLE IF EXISTS agents CASCADE` -- every agent,
# and every constraint and view that depends on the table -- then re-created agents in a legacy
# shape (name, email, company, use_case, status, request_count, ...) that prod's table does not
# have (probe 2026-09-24: the db/agents.py model's columns plus `email`). Nothing it produced
# could serve: agent auth reads the model's columns (agent_name, api_key_hash, is_active). The
# table is built by metadata.create_all at startup; there is no "fix" to run here.
@router.post("/agents-table")
async def fix_agents_table():
    """Retired: answers 501 and touches nothing."""
    raise HTTPException(
        status_code=501,
        detail="The agents table fix is retired; the table is built from the db/agents.py model",
    )
