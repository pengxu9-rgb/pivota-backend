from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, Field

from services.pdp_governance_service import (
    DEFAULT_MARKET,
    LLM_ONLY_PUBLISH_MODULES,
    REVIEW_ACTOR_GPT55,
    REVIEW_ACTOR_HUMAN,
    audit_pdp_identity_groups,
    apply_pdp_identity_review_action,
    assign_pdp_review_task,
    correct_pdp_product_group_membership,
    create_module_draft,
    create_pdp_identity_review_task,
    get_pdp_offer_reconciliation,
    get_pdp_projection,
    get_pdp_gallery_asset,
    get_pdp_review_task,
    get_pdp_subject_index_stats,
    hydrate_pdp_subject_index,
    list_pdp_review_queue,
    list_pdp_subjects,
    normalize_employee_role,
    resolve_pdp_subject,
    review_module_version,
    rollback_module,
    update_pdp_review_task_status,
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
    checklist: Optional[Dict[str, Any]] = None
    policy_labels: Optional[List[str]] = None
    decision_tree_path: Optional[List[str]] = None
    escalation_reason: Optional[str] = None
    review_duration_ms: Optional[int] = None
    override_reason: Optional[str] = None


class ModuleVersionRequest(BaseModel):
    version_id: Optional[str] = None
    notes: Optional[str] = None
    rubric: Optional[Dict[str, Any]] = None


class ModuleRollbackRequest(BaseModel):
    target_version_id: str
    notes: Optional[str] = None


class HydrationRequest(BaseModel):
    limit: int = Field(default=10000, ge=0, le=50000)
    background: bool = True


class IdentityAuditRequest(BaseModel):
    limit: int = Field(default=250, ge=1, le=5000)
    background: bool = True


class ReviewTaskAssignRequest(BaseModel):
    assignee_actor_id: Optional[str] = None
    assignee_role: Optional[str] = None


class ReviewTaskStatusRequest(BaseModel):
    status: str = Field(pattern="^(needs_review|assigned|escalated|skipped|qa_sample|resolved)$")
    reason: Optional[str] = None
    qa_sample: Optional[bool] = None


class IdentityCandidateTaskRequest(BaseModel):
    candidate_type: str = Field(pattern="^(external_seed_near_match|merchant_product_near_match)$")
    candidate_ref: str = Field(min_length=1)
    notes: Optional[str] = None


class IdentityReviewActionRequest(BaseModel):
    action: str = Field(pattern="^(attach_external_offer|merge_product_group|reject_candidate|create_identity_candidate_task|reject_staged_audit)$")
    notes: Optional[str] = None
    reason: Optional[str] = None
    checklist: Optional[Dict[str, Any]] = None
    policy_labels: Optional[List[str]] = None
    decision_tree_path: Optional[List[str]] = None
    review_duration_ms: Optional[int] = None
    override_reason: Optional[str] = None
    target_product_key: Optional[str] = None
    candidate_type: Optional[str] = None
    candidate_ref: Optional[str] = None


class ProductGroupCorrectionRequest(BaseModel):
    add_product_key: Optional[str] = None
    remove_product_keys: List[str] = Field(default_factory=list)
    set_primary_product_key: Optional[str] = None
    reason: str = Field(min_length=1)
    policy_labels: List[str] = Field(default_factory=list)
    checklist: Optional[Dict[str, Any]] = None
    decision_tree_path: Optional[List[str]] = None
    override_reason: Optional[str] = None


def _employee_actor(current_user: Dict[str, Any]) -> str:
    return str(current_user.get("sub") or current_user.get("email") or "employee")


def _employee_role(current_user: Dict[str, Any]) -> str:
    return normalize_employee_role(str(current_user.get("role") or "employee"))


def _map_error(exc: Exception) -> HTTPException:
    message = str(exc)
    if message in {"PDP_NOT_FOUND", "PDP_MODULE_VERSION_NOT_FOUND", "EXTERNAL_SEED_NOT_FOUND", "PDP_GALLERY_ASSET_NOT_FOUND", "PDP_REVIEW_TASK_NOT_FOUND", "PDP_IDENTITY_CANDIDATE_NOT_FOUND", "PDP_PRODUCT_GROUP_MEMBER_NOT_FOUND"}:
        return HTTPException(status_code=404, detail=message)
    if message in {
        "INVALID_PRODUCT_KEY",
        "INVALID_PDP_MODULE",
        "INVALID_REVIEW_DECISION",
        "INVALID_PDP_IDENTITY_ACTION",
        "INVALID_PDP_IDENTITY_CANDIDATE",
        "ROLLBACK_TARGET_MUST_BE_PUBLISHED",
        "UNSUPPORTED_GALLERY_IMAGE_TYPE",
        "EMPTY_GALLERY_IMAGE",
        "GALLERY_IMAGE_TOO_LARGE",
        "INVALID_PDP_REVIEW_TASK_STATUS",
        "PDP_REVIEW_CHECKLIST_REQUIRED",
        "PDP_REVIEW_POLICY_LABEL_REQUIRED",
        "PDP_REVIEW_ESCALATION_REASON_REQUIRED",
        "PDP_REVIEW_REJECTION_REASON_REQUIRED",
        "PDP_IDENTITY_ACTION_NOT_ALLOWED_FOR_CANDIDATE",
        "PDP_IDENTITY_ATTACH_REQUIRES_PRODUCT_KEY",
        "PDP_IDENTITY_CANDIDATE_ALREADY_RESOLVED",
        "PDP_IDENTITY_TASK_ALREADY_RESOLVED",
        "PDP_IDENTITY_TASK_REQUIRED",
        "PDP_PRODUCT_GROUP_REQUIRED",
        "PDP_IDENTITY_CORRECTION_REASON_REQUIRED",
        "PDP_IDENTITY_CORRECTION_POLICY_LABEL_REQUIRED",
        "PDP_IDENTITY_CORRECTION_TARGET_REQUIRED",
        "PDP_IDENTITY_CORRECTION_PRIMARY_REMOVED",
        "PDP_IDENTITY_CORRECTION_PRODUCT_NOT_LIVE",
    }:
        return HTTPException(status_code=400, detail=message)
    if message in {"PDP_MODULE_REQUIRES_HUMAN_REVIEW", "PDP_REVIEW_ACTION_FORBIDDEN", "PDP_REVIEW_OVERRIDE_FORBIDDEN"}:
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


@router.post("/identity-audit")
async def run_identity_audit(
    body: IdentityAuditRequest,
    background_tasks: BackgroundTasks,
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    actor_id = _employee_actor(current_user)
    actor_role = _employee_role(current_user)
    try:
        if body.background:
            background_tasks.add_task(
                audit_pdp_identity_groups,
                limit=body.limit,
                actor_type=REVIEW_ACTOR_HUMAN,
                actor_id=actor_id,
                actor_role=actor_role,
            )
            return {"status": "queued", "limit": body.limit}
        return await audit_pdp_identity_groups(
            limit=body.limit,
            actor_type=REVIEW_ACTOR_HUMAN,
            actor_id=actor_id,
            actor_role=actor_role,
        )
    except Exception as exc:
        raise _map_error(exc)


@router.get("/gallery-assets/{asset_id}")
async def get_gallery_asset(asset_id: str) -> Response:
    try:
        asset = await get_pdp_gallery_asset(asset_id)
        data = asset.get("data") or b""
        if isinstance(data, memoryview):
            data = data.tobytes()
        return Response(
            content=data,
            media_type=str(asset.get("content_type") or "application/octet-stream"),
            headers={
                "Cache-Control": "public, max-age=31536000, immutable",
                "Content-Length": str(asset.get("byte_size") or 0),
            },
        )
    except Exception as exc:
        raise _map_error(exc)


@router.get("/review-queue")
async def get_review_queue(
    tab: str = Query(default="needs_review"),
    module_key: Optional[str] = Query(default=None),
    risk: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    assignee: Optional[str] = Query(default=None),
    external_only: Optional[bool] = Query(default=None),
    market: Optional[str] = Query(default=None),
    seller_count: Optional[str] = Query(default=None),
    source_type: Optional[str] = Query(default=None),
    last_reviewer: Optional[str] = Query(default=None),
    staleness: Optional[str] = Query(default=None),
    priority: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    try:
        return await list_pdp_review_queue(
            actor_role=_employee_role(current_user),
            actor_id=_employee_actor(current_user),
            tab=tab,
            module_key=module_key,
            risk=risk,
            status=status,
            assignee=assignee,
            external_only=external_only,
            market=market,
            seller_count=seller_count,
            source_type=source_type,
            last_reviewer=last_reviewer,
            staleness=staleness,
            priority=priority,
            limit=limit,
            offset=offset,
        )
    except Exception as exc:
        raise _map_error(exc)


@router.get("/review-queue/{task_id}")
async def get_review_task(
    task_id: str,
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    try:
        return await get_pdp_review_task(task_id=task_id, actor_role=_employee_role(current_user))
    except Exception as exc:
        raise _map_error(exc)


@router.post("/review-queue/{task_id}/assign")
async def assign_review_task(
    task_id: str,
    body: ReviewTaskAssignRequest,
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    try:
        return await assign_pdp_review_task(
            task_id=task_id,
            assignee_actor_id=body.assignee_actor_id,
            assignee_role=body.assignee_role,
            actor_role=_employee_role(current_user),
            actor_id=_employee_actor(current_user),
        )
    except Exception as exc:
        raise _map_error(exc)


@router.post("/review-queue/{task_id}/status")
async def update_review_task_status(
    task_id: str,
    body: ReviewTaskStatusRequest,
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    try:
        return await update_pdp_review_task_status(
            task_id=task_id,
            next_status=body.status,
            actor_role=_employee_role(current_user),
            actor_id=_employee_actor(current_user),
            reason=body.reason,
            qa_sample=body.qa_sample,
        )
    except Exception as exc:
        raise _map_error(exc)


@router.post("/review-queue/{task_id}/identity-action")
async def apply_identity_review_action(
    task_id: str,
    body: IdentityReviewActionRequest,
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    try:
        return await apply_pdp_identity_review_action(
            task_id=task_id,
            action=body.action,
            notes=body.notes,
            reason=body.reason,
            checklist=body.checklist,
            policy_labels=body.policy_labels,
            decision_tree_path=body.decision_tree_path,
            review_duration_ms=body.review_duration_ms,
            override_reason=body.override_reason,
            target_product_key=body.target_product_key,
            candidate_type=body.candidate_type,
            candidate_ref=body.candidate_ref,
            actor_role=_employee_role(current_user),
            actor_id=_employee_actor(current_user),
        )
    except Exception as exc:
        raise _map_error(exc)


@router.get("/{pdp_id}/offers/reconciliation")
async def get_offer_reconciliation(
    pdp_id: str,
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    try:
        return await get_pdp_offer_reconciliation(pdp_id=pdp_id, actor_role=_employee_role(current_user))
    except Exception as exc:
        raise _map_error(exc)


@router.post("/{pdp_id}/identity-review-tasks")
async def create_identity_review_task(
    pdp_id: str,
    body: IdentityCandidateTaskRequest,
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    try:
        return await create_pdp_identity_review_task(
            pdp_id=pdp_id,
            candidate_type=body.candidate_type,
            candidate_ref=body.candidate_ref,
            notes=body.notes,
            actor_role=_employee_role(current_user),
            actor_id=_employee_actor(current_user),
        )
    except Exception as exc:
        raise _map_error(exc)


@router.post("/{pdp_id}/identity/product-group-correction")
async def correct_product_group_membership(
    pdp_id: str,
    body: ProductGroupCorrectionRequest,
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    try:
        return await correct_pdp_product_group_membership(
            pdp_id=pdp_id,
            add_product_key=body.add_product_key,
            remove_product_keys=body.remove_product_keys,
            set_primary_product_key=body.set_primary_product_key,
            reason=body.reason,
            policy_labels=body.policy_labels,
            checklist=body.checklist,
            decision_tree_path=body.decision_tree_path,
            override_reason=body.override_reason,
            actor_role=_employee_role(current_user),
            actor_id=_employee_actor(current_user),
        )
    except Exception as exc:
        raise _map_error(exc)


@router.get("/{pdp_id}")
async def get_pdp(
    pdp_id: str,
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    try:
        return await get_pdp_projection(pdp_id=pdp_id, actor_role=_employee_role(current_user))
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
            actor_role=_employee_role(current_user),
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
            actor_role=_employee_role(current_user),
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
    """Run the LLM-only GPT-5.5 gate over a staged module; a pass auto-publishes.

    This lane has NO human-actor RBAC behind it. review_module_version() gates
    on allowed_pdp_review_actions only inside `if actor_type ==
    REVIEW_ACTOR_HUMAN`, and we call it with REVIEW_ACTOR_GPT55 and no
    actor_role -- so the caller's role (admin, employee, or outsourced; see
    get_current_employee) is never consulted and every module in
    MACHINE_PUBLISH_MODULES was auto-publishable from an employee token,
    `offers` and `identity` included.

    The module fence is therefore the whole authorization for this route, and it
    runs HERE, before the service call, so a refused module writes nothing at
    all. It is the same shared set the merchant self-approve twin reads
    (routes/merchant_pdp.py -- "Merchants are NOT direct publish authorities;
    the gate is"). Employees who need to publish a fenced-off module still can,
    through /review and /publish, where their role IS checked.
    """
    if module_key not in LLM_ONLY_PUBLISH_MODULES:
        raise HTTPException(status_code=400, detail="MODULE_NOT_LLM_REVIEWABLE")
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
            actor_role=_employee_role(current_user),
            decision=body.decision,
            notes=body.notes,
            checklist=body.checklist,
            policy_labels=body.policy_labels,
            decision_tree_path=body.decision_tree_path,
            escalation_reason=body.escalation_reason,
            review_duration_ms=body.review_duration_ms,
            override_reason=body.override_reason,
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
            actor_role=_employee_role(current_user),
            decision="pass",
            notes=body.notes,
            checklist=body.rubric.get("checklist") if body.rubric else None,
            policy_labels=body.rubric.get("policy_labels") if body.rubric else None,
            decision_tree_path=body.rubric.get("decision_tree_path") if body.rubric else None,
            override_reason=body.rubric.get("override_reason") if body.rubric else None,
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
            actor_role=_employee_role(current_user),
        )
        return {"status": "success", "module": module}
    except Exception as exc:
        raise _map_error(exc)
