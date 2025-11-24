"""Platform Merchant Onboarding v2 – Routes (EPIC-1/2)

These endpoints are intentionally side-car and **behind a feature flag**.
They provide a stable HTTP surface for the merchant portal to integrate
with, without changing existing `/merchant/onboarding/*` behaviour.
"""

from fastapi import APIRouter, Depends, HTTPException, status, File, Form, UploadFile
from pydantic import BaseModel, EmailStr, Field
import csv
import io
import logging
import uuid
from typing import Optional, Dict, Any, Set

from config.settings import settings
from services.platform_onboarding_service import (
    register_platform_merchant_v2,
    get_platform_onboarding_v2,
    PlatformOnboardingNotFound,
)
from services.platform_import_service import list_import_tasks, schedule_import_task
from services.connector_service import (
    validate_credentials,
    InvalidConnectorError,
    CredentialsError,
)
from utils.auth import get_current_user, can_access_merchant
from db.merchant_onboarding import get_merchant_onboarding
from db.platform_import_reports import save_raw_report, attach_import_task
from db.platform_orders import list_orders_for_merchant

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/platform-onboarding", tags=["platform-onboarding-v2"])

ORDER_REPORT_REQUIRED_COLUMNS: Dict[str, Set[str]] = {
    "amazon": {"order_id", "order_item_id", "sku", "quantity", "price", "currency"},
    "temu": {"order_id", "product_id", "variant_id", "quantity", "price", "currency"},
}


def _decode_csv_bytes(raw_bytes: bytes) -> str:
    """
    Attempt to decode CSV bytes with multiple encodings.
    
    Tries common encodings in order:
    1. UTF-8 with BOM (utf-8-sig)
    2. UTF-8
    3. Latin-1 / ISO-8859-1 (macOS Numbers default)
    4. Windows-1252 (Windows Excel default)
    5. GBK (Simplified Chinese)
    
    Returns the decoded text string.
    Raises ValueError if all encodings fail.
    """
    encodings = [
        "utf-8-sig",     # UTF-8 with BOM (Excel UTF-8 export)
        "utf-8",         # Standard UTF-8
        "latin-1",       # ISO-8859-1 (macOS Numbers, some European languages)
        "windows-1252",  # Windows Excel default
        "gbk",           # Simplified Chinese (中文)
    ]
    
    last_error = None
    for encoding in encodings:
        try:
            text = raw_bytes.decode(encoding)
            # Successfully decoded, log which encoding worked
            logger.debug(f"CSV decoded successfully with {encoding}")
            return text
        except (UnicodeDecodeError, LookupError) as e:
            last_error = e
            continue
    
    # All encodings failed
    raise ValueError(f"Failed to decode CSV with any supported encoding. Last error: {last_error}")


def ensure_feature_enabled() -> None:
    """Guard all v2 endpoints with the global feature flag.

    When the flag is disabled, we deliberately return 404 instead of 403 so
    the routes appear as "non-existent" to external callers and do not leak
    implementation details.
    """

    if not settings.platform_onboarding_v2_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Platform merchant onboarding v2 is not enabled",
        )


class PlatformOnboardingRegisterRequest(BaseModel):
    """Minimal payload for EPIC‑1.5: first real backend step.

    This remains deliberately smaller than the full v1 onboarding payload
    but adds optional fields so we can persist a proper record without
    impacting existing `/merchant/onboarding/*` flows.
    """

    business_name: str
    region: str
    # Optional: hint about the data source
    source_type: Optional[str] = None  # e.g. "connector" | "report" | "unknown"
    # Optional: connector type – when omitted we fall back to a safe default
    connector: Optional[str] = None  # e.g. "shopify" | "manual"
    # Optional: contact + URL if already known; otherwise placeholders are used
    contact_email: Optional[EmailStr] = None
    store_url: Optional[str] = None


