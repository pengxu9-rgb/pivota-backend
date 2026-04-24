from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from services.pdp_governance_service import (
    DEFAULT_MARKET,
    REVIEW_ACTOR_GPT55,
    REVIEW_ACTOR_HUMAN,
    create_module_draft,
    get_pdp_projection,
    get_pdp_subject_index_stats,
    hydrate_pdp_subject_index,
    list_pdp_subjects,
    resolve_pdp_subject,
    review_module_version,
    rollback_module,
    upload_pdp_gallery_image,
)
from utils.auth import get_current_employee


router = APIRouter(prefix="/employee/pdps", tags=["employee-pdp-governance"])


class ModuleDraftRequest(BaseModel):
    payload: Dict[str, Any] = Field(default_factory=dict)
    source_refs: List[Dict[str, Any]] = Field(default_factory=list)
    generated_by: Optional[str] = None
    generation_ref: Optional[str] = None


class ModuleReviewRequest(BaseModel):
    version_id: Optional[str] = None
    decision: str = Field(default="needs_human_review", pattern="^(pass|reject|needs_human_review)$")
    notes: Optional[str] = None


class ModuleVersionRequest(BaseModel):
    version_id: Optional[str] = None
    notes: Optional[str] = None
    rubric: Optional[Dict[str, Any]] = None


class ModuleRollbackRequest(BaseModel):
    target_version_id: str
    notes: Optional[str] = None


class HydrationRequest(BaseModel):
    limit: int = Field(default=500, ge=1, le=2000)
    background: bool = True


def _employee_actor(current_user: Dict[str, Any]) -> str:
    return str(current_user.get("sub") or current_user.get("email") or "employee")


def _map_error(exc: Exception) -> HTTPException:
    message = str(exc)
    if message in {"PDP_NOT_FOUND", "PDP_MODULE_VERSION_NOT_FOUND", "EXTERNAL_SEED_NOT_FOUND"}:
        return HTTPException(status_code=404, detail=message)
    if message in {
        "INVALID_PRODUCT_KEY",
        "INVALID_PDP_MODULE",
        "INVALID_REVIEW_DECISION",
        "ROLLBACK_TARGET_MUST_BE_PUBLISHED",
        "UNSUPPORTED_GALLERY_IMAGE_TYPE",
        "EMPTY_GALLERY_IMAGE",
        "GALLERY_IMAGE_TOO_LARGE",
    }:
        return HTTPException(status_code=400, detail=message)
    if message == "PDP_MODULE_REQUIRES_HUMAN_REVIEW":
        return HTTPException(status_code=403, detail=message)
    if message in {"PDP_GALLERY_STORAGE_NOT_CONFIGURED", "PDP_GALLERY_STORAGE_CLIENT_UNAVAILABLE"} or message.startswith("PDP_GALLERY_STORAGE_UPLOAD_FAILED"):
        return HTTPException(status_code=503, detail=message)
    return HTTPException(status_code=500, detail=message[:300])


