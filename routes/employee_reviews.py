from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from db.database import database
from db.reviews_center import media_assets, review_featured, review_group, review_group_membership
from observability.reviews_metrics import (
    record_employee_action,
    record_import_commit,
    record_import_validate,
)
from services.reviews_service import (
    attach_import_files,
    build_sku_key,
    commit_import_batch,
    create_import_batch,
    create_manual_review_group,
    ensure_membership_for_sku,
    generate_featured_reviews_for_group,
    get_group_counts_by_merchant,
    list_employee_audit_logs,
    remove_group_member,
    redact_review,
    remove_review_media,
    reprocess_import_batch,
    set_featured_pin,
    set_group_featured_frozen,
    set_review_group_status,
    set_review_status,
    validate_import_batch,
)
from utils.auth import require_employee_permissions


router = APIRouter(prefix="/employee/reviews/v1", tags=["Employee Reviews"])


def _import_storage_dir() -> str:
    base = os.getenv("REVIEWS_IMPORT_DIR", os.path.join(os.getcwd(), "tmp", "reviews-imports"))
    os.makedirs(base, exist_ok=True)
    return base


async def _save_upload(file: UploadFile, *, dir_path: str, name_hint: str) -> str:
    os.makedirs(dir_path, exist_ok=True)
    filename = (file.filename or "").strip() or name_hint
    filename = os.path.basename(filename)
    path = os.path.join(dir_path, filename)
    # Stream to disk (avoid loading into memory).
    with open(path, "wb") as f:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
    return path


class CreateImportBatchRequest(BaseModel):
    merchant_id: str = Field(..., min_length=1)
    source_system: str = Field(..., min_length=1)


@router.post(
    "/import/batches",
)
async def employee_create_import_batch(
    body: CreateImportBatchRequest,
    actor: Dict[str, Any] = Depends(require_employee_permissions(["reviews.import.create"])),
) -> Dict[str, Any]:
    try:
        out = await create_import_batch(actor=actor, merchant_id=body.merchant_id, source_system=body.source_system)
        record_employee_action(action="reviews.import.create", result="success")
        return out
    except Exception:
        record_employee_action(action="reviews.import.create", result="fail")
        raise


@router.post(
    "/import/batches/{batch_id}/files",
)
async def employee_upload_import_files(
    batch_id: int,
    reviews_file: UploadFile = File(...),
    media_zip: Optional[UploadFile] = File(None),
    actor: Dict[str, Any] = Depends(require_employee_permissions(["reviews.import.create"])),
) -> Dict[str, Any]:
    bid = int(batch_id)
    base = os.path.join(_import_storage_dir(), "uploads", f"batch_{bid}")
    reviews_path = await _save_upload(reviews_file, dir_path=base, name_hint="reviews.csv")
    media_path = None
    if media_zip is not None:
        media_path = await _save_upload(media_zip, dir_path=base, name_hint="media.zip")
    try:
        out = await attach_import_files(
            actor=actor,
            batch_id=bid,
            reviews_file_path=reviews_path,
            media_zip_path=media_path,
        )
        record_employee_action(action="reviews.import.upload", result="success")
        return out
    except Exception:
        record_employee_action(action="reviews.import.upload", result="fail")
        raise


@router.post(
    "/import/batches/{batch_id}/validate",
)
async def employee_validate_import_batch(
    batch_id: int,
    request: Request,
    actor: Dict[str, Any] = Depends(require_employee_permissions(["reviews.import.create"])),
) -> Dict[str, Any]:
    request.state.operation = "reviews.import.validate"
    started = time.time()
    try:
        out = await validate_import_batch(actor=actor, batch_id=int(batch_id))
        record_import_validate(result="success", reason="ok", duration_seconds=max(0.0, time.time() - started))
        record_employee_action(action="reviews.import.validate", result="success")
        return out
    except HTTPException as e:
        record_import_validate(
            result="fail",
            reason=str(e.detail)[:64] if e.detail else "http_error",
            duration_seconds=max(0.0, time.time() - started),
        )
        record_employee_action(action="reviews.import.validate", result="fail")
        raise
    except Exception as e:
        record_import_validate(
            result="fail",
            reason=type(e).__name__,
            duration_seconds=max(0.0, time.time() - started),
        )
        record_employee_action(action="reviews.import.validate", result="fail")
        raise


