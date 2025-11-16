"""
Admin Connector Credentials Routes

Admin-only utilities for storing per-merchant connector credentials.
These endpoints are intended for internal/staging usage and do not affect v1 flows.
"""

from datetime import datetime
from typing import Dict, Any, Optional, List

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from utils.auth import require_admin
from services.crypto_service import crypto_service
from db.connector_credentials import (
    create_connector_credentials,
    list_connector_credentials_for_merchant,
)
from db.merchant_onboarding import get_merchant_onboarding, update_platform_profile
from services.platform_onboarding_service import (
    get_platform_onboarding_v2,
    PlatformOnboardingNotFound,
)
from services.connector_service import (
    validate_credentials,
    InvalidConnectorError,
    CredentialsError,
)

router = APIRouter(
    prefix="/admin/platform-onboarding",
    tags=["Admin - Connector Credentials"],
)


class AdminStoreConnectorCredentialsRequest(BaseModel):
    connector: str = Field(..., description="Connector identifier, e.g. 'shopify'")
    credentials: Dict[str, Any] = Field(
        ..., description="Connector credentials payload (e.g. Shopify shop_domain/access_token)."
    )
    label: Optional[str] = Field(
        default=None,
        description="Optional human-readable label for this credential entry.",
    )
    validate: bool = Field(
        default=True,
        description="Whether to validate the credentials before storing.",
    )


class AdminStoreConnectorCredentialsResponse(BaseModel):
    credential_id: int
    onboarding_id: str
    connector: str
    label: Optional[str]
    validated: bool
    validation_result: Optional[Dict[str, Any]] = None


class AdminConnectorCredentialInfo(BaseModel):
    id: int
    connector: str
    label: Optional[str]
    is_valid: bool
    last_validated_at: Optional[datetime]
    last_used_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime


class AdminListConnectorCredentialsResponse(BaseModel):
    onboarding_id: str
    connector: Optional[str]
    credentials: List[AdminConnectorCredentialInfo]


@router.post(
    "/{onboarding_id}/connectors/credentials",
    response_model=AdminStoreConnectorCredentialsResponse,
)
async def admin_store_connector_credentials(
    onboarding_id: str,
    payload: AdminStoreConnectorCredentialsRequest,
    current_admin: dict = Depends(require_admin),
) -> AdminStoreConnectorCredentialsResponse:
    """
    Store encrypted connector credentials for a given Platform onboarding (merchant).

    - Admin-only endpoint.
    - Optionally validates credentials before saving.
    - Uses CONNECTOR_CREDENTIALS_KEY-backed AES-GCM encryption at rest.
    """

    # Ensure onboarding exists (and is part of v2).
    try:
        await get_platform_onboarding_v2(onboarding_id)
    except PlatformOnboardingNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Platform onboarding not found",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to load platform onboarding record: {exc}",
        )

    # Optionally validate credentials via connector_service.
    validation_result: Optional[Dict[str, Any]] = None
    if payload.validate:
        try:
            validation_result = await validate_credentials(
                connector=payload.connector,  # type: ignore[arg-type]
                credentials=payload.credentials,
            )
        except InvalidConnectorError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid connector: {exc}",
            )
        except CredentialsError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid credentials: {exc}",
            )
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Connector validation failed: {exc}",
            )

    # Encrypt credentials for storage.
    try:
        encrypted = crypto_service.encrypt_json_secret(payload.credentials)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Secret storage disabled: {exc}",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to encrypt credentials: {exc}",
        )

    # Persist credential row.
    cred_id = await create_connector_credentials(
        merchant_id=onboarding_id,
        connector=payload.connector,
        credentials_encrypted=encrypted,
        credential_label=payload.label,
        last_validation_result=validation_result,
        last_validated_at=datetime.utcnow() if validation_result else None,
    )

    # Attach credential_id into platform_profile.connector for this onboarding (merchant).
    try:
        record = await get_merchant_onboarding(onboarding_id)
        if record:
            profile = record.get("platform_profile") or {}
            connector_info = profile.get("connector") or {}
            # Merge without dropping existing fields (connector name, job_id, etc.).
            connector_info.update(
                {
                    "connector": payload.connector,
                    "credential_id": cred_id,
                    "credential_label": payload.label,
                }
            )
            profile["connector"] = connector_info
            await update_platform_profile(onboarding_id, profile)
    except Exception as exc:
        # Do not fail the main operation if profile update fails; log server-side instead.
        # In this environment we keep error handling minimal.
        print(f"⚠️ Failed to update platform_profile with credential_id: {exc}")

    return AdminStoreConnectorCredentialsResponse(
        credential_id=cred_id,
        onboarding_id=onboarding_id,
        connector=payload.connector,
        label=payload.label,
        validated=bool(validation_result),
        validation_result=validation_result,
    )


@router.get(
    "/{onboarding_id}/connectors/credentials",
    response_model=AdminListConnectorCredentialsResponse,
)
async def admin_list_connector_credentials(
    onboarding_id: str,
    connector: Optional[str] = None,
    current_admin: dict = Depends(require_admin),
) -> AdminListConnectorCredentialsResponse:
    """
    List stored connector credentials for a given Platform onboarding (merchant).

    Returns metadata only; never returns decrypted secrets.
    """

    # Ensure onboarding exists.
    try:
        await get_platform_onboarding_v2(onboarding_id)
    except PlatformOnboardingNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Platform onboarding not found",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to load platform onboarding record: {exc}",
        )

    rows = await list_connector_credentials_for_merchant(onboarding_id, connector=connector)
    items: List[AdminConnectorCredentialInfo] = []
    for r in rows:
        items.append(
            AdminConnectorCredentialInfo(
                id=r["id"],
                connector=r["connector"],
                label=r.get("credential_label"),
                is_valid=bool(r.get("is_valid")),
                last_validated_at=r.get("last_validated_at"),
                last_used_at=r.get("last_used_at"),
                created_at=r.get("created_at"),
                updated_at=r.get("updated_at"),
            )
        )

    return AdminListConnectorCredentialsResponse(
        onboarding_id=onboarding_id,
        connector=connector,
        credentials=items,
    )
