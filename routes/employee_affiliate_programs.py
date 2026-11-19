"""Employee portal: the affiliate program registry (services/affiliate_programs.py, migration 290).

  DIAL   AFFILIATE_PROGRAM_REGISTRY_ENABLED, default off, read per request. Off: every path below is 404,
         before authentication (a router dependency), so the surface does not exist until it is turned on.
  READ   utils.auth.EMPLOYEE_STAFF_ROLES (super_admin, admin, employee).
  WRITE  utils.auth.ADMIN_ROLES (super_admin, admin). Every write records the actor in an append-only event.

Nothing here moves money, issues links or touches clicks.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from services import affiliate_programs as programs
from utils.auth import ADMIN_ROLES, EMPLOYEE_STAFF_ROLES, get_current_user


def _registry_enabled() -> None:
    if not programs.registry_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


router = APIRouter(prefix="/employee", tags=["employee-affiliate-programs"], dependencies=[Depends(_registry_enabled)])


def _require(current_user: Dict[str, Any], roles) -> str:
    if current_user.get("role") not in roles:
        raise HTTPException(status_code=403, detail="Not authorized")
    actor = current_user.get("email") or current_user.get("user_id") or current_user.get("sub") or "unknown"
    return str(actor)[:200]


def _invalid(exc: programs.ValidationFailed) -> HTTPException:
    return HTTPException(status_code=422, detail={"code": "invalid_fields", "errors": exc.errors})


_NOT_FOUND = HTTPException(status_code=404, detail={"code": "program_not_found"})
_DUPLICATE = HTTPException(status_code=409, detail={
    "code": "duplicate_program", "message": "A program with this network and advertiser ID already exists."})


@router.get("/affiliate-programs")
async def list_affiliate_programs(
    status: Optional[str] = Query(None),
    network: Optional[str] = Query(None),
    live: Optional[bool] = Query(None),
    q: Optional[str] = Query(None, max_length=200),
    current_user: dict = Depends(get_current_user),
):
    _require(current_user, EMPLOYEE_STAFF_ROLES)
    return await programs.list_view(status=status, network=network, live=live, q=q)


@router.post("/affiliate-programs", status_code=201)
async def create_affiliate_program(
    body: Dict[str, Any] = Body(...),
    current_user: dict = Depends(get_current_user),
):
    actor = _require(current_user, ADMIN_ROLES)
    try:
        return programs.public(await programs.create_program(body, actor))
    except programs.ValidationFailed as exc:
        raise _invalid(exc)
    except programs.DuplicateProgram:
        raise _DUPLICATE


@router.post("/affiliate-programs/import")
async def import_affiliate_programs(
    body: Dict[str, Any] = Body(...),
    dry_run: bool = Query(True),
    current_user: dict = Depends(get_current_user),
):
    """Body: {"csv": "<file text>"}. A dry run unless `dry_run=false`. Never sets a program live."""
    actor = _require(current_user, ADMIN_ROLES)
    try:
        return await programs.import_csv(body.get("csv"), dry_run=dry_run, actor=actor)
    except programs.ValidationFailed as exc:
        raise _invalid(exc)


@router.get("/affiliate-programs/{program_id}")
async def get_affiliate_program(program_id: str, current_user: dict = Depends(get_current_user)):
    _require(current_user, EMPLOYEE_STAFF_ROLES)
    try:
        return await programs.detail(program_id)
    except programs.ProgramNotFound:
        raise _NOT_FOUND


@router.patch("/affiliate-programs/{program_id}")
async def update_affiliate_program(
    program_id: str,
    body: Dict[str, Any] = Body(...),
    current_user: dict = Depends(get_current_user),
):
    actor = _require(current_user, ADMIN_ROLES)
    try:
        return programs.public(await programs.update_program(program_id, body, actor))
    except programs.ValidationFailed as exc:
        raise _invalid(exc)
    except programs.ProgramNotFound:
        raise _NOT_FOUND
    except programs.DuplicateProgram:
        raise _DUPLICATE


@router.post("/affiliate-programs/{program_id}/mark-live")
async def mark_affiliate_program_live(program_id: str, current_user: dict = Depends(get_current_user)):
    actor = _require(current_user, ADMIN_ROLES)
    try:
        return programs.public(await programs.mark_live(program_id, actor))
    except programs.ProgramNotFound:
        raise _NOT_FOUND
    except programs.NotLiveReady as exc:
        raise HTTPException(status_code=422, detail={
            "code": "not_live_ready",
            "reasons": [{"code": c, "message": programs.LIVE_BLOCKERS[c]} for c in exc.reasons],
        })


@router.post("/affiliate-programs/{program_id}/unlive")
async def unlive_affiliate_program(
    program_id: str,
    body: Dict[str, Any] = Body(...),
    current_user: dict = Depends(get_current_user),
):
    actor = _require(current_user, ADMIN_ROLES)
    try:
        return programs.public(await programs.unlive(program_id, actor, body.get("reason")))
    except programs.ValidationFailed as exc:
        raise _invalid(exc)
    except programs.ProgramNotFound:
        raise _NOT_FOUND
