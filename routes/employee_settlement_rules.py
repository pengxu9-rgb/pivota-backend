from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from db.database import database
from utils.auth import get_current_employee


employee_router = APIRouter(prefix="/employee/settlement", tags=["employee-settlement"])


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


def _d(v: Any) -> Decimal:
    try:
        return Decimal(str(v or "0"))
    except Exception:
        return Decimal("0")


def _money(v: Any) -> Decimal:
    return _d(v).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


async def _ensure_tables() -> None:
    # Phase 2 (MVP): settlement rules and manual FX rates can be configured by employees.
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


class SettlementRule(BaseModel):
    id: str
    status: str
    merchant_id: Optional[str] = None
    platform: Optional[str] = None
    market: Optional[str] = None
    psp: Optional[str] = None
    charge_currency: Optional[str] = None
    settlement_currency: str
    psp_fee_bps: int = 0
    psp_fee_fixed: Decimal = Decimal("0")
    platform_fee_bps: int = 0
    platform_fee_fixed: Decimal = Decimal("0")
    fx_rate: Optional[Decimal] = None
    fx_rate_as_of: Optional[datetime] = None
    fx_spread_bps: int = 0
    notes: Optional[str] = None
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class UpsertSettlementRuleRequest(BaseModel):
    status: str = Field(default="active", description="active|disabled")
    merchant_id: Optional[str] = None
    platform: Optional[str] = None
    market: Optional[str] = None
    psp: Optional[str] = None
    charge_currency: Optional[str] = None
    settlement_currency: str = Field(..., min_length=3, max_length=8)
    psp_fee_bps: int = Field(default=0, ge=0, le=100_000)
    psp_fee_fixed: Decimal = Field(default=Decimal("0"))
    platform_fee_bps: int = Field(default=0, ge=0, le=100_000)
    platform_fee_fixed: Decimal = Field(default=Decimal("0"))
    fx_rate: Optional[Decimal] = None
    fx_rate_as_of: Optional[datetime] = None
    fx_spread_bps: int = Field(default=0, ge=0, le=100_000)
    notes: Optional[str] = None


class EstimateRequest(BaseModel):
    merchant_id: str
    charge_currency: str
    platform: Optional[str] = None
    market: Optional[str] = None
    psp: Optional[str] = None
    # Simple mode (backward compatible): provide a precomputed gross amount.
    amount: Optional[Decimal] = Field(default=None, description="Charge gross amount in charge_currency")
    # Quote mode: provide pricing breakdown and select which parts are included in the fee/settlement basis.
    pricing: Optional[Dict[str, Any]] = None
    include_tax: bool = True
    include_shipping: bool = True


class EstimateResponse(BaseModel):
    ok: bool
    rule: Optional[SettlementRule] = None
    charge: Dict[str, Any]
    settlement: Dict[str, Any]
    warnings: List[str] = Field(default_factory=list)


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


async def _select_best_rule(*, merchant_id: str, platform: Optional[str], market: Optional[str], psp: Optional[str], charge_currency: Optional[str]) -> Optional[Dict[str, Any]]:
    await _ensure_tables()
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
        "merchant_id": merchant_id,
        "platform": platform,
        "market": market,
        "psp": psp,
        "charge_currency": charge_currency,
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


