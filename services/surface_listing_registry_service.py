from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import select

from db.database import database
from db.surface_listing_registry import (
    surface_listing_errors,
    surface_listing_receipts,
    surface_listing_states,
)
from readiness.models import ChannelReadinessReport, MerchantReadinessSnapshot
from services.commerce_interaction_service import record_commerce_event_best_effort
from services.commerce_ledger_provenance import ledger_provenance
from services.canonical_commerce_service import (
    make_canonical_product_id,
    make_canonical_variant_id,
)

_STATE_RANK = {
    "pending": 0,
    "error": 1,
    "blocked": 2,
    "exported": 3,
    "indexed": 4,
    "tradeable": 5,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _listing_key(surface: str, merchant_id: str, product_id: str, variant_id: str) -> str:
    return f"{surface}:{merchant_id}:{product_id}:{variant_id}"


def _listing_id(surface: str, merchant_id: str, product_id: str, variant_id: str) -> str:
    return f"sls_{uuid.uuid5(uuid.NAMESPACE_URL, _listing_key(surface, merchant_id, product_id, variant_id)).hex[:24]}"


def _receipt_id(listing_id: str, status: str) -> str:
    return f"slr_{uuid.uuid5(uuid.NAMESPACE_URL, f'{listing_id}:{status}:{uuid.uuid4().hex}').hex[:24]}"


def _error_id(listing_id: str, code: str) -> str:
    return f"sle_{uuid.uuid5(uuid.NAMESPACE_URL, f'{listing_id}:{code}:{uuid.uuid4().hex}').hex[:24]}"


def _best_status(current: Optional[str], candidate: str) -> str:
    current_rank = _STATE_RANK.get(str(current or "").lower(), -1)
    candidate_rank = _STATE_RANK.get(candidate, -1)
    return current if current_rank > candidate_rank else candidate


def _dedupe_codes(*groups: Any) -> list[str]:
    seen: set[str] = set()
    codes: list[str] = []
    for group in groups:
        for item in group or []:
            code = str(item or "").strip()
            if not code or code in seen:
                continue
            seen.add(code)
            codes.append(code)
    return codes


def _channel_blockers(variant: Any, channel: str) -> list[str]:
    return _dedupe_codes(
        getattr(variant, "blockers", {}).get(channel),
        getattr(getattr(variant, "discovery", None), "blockers", None),
        getattr(getattr(variant, "checkout", None), "blockers", None),
    )


def _channel_warnings(variant: Any, channel: str) -> list[str]:
    return _dedupe_codes(
        getattr(variant, "warnings", {}).get(channel),
        getattr(getattr(variant, "discovery", None), "warnings", None),
        getattr(getattr(variant, "checkout", None), "warnings", None),
    )


async def _upsert_listing_state(
    *,
    listing_id: str,
    interaction_id: str,
    merchant_id: str,
    surface: str,
    listing_key: str,
    canonical_product_id: str,
    canonical_variant_id: str,
    status: str,
    metadata: Dict[str, Any],
    latest_receipt_id: Optional[str] = None,
    latest_error_id: Optional[str] = None,
) -> None:
    now = _now()
    existing = await database.fetch_one(
        select(surface_listing_states).where(surface_listing_states.c.listing_id == listing_id)
    )
    if existing:
        row = dict(existing)
        final_status = _best_status(row.get("status"), status)
        values = {
            "merchant_id": merchant_id,
            "interaction_id": interaction_id,
            "surface": surface,
            "listing_key": listing_key,
            "canonical_product_id": canonical_product_id,
            "canonical_variant_id": canonical_variant_id,
            "status": final_status,
            "last_exported_at": now if status == "exported" else row.get("last_exported_at"),
            "last_indexed_at": row.get("last_indexed_at"),
            "last_tradeable_at": row.get("last_tradeable_at"),
            "latest_receipt_id": latest_receipt_id or row.get("latest_receipt_id"),
            "latest_error_id": latest_error_id or row.get("latest_error_id"),
            "metadata": metadata,
            "updated_at": now,
        }
        await database.execute(
            surface_listing_states.update()
            .where(surface_listing_states.c.listing_id == listing_id)
            .values(**values)
        )
        return

    values = {
        "listing_id": listing_id,
        "merchant_id": merchant_id,
        "interaction_id": interaction_id,
        "surface": surface,
        "listing_key": listing_key,
        "canonical_product_id": canonical_product_id,
        "canonical_variant_id": canonical_variant_id,
        "status": status,
        "last_exported_at": now if status == "exported" else None,
        "last_indexed_at": None,
        "last_tradeable_at": None,
        "latest_receipt_id": latest_receipt_id,
        "latest_error_id": latest_error_id,
        "metadata": metadata,
        "created_at": now,
        "updated_at": now,
    }
    await database.execute(surface_listing_states.insert().values(**values))


async def persist_channel_export(
    snapshot: MerchantReadinessSnapshot,
    report: ChannelReadinessReport,
) -> Dict[str, int]:
    ready_offer_map = {
        f"{offer.get('product_id')}::{offer.get('variant_id')}": offer
        for offer in (report.offers or [])
    }
    exported = 0
    blocked = 0
    errors = 0

    for product in snapshot.products:
        platform = str(product.platform or "shopify")
        canonical_product_id = make_canonical_product_id(
            snapshot.merchant_id,
            platform,
            product.product_id,
        )
        for variant in product.variants:
            listing_key = _listing_key(report.channel, snapshot.merchant_id, product.product_id, variant.variant_id)
            listing_id = _listing_id(report.channel, snapshot.merchant_id, product.product_id, variant.variant_id)
            canonical_variant_id = make_canonical_variant_id(
                snapshot.merchant_id,
                platform,
                product.product_id,
                variant.variant_id,
            )
            ready = variant.channel_coverage.get(report.channel) == "ready"
            offer = ready_offer_map.get(f"{product.product_id}::{variant.variant_id}")
            blocker_codes = _channel_blockers(variant, report.channel)
            warning_codes = _channel_warnings(variant, report.channel)
            metadata = {
                "channel": report.channel,
                "product_id": product.product_id,
                "variant_id": variant.variant_id,
                "platform": platform,
                "availability": (offer or {}).get("availability") or variant.inventory.get("availability"),
                "readiness_status": variant.channel_coverage.get(report.channel),
                "readiness_blockers": blocker_codes,
                "readiness_warnings": warning_codes,
                "offer_id": (offer or {}).get("offer_id"),
                "export_version": report.export_version,
                "generated_at": report.generated_at,
            }

            if ready:
                interaction_event = await record_commerce_event_best_effort(
                    event_type="listing.exported",
                    metadata={
                        "merchant_id": snapshot.merchant_id,
                        "platform": platform,
                        "surface": report.channel,
                        "canonical_product_id": canonical_product_id,
                        "canonical_variant_id": canonical_variant_id,
                        "listing_id": listing_id,
                        "listing_key": listing_key,
                        "status": "exported",
                    },
                    source="surface_listing_registry",
                    upstream_idempotency_key=f"listing:{listing_id}:exported:{report.generated_at}",
                    **ledger_provenance("surface_listing_registry", "unknown"),
                )
                receipt_id = _receipt_id(listing_id, "exported")
                await database.execute(
                    surface_listing_receipts.insert().values(
                        receipt_id=receipt_id,
                        listing_id=listing_id,
                        merchant_id=snapshot.merchant_id,
                        surface=report.channel,
                        listing_key=listing_key,
                        status="exported",
                        payload=offer or metadata,
                    )
                )
                await _upsert_listing_state(
                    listing_id=listing_id,
                    interaction_id=interaction_event["interaction_id"],
                    merchant_id=snapshot.merchant_id,
                    surface=report.channel,
                    listing_key=listing_key,
                    canonical_product_id=canonical_product_id,
                    canonical_variant_id=canonical_variant_id,
                    status="exported",
                    metadata=metadata,
                    latest_receipt_id=receipt_id,
                )
                exported += 1
                continue

            error_code = blocker_codes[0] if blocker_codes else "variant_not_ready"
            error_message = ", ".join(blocker_codes or warning_codes or [error_code])[:1024]
            error_id = _error_id(listing_id, error_code)
            interaction_event = await record_commerce_event_best_effort(
                event_type="listing.blocked" if blocker_codes else "listing.error",
                metadata={
                    "merchant_id": snapshot.merchant_id,
                    "platform": platform,
                    "surface": report.channel,
                    "canonical_product_id": canonical_product_id,
                    "canonical_variant_id": canonical_variant_id,
                    "listing_id": listing_id,
                    "listing_key": listing_key,
                    "status": "blocked" if blocker_codes else "error",
                    "diagnostic_code": "LISTING_ERROR",
                    "error_code": error_code,
                    "error_message": error_message,
                },
                source="surface_listing_registry",
                upstream_idempotency_key=f"listing:{listing_id}:{error_code}:{report.generated_at}",
                **ledger_provenance("surface_listing_registry", "unknown"),
            )
            await database.execute(
                surface_listing_errors.insert().values(
                    error_id=error_id,
                    listing_id=listing_id,
                    merchant_id=snapshot.merchant_id,
                    surface=report.channel,
                    listing_key=listing_key,
                    error_code=error_code,
                    error_message=error_message,
                    payload=metadata,
                )
            )
            await _upsert_listing_state(
                listing_id=listing_id,
                interaction_id=interaction_event["interaction_id"],
                merchant_id=snapshot.merchant_id,
                surface=report.channel,
                listing_key=listing_key,
                canonical_product_id=canonical_product_id,
                canonical_variant_id=canonical_variant_id,
                status="blocked" if blocker_codes else "error",
                metadata=metadata,
                latest_error_id=error_id,
            )
            if blocker_codes:
                blocked += 1
            else:
                errors += 1

    return {
        "exported": exported,
        "blocked": blocked,
        "errors": errors,
    }
