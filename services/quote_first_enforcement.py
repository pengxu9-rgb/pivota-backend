from __future__ import annotations

import os
from typing import Any, Dict, Tuple

from config.feature_flags import ENABLE_QUOTE_FIRST_ORDER_CREATE, ENABLE_QUOTE_FIRST_TIERED_ENFORCEMENT
from services.pcs_tier_service import get_merchant_pcs_tier, tier_at_least


def _parse_csv_env(name: str) -> list[str]:
    raw = os.getenv(name, "") or ""
    parts = []
    for item in raw.split(","):
        v = item.strip()
        if v:
            parts.append(v)
    return parts


async def should_require_quote_for_order_create(*, merchant_id: str) -> Tuple[bool, Dict[str, Any]]:
    """
    Decide if quote_id must be present for order creation.

    Precedence:
    1) FF_ENABLE_QUOTE_FIRST_ORDER_CREATE=true => require for all (existing semantics).
    2) FF_ENABLE_QUOTE_FIRST_TIERED_ENFORCEMENT=true:
       - require if merchant is allowlisted (FF_QUOTE_FIRST_REQUIRED_MERCHANT_IDS), OR
       - require if merchant PCS tier >= FF_QUOTE_FIRST_MIN_TIER (default: L1C)

    Defaults to allow (backward-compatible).
    """
    if ENABLE_QUOTE_FIRST_ORDER_CREATE:
        return True, {"mode": "global", "merchant_id": merchant_id}

    if not ENABLE_QUOTE_FIRST_TIERED_ENFORCEMENT:
        return False, {"mode": "off", "merchant_id": merchant_id}

    allowlist = set(_parse_csv_env("FF_QUOTE_FIRST_REQUIRED_MERCHANT_IDS"))
    if merchant_id in allowlist:
        return True, {"mode": "allowlist", "merchant_id": merchant_id}

    min_tier = (os.getenv("FF_QUOTE_FIRST_MIN_TIER", "L1C") or "L1C").upper()
    tier = await get_merchant_pcs_tier(merchant_id=merchant_id)

    require = tier_at_least(tier, min_tier)
    return require, {"mode": "tiered", "merchant_id": merchant_id, "tier": tier, "min_tier": min_tier}

