"""
Platform Merchant Onboarding v2 – core service (EPIC‑1)

For EPIC‑1 this service does the minimum necessary to:
1) Create a merchant_onboarding row without touching existing v1 flows.
2) Persist a basic platform_profile JSON side-car for future EPICs.

It intentionally avoids:
- Auto-KYB / PSP setup (those stay in v1 routes for now).
- User account creation – platform merchants will log in via a later flow.
"""

from datetime import datetime
from typing import Any, Dict, Optional
import logging
import re

from db.merchant_onboarding import (
    create_merchant_onboarding,
    get_merchant_onboarding,
    update_platform_profile,
)
from services.platform_import_service import schedule_import_task
from services.connector_service import (
    ConnectorType,
    prepare_import_job,
    InvalidConnectorError,
)

logger = logging.getLogger(__name__)


class PlatformOnboardingNotFound(Exception):
    """Raised when a Platform onboarding record cannot be found."""
    pass


def _slugify_name(name: str) -> str:
    """Very small helper to build a stable placeholder slug from the business name."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()
    return slug or "platform-merchant"


async def register_platform_merchant_v2(
    *,
    business_name: str,
    region: str,
    source_type: Optional[str] = None,
    contact_email: Optional[str] = None,
    store_url: Optional[str] = None,
    connector: Optional[ConnectorType] = None,
) -> Dict[str, Any]:
    """
    Create a new Platform merchant onboarding record.

    - Writes a row into merchant_onboarding with safe placeholder values
      for fields that v1 flows require (store_url, contact_email).
    - Attaches a minimal platform_profile JSON side-car that future EPICs
      will extend with connector/import metadata.
    """

    now = datetime.utcnow()

    # Derive safe values for required legacy columns.
    slug = _slugify_name(business_name)
    safe_store_url = store_url or f"{slug}.platform-placeholder.local"
    safe_contact_email = contact_email or f"{slug}+platform-{int(now.timestamp())}@placeholder.pivota"

    merchant_data: Dict[str, Any] = {
        "business_name": business_name,
        "store_url": safe_store_url,
        "region": region,
        "contact_email": safe_contact_email,
        # Leave optional fields (website, contact_phone, etc.) as defaults.
    }

    logger.info(
        "Creating platform merchant onboarding record",
        extra={
            "business_name": business_name,
            "region": region,
            "source_type": source_type or "unknown",
        },
    )

    merchant_id = await create_merchant_onboarding(merchant_data)

    resolved_source_type = source_type or "unknown"

    profile: Dict[str, Any] = {
        "version": "v2",
        "created_via": "platform_onboarding_v2",
        "created_at": now.isoformat(),
        "business_name": business_name,
        "region": region,
        "source_type": resolved_source_type,
    }
    if store_url:
        profile["original_store_url"] = store_url
    if contact_email:
        profile["original_contact_email"] = contact_email

    # EPIC‑2 connector integration: if this onboarding is marked as connector-based,
    # prepare a connector import job via connector_service. For now we default to
    # the "manual" connector when none is explicitly provided.
    resolved_connector: Optional[str] = connector
    if resolved_source_type == "connector":
        if not resolved_connector:
            resolved_connector = "manual"
        try:
            job = await prepare_import_job(
                merchant_id=merchant_id,
                connector=resolved_connector,  # type: ignore[arg-type]
                credentials={},
                options=None,
            )
            profile["connector"] = {
                "connector": job.get("connector"),
                "name": job.get("connector_info", {}).get("name"),
                "connector_type": job.get("connector_info", {}).get("type"),
                "job_id": job.get("job_id"),
            }
        except InvalidConnectorError:
            logger.exception("Invalid connector for merchant %s", merchant_id)
        except Exception:
            logger.exception("Failed to prepare connector import job for merchant %s", merchant_id)

    await update_platform_profile(merchant_id, profile)

    # EPIC‑2: schedule an ImportTask so we can start tracking imports without
    # changing any v1 flows.
    import_task_id: Optional[int] = None
    try:
        import_task_id = await schedule_import_task(
            merchant_id=merchant_id,
            source_type=resolved_source_type,
            connector=resolved_connector,
            saga_id=None,
        )
    except Exception:
        # Import tracking is best-effort at this stage; failures should not
        # block the onboarding record creation.
        logger.exception("Failed to schedule platform import task")

    return {
        "onboarding_id": merchant_id,
        "business_name": business_name,
        "region": region,
        "source_type": resolved_source_type,
        "status": "pending_verification",
        "platform_profile": profile,
        "import_task_id": import_task_id,
        "message": (
            "Platform onboarding v2 record created. "
            "KYC/PSP steps will be wired in future EPICs."
        ),
    }


async def get_platform_onboarding_v2(onboarding_id: str) -> Dict[str, Any]:
    """
    Fetch a Platform onboarding record by merchant_id/onboarding_id.

    Returns a minimal, v2-focused view that is safe for the portal to consume.
    """

    record = await get_merchant_onboarding(onboarding_id)
    if not record:
        raise PlatformOnboardingNotFound(f"Platform onboarding not found: {onboarding_id}")

    profile = record.get("platform_profile") or {}

    return {
        "onboarding_id": record.get("merchant_id"),
        "business_name": record.get("business_name"),
        "region": record.get("region"),
        "status": record.get("status"),
        "platform_profile": profile,
    }