@router.get("")
async def list_pdps(
    module_status: Optional[str] = Query(default=None),
    review_actor: Optional[str] = Query(default=None),
    risk: Optional[str] = Query(default=None),
    external_only: Optional[bool] = Query(default=None),
    market: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    try:
        return await list_pdp_subjects(
            module_status=module_status,
            review_actor=review_actor,
            risk=risk,
            external_only=external_only,
            market=market,
            limit=limit,
            offset=offset,
        )
    except Exception as exc:
        raise _map_error(exc)


@router.get("/resolve")
async def resolve_pdp(
    product_key: Optional[str] = Query(default=None),
    external_seed_id: Optional[str] = Query(default=None),
    pdp_id: Optional[str] = Query(default=None),
    market: str = Query(default=DEFAULT_MARKET),
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    try:
        subject = await resolve_pdp_subject(
            pdp_id=pdp_id,
            product_key=product_key,
            external_seed_id=external_seed_id,
            market=market,
        )
        return {"status": "success", "pdp": subject}
    except Exception as exc:
        raise _map_error(exc)


@router.get("/hydration")
async def get_hydration_status(
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    try:
        return await get_pdp_subject_index_stats()
    except Exception as exc:
        raise _map_error(exc)


@router.post("/hydration")
async def hydrate_pdp_subjects(
    body: HydrationRequest,
    background_tasks: BackgroundTasks,
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    actor_id = _employee_actor(current_user)
    try:
        if body.background:
            background_tasks.add_task(
                hydrate_pdp_subject_index,
                limit=body.limit,
                actor_type=REVIEW_ACTOR_HUMAN,
                actor_id=actor_id,
            )
            current = await get_pdp_subject_index_stats()
            return {"status": "queued", "limit": body.limit, "current": current}
        return await hydrate_pdp_subject_index(
            limit=body.limit,
            actor_type=REVIEW_ACTOR_HUMAN,
            actor_id=actor_id,
        )
    except Exception as exc:
        raise _map_error(exc)


@router.get("/{pdp_id}")
async def get_pdp(
    pdp_id: str,
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    try:
        return await get_pdp_projection(pdp_id=pdp_id)
    except Exception as exc:
        raise _map_error(exc)


@router.post("/{pdp_id}/modules/{module_key}/draft")
async def save_module_draft(
    pdp_id: str,
    module_key: str,
    body: ModuleDraftRequest,
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    try:
        module = await create_module_draft(
            pdp_id=pdp_id,
            module_key=module_key,
            payload=body.payload,
            source_refs=body.source_refs,
            generated_by=body.generated_by,
            generation_ref=body.generation_ref,
            actor_type=REVIEW_ACTOR_HUMAN,
            actor_id=_employee_actor(current_user),
        )
        return {"status": "success", "module": module}
    except Exception as exc:
        raise _map_error(exc)


@router.post("/{pdp_id}/modules/gallery/images")
async def upload_gallery_image(
    pdp_id: str,
    file: UploadFile = File(...),
    alt_text: Optional[str] = Form(default=None),
    role: str = Form(default="gallery"),
    variant_id: Optional[str] = Form(default=None),
    rights_status: str = Form(default="owned_or_licensed"),
    source_note: Optional[str] = Form(default=None),
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    try:
        blob = await file.read()
        return await upload_pdp_gallery_image(
            pdp_id=pdp_id,
            filename=file.filename or "gallery-image",
            content_type=file.content_type or "application/octet-stream",
            blob=blob,
            alt_text=alt_text,
            role=role,
            variant_id=variant_id,
            rights_status=rights_status,
            source_note=source_note,
            actor_type=REVIEW_ACTOR_HUMAN,
            actor_id=_employee_actor(current_user),
        )
    except Exception as exc:
        raise _map_error(exc)


@router.post("/{pdp_id}/modules/{module_key}/gpt55-review")
async def run_gpt55_review(
    pdp_id: str,
    module_key: str,
    body: ModuleVersionRequest,
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    try:
        return await review_module_version(
            pdp_id=pdp_id,
            module_key=module_key,
            version_id=body.version_id,
            actor_type=REVIEW_ACTOR_GPT55,
            actor_id="gpt-5.5",
            notes=body.notes,
            external_rubric=body.rubric,
        )
    except Exception as exc:
        raise _map_error(exc)


@router.post("/{pdp_id}/modules/{module_key}/review")
async def human_review_module(
    pdp_id: str,
    module_key: str,
    body: ModuleReviewRequest,
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    try:
        return await review_module_version(
            pdp_id=pdp_id,
            module_key=module_key,
            version_id=body.version_id,
            actor_type=REVIEW_ACTOR_HUMAN,
            actor_id=_employee_actor(current_user),
            decision=body.decision,
            notes=body.notes,
        )
    except Exception as exc:
        raise _map_error(exc)


@router.post("/{pdp_id}/modules/{module_key}/publish")
async def publish_module(
    pdp_id: str,
    module_key: str,
    body: ModuleVersionRequest,
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    try:
        return await review_module_version(
            pdp_id=pdp_id,
            module_key=module_key,
            version_id=body.version_id,
            actor_type=REVIEW_ACTOR_HUMAN,
            actor_id=_employee_actor(current_user),
            decision="pass",
            notes=body.notes,
        )
    except Exception as exc:
        raise _map_error(exc)


@router.post("/{pdp_id}/modules/{module_key}/rollback")
async def rollback_pdp_module(
    pdp_id: str,
    module_key: str,
    body: ModuleRollbackRequest,
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    try:
        module = await rollback_module(
            pdp_id=pdp_id,
            module_key=module_key,
            target_version_id=body.target_version_id,
            actor_type=REVIEW_ACTOR_HUMAN,
            actor_id=_employee_actor(current_user),
        )
        return {"status": "success", "module": module}
    except Exception as exc:
        raise _map_error(exc)