def _row_to_rule(row: Dict[str, Any]) -> SettlementRule:
    return SettlementRule(
        id=str(row.get("id") or ""),
        status=str(row.get("status") or "active"),
        merchant_id=_norm_opt(row.get("merchant_id")),
        platform=_norm_opt(row.get("platform")),
        market=_norm_opt(row.get("market")),
        psp=_norm_opt(row.get("psp")),
        charge_currency=_norm_opt(row.get("charge_currency")),
        settlement_currency=str(row.get("settlement_currency") or ""),
        psp_fee_bps=int(row.get("psp_fee_bps") or 0),
        psp_fee_fixed=_d(row.get("psp_fee_fixed")),
        platform_fee_bps=int(row.get("platform_fee_bps") or 0),
        platform_fee_fixed=_d(row.get("platform_fee_fixed")),
        fx_rate=_d(row.get("fx_rate")) if row.get("fx_rate") is not None else None,
        fx_rate_as_of=row.get("fx_rate_as_of"),
        fx_spread_bps=int(row.get("fx_spread_bps") or 0),
        notes=_norm_opt(row.get("notes")),
        created_by=_norm_opt(row.get("created_by")),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


@employee_router.get("/rules")
async def list_rules(
    merchant_id: Optional[str] = Query(default=None),
    platform: Optional[str] = Query(default=None),
    market: Optional[str] = Query(default=None),
    psp: Optional[str] = Query(default=None),
    charge_currency: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    current_user: dict = Depends(get_current_employee),
):
    await _ensure_tables()
    where = []
    params: Dict[str, Any] = {"limit": limit}
    if merchant_id:
        where.append("merchant_id = :merchant_id")
        params["merchant_id"] = _norm_opt(merchant_id)
    if platform:
        where.append("platform = :platform")
        params["platform"] = _norm_opt(platform)
    if market:
        where.append("market = :market")
        params["market"] = _norm_opt(market)
    if psp:
        where.append("psp = :psp")
        params["psp"] = _norm_opt(psp)
    if charge_currency:
        where.append("charge_currency = :charge_currency")
        params["charge_currency"] = _norm_upper(charge_currency)
    if status:
        where.append("status = :status")
        params["status"] = _norm_opt(status)

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    rows = await database.fetch_all(
        f"""
        SELECT *
        FROM settlement_rules
        {where_sql}
        ORDER BY updated_at DESC
        LIMIT :limit
        """,
        params,
    )
    return {"rules": [_row_to_rule(dict(r)).model_dump(mode="json") for r in rows or []]}


@employee_router.post("/rules")
async def create_rule(
    body: UpsertSettlementRuleRequest,
    current_user: dict = Depends(get_current_employee),
):
    await _ensure_tables()
    rid = f"sr_{uuid.uuid4().hex}"
    created_by = str(current_user.get("email") or current_user.get("sub") or current_user.get("user_id") or "")
    now = _now()
    await database.execute(
        """
        INSERT INTO settlement_rules (
          id, status, merchant_id, platform, market, psp, charge_currency, settlement_currency,
          psp_fee_bps, psp_fee_fixed, platform_fee_bps, platform_fee_fixed,
          fx_rate, fx_rate_as_of, fx_spread_bps, notes, created_by, created_at, updated_at
        )
        VALUES (
          :id, :status, :merchant_id, :platform, :market, :psp, :charge_currency, :settlement_currency,
          :psp_fee_bps, :psp_fee_fixed, :platform_fee_bps, :platform_fee_fixed,
          :fx_rate, :fx_rate_as_of, :fx_spread_bps, :notes, :created_by, :created_at, :updated_at
        )
        """,
        {
            "id": rid,
            "status": str(body.status or "active").lower(),
            "merchant_id": _norm_opt(body.merchant_id),
            "platform": _norm_opt(body.platform),
            "market": _norm_opt(body.market),
            "psp": _norm_opt(body.psp),
            "charge_currency": _norm_upper(body.charge_currency),
            "settlement_currency": _norm_upper(body.settlement_currency) or "USD",
            "psp_fee_bps": int(body.psp_fee_bps or 0),
            "psp_fee_fixed": str(_d(body.psp_fee_fixed)),
            "platform_fee_bps": int(body.platform_fee_bps or 0),
            "platform_fee_fixed": str(_d(body.platform_fee_fixed)),
            "fx_rate": str(_d(body.fx_rate)) if body.fx_rate is not None else None,
            "fx_rate_as_of": body.fx_rate_as_of,
            "fx_spread_bps": int(body.fx_spread_bps or 0),
            "notes": _norm_opt(body.notes),
            "created_by": created_by or None,
            "created_at": now,
            "updated_at": now,
        },
    )
    row = await database.fetch_one("SELECT * FROM settlement_rules WHERE id = :id", {"id": rid})
    return {"rule": _row_to_rule(dict(row)).model_dump(mode="json") if row else None}


@employee_router.put("/rules/{rule_id}")
async def update_rule(
    rule_id: str,
    body: UpsertSettlementRuleRequest,
    current_user: dict = Depends(get_current_employee),
):
    await _ensure_tables()
    existing = await database.fetch_one("SELECT * FROM settlement_rules WHERE id = :id", {"id": rule_id})
    if not existing:
        raise HTTPException(status_code=404, detail="Rule not found")
    now = _now()
    await database.execute(
        """
        UPDATE settlement_rules
        SET
          status = :status,
          merchant_id = :merchant_id,
          platform = :platform,
          market = :market,
          psp = :psp,
          charge_currency = :charge_currency,
          settlement_currency = :settlement_currency,
          psp_fee_bps = :psp_fee_bps,
          psp_fee_fixed = :psp_fee_fixed,
          platform_fee_bps = :platform_fee_bps,
          platform_fee_fixed = :platform_fee_fixed,
          fx_rate = :fx_rate,
          fx_rate_as_of = :fx_rate_as_of,
          fx_spread_bps = :fx_spread_bps,
          notes = :notes,
          updated_at = :updated_at
        WHERE id = :id
        """,
        {
            "id": rule_id,
            "status": str(body.status or "active").lower(),
            "merchant_id": _norm_opt(body.merchant_id),
            "platform": _norm_opt(body.platform),
            "market": _norm_opt(body.market),
            "psp": _norm_opt(body.psp),
            "charge_currency": _norm_upper(body.charge_currency),
            "settlement_currency": _norm_upper(body.settlement_currency) or "USD",
            "psp_fee_bps": int(body.psp_fee_bps or 0),
            "psp_fee_fixed": str(_d(body.psp_fee_fixed)),
            "platform_fee_bps": int(body.platform_fee_bps or 0),
            "platform_fee_fixed": str(_d(body.platform_fee_fixed)),
            "fx_rate": str(_d(body.fx_rate)) if body.fx_rate is not None else None,
            "fx_rate_as_of": body.fx_rate_as_of,
            "fx_spread_bps": int(body.fx_spread_bps or 0),
            "notes": _norm_opt(body.notes),
            "updated_at": now,
        },
    )
    row = await database.fetch_one("SELECT * FROM settlement_rules WHERE id = :id", {"id": rule_id})
    return {"rule": _row_to_rule(dict(row)).model_dump(mode="json") if row else None}


@employee_router.delete("/rules/{rule_id}")
async def delete_rule(
    rule_id: str,
    current_user: dict = Depends(get_current_employee),
):
    await _ensure_tables()
    await database.execute("DELETE FROM settlement_rules WHERE id = :id", {"id": rule_id})
    return {"status": "success"}


@employee_router.post("/estimate", response_model=EstimateResponse)
async def estimate_settlement(
    body: EstimateRequest,
    current_user: dict = Depends(get_current_employee),
):
    await _ensure_tables()
    merchant_id = _norm_opt(body.merchant_id) or ""
    if not merchant_id:
        raise HTTPException(status_code=400, detail="merchant_id is required")
    charge_currency = _norm_upper(body.charge_currency) or "USD"
    rule_row = await _select_best_rule(
        merchant_id=merchant_id,
        platform=_norm_opt(body.platform),
        market=_norm_opt(body.market),
        psp=_norm_opt(body.psp),
        charge_currency=charge_currency,
    )

    warnings: List[str] = []
    if not rule_row:
        return EstimateResponse(
            ok=False,
            rule=None,
            charge={"currency": charge_currency, "gross": str(_money(body.amount))},
            settlement={},
            warnings=["NO_RULE_MATCH"],
        )

    rule = _row_to_rule(rule_row)
    pricing = body.pricing if isinstance(body.pricing, dict) else None
    gross_total = None
    basis_amount = None
    charge_breakdown = None

    if pricing:
        # Expect quote-like fields (all in charge_currency):
        # subtotal, discount_total, shipping_fee, tax, total
        subtotal = _money(pricing.get("subtotal"))
        discount_total = _money(pricing.get("discount_total"))
        shipping_fee = _money(pricing.get("shipping_fee"))
        tax = _money(pricing.get("tax"))
        total = _money(pricing.get("total"))

        # Items subtotal after discounts (charge basis before tax/shipping).
        items_net = _money(subtotal - discount_total)
        if items_net < 0:
            items_net = Decimal("0.00")
            warnings.append("ITEMS_NET_NEGATIVE_CLAMPED")

        basis_amount = items_net
        if body.include_shipping:
            basis_amount = _money(basis_amount + shipping_fee)
        if body.include_tax:
            basis_amount = _money(basis_amount + tax)

        # Default gross_total to quote total (still useful for display).
        gross_total = total

        # Best-effort consistency warning.
        expected_total = _money(items_net + shipping_fee + tax)
        if expected_total != total:
            warnings.append("QUOTE_TOTAL_MISMATCH")

        charge_breakdown = {
            "subtotal": str(subtotal),
            "discount_total": str(discount_total),
            "shipping_fee": str(shipping_fee),
            "tax": str(tax),
            "total": str(total),
        }

    # Fallback: use explicit amount as both gross_total and fee basis.
    if basis_amount is None:
        if body.amount is None:
            raise HTTPException(status_code=400, detail="Provide either amount or pricing")
        gross_total = _money(body.amount)
        basis_amount = gross_total

    gross_total = _money(gross_total)

    psp_fee = _money(basis_amount * Decimal(rule.psp_fee_bps) / Decimal(10_000) + _d(rule.psp_fee_fixed))
    platform_fee = _money(basis_amount * Decimal(rule.platform_fee_bps) / Decimal(10_000) + _d(rule.platform_fee_fixed))
    net_charge = _money(basis_amount - psp_fee - platform_fee)
    if net_charge < 0:
        net_charge = Decimal("0.00")
        warnings.append("NET_NEGATIVE_CLAMPED")

    settlement_currency = _norm_upper(rule.settlement_currency) or charge_currency
    settlement_gross = net_charge
    fx_info: Dict[str, Any] = {}

    if settlement_currency != charge_currency:
        if rule.fx_rate is None or Decimal(str(rule.fx_rate or "0")) <= 0:
            warnings.append("FX_RATE_MISSING")
            return EstimateResponse(
                ok=False,
                rule=rule,
                charge={"currency": charge_currency, "gross": str(gross), "net_after_fees": str(net_charge)},
                settlement={
                    "currency": settlement_currency,
                    "net": None,
                    "fees": {"psp_fee": str(psp_fee), "platform_fee": str(platform_fee)},
                },
                warnings=warnings,
            )
        base_rate = Decimal(str(rule.fx_rate))
        spread = Decimal(rule.fx_spread_bps) / Decimal(10_000)
        effective_rate = base_rate * (Decimal("1") - spread)
        if effective_rate <= 0:
            warnings.append("FX_EFFECTIVE_RATE_INVALID")
            effective_rate = base_rate
        settlement_gross = _money(net_charge * effective_rate)
        fx_info = {
            "rate": str(base_rate),
            "spread_bps": int(rule.fx_spread_bps),
            "effective_rate": str(effective_rate),
            "as_of": rule.fx_rate_as_of.isoformat() if rule.fx_rate_as_of else None,
        }

    return EstimateResponse(
        ok=True,
        rule=rule,
        charge={
            "currency": charge_currency,
            # Standard naming: charge gross is what the buyer pays (total).
            "gross_total": str(gross_total),
            # Settlement/fee basis is what we apply fees + FX to (configurable for pass-through).
            "fee_basis": {
                "amount": str(basis_amount),
                "include_tax": bool(body.include_tax),
                "include_shipping": bool(body.include_shipping),
                "source": "pricing" if pricing else "amount",
            },
            **({"pricing": charge_breakdown} if charge_breakdown else {}),
            "fees": {"psp_fee": str(psp_fee), "platform_fee": str(platform_fee)},
            "net_after_fees": str(net_charge),
        },
        settlement={
            "currency": settlement_currency,
            "net": str(settlement_gross),
            **({"fx": fx_info} if fx_info else {}),
        },
        warnings=warnings,
    )
