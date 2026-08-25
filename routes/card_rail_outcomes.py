"""POST /agent/v1/outcomes — the ingestion endpoint for an agent-reported handoff outcome.

THE OTHER HALF OF `recommendation_id`. #2080 mints the key, #1846/#2091 carry it to the agent in
the execution spec, and until now nothing could report back — so the join key existed with
nothing to join to. The audit calls this the compounding asset: `failure_reason` is what turns a
completion-probability term from a guess into a measurement.

PULL-FIRST BY DESIGN, and the reason is a specific failure. The `pivota-acp` webhook outbox
failed *quietly* because every layer swallowed its own outcome — a sender returning None on
success, on transport error, and on an upstream 500 alike, so the caller could not distinguish
delivered from rejected from never attempted. The rule this teaches is not "prefer pull"; it is
that the transport must be able to say which of the three happened. An inbound endpoint gets that
for free: the absence of a row is unambiguous, and the response says exactly what was stored.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from db.card_rail_outcomes import (
    FAILURE_REASONS,
    OUTCOMES,
    REPORTERS,
    record_outcome,
)
from routes.agent_auth import AgentContext, get_agent_context
from utils.logger import logger

router = APIRouter(prefix="/agent/v1", tags=["card-rail-outcomes"])

_MAX_LATENCY_KEYS = 16

# NOTE ON STATUS CODES. The 422s raised below reach the caller as 400 / INVALID_REQUEST: the app
# converts them in middleware/error_handler.py (line ~146), which is a house convention shared by
# every route here. Raising 422 keeps the intent legible at the raise site; the `detail` string is
# what the caller actually reads, so each one names the field and, where there is one, the
# vocabulary.


class CardRailOutcome(BaseModel):
    """One handoff's result, as the acting agent saw it.

    Deliberately NOT including `agent_id`: it is stamped from the authenticated context. An agent
    that could name itself in the body could attribute its own failures to a competitor, and this
    table is meant to be evidence.
    """

    recommendation_id: str = Field(min_length=1, max_length=64)
    outcome: str

    recommendation_set_id: Optional[str] = Field(default=None, max_length=64)
    trace_id: Optional[str] = Field(default=None, max_length=128)
    click_id: Optional[str] = Field(default=None, max_length=64)

    merchant_domain: Optional[str] = Field(default=None, max_length=255)
    product_key: Optional[str] = Field(default=None, max_length=255)
    variant_id: Optional[str] = Field(default=None, max_length=64)
    rail: Optional[str] = Field(default=None, max_length=32)

    quoted_item_total: Optional[float] = None
    quoted_grand_total: Optional[float] = None
    quoted_currency: Optional[str] = Field(default=None, max_length=8)
    quoted_at: Optional[datetime] = None
    spec_expires_at: Optional[datetime] = None

    actual_item_total: Optional[float] = None
    actual_grand_total: Optional[float] = None
    actual_currency: Optional[str] = Field(default=None, max_length=8)

    failure_reason: Optional[str] = Field(default=None, max_length=255)
    latency_ms: Optional[Dict[str, Any]] = None
    auth_outcome: Optional[str] = Field(default=None, max_length=48)
    reported_by: Optional[str] = Field(default=None, max_length=32)
    occurred_at: Optional[datetime] = None


def _money(value: Optional[float], field: str) -> Optional[Decimal]:
    """A total, or a 4xx. Never a silently-coerced number.

    An amount that cannot be represented exactly is refused rather than rounded: this table's
    whole point is measuring the gap between what we quoted and what was charged, and a rounding
    we introduce ourselves would be indistinguishable from the merchant's error.
    """
    if value is None:
        return None
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=422, detail=f"{field} is not a number")
    if not dec.is_finite():
        raise HTTPException(status_code=422, detail=f"{field} must be finite")
    if dec < 0:
        raise HTTPException(status_code=422, detail=f"{field} must not be negative")
    if abs(dec.as_tuple().exponent) > 4:
        raise HTTPException(status_code=422, detail=f"{field} has more than 4 decimal places")
    return dec


def _classify_reason(raw: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Split a reported reason into (known vocabulary term, raw string).

    An unknown reason is KEPT, not rejected and not coerced. Rejecting would lose the outcome
    entirely and teach us nothing; mapping it to 'other' would erase the evidence that our
    vocabulary is incomplete. Storing both means it is still counted AND still legible, and the
    typed column stays countable so the metric can be trended.
    """
    text = (raw or "").strip()
    if not text:
        return None, None
    normalized = text.lower().replace("-", "_").replace(" ", "_")
    if normalized in FAILURE_REASONS:
        return normalized, text
    logger.info("card-rail outcome: unrecognised failure_reason %r (kept raw)", text[:120])
    return None, text[:255]


