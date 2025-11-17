"""
Admin Platform Report Import Routes - EPIC-6 Phase 1

Admin-only CSV upload endpoint for Amazon/Temu-like platform reports.
Stores raw reports in platform_import_reports and schedules a Platform
ImportTask with source_type="report" for catalog_import_worker.
"""

from typing import Dict, Any, Set
import csv
import io
import logging

from fastapi import APIRouter, Depends, HTTPException, File, Form, UploadFile, status

from utils.auth import require_admin
from db.merchant_onboarding import get_merchant_onboarding
from db.platform_import_reports import save_raw_report, attach_import_task, get_platform_report
from services.platform_import_service import schedule_import_task, get_import_task_details

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/platform-onboarding",
    tags=["Admin - Platform Report Import"],
)


ALLOWED_REPORT_TYPES = {"amazon", "temu"}  # Phase 1: Amazon; Phase 2: Temu.


def _get_required_columns_for_report(report_type: str) -> Set[str]:
    """
    Return the minimal required CSV header columns for a given report_type.

    Supported templates from EPIC-6:
    - Amazon: asin, seller_sku, title, price, currency
    - Temu: product_id, variant_id, name, price, currency
    """
    if report_type == "amazon":
        return {"asin", "seller_sku", "title", "price", "currency"}
    if report_type == "temu":
        return {"product_id", "variant_id", "name", "price", "currency"}
    return set()


