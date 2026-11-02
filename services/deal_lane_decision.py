"""The deal decision (ADR-027 D4, D7): may this click-out show, and carry, a checkout-verified deal?

One set of rules, used twice:
  * at ISSUE time, `resolve_deal_decision` picks at most one deal for an offer. Its caller passes the
    decision to `compose_attributed_destinations`, which stays pure: it signs `DealDecision.reference()`
    into the `/r` token and shows the effective price, so the display and the link cannot disagree;
  * at CLICK time, `recheck_deal_reference` re-reads that one deal for `/r`, which appends `discount=`
    only if it still passes. Kill switches and stop rows therefore reach links already handed out.

The token signs a REFERENCE (deal, variant, market), never the code: tokens are signed, not
encrypted, and a code an agent could read out of a link could no longer be withdrawn.

v1 scope, the Shopify cart-permalink lane only (D7 lane matrix):
  * the link is a Shopify cart permalink for ONE unit of the verified variant, pinned with
    `country=` to the buyer's DECLARED market, which equals the verification market;
  * the deal is a merchant-published code (D2). An automatic discount has no code to carry; a feed
    code travels only on the network link (Q14); channel-exclusive codes are Phase 3 (D8);
  * the deal is a percent or fixed-amount code whose eligibility is known and unlimited (D3);
  * its verification is `checkout_verified`, started, unexpired, in the market's currency, and at
    issue time the variant's observed unit price still equals the reference cart's (D3);
  * DEAL_LANE_EMIT_ENABLED and DEAL_LANE_DISCOUNT_PARAM_ENABLED are both on. With the second off,
    `/r` would not append the code, so the lane would not carry the deal it shows;
  * the market has no active stop row and the seller has not opted out.

Everything else returns no decision, with a reason. Nothing here writes, and with either flag off
nothing here reads the database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from sqlalchemy import and_, select

from config.settings import settings
from db.database import database
from db.deal_lane import (
    deal_lane_optout,
    deal_lane_stops,
    observed_store_offer_verifications,
    observed_store_offers,
)
from services import market_registry

LANE_SHOPIFY_CART = "shopify_cart_permalink"
BASIS = "checkout_verified"
# The token key PR 3's composer signs `DealDecision.reference()` under.
TOKEN_DEAL_REF_KEY = "deal_ref"

_EMITTABLE_KINDS = ("percent", "amount")
_LANE_SOURCES = ("merchant_published",)
_REFERENCE_KEYS = ("deal_id", "variant_id", "market")


@dataclass(frozen=True)
class DealContext:
    """What the caller knows about the click-out it is composing (or, at click time, what the
    token says it composed)."""

    seller_id: Optional[str]
    # The Shopify variant id the cart permalink is built from (`cart_variant_id`), never an
    # attribution variant id.
    variant_id: Optional[str]
    platform: Optional[str]
    quantity: int
    # `country=` on the cart link, from `_cart_country_pin`: the buyer's declared market, or None.
    pin_country: Optional[str]
    # The variant's current unit price and currency as the caller observed them. Required at issue
    # time; at click time the verification's own invalidation stands for it (D3).
    observed_unit_price: Optional[Decimal] = None
    observed_currency: Optional[str] = None


@dataclass(frozen=True)
class DealCandidate:
    """One (deal, verification) row as read."""

    deal_id: str
    seller_id: str
    market: Optional[str]
    code: Optional[str]
    kind: str
    source: str
    currency: Optional[str]
    new_customer_only: bool
    once_per_customer: bool
    segment_limited: bool
    usage_limited: bool
    eligibility_known: bool
    starts_at: Optional[datetime]
    ends_at: Optional[datetime]
    variant_id: str
    state: str
    verified_at: Optional[datetime]
    expires_at: Optional[datetime]
    ref_unit_price: Optional[Decimal]
    ref_subtotal: Optional[Decimal]
    ref_discount_amount: Optional[Decimal]
    ref_currency: Optional[str]


@dataclass(frozen=True)
class DealDecision:
    deal_id: str
    variant_id: str
    market: str
    code: str
    kind: str
    currency: str
    ref_unit_price: Decimal
    discount_amount: Decimal
    # The reference cart's subtotal less the code's allocation: one unit, in the market's currency.
    # An estimate the checkout confirms or not; `pricing.total` stays the only amount charged (D4).
    estimated_effective_price: Decimal
    valid_until: datetime
    basis: str = BASIS
    lane: str = LANE_SHOPIFY_CART

    def reference(self) -> Dict[str, str]:
        """What the `/r` token signs: enough to re-read the deal, never the code."""
        return {"deal_id": self.deal_id, "variant_id": self.variant_id, "market": self.market}


@dataclass(frozen=True)
class DealSwitches:
    emit_enabled: bool
    discount_param_enabled: bool
    market_stopped: bool = False
    seller_opted_out: bool = False


def current_switches(*, market_stopped: bool = False, seller_opted_out: bool = False) -> DealSwitches:
    return DealSwitches(
        emit_enabled=settings.deal_lane_emit_enabled,
        discount_param_enabled=settings.deal_lane_discount_param_enabled,
        market_stopped=market_stopped,
        seller_opted_out=seller_opted_out,
    )


# ---- the rules ------------------------------------------------------------------------------------


def _flags_reason(switches: DealSwitches) -> Optional[str]:
    if not switches.emit_enabled:
        return "emit_off"
    if not switches.discount_param_enabled:
        return "discount_param_off"
    return None


def _lane_reason(ctx: DealContext) -> Optional[str]:
    """The link itself: a one-unit Shopify cart permalink pinned to a declared, listed market."""
    if str(ctx.platform or "").strip().lower() != "shopify":
        return "not_shopify"
    if not str(ctx.variant_id or "").strip():
        return "no_cart_variant"
    if ctx.quantity != 1:
        return "quantity_not_one"
    if not str(ctx.seller_id or "").strip():
        return "no_seller"
    pin = market_registry.normalize_market(ctx.pin_country)
    if not pin or market_registry.market_or_none(pin) is None:
        return "market_not_pinned"
    return None


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def candidate_reason(
    candidate: DealCandidate, ctx: DealContext, now: datetime, *, require_price_match: bool
) -> Optional[str]:
    """None when this verified deal may ride this link; otherwise why not."""
    pin = market_registry.normalize_market(ctx.pin_country)
    if candidate.seller_id != str(ctx.seller_id or "").strip():
        return "other_seller"
    if candidate.variant_id != str(ctx.variant_id or "").strip():
        return "other_variant"
    if market_registry.normalize_market(candidate.market) != pin:
        return "other_market"
    if candidate.source not in _LANE_SOURCES:
        return "source_not_on_this_lane"
    if not str(candidate.code or "").strip():
        return "no_code"
    if candidate.kind not in _EMITTABLE_KINDS:
        return "kind_not_emitted"
    if not candidate.eligibility_known or any(
        (candidate.new_customer_only, candidate.once_per_customer, candidate.segment_limited, candidate.usage_limited)
    ):
        return "eligibility_limited_or_unknown"
    if candidate.state != "checkout_verified":
        return "not_verified"
    verified_at, expires_at = _aware(candidate.verified_at), _aware(candidate.expires_at)
    if verified_at is None or expires_at is None or not (verified_at <= now < expires_at):
        return "verification_expired"
    starts_at, ends_at = _aware(candidate.starts_at), _aware(candidate.ends_at)
    if starts_at is not None and now < starts_at:
        return "not_started"
    if ends_at is not None and now >= ends_at:
        return "ended"
    market_currency = (market_registry.market_or_none(pin) or {}).get("currency")
    if not candidate.ref_currency or candidate.ref_currency != market_currency:
        return "currency_not_market"
    if candidate.currency is not None and candidate.currency != candidate.ref_currency:
        return "currency_not_market"
    unit, subtotal, discount = candidate.ref_unit_price, candidate.ref_subtotal, candidate.ref_discount_amount
    if unit is None or subtotal is None or discount is None or not (Decimal(0) < discount < subtotal):
        return "reference_cart_incomplete"
    if require_price_match:
        if ctx.observed_unit_price is None or not ctx.observed_currency:
            return "price_unobserved"
        observed = _decimal(ctx.observed_unit_price)
        currency = str(ctx.observed_currency).strip().upper()
        if currency != candidate.ref_currency or observed is None or observed != unit:
            return "price_changed"
    return None


def _decision(candidate: DealCandidate) -> DealDecision:
    ends_at = _aware(candidate.ends_at)
    expires_at = _aware(candidate.expires_at)
    valid_until = min(expires_at, ends_at) if ends_at is not None else expires_at
    return DealDecision(
        deal_id=candidate.deal_id,
        variant_id=candidate.variant_id,
        market=market_registry.normalize_market(candidate.market),
        code=str(candidate.code).strip(),
        kind=candidate.kind,
        currency=str(candidate.ref_currency),
        ref_unit_price=candidate.ref_unit_price,
        discount_amount=candidate.ref_discount_amount,
        estimated_effective_price=candidate.ref_subtotal - candidate.ref_discount_amount,
        valid_until=valid_until,
    )


def decide(
    candidates: Iterable[DealCandidate],
    ctx: DealContext,
    switches: DealSwitches,
    now: datetime,
    *,
    require_price_match: bool,
) -> Tuple[Optional[DealDecision], str]:
    """The pure decision: (decision, "ok") or (None, reason). Of several deals that pass, the one
    with the largest verified allocation wins (the buyer's price comes first, Q6), ties by deal_id."""
    now = _aware(now)
    for reason in (_flags_reason(switches), _lane_reason(ctx)):
        if reason:
            return None, reason
    if switches.market_stopped:
        return None, "market_stopped"
    if switches.seller_opted_out:
        return None, "seller_opted_out"
    passing: List[DealCandidate] = []
    rejected = set()
    for candidate in candidates:
        reason = candidate_reason(candidate, ctx, now, require_price_match=require_price_match)
        if reason is None:
            passing.append(candidate)
        else:
            rejected.add(reason)
    if not passing:
        return None, ("no_emittable_deal:" + ",".join(sorted(rejected))) if rejected else "no_deal"
    best = min(passing, key=lambda c: (-c.ref_discount_amount, c.deal_id))
    return _decision(best), "ok"


# ---- the reads ------------------------------------------------------------------------------------


def _decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _datetime(value: Any) -> Optional[datetime]:
    if value is None or isinstance(value, datetime):
        return _aware(value)
    try:
        return _aware(datetime.fromisoformat(str(value)))
    except ValueError:
        return None


def _candidate(row: Mapping[str, Any]) -> DealCandidate:
    return DealCandidate(
        deal_id=row["deal_id"],
        seller_id=row["seller_id"],
        market=row["market"],
        code=row["code"],
        kind=row["kind"],
        source=row["source"],
        currency=row["currency"],
        new_customer_only=bool(row["new_customer_only"]),
        once_per_customer=bool(row["once_per_customer"]),
        segment_limited=bool(row["segment_limited"]),
        usage_limited=bool(row["usage_limited"]),
        eligibility_known=bool(row["eligibility_known"]),
        starts_at=_datetime(row["starts_at"]),
        ends_at=_datetime(row["ends_at"]),
        variant_id=row["variant_id"],
        state=row["state"],
        verified_at=_datetime(row["verified_at"]),
        expires_at=_datetime(row["expires_at"]),
        ref_unit_price=_decimal(row["ref_unit_price"]),
        ref_subtotal=_decimal(row["ref_subtotal"]),
        ref_discount_amount=_decimal(row["ref_discount_amount"]),
        ref_currency=row["ref_currency"],
    )


async def _read_candidates(
    *, seller_id: str, variant_id: str, market: str, deal_id: Optional[str] = None
) -> List[DealCandidate]:
    o, v = observed_store_offers, observed_store_offer_verifications
    conditions = [
        o.c.seller_id == seller_id,
        o.c.market == market,
        v.c.variant_id == variant_id,
        v.c.state == "checkout_verified",
    ]
    if deal_id is not None:
        conditions.append(o.c.deal_id == deal_id)
    query = select(
        o.c.deal_id, o.c.seller_id, o.c.market, o.c.code, o.c.kind, o.c.source, o.c.currency,
        o.c.new_customer_only, o.c.once_per_customer, o.c.segment_limited, o.c.usage_limited,
        o.c.eligibility_known, o.c.starts_at, o.c.ends_at,
        v.c.variant_id, v.c.state, v.c.verified_at, v.c.expires_at,
        v.c.ref_unit_price, v.c.ref_subtotal, v.c.ref_discount_amount, v.c.ref_currency,
    ).select_from(o.join(v, o.c.deal_id == v.c.deal_id)).where(and_(*conditions))
    return [_candidate(row) for row in await database.fetch_all(query)]


async def _market_stopped(market: str) -> bool:
    query = select(deal_lane_stops.c.stop_id).where(
        and_(deal_lane_stops.c.market == market, deal_lane_stops.c.cleared_at.is_(None))
    )
    return await database.fetch_one(query) is not None


async def _seller_opted_out(seller_id: str) -> bool:
    query = select(deal_lane_optout.c.seller_id).where(deal_lane_optout.c.seller_id == seller_id)
    return await database.fetch_one(query) is not None


async def _resolve(
    ctx: DealContext, *, now: Optional[datetime], require_price_match: bool, deal_id: Optional[str] = None
) -> Tuple[Optional[DealDecision], str]:
    now = _aware(now) or datetime.now(timezone.utc)
    # Everything that needs no read first: with a flag off, or a link this lane cannot carry, the
    # database is never touched.
    off = current_switches()
    for reason in (_flags_reason(off), _lane_reason(ctx)):
        if reason:
            return None, reason
    seller_id = str(ctx.seller_id).strip()
    variant_id = str(ctx.variant_id).strip()
    market = market_registry.normalize_market(ctx.pin_country)
    switches = current_switches(
        market_stopped=await _market_stopped(market),
        seller_opted_out=await _seller_opted_out(seller_id),
    )
    if switches.market_stopped or switches.seller_opted_out:
        return decide((), ctx, switches, now, require_price_match=require_price_match)
    candidates = await _read_candidates(
        seller_id=seller_id, variant_id=variant_id, market=market, deal_id=deal_id
    )
    return decide(candidates, ctx, switches, now, require_price_match=require_price_match)


async def resolve_deal_decision(
    ctx: DealContext, *, now: Optional[datetime] = None
) -> Tuple[Optional[DealDecision], str]:
    """Issue time: the deal this click-out may show and carry, or (None, reason). Requires the
    variant's observed unit price (D3 invalidation at serving)."""
    return await _resolve(ctx, now=now, require_price_match=True)


def parse_deal_reference(value: Any) -> Optional[Dict[str, str]]:
    """A signed token's deal reference, or None when it is not exactly one."""
    if not isinstance(value, Mapping) or set(value) != set(_REFERENCE_KEYS):
        return None
    out = {key: str(value[key] or "").strip() for key in _REFERENCE_KEYS}
    return out if all(out.values()) else None


async def recheck_deal_reference(
    reference: Any, ctx: DealContext, *, now: Optional[datetime] = None
) -> Tuple[Optional[DealDecision], str]:
    """Click time (`/r`): the referenced deal, re-checked against the same rules, or (None, reason).
    The reference must name the token's own variant and pinned market. No price is required here:
    a price, sale or automatic-discount change sets the verification back to unverified (D3)."""
    ref = parse_deal_reference(reference)
    if ref is None:
        return None, "bad_reference"
    if ref["variant_id"] != str(ctx.variant_id or "").strip():
        return None, "reference_other_variant"
    if market_registry.normalize_market(ref["market"]) != market_registry.normalize_market(ctx.pin_country):
        return None, "reference_other_market"
    return await _resolve(ctx, now=now, require_price_match=False, deal_id=ref["deal_id"])