class CommitImportBatchRequest(BaseModel):
    reason: str = Field(..., min_length=1)


@router.post(
    "/import/batches/{batch_id}/commit",
)
async def employee_commit_import_batch(
    batch_id: int,
    body: CommitImportBatchRequest,
    request: Request,
    actor: Dict[str, Any] = Depends(require_employee_permissions(["reviews.import.commit"])),
) -> Dict[str, Any]:
    request.state.operation = "reviews.import.commit"
    started = time.time()
    succeeded = False
    try:
        out = await commit_import_batch(actor=actor, batch_id=int(batch_id), reason=body.reason)
        succeeded = True
        record_employee_action(action="reviews.import.commit", result="success")
        return out
    except HTTPException as e:
        record_employee_action(action="reviews.import.commit", result="fail")
        raise
    except Exception:
        record_employee_action(action="reviews.import.commit", result="fail")
        raise
    finally:
        record_import_commit(
            result="success" if succeeded else "fail",
            reason="ok" if succeeded else "error",
            duration_seconds=max(0.0, time.time() - started),
            succeeded=succeeded,
        )


class ReprocessImportBatchRequest(BaseModel):
    mode: str = Field(..., min_length=1)  # variant_match | group_resolve


@router.post("/import/batches/{batch_id}/reprocess")
async def employee_reprocess_import_batch(
    batch_id: int,
    body: ReprocessImportBatchRequest,
    actor: Dict[str, Any] = Depends(require_employee_permissions(["reviews.import.reprocess"])),
) -> Dict[str, Any]:
    try:
        out = await reprocess_import_batch(actor=actor, batch_id=int(batch_id), mode=body.mode)
        record_employee_action(action="reviews.import.reprocess", result="success")
        return out
    except Exception:
        record_employee_action(action="reviews.import.reprocess", result="fail")
        raise