class ConnectorValidationRequest(BaseModel):
    """Request payload for validating connector credentials."""

    connector: str = Field(
        ...,
        description="Connector identifier (e.g. 'shopify', 'linnworks', 'channeladvisor', 'manual')",
    )
    credentials: Dict[str, Any] = Field(
        default_factory=dict,
        description="Connector-specific credentials (e.g. Shopify shop_domain/access_token).",
    )
    # Reserved for future use; current implementation always performs a real validation
    # when credentials are provided, and a lightweight test when credentials are empty.
    test_mode: Optional[bool] = Field(
        default=None,
        description="Optional hint for validation mode; currently informational only.",
    )


class ConnectorValidationResponse(BaseModel):
    """Standardized response for connector credential validation."""

    onboarding_id: str
    connector: str
    validated: bool
    status: str  # "valid" | "invalid" | "error"
    test_mode: bool
    validation_result: Dict[str, Any]
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


@router.get("/feature-status", dependencies=[Depends(ensure_feature_enabled)])
async def feature_status() -> dict:
    """Simple health-style endpoint for the portal to probe.

    Returns static metadata for now; can be extended later with per-merchant
    settings (e.g. which connectors are supported in their region).
    """

    return {
        "enabled": True,
        "version": "v2-skeleton",
    }


