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
from services.platform_import_service import schedule_import_task

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/platform-onboarding",
    tags=["Admin - Platform Report Import"],
)


ALLOWED_REPORT_TYPES = {"amazon"}  # Phase 1: only Amazon; Temu can be added later.


def _get_required_columns_for_report(report_type: str) -> Set[str]:
    """
    Return the minimal required CSV header columns for a given report_type.

    Phase 1 supports only the Amazon template described in EPIC-6:
    asin, seller_sku, title, price, currency
    """
    if report_type == "amazon":
        return {"asin", "seller_sku", "title", "price", "currency"}
    # Placeholder for future extensions (e.g. Temu)
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

    Phase 1:
    - Supports report_type="amazon" only.
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

