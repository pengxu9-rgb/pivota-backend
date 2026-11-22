"""Employee portal: the affiliate program registry (services/affiliate_programs.py, migration 291).

  DIAL   AFFILIATE_PROGRAM_REGISTRY_ENABLED, default off, read per request. Off: every request under PREFIX is
         the app's own 404 for any method, body or sub-path, answered BEFORE routing by RegistryDarkMiddleware
         (registered in main.py), so no 405 or 400 can show that the surface exists. The router dependency
         repeats the check for any app that mounts the router without the middleware.
  READ   utils.auth.EMPLOYEE_STAFF_ROLES (super_admin, admin, employee).
  WRITE  utils.auth.ADMIN_ROLES (super_admin, admin). Every write records the actor in an append-only event.

Nothing here moves money, issues links or touches clicks.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from starlette.responses import JSONResponse

from services import affiliate_programs as programs
from utils.auth import ADMIN_ROLES, EMPLOYEE_STAFF_ROLES, get_current_user


PREFIX = "/employee/affiliate-programs"


def _registry_enabled() -> None:
    if not programs.registry_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


class RegistryDarkMiddleware:
    """Pure ASGI. With the dial off, any HTTP request whose path is PREFIX or below it gets the 404 an absent
    path gets ({"detail": "Not Found"}), before routing, body parsing or auth. OPTIONS is left alone: the
    app answers OPTIONS on every path alike (cors_preflight_passthrough), so it reveals nothing."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("method") != "OPTIONS":
            path = scope.get("path") or ""
            if (path == PREFIX or path.startswith(PREFIX + "/")) and not programs.registry_enabled():
                await JSONResponse({"detail": "Not Found"}, status_code=404)(scope, receive, send)
                return
        await self.app(scope, receive, send)


router = APIRouter(prefix="/employee", tags=["employee-affiliate-programs"], dependencies=[Depends(_registry_enabled)])


def _require(current_user: Dict[str, Any], roles) -> str:
    if current_user.get("role") not in roles:
        raise HTTPException(status_code=403, detail="Not authorized")
    actor = current_user.get("email") or current_user.get("user_id") or current_user.get("sub") or "unknown"
    return str(actor)[:200]


def _invalid(exc: programs.ValidationFailed) -> HTTPException:
    return HTTPException(status_code=422, detail={"code": "invalid_fields", "errors": exc.errors})


# Every answer below is a NEW HTTPException per raise. One shared module-level instance would grow its
# __traceback__ on every raise and keep each failed request's frames (and its body) alive.


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail={"code": "program_not_found"})


def _duplicate() -> HTTPException:
    return HTTPException(status_code=409, detail={
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
    try:
        return await programs.list_view(status=status, network=network, live=live, q=q)
    except programs.ValidationFailed as exc:
        raise _invalid(exc)


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
        raise _duplicate() from None


IMPORT_CONFLICT_MESSAGE = (
    "Another change to the same programs was saved at the same moment, so nothing from this file was written. "
    "Run a dry run again and check the plan before importing: the registry may have changed.")


def _import_conflict() -> HTTPException:
    return HTTPException(status_code=409, detail={
        "code": "import_conflict", "written": False, "message": IMPORT_CONFLICT_MESSAGE})

#: The JSON envelope around the CSV text: quotes, escapes and the key. A body over this is refused unparsed.
_MAX_IMPORT_BODY_BYTES = 2 * programs.MAX_IMPORT_BYTES + 4096


@router.post("/affiliate-programs/import")
async def import_affiliate_programs(
    request: Request,
    dry_run: bool = Query(True),
    current_user: dict = Depends(get_current_user),
):
    """Body: {"csv": "<file text>"}. A dry run unless `dry_run=false`. Never sets a program live.
    A real run is all-or-nothing: any invalid row refuses the whole file (422, nothing written), and a run
    Postgres aborted to break a lock cycle with another write is a 409 import_conflict (nothing written; dry-run
    it again before re-importing)."""
    actor = _require(current_user, ADMIN_ROLES)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > _MAX_IMPORT_BODY_BYTES:
        raise _invalid(programs.ValidationFailed({"csv": f"larger than {programs.MAX_IMPORT_BYTES} bytes"}))
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > _MAX_IMPORT_BODY_BYTES:
            raise _invalid(programs.ValidationFailed({"csv": f"larger than {programs.MAX_IMPORT_BYTES} bytes"}))
    try:
        body = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise _invalid(programs.ValidationFailed({"body": "must be a JSON object"}))
    if not isinstance(body, dict):
        raise _invalid(programs.ValidationFailed({"body": "must be a JSON object"}))
    try:
        return await programs.import_csv(body.get("csv"), dry_run=dry_run, actor=actor)
    except programs.ValidationFailed as exc:
        raise _invalid(exc)
    except programs.ImportRefused as exc:
        raise HTTPException(status_code=422, detail={"code": "import_refused", **exc.report})
    except programs.ImportConflict:
        raise _import_conflict() from None


@router.get("/affiliate-programs/{program_id}")
async def get_affiliate_program(program_id: str, current_user: dict = Depends(get_current_user)):
    _require(current_user, EMPLOYEE_STAFF_ROLES)
    try:
        return await programs.detail(program_id)
    except programs.ProgramNotFound:
        raise _not_found() from None


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
        raise _not_found() from None
    except programs.DuplicateProgram:
        raise _duplicate() from None


@router.post("/affiliate-programs/{program_id}/mark-live")
async def mark_affiliate_program_live(program_id: str, current_user: dict = Depends(get_current_user)):
    actor = _require(current_user, ADMIN_ROLES)
    try:
        return programs.public(await programs.mark_live(program_id, actor))
    except programs.ProgramNotFound:
        raise _not_found() from None
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
        raise _not_found() from None