@router.post("/register", dependencies=[Depends(ensure_feature_enabled)])
async def register_platform_merchant(
    payload: PlatformOnboardingRegisterRequest,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Register a Platform merchant – first real v2 step.

    Now uses the current logged-in merchant's ID instead of creating a new one.
    This ensures that:
    - The merchant owns the platform_profile they create
    - Access control checks work correctly
    - merchant_id remains consistent across all operations

    It intentionally does NOT:
    - run auto-KYB,
    - create user accounts,
    - connect PSPs.
    """

    # Extract merchant_id from current user
    merchant_id = current_user.get("merchant_id")
    if not merchant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current user does not have a merchant_id",
        )

    try:
        return await register_platform_merchant_v2(
            merchant_id=merchant_id,
            business_name=payload.business_name,
            region=payload.region,
            source_type=payload.source_type,
            connector=payload.connector,
            contact_email=payload.contact_email,
            store_url=payload.store_url,
        )
    except Exception as exc:
        # Do not leak internal details to the client; log server-side instead.
        # FastAPI's default logging will capture this if unhandled.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to register platform merchant",
        ) from exc


@router.get("/{onboarding_id}", dependencies=[Depends(ensure_feature_enabled)])
async def get_platform_onboarding(onboarding_id: str) -> dict:
    """Read-only view of a Platform onboarding record.

    This endpoint is side-car only and does not expose any v1-specific fields.
    """

    try:
        return await get_platform_onboarding_v2(onboarding_id)
    except PlatformOnboardingNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Platform onboarding not found",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to load platform onboarding record",
        ) from exc


@router.get("/{onboarding_id}/import-tasks", dependencies=[Depends(ensure_feature_enabled)])
async def get_platform_import_tasks(onboarding_id: str) -> dict:
    """List ImportTasks for a Platform merchant (read-only, v2-only).

    Uses onboarding_id as merchant_id since v2 onboarding IDs are merchant IDs.
    """

    try:
        tasks = await list_import_tasks(onboarding_id)
        return {"onboarding_id": onboarding_id, "tasks": tasks}
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to load platform import tasks",
        ) from exc


@router.get("/{onboarding_id}/orders", dependencies=[Depends(ensure_feature_enabled)])
async def list_platform_orders(
    onboarding_id: str,
    platform: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """
    List imported platform orders (side-car cache).
    Version: 2.0 - Using SQLAlchemy Table API
    """

    if not can_access_merchant(current_user, onboarding_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to access orders for this merchant",
        )

    try:
        safe_platform = platform.strip().lower() if platform else None
        logger.info(f"Listing orders for merchant={onboarding_id}, platform={safe_platform}, limit={limit}, offset={offset}")
        result = await list_orders_for_merchant(
            merchant_id=onboarding_id,
            platform=safe_platform,
            limit=limit,
            offset=offset,
        )
        logger.info(f"Successfully retrieved {len(result.get('orders', []))} orders, total={result.get('total', 0)}")
        return result
    except Exception as exc:
        logger.error(f"Failed to list platform orders (v2.0): {exc}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list orders (v2.0): {str(exc)}",
        ) from exc


@router.post(
    "/{onboarding_id}/connectors/validate",
    dependencies=[Depends(ensure_feature_enabled)],
    response_model=ConnectorValidationResponse,
)
async def validate_platform_connector_credentials(
    onboarding_id: str,
    payload: ConnectorValidationRequest,
    current_user: dict = Depends(get_current_user),
) -> ConnectorValidationResponse:
    """
    Validate connector credentials for a given Platform onboarding record.

    - Auth: any authenticated user who can access the merchant/onboarding.
    - Scope: does NOT persist credentials; only performs validation and returns
      a standardized result for the portal to interpret.
    """

    # Access control: ensure the caller is allowed to see this onboarding/merchant.
    if not can_access_merchant(current_user, onboarding_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": "ACCESS_DENIED",
                "message": "Not authorized to validate connectors for this merchant",
            },
        )

    # Ensure onboarding exists (and is part of v2).
    try:
        await get_platform_onboarding_v2(onboarding_id)
    except PlatformOnboardingNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": "ONBOARDING_NOT_FOUND",
                "message": "Platform onboarding not found",
            },
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error_code": "INTERNAL_ERROR",
                "message": "Failed to load platform onboarding record",
            },
        ) from exc

    # Delegate credential checks to connector_service.
    try:
        validation = await validate_credentials(
            connector=payload.connector,  # type: ignore[arg-type]
            credentials=payload.credentials or {},
        )
    except InvalidConnectorError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "INVALID_CONNECTOR",
                "message": str(exc),
            },
        )
    except CredentialsError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "INVALID_CREDENTIALS",
                "message": str(exc),
            },
        )
    except HTTPException:
        # Propagate existing HTTPExceptions unchanged.
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error_code": "INTERNAL_ERROR",
                "message": "Connector validation failed",
            },
        ) from exc

    validated = bool(validation.get("valid", False))
    test_mode = bool(validation.get("test_mode", False))

    metadata: Dict[str, Any] = {
        "validated_at": validation.get("validated_at"),
        "http_status": status.HTTP_200_OK,
    }

    return ConnectorValidationResponse(
        onboarding_id=onboarding_id,
        connector=validation.get("connector") or payload.connector,
        validated=validated,
        status="valid" if validated else "invalid",
        test_mode=test_mode,
        validation_result=validation,
        error_code=None,
        error_message=None,
        metadata=metadata,
    )


# ============================================================================
# Merchant-facing Platform Reports Endpoints (EPIC-6 / Merchant Portal CSV)
# ============================================================================

ALLOWED_REPORT_TYPES = {"amazon", "temu"}


def _get_required_columns_for_report(report_type: str) -> Set[str]:
    """Return minimal required CSV columns for a report type."""
    if report_type == "amazon":
        return {"asin", "seller_sku", "title", "price", "currency"}
    if report_type == "temu":
        return {"product_id", "variant_id", "name", "price", "currency"}
    return set()


@router.post("/{onboarding_id}/reports/validate", dependencies=[Depends(ensure_feature_enabled)])
async def validate_platform_report(
    onboarding_id: str,
    report_type: str = Form(...),
    file: UploadFile = File(...),
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Merchant-facing: Validate CSV before upload.
    
    Returns validation status, missing columns, row preview, etc.
    """
    # Access control
    if not can_access_merchant(current_user, onboarding_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to validate reports for this merchant",
        )
    
    report_type = (report_type or "").strip().lower()
    if report_type not in ALLOWED_REPORT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported report_type: {report_type}",
        )
    
    # Verify onboarding exists
    record = await get_merchant_onboarding(onboarding_id)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Onboarding not found",
        )
    
    # Read file with automatic encoding detection
    try:
        raw_bytes = await file.read()
        text = _decode_csv_bytes(raw_bytes)
    except ValueError as exc:
        # Encoding detection failed
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to decode file: {exc}. Please ensure the file is a valid CSV with UTF-8, Latin-1, or Windows-1252 encoding.",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to read file: {exc}",
        )
    
    if not text.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File is empty",
        )
    
    # Parse CSV
    try:
        reader = csv.DictReader(io.StringIO(text))
        headers = set(reader.fieldnames or [])
        rows = list(reader)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid CSV format: {exc}",
        )
    
    required_cols = _get_required_columns_for_report(report_type)
    missing_cols = required_cols - headers
    
    # Scan first few rows for validation
    rows_scanned = min(5, len(rows))
    rows_with_issues = []
    
    for idx, row in enumerate(rows[:rows_scanned], start=1):
        missing_required = [col for col in required_cols if not row.get(col, "").strip()]
        if missing_required:
            rows_with_issues.append({
                "row_number": idx,
                "missing_required_fields": missing_required,
                "valid": False,
            })
    
    ready_to_import = len(missing_cols) == 0
    
    return {
        "report_type": report_type,
        "required_columns": list(required_cols),
        "found_columns": list(headers),
        "missing_columns": list(missing_cols),
        "rows_scanned": rows_scanned,
        "rows_with_missing_required_fields": len(rows_with_issues),
        "ready_to_import": ready_to_import,
        "preview": rows_with_issues[:3] if rows_with_issues else [],
    }