def _latency(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Numeric milliseconds only, bounded in size.

    The field is agent-supplied and lands in JSONB, so it is an obvious place to smuggle an
    arbitrary blob into our storage. Non-numeric values are dropped rather than rejected — a
    malformed latency should not cost us the outcome it was attached to.
    """
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Any] = {}
    for key, value in list(raw.items())[:_MAX_LATENCY_KEYS]:
        name = str(key)[:32]
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and float(value) == float(value) and value >= 0:
            out[name] = float(value)
    return out


@router.post("/outcomes")
async def report_card_rail_outcome(
    body: CardRailOutcome,
    context: AgentContext = Depends(get_agent_context),
) -> Dict[str, Any]:
    """Record what actually happened to one handoff.

    Idempotent on `recommendation_id`: a re-report corrects the row rather than appending, so the
    question this table exists to answer ("of what we recommended, what completed") stays a single
    scan instead of a windowing query.
    """
    outcome = (body.outcome or "").strip().lower()
    if outcome not in OUTCOMES:
        raise HTTPException(
            status_code=422,
            detail=f"outcome must be one of {', '.join(OUTCOMES)}",
        )

    reported_by = (body.reported_by or "agent").strip().lower()
    if reported_by not in REPORTERS:
        raise HTTPException(
            status_code=422,
            detail=f"reported_by must be one of {', '.join(REPORTERS)}",
        )

    reason, reason_raw = _classify_reason(body.failure_reason)
    # Mirrors the CHECK in migration 199 so a caller gets a 422 naming the field rather than a
    # 500 from the database. A failure that does not say why is the one row that teaches nothing.
    if outcome == "failed" and not (reason or reason_raw):
        raise HTTPException(
            status_code=422,
            detail=(
                "outcome=failed requires failure_reason; known values: "
                + ", ".join(FAILURE_REASONS)
            ),
        )

    values = {
        "recommendation_id": body.recommendation_id.strip(),
        "recommendation_set_id": (body.recommendation_set_id or None),
        "trace_id": (body.trace_id or None),
        "click_id": (body.click_id or None),
        # FROM THE TOKEN. Never from the body.
        "agent_id": str(context.agent_id),
        "merchant_domain": (body.merchant_domain or None),
        "product_key": (body.product_key or None),
        "variant_id": (body.variant_id or None),
        "rail": (body.rail or None),
        "quoted_item_total": _money(body.quoted_item_total, "quoted_item_total"),
        "quoted_grand_total": _money(body.quoted_grand_total, "quoted_grand_total"),
        "quoted_currency": (body.quoted_currency or None),
        "quoted_at": body.quoted_at,
        "spec_expires_at": body.spec_expires_at,
        "actual_item_total": _money(body.actual_item_total, "actual_item_total"),
        "actual_grand_total": _money(body.actual_grand_total, "actual_grand_total"),
        "actual_currency": (body.actual_currency or None),
        "outcome": outcome,
        "failure_reason": reason,
        "failure_reason_raw": reason_raw,
        "latency_ms": _latency(body.latency_ms),
        "auth_outcome": (body.auth_outcome or None),
        "reported_by": reported_by,
        # The agent's clock is not trusted for ordering, but its own timestamp is worth keeping
        # when offered; absent one, the moment we received it is the honest answer.
        "occurred_at": body.occurred_at or datetime.now(tz=timezone.utc),
    }

    import json as _json

    values["latency_ms"] = _json.dumps(values["latency_ms"])

    stored = await record_outcome(values)
    if not stored:
        # The write did not land. Say so — this endpoint exists because a transport that cannot
        # distinguish delivered from rejected is what made the outbox fail silently.
        raise HTTPException(status_code=500, detail="outcome was not recorded")

    return {
        "ok": True,
        "recommendation_id": stored["recommendation_id"],
        # So a caller can tell a first report from a correction without a second round trip.
        "created": bool(stored.get("inserted")),
        "outcome": outcome,
        "failure_reason": reason,
        # Echoed back when we did not recognise it, so the caller learns immediately that the
        # value is being counted but not typed — rather than discovering it in a dashboard.
        "failure_reason_unrecognised": bool(reason_raw and not reason),
    }
