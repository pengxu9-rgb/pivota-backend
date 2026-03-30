from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from db.commerce_attribution import commerce_attribution_edges, surface_click_events
from db.database import database
from db.merchant_commerce_readiness import merchant_commerce_readiness_state
from db.merchant_onboarding import get_merchant_onboarding
from services.merchant_catalog_listing_fallback_service import fetch_listing_rows_with_catalog_fallback
from services.merchant_psp_config_service import evaluate_psp_readiness
from services.merchant_store_service import get_primary_store

READY = "ready"
BLOCKED = "blocked"


def _normalize_timestamp(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


async def _fetch_active_psps(merchant_id: str) -> List[Dict[str, Any]]:
    rows = await database.fetch_all(
        """
        SELECT provider, status, api_key, account_id, provider_config, environment, validation_status, validation_error
        FROM merchant_psps
        WHERE merchant_id = :merchant_id
          AND status = 'active'
        ORDER BY connected_at DESC NULLS LAST, psp_id ASC
        """,
        {"merchant_id": merchant_id},
    )
    return [dict(row) for row in rows or []]


async def _fetch_listing_rows(merchant_id: str) -> List[Dict[str, Any]]:
    return await fetch_listing_rows_with_catalog_fallback(merchant_id)


async def _fetch_click_rows(merchant_id: str) -> List[Dict[str, Any]]:
    rows = await database.fetch_all(
        select(surface_click_events).where(surface_click_events.c.merchant_id == merchant_id)
    )
    return [dict(row) for row in rows]


async def _fetch_edge_rows(merchant_id: str) -> List[Dict[str, Any]]:
    rows = await database.fetch_all(
        select(commerce_attribution_edges).where(commerce_attribution_edges.c.merchant_id == merchant_id)
    )
    return [dict(row) for row in rows]


def _status(blockers: List[str]) -> str:
    return READY if not blockers else BLOCKED


def _days_between(start: Optional[datetime], end: Optional[datetime]) -> Optional[int]:
    if not start or not end:
        return None
    delta = end.astimezone(timezone.utc).date() - start.astimezone(timezone.utc).date()
    return max(delta.days, 0)


def _surfaced_exposure_supported(platform: Optional[str]) -> bool:
    return str(platform or "").strip().lower() in {"shopify", "wix"}


async def compute_merchant_commerce_readiness_state(merchant_id: str) -> Dict[str, Any]:
    merchant = await get_merchant_onboarding(merchant_id)
    store = await get_primary_store(merchant_id)
    platform = str((store or {}).get("platform") or (merchant or {}).get("mcp_platform") or "").strip().lower() or None
    store_connected_at = _normalize_timestamp((store or {}).get("connected_at") or (merchant or {}).get("mcp_connected_at"))
    listing_rows = await _fetch_listing_rows(merchant_id)
    click_rows = await _fetch_click_rows(merchant_id)
    edge_rows = await _fetch_edge_rows(merchant_id)
    psp_rows = await _fetch_active_psps(merchant_id)

    supported_checkout_platform = platform in {"shopify", "wix"}
    indexed_statuses = {"exported", "indexed", "tradeable"}
    indexed_rows = [row for row in listing_rows if str(row.get("status") or "").strip().lower() in indexed_statuses]
    last_catalog_sync = max(
        [_normalize_timestamp(row.get("updated_at")) for row in listing_rows if _normalize_timestamp(row.get("updated_at")) is not None],
        default=None,
    )
    freshness_stale = False
    if last_catalog_sync:
        freshness_stale = (datetime.now(timezone.utc) - last_catalog_sync.astimezone(timezone.utc)).total_seconds() > 7 * 24 * 3600

    psp_readiness = [
        evaluate_psp_readiness(
            row.get("provider") or "",
            status=row.get("status"),
            api_key=row.get("api_key"),
            account_id=row.get("account_id"),
            provider_config=row.get("provider_config"),
            environment=row.get("environment"),
            validation_status=row.get("validation_status"),
            validation_error=row.get("validation_error"),
        )
        for row in psp_rows
    ]
    live_psp = next((row for row in psp_readiness if row.get("live_charge_ready")), None)
    surfaced_supported = _surfaced_exposure_supported(platform)
    surfaced_exposure = len(
        {str(row.get("click_id") or "").strip() for row in click_rows if int(row.get("impression_count") or 0) > 0 and str(row.get("click_id") or "").strip()}
    )
    clicked_exposure = len(
        {str(row.get("click_id") or "").strip() for row in click_rows if int(row.get("click_count") or 0) > 0 and str(row.get("click_id") or "").strip()}
    )
    unattributed_orders = sum(1 for row in edge_rows if not str(row.get("click_id") or "").strip())
    listing_status_breakdown = dict(Counter(str(row.get("status") or "unknown") for row in listing_rows))

    foundation_blockers: List[str] = []
    if not store:
        foundation_blockers.append("missing_store_connection")
    if not platform:
        foundation_blockers.append("missing_platform")
    if not supported_checkout_platform:
        foundation_blockers.append("unsupported_platform")
    if not indexed_rows:
        foundation_blockers.append("missing_canonical_listing_rows")

    discover_blockers: List[str] = []
    if not store:
        discover_blockers.append("missing_store_connection")
    if not indexed_rows:
        discover_blockers.append("no_indexed_listing_rows")
    if freshness_stale:
        discover_blockers.append("catalog_freshness_stale")

    signals_blockers: List[str] = []
    if not surfaced_supported:
        signals_blockers.append("surfaced_exposure_not_supported")
    if indexed_rows and surfaced_supported and surfaced_exposure == 0:
        signals_blockers.append("missing_surface_impressions")
    if unattributed_orders > 0:
        signals_blockers.append("unattributed_orders_present")

    execute_blockers: List[str] = []
    if not store:
        execute_blockers.append("missing_store_connection")
    if not supported_checkout_platform:
        execute_blockers.append("unsupported_platform")
    if not (live_psp or supported_checkout_platform):
        execute_blockers.append("missing_live_psp_or_checkout_path")

    first_discover_ready_at = store_connected_at if not discover_blockers else None
    values = {
        "merchant_id": merchant_id,
        "primary_platform": platform,
        "active_psp": (live_psp or {}).get("provider"),
        "foundation_status": _status(foundation_blockers),
        "discover_status": _status(discover_blockers),
        "signals_status": _status(signals_blockers),
        "execute_status": _status(execute_blockers),
        "foundation_blockers": foundation_blockers,
        "discover_blockers": discover_blockers,
        "signals_blockers": signals_blockers,
        "execute_blockers": execute_blockers,
        "surfaced_exposure_supported": surfaced_supported,
        "first_store_connected_at": store_connected_at,
        "first_catalog_synced_at": last_catalog_sync,
        "first_discover_ready_at": first_discover_ready_at,
        "days_to_discover_ready": _days_between(store_connected_at, first_discover_ready_at),
        "observed_at": datetime.now(timezone.utc),
        "metadata": {
            "listing_rows_total": len(listing_rows),
            "listing_status_breakdown": listing_status_breakdown,
            "indexed_exposure": len({row.get("canonical_variant_id") for row in indexed_rows if row.get("canonical_variant_id")}),
            "surfaced_exposure": surfaced_exposure,
            "clicked_exposure": clicked_exposure,
            "ordered_conversion": len({row.get("order_id") for row in edge_rows if row.get("order_id")}),
            "live_psp_candidates": psp_readiness,
        },
    }
    return values


async def upsert_merchant_commerce_readiness_state(merchant_id: str) -> Dict[str, Any]:
    values = await compute_merchant_commerce_readiness_state(merchant_id)
    existing = await database.fetch_one(
        select(merchant_commerce_readiness_state).where(merchant_commerce_readiness_state.c.merchant_id == merchant_id)
    )
    if existing:
        await database.execute(
            merchant_commerce_readiness_state.update()
            .where(merchant_commerce_readiness_state.c.merchant_id == merchant_id)
            .values(**values)
        )
    else:
        await database.execute(merchant_commerce_readiness_state.insert().values(**values))
    row = await database.fetch_one(
        select(merchant_commerce_readiness_state).where(merchant_commerce_readiness_state.c.merchant_id == merchant_id)
    )
    return dict(row) if row else values