@router.post("/{onboarding_id}/reports/upload", dependencies=[Depends(ensure_feature_enabled)])
async def upload_platform_report(
    onboarding_id: str,
    report_type: str = Form(...),
    file: UploadFile = File(...),
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Merchant-facing: Upload platform report (CSV).
    
    Stores raw content and schedules import task.
    """
    # Access control
    if not can_access_merchant(current_user, onboarding_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to upload reports for this merchant",
        )
    
    report_type = (report_type or "").strip().lower()
    if report_type not in ALLOWED_REPORT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported report_type: {report_type}",
        )
    
    # Verify onboarding
    record = await get_merchant_onboarding(onboarding_id)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Onboarding not found",
        )
    
    # Read file with automatic encoding detection
    try:
        raw_bytes = await file.read()
        text = _decode_csv_bytes(raw_bytes)
    except ValueError as exc:
        # Encoding detection failed
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to decode file: {exc}. Please ensure the file is a valid CSV with UTF-8, Latin-1, or Windows-1252 encoding.",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to read file: {exc}",
        )
    
    if not text.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File is empty",
        )
    
    # Basic CSV validation
    try:
        reader = csv.DictReader(io.StringIO(text))
        headers = set(reader.fieldnames or [])
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid CSV format: {exc}",
        )
    
    required_cols = _get_required_columns_for_report(report_type)
    missing_cols = required_cols - headers
    
    if missing_cols:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Missing required columns: {sorted(missing_cols)}",
        )
    
    # Save raw report
    filename = file.filename or f"{report_type}_report.csv"
    report_id = await save_raw_report(
        merchant_id=onboarding_id,
        report_type=report_type,
        raw_content=text,
        original_filename=filename,
        created_by=current_user.get("email") or current_user.get("merchant_id") or "system",
    )
    
    # Schedule import task, using the saved report as saga context
    connector_value = f"{report_type}_report"
    try:
        import_task_id = await schedule_import_task(
            merchant_id=onboarding_id,
            source_type="report",
            connector=connector_value,
            saga_id=str(report_id),
        )
        logger.info(
            f"Platform report uploaded for merchant {onboarding_id}: "
            f"report_id={report_id}, import_task_id={import_task_id}"
        )
        # Link report to import task for Details view
        await attach_import_task(report_id, import_task_id)
    except Exception as exc:
        logger.exception(f"Failed to schedule import task for report {report_id}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Report saved but failed to schedule import task",
        ) from exc
    
    return {
        "report_id": report_id,
        "import_task_id": import_task_id,
        "message": "Report uploaded successfully. Import task scheduled.",
    }


@router.post("/{onboarding_id}/orders/validate", dependencies=[Depends(ensure_feature_enabled)])
async def validate_platform_orders_csv(
    onboarding_id: str,
    platform: str = Form(..., description='Platform, e.g. "amazon" or "temu"'),
    file: UploadFile = File(...),
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Merchant-facing validation for platform orders CSV (side-car, stub).

    This is a non-persistent validator to shape the future orders import pipeline.
    """
    if not can_access_merchant(current_user, onboarding_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to validate orders for this merchant",
        )

    platform = (platform or "").strip().lower()
    required_cols = ORDER_REPORT_REQUIRED_COLUMNS.get(platform)
    if not required_cols:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported platform: {platform}. Supported: {sorted(ORDER_REPORT_REQUIRED_COLUMNS.keys())}",
        )

    record = await get_merchant_onboarding(onboarding_id)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Onboarding not found",
        )

    try:
        raw_bytes = await file.read()
        text = _decode_csv_bytes(raw_bytes)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to decode file: {exc}",
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

    missing = required_cols.difference(set(header))

    preview_rows = []
    total_scanned = 0
    max_preview_rows = 5
    for row in reader:
        total_scanned += 1
        if len(preview_rows) >= max_preview_rows:
            continue
        row_missing = [col for col in required_cols if not (row.get(col) or "").strip()]
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
        "platform": platform,
        "header": header,
        "required_columns": sorted(required_cols),
        "missing_columns": sorted(missing),
        "preview": preview_rows,
        "issues": issues,
        "ready_to_import": len(missing) == 0,
    }