@router.post("/{onboarding_id}/reports/upload")
async def upload_platform_report(
    onboarding_id: str,
    report_type: str = Form(..., description='Report type, e.g. "amazon"'),
    file: UploadFile = File(...),
    current_admin: Dict[str, Any] = Depends(require_admin),
) -> Dict[str, Any]:
    """
    Admin-only endpoint to upload a platform product report (CSV).

    Phase 1/2:
    - Supports report_type="amazon" and "temu".
    - Validates onboarding exists.
    - Performs basic CSV header validation.
    - Stores raw content in platform_import_reports.
    - Schedules a Platform ImportTask with source_type="report".
    """

    report_type = (report_type or "").strip().lower()
    if report_type not in ALLOWED_REPORT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported report_type: {report_type}. Supported: {sorted(ALLOWED_REPORT_TYPES)}",
        )

    # 1. Ensure onboarding/merchant exists (v2 onboarding shares the same merchant_id space).
    record = await get_merchant_onboarding(onboarding_id)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Onboarding not found",
        )

    # 2. Read file content (small CSVs; in-memory read is acceptable for Phase 1).
    try:
        raw_bytes = await file.read()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to read uploaded file: {exc}",
        )

    if not raw_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty",
        )

    try:
        # utf-8-sig to be tolerant of BOM
        text = raw_bytes.decode("utf-8-sig")
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to decode CSV as UTF-8: {exc}",
        )

    # 3. Basic CSV header validation.
    try:
        reader = csv.DictReader(io.StringIO(text))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to parse CSV header: {exc}",
        )

    header = reader.fieldnames or []
    if not header:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="CSV header is missing or empty",
        )

    required_columns = _get_required_columns_for_report(report_type)
    missing = required_columns.difference(set(header))
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Missing required columns: {sorted(missing)}",
        )

    # 4. Persist raw report content.
    created_by = (
        current_admin.get("sub")
        or current_admin.get("email")
        or current_admin.get("id")
        or "admin"
    )
    original_filename = file.filename or f"{report_type}_report.csv"

    try:
        report_id = await save_raw_report(
            merchant_id=onboarding_id,
            report_type=report_type,
            raw_content=text,
            original_filename=original_filename,
            created_by=str(created_by),
        )
    except Exception as exc:
        logger.exception("Failed to save platform report", extra={"onboarding_id": onboarding_id, "error": str(exc)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to persist platform report: {exc}",
        )

    # Optionally fetch rows_total for response.
    rows_total = None
    try:
        report_row = await get_platform_report(report_id)
        if report_row is not None:
            rows_total = report_row.get("rows_total")
    except Exception:
        # Non-fatal; keep rows_total as None if this fails.
        rows_total = None

    # 5. Schedule an ImportTask for catalog_import_worker.
    connector_name = f"{report_type}_report"
    try:
        import_task_id = await schedule_import_task(
            merchant_id=onboarding_id,
            source_type="report",
            connector=connector_name,
            saga_id=str(report_id),
        )
        await attach_import_task(report_id, import_task_id)
    except Exception as exc:
        logger.exception(
            "Failed to schedule import task for platform report",
            extra={"onboarding_id": onboarding_id, "report_id": report_id, "error": str(exc)},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to schedule import task: {exc}",
        )

    logger.info(
        "Admin platform report uploaded and import task scheduled",
        extra={
            "onboarding_id": onboarding_id,
            "report_type": report_type,
            "report_id": report_id,
            "import_task_id": import_task_id,
            "rows_total": rows_total,
        },
    )

    return {
        "status": "accepted",
        "onboarding_id": onboarding_id,
        "report_type": report_type,
        "report_id": report_id,
        "import_task_id": import_task_id,
        "rows_total": rows_total,
        "message": "Report uploaded and import task scheduled",
    }


@router.post("/{onboarding_id}/reports/validate")
async def validate_platform_report(
    onboarding_id: str,
    report_type: str = Form(..., description='Report type, e.g. "amazon" or "temu"'),
    file: UploadFile = File(...),
    current_admin: Dict[str, Any] = Depends(require_admin),
) -> Dict[str, Any]:
    """
    Validate a platform product report (CSV) without persisting or scheduling an import.

    Returns:
    - header presence and missing required columns
    - basic row count (sampled)
    - preview of first few rows with mapping validity
    """

    report_type = (report_type or "").strip().lower()
    if report_type not in ALLOWED_REPORT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported report_type: {report_type}. Supported: {sorted(ALLOWED_REPORT_TYPES)}",
        )

    # Ensure onboarding exists (reuse same check as upload).
    record = await get_merchant_onboarding(onboarding_id)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Onboarding not found",
        )

    try:
        raw_bytes = await file.read()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to read uploaded file: {exc}",
        )

    if not raw_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty",
        )

    try:
        text = raw_bytes.decode("utf-8-sig")
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to decode CSV as UTF-8: {exc}",
        )

    try:
        reader = csv.DictReader(io.StringIO(text))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to parse CSV header: {exc}",
        )

    header = reader.fieldnames or []
    if not header:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="CSV header is missing or empty",
        )

    required_columns = _get_required_columns_for_report(report_type)
    missing = required_columns.difference(set(header))

    preview_rows = []
    total_scanned = 0
    MAX_PREVIEW_ROWS = 5

    for row in reader:
        total_scanned += 1
        if len(preview_rows) >= MAX_PREVIEW_ROWS:
            continue
        # For Phase 2 we keep preview simple: just echo the row and mark missing required fields.
        row_missing = [col for col in required_columns if not (row.get(col) or "").strip()]
        preview_rows.append(
            {
                "row_number": total_scanned,
                "row_data": row,
                "missing_required_fields": row_missing,
                "valid": not row_missing,
            }
        )

    issues = {
        "missing_columns": sorted(missing),
        "rows_scanned": total_scanned,
        "preview_rows": len(preview_rows),
        "rows_with_missing_required_fields": sum(1 for r in preview_rows if r["missing_required_fields"]),
    }

    return {
        "status": "validated",
        "onboarding_id": onboarding_id,
        "report_type": report_type,
        "header": header,
        "required_columns": sorted(required_columns),
        "missing_columns": sorted(missing),
        "preview": preview_rows,
        "issues": issues,
        "ready_to_import": len(missing) == 0,
    }


@router.get("/{onboarding_id}/reports/{report_id}/status")
async def get_report_import_status(
    onboarding_id: str,
    report_id: int,
    current_admin: Dict[str, Any] = Depends(require_admin),
) -> Dict[str, Any]:
    """
    Return a combined view of a stored report and its associated ImportTask (if any).
    """

    report = await get_platform_report(report_id)
    if not report or report.get("merchant_id") != onboarding_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Report not found",
        )

    import_task_id = report.get("import_task_id")
    task = None
    if import_task_id is not None:
        task = await get_import_task_details(import_task_id)

    return {
        "onboarding_id": onboarding_id,
        "report_id": report_id,
        "report": {
            "report_type": report.get("report_type"),
            "original_filename": report.get("original_filename"),
            "file_size_bytes": report.get("file_size_bytes"),
            "rows_total": report.get("rows_total"),
            "created_at": report.get("created_at"),
            "created_by": report.get("created_by"),
            "import_task_id": import_task_id,
        },
        "import_task": task,
    }
