"""Admin-only read of the MERCHANT PURCHASABILITY fact.

WHY THIS EXISTS. The fact is the only thing standing between an allowlisted merchant and a buyer
being sent to a checkout that cannot take their card. When the Reap rail refuses
`merchant_not_purchasable`, the reason is in this table and NOWHERE else — the sweep's report is
counts-only by design (no domains, no rows), and Cloud SQL is private-IP only, so nothing outside
the VPC can read it. Without this route the only answer to "why did that merchant stop being
purchasable" is a one-off SQL job.

WHAT IT IS NOT. Not a query endpoint. One fixed question, no caller-supplied SQL, no table names
on the wire, admin-gated. Domain and market are the only free inputs and both are normalized
through the same functions the writer keys on, so an operator cannot be shown a different row
than the door reads.

THE ONE WAY THIS ROUTE DIFFERS FROM ITS SIBLINGS. Since the OIDC follow-up it depends on
`require_admin_or_gateway_identity` rather than the bare `Depends(require_admin)` every handler
in routes/store_audit_ops.py uses. That dependency runs `require_admin` first and unchanged, and
additionally accepts the PIVOTA-Agent gateway's Google-signed Cloud Run identity token — but
only once an operator sets BOTH `OPS_GATEWAY_OIDC_AUDIENCE` and `OPS_GATEWAY_SERVICE_ACCOUNTS`.
With the shipped defaults this route's behaviour, including every refusal body, is unchanged.

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
from utils.gateway_oidc_auth import require_admin_or_gateway_identity

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
    #: The ISO-2 market the answer is keyed on, or null when the request named no usable market
    #: (then `reason == "market_unknown"`). NEVER a substituted "US".
    market: Optional[str] = None
    #: What the DOOR would answer right now for this merchant x market. `purchase` only when a
    #: fresh positive fact exists FROM THE BUYER VANTAGE; every other state is `browse_only`.
    tier: str
    #: Why `tier` is not `purchase` when the answer is structural rather than a fact: today the
    #: only value is `market_unknown` — no market, so no purchasability claim is possible either
    #: way (fail-open for browse / links-out, never a purchase). Null whenever a market was keyed,
    #: so a keyed response differs from the pre-field one only by this one null key.
    reason: Optional[str] = None
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
    # OPTIONAL since the market-never-defaulted change: a caller that has no market (the gateway's
    # `get_checkout` re-read, an escalation with no address country) gets an explicit
    # `reason: market_unknown` answer instead of a 422 it would read as "backend down" and fail
    # open on. ANY string is accepted and put through the one helper: `" us"` is US, and `""`,
    # `"USA"`, `"U1"` are `market_unknown` — the same rule the fact store keys on, never a
    # truncation and never a 422 the caller would read as an outage.
    market: Optional[str] = Query(None),
    # ADMIN JWT **OR** THE GATEWAY'S GOOGLE IDENTITY TOKEN. This is the only route in the
    # repo that accepts the second, and it is read-only. See utils/gateway_oidc_auth.py: the
    # OIDC path is DISABLED until both `OPS_GATEWAY_OIDC_AUDIENCE` and
    # `OPS_GATEWAY_SERVICE_ACCOUNTS` are set, so with the shipped defaults this is exactly
    # `require_admin` and every refusal is byte-identical to today's.
    #
    # It is NOT `require_admin_or_key`: `X-ADMIN-KEY` is still refused here, as on every
    # other ops route. The reason the gateway needed this at all is that its credential was a
    # standing admin JWT in an env var, and the gateway fails OPEN on a non-200 — so the day
    # that JWT expired the gate would have disarmed itself silently.
    _principal: Dict[str, Any] = Depends(require_admin_or_gateway_identity),
) -> PurchasabilityResponse:
    """Every purchasability row for one merchant x market, one per vantage, plus the tier the
    agent door would serve.

    `tier` is the answer, not the rows: a reader who looks only at `card_available` will be
    misled by a positive fact from the WRONG VANTAGE, which is exactly the trap the vantage
    column exists for. It is computed through `is_purchasable`, the same function the rail calls.
    """
    normalized = purchasability.normalize_domain(domain)
    normalized_market = purchasability.normalize_market(market)
    vantage = purchasability.buyer_vantage()

    if normalized_market is None:
        # NO MARKET, NO CLAIM — and no database read, no log line. `is_purchasable` answers False
        # for the same input without asking the database; this says WHY, so an operator (or the
        # gateway) can tell "unknown market" from "known market, no positive fact".
        return PurchasabilityResponse(
            domain=normalized,
            market=None,
            tier="browse_only",
            reason=purchasability.MARKET_UNKNOWN,
            enforced=purchasability.is_enforcement_enabled(),
            buyer_vantage=vantage,
            sweep_enabled=purchasability.is_sweep_enabled(),
            ttl_hours=purchasability.ttl_hours(),
            facts=[],
            note=(
                "market_unknown: the request named no ISO-2 market, so no purchasability claim "
                "can be made either way. The market is never defaulted (not to US, not to "
                "anything); the door answers browse_only for purchase and leaves browse and "
                "links-out untouched."
            ),
        )

    rows = await purchasability.list_facts(normalized, normalized_market)
    purchasable = await purchasability.is_purchasable(normalized, normalized_market)

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
