"""Merchant-authorized source registration for Commerce Index v2.

This is deliberately metadata-only: connector secrets stay in their existing
secret stores.  The Commerce Index records consent, capabilities, freshness
policy, and activation state so each observed field can be governed by a
traceable source contract.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

from sqlalchemy import select

from sqlalchemy.dialects.postgresql import insert as pg_insert

from db.commerce_index import commerce_index_sources
from db.database import database
from services.commerce_source_registry import get_commerce_source, normalize_commerce_provider


_VALID_STATUSES = {"pending", "active", "disabled"}
_SENSITIVE_CONFIG_TOKENS = {"secret", "password", "token", "private_key", "api_key", "credential"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def commerce_index_source_id(merchant_id: str, provider: str, integration_layer: str) -> str:
    """Return a deterministic ID: reconnects update the same authority record."""
    raw = "|".join(
        (
            str(merchant_id or "").strip(),
            normalize_commerce_provider(provider),
            str(integration_layer or "").strip().lower(),
        )
    )
    return f"ci_source_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]}"


def _capabilities_payload(capabilities: Any) -> Dict[str, bool]:
    return {
        "catalog_pull": bool(capabilities.catalog_pull),
        "catalog_events": bool(capabilities.catalog_events),
        "reviews_pull": bool(capabilities.reviews_pull),
        "live_quote": bool(capabilities.live_quote),
        "checkout": bool(capabilities.checkout),
        "payment_webhooks": bool(capabilities.payment_webhooks),
    }


def _default_refresh_policy(definition: Any) -> Dict[str, Any]:
    capabilities = definition.capabilities
    if definition.integration_layer == "payment":
        return {
            "mode": "webhook_only",
            "catalog_refresh": "not_applicable",
            "checkout_validation": "existing_live_quote_before_order",
        }
    return {
        "mode": "events_plus_pull" if capabilities.catalog_events else "scheduled_pull",
        "event_sla_seconds": 300 if capabilities.catalog_events else None,
        "full_reconciliation_hours": 6 if capabilities.catalog_pull else None,
        "fields": {
            "price_inventory": "event_then_live_quote",
            "content_media": "event_then_reconcile",
            "reviews": "provider_schedule" if capabilities.reviews_pull else "separate_authorized_source",
        },
    }


def _validate_metadata(value: Any) -> Any:
    """Allow connector references, never credentials, in the fact-layer DB."""
    if isinstance(value, Mapping):
        result = dict(value)
        for key, nested in result.items():
            normalized = str(key).strip().lower()
            if normalized in _SENSITIVE_CONFIG_TOKENS or any(token in normalized for token in _SENSITIVE_CONFIG_TOKENS):
                raise ValueError("Commerce Index source metadata must not contain credentials")
            result[key] = _validate_metadata(nested)
        return result
    if isinstance(value, (list, tuple)):
        return [_validate_metadata(item) for item in value]
    return value


def source_kind_for_definition(definition: Any) -> str:
    """Map a registered connector contract to the field-authority taxonomy."""
    if definition.integration_layer == "catalog" and definition.source_kind == "storefront":
        return "merchant_api"
    if definition.integration_layer == "catalog" and definition.source_kind == "catalog_feed":
        return "contracted_feed"
    return "public_crawl"


async def resolve_active_catalog_source(*, merchant_id: str, provider: str) -> Optional[Dict[str, Any]]:
    """Resolve the consented source allowed to emit v2 catalog publications."""
    definition = get_commerce_source(provider)
    normalized_merchant_id = str(merchant_id or "").strip()
    if (
        definition is None
        or not normalized_merchant_id
        or definition.integration_layer != "catalog"
        or not definition.capabilities.catalog_pull
    ):
        return None
    row = await database.fetch_one(
        select(commerce_index_sources)
        .where(commerce_index_sources.c.merchant_id == normalized_merchant_id)
        .where(commerce_index_sources.c.provider == definition.provider)
        .where(commerce_index_sources.c.integration_layer == definition.integration_layer)
        .where(commerce_index_sources.c.status == "active")
        .where(commerce_index_sources.c.consent_ref.isnot(None))
        .limit(1)
    )
    if not row:
        return None
    source = dict(row)
    source["field_source_kind"] = source_kind_for_definition(definition)
    return source


async def register_commerce_index_source(
    *,
    merchant_id: str,
    provider: str,
    status: str = "pending",
    consent_ref: Optional[str] = None,
    source_metadata: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Create or update the authority record for one merchant/provider layer.

    ``active`` means that the merchant authorized this integration.  It does
    not manufacture a capability: Antom UCP remains payment-only, while the
    separately modelled Antom catalogue source cannot be activated until its
    contracted feed adapter is supplied.
    """
    normalized_merchant_id = str(merchant_id or "").strip()
    if not normalized_merchant_id:
        raise ValueError("merchant_id is required")
    definition = get_commerce_source(provider)
    if definition is None:
        raise ValueError(f"Unsupported Commerce Index source: {normalize_commerce_provider(provider) or 'unknown'}")
    normalized_status = str(status or "pending").strip().lower()
    if normalized_status not in _VALID_STATUSES:
        raise ValueError("status must be pending, active, or disabled")
    normalized_consent_ref = str(consent_ref or "").strip() or None
    if normalized_status == "active" and not normalized_consent_ref:
        raise ValueError("An active Commerce Index source requires a merchant consent_ref")
    if normalized_status == "active" and definition.provider == "antom_catalog":
        raise ValueError(
            "Antom Catalog cannot be activated until its merchant-authorized contracted feed adapter is configured"
        )

    now = _utcnow()
    source_id = commerce_index_source_id(
        normalized_merchant_id, definition.provider, definition.integration_layer
    )
    values = {
        "source_id": source_id,
        "merchant_id": normalized_merchant_id,
        "provider": definition.provider,
        "integration_layer": definition.integration_layer,
        "source_kind": definition.source_kind,
        "status": normalized_status,
        "consent_ref": normalized_consent_ref,
        "capabilities_json": _capabilities_payload(definition.capabilities),
        "refresh_policy_json": _default_refresh_policy(definition),
        "source_config_json": _validate_metadata(dict(source_metadata or {})),
        "updated_at": now,
    }
    insert_stmt = pg_insert(commerce_index_sources).values(created_at=now, **values)
    await database.execute(
        insert_stmt.on_conflict_do_update(
            index_elements=["source_id"],
            set_={key: value for key, value in values.items() if key != "source_id"},
        )
    )
    return values
