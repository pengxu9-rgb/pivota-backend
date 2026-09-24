"""
Initialize API key for agent@test.com
Temporary admin endpoint -- RETIRED (501), see below.
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
# POST /admin/init/agent-test-key MINTED AN API KEY for an anonymous caller.
router = APIRouter(prefix="/admin/init", tags=["admin-init"], dependencies=[Depends(require_admin_or_key)])

# RETIRED (501). Both branches were wrong on prod's agents table (the db/agents.py `agents`
# model, plus an out-of-band email column):
# - agent@test.com absent: INSERT (name, email, company, status) names three columns the table
#   does not have, and skips agent_name / agent_type / api_key_hash (NOT NULL);
# - agent@test.com present: UPDATE agents SET api_key = <plaintext ak_live_ key> overwrote the
#   redacted:<agent_id> marker with a plaintext secret at rest, and handed back a key that did
#   not authenticate -- auth reads the sha256 in the key table (api_keys when it exists), which
#   this never wrote, and consults agents.api_key only when no key table exists or
#   AGENT_AUTH_ENABLE_LEGACY_API_KEY_FALLBACK is on (default off).
# Test agents register through POST /agent/account/register like any other agent.
@router.post("/agent-test-key")
async def initialize_agent_test_key():
    """Retired: answers 501 and writes nothing."""
    raise HTTPException(
        status_code=501,
        detail="Test agent key minting is retired; register through POST /agent/account/register",
    )
