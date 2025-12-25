from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from db.database import database


PCS_TIER_ORDER = {
    "L0": 0,   # not verified / unknown
    "L1": 10,  # verified onboarding (scopes ok)
    "L1C": 20, # L1 + observed live webhook delivery
    "L2": 30,  # L1C + Shopify Payments present (disputes possible)
}


def _decode_jsonb(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {"raw": parsed}
        except Exception:
            return {"raw": value}
    return {"raw": str(value)}


def tier_at_least(tier: Optional[str], minimum: Optional[str]) -> bool:
    t = (tier or "").upper()
    m = (minimum or "").upper()
    if m not in PCS_TIER_ORDER:
        return False
    return PCS_TIER_ORDER.get(t, 0) >= PCS_TIER_ORDER[m]


async def get_merchant_pcs_tier(
    *,
    merchant_id: str,
    observed_window_days: int = 7,
) -> str:
    """
    Minimal PCS tier heuristic for v0.2 rollouts.

    This is intentionally conservative and based only on verified onboarding data + observed webhook delivery:
    - L0: no capability snapshot OR missing required scopes
    - L1: capability snapshot exists AND missing_required_scopes is empty
    - L1C: L1 AND at least one signature-verified Shopify webhook event observed recently
    - L2: L1C AND has_shopify_payments (Shopify Payments + disputes are possible)

    Notes:
    - This is NOT the full metrics/tier engine; it is a pragmatic rollout guard.
    - It is safe-by-default: unknown => L0.
    """
    row = await database.fetch_one(
        """
        SELECT scopes_json, has_shopify_payments, last_checked_at
        FROM pcs_merchant_capabilities
        WHERE merchant_id = :merchant_id
        """,
        {"merchant_id": merchant_id},
    )
    if not row:
        return "L0"

    scopes_json = _decode_jsonb(dict(row).get("scopes_json"))
    missing_required = scopes_json.get("missing_required_scopes") or []
    if missing_required:
        return "L0"

    tier = "L1"

    # L1C: observed webhook delivery (signature verified)
    since = datetime.now(timezone.utc) - timedelta(days=max(1, int(observed_window_days)))
    observed = await database.fetch_one(
        """
        SELECT COUNT(1) AS cnt
        FROM pcs_shopify_webhook_events
        WHERE merchant_id = :merchant_id
          AND signature_verified = true
          AND received_at >= :since
        """,
        {"merchant_id": merchant_id, "since": since},
    )
    cnt = int(dict(observed or {}).get("cnt") or 0)
    if cnt > 0:
        tier = "L1C"

    # L2: Shopify Payments capability
    if tier == "L1C" and bool(dict(row).get("has_shopify_payments")):
        tier = "L2"

    return tier