@router.post("/{onboarding_id}/orders/upload", dependencies=[Depends(ensure_feature_enabled)])
async def upload_platform_orders_csv(
    onboarding_id: str,
    platform: str = Form(..., description='Platform, e.g. "amazon"'),
    file: UploadFile = File(...),
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Merchant-facing upload for platform orders CSV.

    Flow:
    - Validate required columns
    - Persist raw report to platform_import_reports
    - Schedule ImportTask with source_type="orders_report" (connector: "<platform>_orders")
    """
    if not can_access_merchant(current_user, onboarding_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to upload orders for this merchant",
        )

    platform = (platform or "").strip().lower()
    required_cols = ORDER_REPORT_REQUIRED_COLUMNS.get(platform)
    if not required_cols:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported platform: {platform}. Supported: {sorted(ORDER_REPORT_REQUIRED_COLUMNS.keys())}",
        )

    record = await get_merchant_onboarding(onboarding_id)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Onboarding not found",
        )

    try:
        raw_bytes = await file.read()
        text = _decode_csv_bytes(raw_bytes)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to decode file: {exc}",
        )

    try:
        reader = csv.DictReader(io.StringIO(text))
        headers = set(reader.fieldnames or [])
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid CSV format: {exc}",
        )

    missing = required_cols.difference(headers)
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Missing required columns: {sorted(missing)}",
        )

    filename = file.filename or f"{platform}_orders.csv"
    try:
        report_id = await save_raw_report(
            merchant_id=onboarding_id,
            report_type=platform,  # keep platform name for worker to derive
            raw_content=text,
            original_filename=filename,
            created_by=current_user.get("email") or current_user.get("merchant_id") or "merchant",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to persist orders report: {exc}",
        ) from exc

    # Schedule import task for orders_report
    connector_value = f"{platform}_orders"
    try:
        import_task_id = await schedule_import_task(
            merchant_id=onboarding_id,
            source_type="orders_report",
            connector=connector_value,
            saga_id=str(report_id),
        )
        await attach_import_task(report_id, import_task_id)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to schedule orders import task: {exc}",
        ) from exc

    return {
        "status": "accepted",
        "onboarding_id": onboarding_id,
        "platform": platform,
        "report_id": report_id,
        "import_task_id": import_task_id,
        "message": "Orders CSV accepted. Import task scheduled.",
    }
