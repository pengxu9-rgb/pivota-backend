from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from db.database import database


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _norm_opt(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _norm_upper(v: Optional[str]) -> Optional[str]:
    s = _norm_opt(v)
    return s.upper() if s else None


async def ensure_settlement_rules_table() -> None:
    # Best-effort self-healing for environments where migrations cannot be run manually.
    await database.execute(
        """
        CREATE TABLE IF NOT EXISTS settlement_rules (
          id TEXT PRIMARY KEY,
          status TEXT NOT NULL DEFAULT 'active', -- active|disabled
          merchant_id TEXT NULL,
          platform TEXT NULL,
          market TEXT NULL,
          psp TEXT NULL,
          charge_currency TEXT NULL,
          settlement_currency TEXT NOT NULL,
          psp_fee_bps INTEGER NOT NULL DEFAULT 0,
          psp_fee_fixed NUMERIC(18,6) NOT NULL DEFAULT 0,
          platform_fee_bps INTEGER NOT NULL DEFAULT 0,
          platform_fee_fixed NUMERIC(18,6) NOT NULL DEFAULT 0,
          fx_rate NUMERIC(18,10) NULL,
          fx_rate_as_of TIMESTAMPTZ NULL,
          fx_spread_bps INTEGER NOT NULL DEFAULT 0,
          notes TEXT NULL,
          created_by TEXT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    await database.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_settlement_rules_lookup
          ON settlement_rules(merchant_id, platform, market, psp, charge_currency, status, updated_at)
        """
    )


def _match_score(rule: Dict[str, Any], ctx: Dict[str, Optional[str]]) -> int:
    # Exact-match scoring with null as wildcard. Higher = better.
    # Merchant match dominates; then platform/market/psp/currency.
    score = 0

    def match(field: str, weight: int) -> bool:
        nonlocal score
        rv = _norm_opt(rule.get(field))
        cv = _norm_opt(ctx.get(field))
        if rv is None:
            return True
        if cv is None:
            return False
        if rv.lower() != cv.lower():
            return False
        score += weight
        return True

    if not match("merchant_id", 100):
        return -1
    if not match("platform", 20):
        return -1
    if not match("market", 10):
        return -1
    if not match("psp", 10):
        return -1
    if not match("charge_currency", 5):
        return -1

    status = str(rule.get("status") or "active").lower()
    if status != "active":
        return -1

    return score


async def select_best_settlement_rule(
    *,
    merchant_id: str,
    platform: Optional[str] = None,
    market: Optional[str] = None,
    psp: Optional[str] = None,
    charge_currency: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    await ensure_settlement_rules_table()
    rows = await database.fetch_all(
        """
        SELECT *
        FROM settlement_rules
        WHERE status = 'active'
          AND (merchant_id IS NULL OR merchant_id = :merchant_id)
        ORDER BY updated_at DESC
        LIMIT 200
        """,
        {"merchant_id": merchant_id},
    )
    ctx = {
        "merchant_id": _norm_opt(merchant_id),
        "platform": _norm_opt(platform),
        "market": _norm_opt(market),
        "psp": _norm_opt(psp),
        "charge_currency": _norm_upper(charge_currency),
    }
    best = None
    best_score = -1
    for r in rows or []:
        d = dict(r)
        score = _match_score(d, ctx)
        if score > best_score:
            best = d
            best_score = score
    return best

