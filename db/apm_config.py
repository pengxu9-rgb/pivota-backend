"""Merchant Agent Presence Monitoring configuration accessors."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.sql import func

from db._jsonb_safe import _json_safe, coerce_jsonb_to_dict
from db.database import database
from db.merchant_onboarding import merchant_onboarding


APM_CADENCE_DAYS = {7, 14, 30}
APM_SCAN_MODES = {
    "open_product_visibility_test",
    "merchant_store_attribution_test",
    "category_visibility_test",
}
APM_PROVIDERS = {"gemini", "deepseek", "chatgpt", "claude"}
DEFAULT_APM_SCOPE = {
    "scan_modes": [
        "open_product_visibility_test",
        "merchant_store_attribution_test",
        "category_visibility_test",
    ],
    "providers": ["gemini"],
    "max_products_per_audit": 5,
}


class ApmConfigValidationError(ValueError):
    """Validation error carrying FastAPI-compatible field details."""

    def __init__(self, errors: List[Dict[str, Any]]) -> None:
        super().__init__("invalid APM configuration")
        self.errors = errors


def _field_error(field_path: List[str], msg: str) -> Dict[str, Any]:
    return {
        "loc": ["body", *field_path],
        "msg": msg,
        "type": "value_error",
    }


def validate_apm_config(
    *,
    cadence_days: Any,
    scope: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Validate and normalize the merchant-controlled APM scope."""
    errors: List[Dict[str, Any]] = []

    if cadence_days not in APM_CADENCE_DAYS:
        errors.append(
            _field_error(
                ["cadence_days"],
                "cadence_days must be one of 7, 14, or 30",
            )
        )

    if scope is None:
        raw_scope: Dict[str, Any] = {}
    elif isinstance(scope, dict):
        raw_scope = scope
    else:
        errors.append(_field_error(["scope"], "scope must be an object"))
        raw_scope = {}

    scan_modes = raw_scope.get("scan_modes", DEFAULT_APM_SCOPE["scan_modes"])
    providers = raw_scope.get("providers", DEFAULT_APM_SCOPE["providers"])
    max_products = raw_scope.get(
        "max_products_per_audit",
        DEFAULT_APM_SCOPE["max_products_per_audit"],
    )

    if (
        not isinstance(scan_modes, list)
        or not all(isinstance(item, str) for item in scan_modes)
    ):
        errors.append(
            _field_error(
                ["scope", "scan_modes"],
                "scan_modes must be a list of strings",
            )
        )
        normalized_scan_modes = list(DEFAULT_APM_SCOPE["scan_modes"])
    else:
        invalid_scan_modes = [
            item for item in scan_modes if item not in APM_SCAN_MODES
        ]
        if invalid_scan_modes:
            errors.append(
                _field_error(
                    ["scope", "scan_modes"],
                    (
                        "scan_modes contains unsupported values: "
                        + ", ".join(invalid_scan_modes)
                    ),
                )
            )
        normalized_scan_modes = list(scan_modes)

    if (
        not isinstance(providers, list)
        or not all(isinstance(item, str) for item in providers)
    ):
        errors.append(
            _field_error(
                ["scope", "providers"],
                "providers must be a list of strings",
            )
        )
        normalized_providers = list(DEFAULT_APM_SCOPE["providers"])
    else:
        invalid_providers = [
            item for item in providers if item not in APM_PROVIDERS
        ]
        if invalid_providers:
            errors.append(
                _field_error(
                    ["scope", "providers"],
                    (
                        "providers contains unsupported values: "
                        + ", ".join(invalid_providers)
                        + "; allowed providers are gemini, deepseek, "
                        "chatgpt, and claude"
                    ),
                )
            )
        normalized_providers = list(providers)

    if (
        not isinstance(max_products, int)
        or isinstance(max_products, bool)
        or max_products < 1
        or max_products > 10
    ):
        errors.append(
            _field_error(
                ["scope", "max_products_per_audit"],
                "max_products_per_audit must be between 1 and 10",
            )
        )
        normalized_max_products = DEFAULT_APM_SCOPE["max_products_per_audit"]
    else:
        normalized_max_products = max_products

    if errors:
        raise ApmConfigValidationError(errors)

    return {
        "scan_modes": normalized_scan_modes,
        "providers": normalized_providers,
        "max_products_per_audit": normalized_max_products,
    }


def _serialize_apm_row(row: Dict[str, Any]) -> Dict[str, Any]:
    scope = coerce_jsonb_to_dict(row.get("apm_scope_jsonb")) or DEFAULT_APM_SCOPE
    return {
        "merchant_id": row["merchant_id"],
        "enabled": bool(row.get("apm_enabled")),
        "cadence_days": row.get("apm_cadence_days"),
        "scope": {
            "scan_modes": list(
                scope.get("scan_modes", DEFAULT_APM_SCOPE["scan_modes"])
            ),
            "providers": list(
                scope.get("providers", DEFAULT_APM_SCOPE["providers"])
            ),
            "max_products_per_audit": scope.get(
                "max_products_per_audit",
                DEFAULT_APM_SCOPE["max_products_per_audit"],
            ),
        },
        "apm_configured_at": row.get("apm_configured_at"),
        "apm_last_run_at": row.get("apm_last_run_at"),
    }


async def get_apm_config(merchant_id: str) -> Optional[Dict[str, Any]]:
    """Return a merchant's configured APM settings, or None."""
    query = select(
        merchant_onboarding.c.merchant_id,
        merchant_onboarding.c.apm_enabled,
        merchant_onboarding.c.apm_cadence_days,
        merchant_onboarding.c.apm_scope_jsonb,
        merchant_onboarding.c.apm_configured_at,
        merchant_onboarding.c.apm_last_run_at,
    ).where(merchant_onboarding.c.merchant_id == merchant_id)
    row = await database.fetch_one(query)
    if not row:
        return None
    data = dict(row)
    if data.get("apm_configured_at") is None:
        return None
    return _serialize_apm_row(data)


async def upsert_apm_config(
    *,
    merchant_id: str,
    enabled: bool,
    cadence_days: int,
    scope: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Persist merchant APM settings and return the saved config.

    The merchant_onboarding row must already exist; public signup owns
    required fields such as business_name and contact_email.
    """
    normalized_scope = validate_apm_config(
        cadence_days=cadence_days,
        scope=scope,
    )

    existing = await database.fetch_one(
        select(merchant_onboarding.c.merchant_id).where(
            merchant_onboarding.c.merchant_id == merchant_id
        )
    )
    if not existing:
        return None

    await database.execute(
        merchant_onboarding.update()
        .where(merchant_onboarding.c.merchant_id == merchant_id)
        .values(
            apm_enabled=enabled,
            apm_cadence_days=cadence_days,
            apm_scope_jsonb=_json_safe(normalized_scope),
            apm_configured_at=func.now(),
            updated_at=func.now(),
        )
    )
    return await get_apm_config(merchant_id)


async def mark_apm_audit_run_completed(
    merchant_id: str,
    *,
    run_at: Optional[datetime] = None,
) -> None:
    """Record the time of a successful scheduled APM audit."""
    values: Dict[str, Any] = {
        "apm_last_run_at": run_at if run_at is not None else func.now(),
        "updated_at": func.now(),
    }
    await database.execute(
        merchant_onboarding.update()
        .where(merchant_onboarding.c.merchant_id == merchant_id)
        .values(**values)
    )