@router.get("/import/batches/{batch_id}/report.csv")
async def employee_download_import_report_csv(
    batch_id: int,
    kind: str = "unmatched",
    actor: Dict[str, Any] = Depends(require_employee_permissions(["reviews.import.read"])),
):
    """
    Download a lightweight CSV report for BD/Ops to share back with partners.

    kind=unmatched: rejected + downgraded_to_product_level rows only.
    kind=all: all rows in batch.

    NOTE: Do not include review body or any author PII here.
    """
    bid = int(batch_id)
    kind = (kind or "").strip().lower() or "unmatched"
    if kind not in {"unmatched", "all"}:
        raise HTTPException(status_code=400, detail="INVALID_KIND")

    where = ["batch_id = :bid"]
    if kind == "unmatched":
        where.append("status IN ('rejected', 'downgraded_to_product_level')")

    rows = await database.fetch_all(
        f"""
        SELECT id, merchant_id, source_system, external_review_id, status, error_reason,
               match_product_key, match_sku_key, match_confidence, group_id, group_confidence,
               payload_json
        FROM import_items
        WHERE {' AND '.join(where)}
        ORDER BY id ASC
        """,
        {"bid": bid},
    )

    import csv
    import io

    out = io.StringIO()
    writer = csv.DictWriter(
        out,
        fieldnames=[
            "id",
            "merchant_id",
            "source_system",
            "external_review_id",
            "status",
            "error_reason",
            "platform",
            "platform_product_id",
            "variant_id",
            "match_product_key",
            "match_sku_key",
            "match_confidence",
            "group_id",
            "group_confidence",
        ],
    )
    writer.writeheader()
    for r in rows:
        payload = r.get("payload_json") if isinstance(r.get("payload_json"), dict) else {}
        writer.writerow(
            {
                "id": int(r["id"]),
                "merchant_id": r.get("merchant_id"),
                "source_system": r.get("source_system"),
                "external_review_id": r.get("external_review_id"),
                "status": r.get("status"),
                "error_reason": r.get("error_reason"),
                "platform": payload.get("platform"),
                "platform_product_id": payload.get("platform_product_id") or payload.get("product_id"),
                "variant_id": payload.get("variant_id"),
                "match_product_key": r.get("match_product_key"),
                "match_sku_key": r.get("match_sku_key"),
                "match_confidence": r.get("match_confidence"),
                "group_id": r.get("group_id"),
                "group_confidence": r.get("group_confidence"),
            }
        )

    csv_bytes = out.getvalue().encode("utf-8")
    filename = f"import_batch_{bid}_{kind}.csv"
    return Response(
        content=csv_bytes,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


class SetReviewStatusRequest(BaseModel):
    status: str = Field(..., min_length=1)
    reason: Optional[str] = None


@router.post(
    "/reviews/{review_id}/status",
)
async def employee_set_review_status(
    review_id: int,
    body: SetReviewStatusRequest,
    actor: Dict[str, Any] = Depends(require_employee_permissions(["reviews.moderate.status"])),
) -> Dict[str, Any]:
    if (body.status or "").strip().lower() == "removed" and not (body.reason or "").strip():
        raise HTTPException(status_code=400, detail="REASON_REQUIRED")
    try:
        out = await set_review_status(actor=actor, review_id=int(review_id), status=body.status, reason=body.reason)
        record_employee_action(action="reviews.moderate.status", result="success")
        return out
    except Exception:
        record_employee_action(action="reviews.moderate.status", result="fail")
        raise


class RedactReviewRequest(BaseModel):
    fields: List[str] = Field(default_factory=list)
    reason: Optional[str] = None
    editor_note: Optional[str] = None


@router.post(
    "/reviews/{review_id}/redact",
)
async def employee_redact_review(
    review_id: int,
    body: RedactReviewRequest,
    actor: Dict[str, Any] = Depends(require_employee_permissions(["reviews.moderate.redact"])),
) -> Dict[str, Any]:
    try:
        out = await redact_review(
            actor=actor,
            review_id=int(review_id),
            fields=body.fields,
            reason=body.reason,
            editor_note=body.editor_note,
        )
        record_employee_action(action="reviews.moderate.redact", result="success")
        return out
    except Exception:
        record_employee_action(action="reviews.moderate.redact", result="fail")
        raise


class RemoveReviewMediaRequest(BaseModel):
    reason: Optional[str] = None


@router.post(
    "/reviews/{review_id}/media/{media_id}/remove",
)
async def employee_remove_review_media(
    review_id: int,
    media_id: int,
    body: RemoveReviewMediaRequest,
    actor: Dict[str, Any] = Depends(require_employee_permissions(["reviews.moderate.media"])),
) -> Dict[str, Any]:
    return await remove_review_media(
        actor=actor,
        review_id=int(review_id),
        media_id=int(media_id),
        reason=body.reason,
    )


class CreateGroupRequest(BaseModel):
    group_key: Optional[str] = None
    reason: Optional[str] = None


@router.post(
    "/groups",
)
async def employee_create_group(
    body: CreateGroupRequest,
    actor: Dict[str, Any] = Depends(require_employee_permissions(["reviews.group.manage"])),
) -> Dict[str, Any]:
    return await create_manual_review_group(actor=actor, group_key=body.group_key, reason=body.reason)


class SetGroupStatusRequest(BaseModel):
    status: str = Field(..., min_length=1)  # active | disabled
    reason: Optional[str] = None


@router.post(
    "/groups/{group_id}/status",
)
async def employee_set_group_status(
    group_id: int,
    body: SetGroupStatusRequest,
    actor: Dict[str, Any] = Depends(require_employee_permissions(["reviews.group.manage"])),
) -> Dict[str, Any]:
    if (body.status or "").strip().lower() == "disabled" and not (body.reason or "").strip():
        raise HTTPException(status_code=400, detail="REASON_REQUIRED")
    return await set_review_group_status(actor=actor, group_id=int(group_id), status=body.status, reason=body.reason)


class AddMemberRequest(BaseModel):
    merchant_id: str = Field(..., min_length=1)
    platform: str = Field(..., min_length=1)
    platform_product_id: str = Field(..., min_length=1)
    variant_id: Optional[str] = None
    evidence: Optional[Dict[str, Any]] = None
    confidence: float = 1.0
    reason: Optional[str] = None


@router.post(
    "/groups/{group_id}/members",
)
async def employee_add_group_member(
    group_id: int,
    body: AddMemberRequest,
    actor: Dict[str, Any] = Depends(require_employee_permissions(["reviews.group.manage"])),
) -> Dict[str, Any]:
    await ensure_membership_for_sku(
        actor=actor,
        group_id=int(group_id),
        match_type="MANUAL",
        confidence=float(body.confidence or 1.0),
        evidence=body.evidence,
        merchant_id=body.merchant_id,
        platform=body.platform,
        platform_product_id=body.platform_product_id,
        variant_id=body.variant_id,
        created_by="employee",
        created_by_employee_id=str(actor.get("employee_id") or actor.get("user_id") or actor.get("sub") or ""),
    )
    return {"status": "success", "group_id": int(group_id)}


class RemoveMemberRequest(BaseModel):
    merchant_id: str = Field(..., min_length=1)
    platform: str = Field(..., min_length=1)
    platform_product_id: str = Field(..., min_length=1)
    variant_id: Optional[str] = None
    reason: Optional[str] = None


@router.post(
    "/groups/{group_id}/members/remove",
)
async def employee_remove_group_member(
    group_id: int,
    body: RemoveMemberRequest,
    actor: Dict[str, Any] = Depends(require_employee_permissions(["reviews.group.manage"])),
) -> Dict[str, Any]:
    sku_key = build_sku_key(
        merchant_id=body.merchant_id,
        platform=body.platform,
        platform_product_id=body.platform_product_id,
        variant_id=body.variant_id,
    )
    return await remove_group_member(actor=actor, group_id=int(group_id), sku_key=sku_key, reason=body.reason)


@router.get(
    "/groups/{group_id}",
)
async def employee_get_group_detail(
    group_id: int,
    actor: Dict[str, Any] = Depends(require_employee_permissions(["reviews.group.manage"])),
) -> Dict[str, Any]:
    gid = int(group_id)
    g = await database.fetch_one(review_group.select().where(review_group.c.id == gid))
    if not g:
        raise HTTPException(status_code=404, detail="GROUP_NOT_FOUND")
    members = await database.fetch_all(
        review_group_membership.select()
        .where((review_group_membership.c.group_id == gid) & (review_group_membership.c.status == "active"))
        .order_by(review_group_membership.c.created_at.desc())
        .limit(200)
    )
    counts_by_merchant = await get_group_counts_by_merchant(gid)
    review_count_row = await database.fetch_one(
        "SELECT COUNT(*)::int AS c FROM product_reviews WHERE group_id = :gid AND status = 'active'",
        {"gid": gid},
    )
    featured_count_row = await database.fetch_one(
        "SELECT COUNT(*)::int AS c FROM review_featured WHERE group_id = :gid",
        {"gid": gid},
    )
    return {
        "group": dict(g),
        "members": [dict(m) for m in members],
        "counts_by_merchant": counts_by_merchant,
        "active_review_count": int(review_count_row["c"] or 0) if review_count_row else 0,
        "featured_review_count": int(featured_count_row["c"] or 0) if featured_count_row else 0,
    }


class SetFeaturedPinRequest(BaseModel):
    review_id: int = Field(..., ge=1)
    pinned: bool = True
    reason: Optional[str] = None


@router.post(
    "/groups/{group_id}/featured/pin",
)
async def employee_set_featured_pin(
    group_id: int,
    body: SetFeaturedPinRequest,
    actor: Dict[str, Any] = Depends(require_employee_permissions(["reviews.feature.manage"])),
) -> Dict[str, Any]:
    return await set_featured_pin(
        actor=actor,
        group_id=int(group_id),
        review_id=int(body.review_id),
        pinned=bool(body.pinned),
        reason=body.reason,
    )


class FreezeFeaturedRequest(BaseModel):
    frozen: bool = True
    reason: Optional[str] = None


@router.post(
    "/groups/{group_id}/featured/freeze",
)
async def employee_freeze_featured(
    group_id: int,
    body: FreezeFeaturedRequest,
    actor: Dict[str, Any] = Depends(require_employee_permissions(["reviews.feature.manage"])),
) -> Dict[str, Any]:
    return await set_group_featured_frozen(actor=actor, group_id=int(group_id), frozen=bool(body.frozen), reason=body.reason)


class GenerateFeaturedRequest(BaseModel):
    limit: int = Field(12, ge=1, le=30)
    per_merchant_cap: int = Field(2, ge=1, le=5)


@router.post(
    "/groups/{group_id}/featured/generate",
)
async def employee_generate_featured(
    group_id: int,
    body: GenerateFeaturedRequest,
    actor: Dict[str, Any] = Depends(require_employee_permissions(["reviews.feature.manage"])),
) -> Dict[str, Any]:
    return await generate_featured_reviews_for_group(
        actor=actor,
        group_id=int(group_id),
        limit=int(body.limit),
        per_merchant_cap=int(body.per_merchant_cap),
    )


@router.get(
    "/audit",
)
async def employee_list_audit_logs(
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    limit: int = 100,
    actor: Dict[str, Any] = Depends(require_employee_permissions(["reviews.audit.read"])),
) -> Dict[str, Any]:
    return await list_employee_audit_logs(target_type=target_type, target_id=target_id, limit=int(limit))


@router.get(
    "/moderation/reviews",
)
async def employee_list_reviews_for_moderation(
    merchant_id: Optional[str] = None,
    status: Optional[str] = None,
    source_type: Optional[str] = None,
    source_system: Optional[str] = None,
    limit: int = 50,
    actor: Dict[str, Any] = Depends(require_employee_permissions(["reviews.read"])),
) -> Dict[str, Any]:
    limit = max(1, min(int(limit or 50), 200))
    where = ["1=1"]
    params: Dict[str, Any] = {}
    if merchant_id:
        where.append("merchant_id = :mid")
        params["mid"] = str(merchant_id)
    if status:
        where.append("status = :st")
        params["st"] = str(status)
    if source_type:
        where.append("source_type = :stype")
        params["stype"] = str(source_type)
    if source_system:
        where.append("source_system = :ssys")
        params["ssys"] = str(source_system)
    rows = await database.fetch_all(
        f"""
        SELECT id, merchant_id, platform, platform_product_id, variant_id, group_id,
               source_type, source_system, external_review_id,
               verification, rating, title,
               COALESCE(NULLIF(body_redacted, ''), body) AS body_effective,
               media_count, status, created_at, updated_at
        FROM product_reviews
        WHERE {' AND '.join(where)}
        ORDER BY created_at DESC, id DESC
        LIMIT {limit}
        """,
        params,
    )
    return {"items": [dict(r) for r in rows], "limit": limit}


@router.get(
    "/moderation/reviews/{review_id}/media",
)
async def employee_list_review_media(
    review_id: int,
    actor: Dict[str, Any] = Depends(require_employee_permissions(["reviews.read"])),
) -> Dict[str, Any]:
    rows = await database.fetch_all(
        media_assets.select()
        .where((media_assets.c.review_id == int(review_id)) & (media_assets.c.status == "active"))
        .order_by(media_assets.c.id.asc())
    )
    return {"items": [dict(r) for r in rows]}
