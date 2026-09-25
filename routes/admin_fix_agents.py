"""Admin endpoint to fix agent data (name/email) -- RETIRED (501), see below."""
from fastapi import APIRouter, Depends, HTTPException, status
from utils.auth import ADMIN_ROLES, get_current_user

# This module's whole body used to appear twice, the second copy rebinding `router`; the dead
# first copy is gone, so this is the one main.py mounts.
router = APIRouter(prefix="/admin/fix", tags=["Admin Fix"])

# RETIRED (501), both routes. They were written for the legacy agents table (name, email,
# company, use_case) that prod's does not have (probe 2026-09-24: the db/agents.py model's
# columns plus `email`); see pivota-backend#2305 / #2317.
# - POST /agents-data "fixed" agents with an empty name/email by filling name from
#   company/use_case and email with a made-up `<company>@example.com`. Its first query names
#   `name`, so on prod it 500'd before writing anything -- but prod does have `email`, so a fix
#   that only dropped the `name` reference would start stamping fabricated addresses on real
#   agents, and routes/agent_management.py matches a token's email against that column.
# - GET /agents-status counted and sampled rows by the same legacy columns (500 on prod). An
#   agent's display name is agent_name and its owner address is owner_email.
@router.post("/agents-data")
async def fix_agents_data(current_user: dict = Depends(get_current_user)):
    """Retired: answers 501 and writes nothing."""
    # Check if user is admin
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can execute this fix"
        )
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="The agents data fix is retired; it wrote placeholder names and emails",
    )

@router.get("/agents-status")
async def check_agents_status(current_user: dict = Depends(get_current_user)):
    """Retired: answers 501."""
    # Check if user is admin
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can check agent status"
        )
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="The agents status check is retired; it read legacy agents columns",
    )
