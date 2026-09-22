"""Admin-only read of the MERCHANT PURCHASABILITY fact.

WHY THIS EXISTS. The fact is the only thing standing between an allowlisted merchant and a buyer
being sent to a checkout that cannot take their card. When the Reap rail refuses
`merchant_not_purchasable`, the reason is in this table and NOWHERE else — the sweep's report is
counts-only by design (no domains, no rows), and Cloud SQL is private-IP only, so nothing outside
the VPC can read it. Without this route the only answer to "why did that merchant stop being
purchasable" is a one-off SQL job.

WHAT IT IS NOT. Not a query endpoint. One fixed question, no caller-supplied SQL, no table names
on the wire, admin-gated with the same `Depends(require_admin)` every handler in
routes/store_audit_ops.py uses. Domain and market are the only free inputs and both are
normalized through the same functions the writer keys on, so an operator cannot be shown a
different row than the door reads.

IT IS A READ ONLY, AND IT IS GATED ON NEITHER DIAL. With both dials off the sweep gathers nothing
and the consumers ignore what is there, but an operator arming the rail needs to see the facts
FIRST — a route that went dark with a dial would be unreadable at exactly the moment it is
needed. It reports both dials' state instead, so its numbers are never mistaken for enforcement.

THIS ROUTE IS ALSO THE GATEWAY'S CONTRACT. The per-merchant checkout tier is decided in the
PIVOTA-Agent gateway (`services/ucpStoreAuditProbe.js`), not here, so the gateway reads `tier`
from this response — but ONLY when `enforced` is true. That field is not decoration: with
enforcement off every merchant reads `browse_only`, so a gateway that acted on `tier` alone
would take the entire catalogue browse-only on the day it shipped. See the "Gateway
(PIVOTA-Agent) change" section of docs/runbooks/merchant_purchasability.md.

NO BUYER DATA CAN REACH THIS RESPONSE. The evidence blob is written by an allow-list of keys
(see `db/merchant_purchasability._evidence`) and the sweep runs with `buyer=None`; there is no
buyer field on this path to redact.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

import db.merchant_purchasability as purchasability
from utils.auth import require_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ops", tags=["merchant-purchasability-ops"])


class PurchasabilityFact(BaseModel):
    vantage: str
    verdict: Optional[str] = None
    card_available: Optional[bool] = None
    payment_methods: List[str] = []
    landed_price_minor: Optional[int] = None
    landed_currency: Optional[str] = None
    expected_price_minor: Optional[int] = None
    price_drift_minor: Optional[int] = None
    variant_id: Optional[str] = None
    consecutive_failures: int = 0
    checked_at: Optional[str] = None
    positive_until: Optional[str] = None
    positive_now: bool = False
    evidence: Optional[Dict[str, Any]] = None


class PurchasabilityResponse(BaseModel):
    domain: str
    market: str
    #: What the DOOR would answer right now for this merchant x market. `purchase` only when a
    #: fresh positive fact exists FROM THE BUYER VANTAGE; every other state is `browse_only`.
    tier: str
    #: THE GATEWAY CONTRACT. False means the backend is NOT refusing on this fact yet, so a
    #: consumer must keep its previous behaviour and treat `tier` as advisory. A gateway that
    #: acted on `tier` without reading this would turn the whole catalogue browse-only on the
    #: day the field shipped, because with enforcement off every merchant reads browse_only.
    enforced: bool
    buyer_vantage: str
    #: Whether the SWEEP is arming. `sweep_enabled=False` with `enforced=True` is the misordered
    #: state the runbook warns about: nothing is gathering facts and everything is being refused.
    sweep_enabled: bool
    ttl_hours: int
    facts: List[PurchasabilityFact]
    note: str


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


@router.get("/merchant-purchasability", response_model=PurchasabilityResponse)
async def merchant_purchasability(
    domain: str = Query(..., min_length=3, max_length=253),
    market: str = Query(..., min_length=2, max_length=2),
    _admin: Dict[str, Any] = Depends(require_admin),
) -> PurchasabilityResponse:
    """Every purchasability row for one merchant x market, one per vantage, plus the tier the
    agent door would serve.

    `tier` is the answer, not the rows: a reader who looks only at `card_available` will be
    misled by a positive fact from the WRONG VANTAGE, which is exactly the trap the vantage
    column exists for. It is computed through `is_purchasable`, the same function the rail calls.
    """
    normalized = purchasability.normalize_domain(domain)
    normalized_market = purchasability.normalize_market(market)

    rows = await purchasability.list_facts(normalized, normalized_market)
    purchasable = await purchasability.is_purchasable(normalized, normalized_market)
    vantage = purchasability.buyer_vantage()

    facts = [
        PurchasabilityFact(
            vantage=str(row.get("vantage") or ""),
            verdict=(str(row["verdict"]) if row.get("verdict") else None),
            card_available=row.get("card_available"),
            payment_methods=[str(m) for m in (row.get("payment_methods") or []) if isinstance(m, str)],
            landed_price_minor=row.get("landed_price_minor"),
            landed_currency=(str(row["landed_currency"]) if row.get("landed_currency") else None),
            expected_price_minor=row.get("expected_price_minor"),
            price_drift_minor=row.get("price_drift_minor"),
            variant_id=(str(row["variant_id"]) if row.get("variant_id") else None),
            consecutive_failures=int(row.get("consecutive_failures") or 0),
            checked_at=_iso(row.get("checked_at")),
            positive_until=_iso(row.get("positive_until")),
            # Per-ROW, and deliberately not the same thing as `tier`: a row can hold a live
            # window and still not be the vantage the door reads.
            positive_now=bool(row.get("positive_until")) and str(row.get("vantage") or "") == vantage
            and purchasable,
            evidence=(row.get("evidence") if isinstance(row.get("evidence"), dict) else None),
        )
        for row in rows
    ]

    if not rows:
        note = (
            "no rows: this merchant x market has never been checked, OR the lookup failed "
            "(they are indistinguishable here — check service logs). Either way the door "
            "answers browse_only while the gate is on."
        )
    elif purchasable:
        note = f"a fresh positive fact from vantage {vantage!r}; the door may offer purchase"
    else:
        note = (
            f"no fresh positive fact from the buyer vantage {vantage!r} — check whether a "
            "positive row exists under a DIFFERENT vantage (reachability is egress-dependent), "
            "whether the window expired, or whether two consecutive negatives demoted it"
        )

    return PurchasabilityResponse(
        domain=normalized,
        market=normalized_market,
        tier=("purchase" if purchasable else "browse_only"),
        enforced=purchasability.is_enforcement_enabled(),
        buyer_vantage=vantage,
        sweep_enabled=purchasability.is_sweep_enabled(),
        ttl_hours=purchasability.ttl_hours(),
        facts=facts,
        note=note,
    )
