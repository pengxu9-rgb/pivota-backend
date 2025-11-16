"""Platform Merchant Onboarding v2 – Routes (EPIC-1/2)

These endpoints are intentionally side-car and **behind a feature flag**.
They provide a stable HTTP surface for the merchant portal to integrate
with, without changing existing `/merchant/onboarding/*` behaviour.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field

from config.settings import settings
from services.platform_onboarding_service import (
    register_platform_merchant_v2,
    get_platform_onboarding_v2,
    PlatformOnboardingNotFound,
)
from services.platform_import_service import list_import_tasks
from services.connector_service import (
    validate_credentials,
    InvalidConnectorError,
    CredentialsError,
)
from utils.auth import get_current_user, can_access_merchant

from typing import Optional, Dict, Any

router = APIRouter(prefix="/platform-onboarding", tags=["platform-onboarding-v2"])


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
async def register_platform_merchant(payload: PlatformOnboardingRegisterRequest) -> dict:
    """Register a Platform merchant – first real v2 step.

    Unlike the initial skeleton, this endpoint now:
    - creates a `merchant_onboarding` row (side-car to v1),
    - writes a minimal `platform_profile` JSON blob,
    - returns a real onboarding_id (merchant_id).

    It intentionally does NOT:
    - run auto-KYB,
    - create user accounts,
    - connect PSPs.
    """

    try:
        return await register_platform_merchant_v2(
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
