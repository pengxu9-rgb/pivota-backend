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
    `country=` to the buyer's DECLARED market, which equals the verification market. The market must
    be one the registry SERVES (today US and SG); a listed market that is off or acquisition-only
    (e.g. AU, JP) gets no deal until the registry serves it;
  * the deal is a merchant-published code (D2). An automatic discount has no code to carry; a feed
    code travels only on the network link (Q14); channel-exclusive codes are Phase 3 (D8);
  * the deal is a percent or fixed-amount code whose eligibility is known and unlimited (D3);
  * its verification is `checkout_verified`, started, unexpired, no older than DEAL_VERIFY_TTL_HOURS
    whatever the stored expiry says, in the market's currency, and at issue time the variant's
    observed unit price still equals the reference cart's (D3);
  * DEAL_LANE_EMIT_ENABLED and DEAL_LANE_DISCOUNT_PARAM_ENABLED are both on. With the second off,
    `/r` would not append the code, so the lane would not carry the deal it shows;
  * the market has no active stop row and the seller has not opted out.

The effective price (D4): the reference cart's pre-discount UNIT price, minus the code's allocation,
minus every automatic allocation on that one-unit cart. Never the cart subtotal, whose treatment of
allocations the arithmetic does not rely on; a subtotal outside [effective price, unit price] means the
reference cart is not what this assumes, and the deal is refused. Every amount must be finite.

Fail closed: any rule that cannot be checked, a malformed row, or a failed read returns no decision
with a reason, never an exception. The one exception is a caller passing a naive `now`, a programming
error that raises ValueError. Nothing here writes, and with either flag off nothing is read.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import parse_qs, quote, urlparse

from sqlalchemy import and_, exists, select

from config.settings import settings
from db.database import database
from db.deal_lane import (
    deal_lane_optout,
    deal_lane_stops,
    observed_store_offer_verifications,
    observed_store_offers,
)
from services import market_registry

logger = logging.getLogger(__name__)

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
    observed_unit_price: Any = None
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
    # The automatic allocations on the reference cart, as the verifier writes them: a list of
    # {"title": str, "type": "line" | "order", "amount": "<decimal>", "currency"?: ref_currency};
    # [] when there were none. Shipping allocations are never listed. Anything else is refused.
    ref_automatic_discounts: Any = field(default_factory=list)


@dataclass(frozen=True)
class DealDecision:
    deal_id: str
    variant_id: str
    market: str
    code: str
    kind: str
    currency: str
    # The reference cart's pre-discount unit price (D4's base).
    ref_unit_price: Decimal
    # The code's own allocation: the only saving credited to the code (D4).
    discount_amount: Decimal
    # Automatic discounts that applied anyway: part of the price, never the code's saving.
    automatic_amount: Decimal
    # ref_unit_price - discount_amount - automatic_amount, for one unit, before tax and shipping.
    # An estimate the checkout confirms or not; `pricing.total` stays the only amount charged (D4).
    estimated_effective_price: Decimal
    verified_at: datetime
    # The earliest of the stored expiry, the deal's end and verified_at + DEAL_VERIFY_TTL_HOURS.
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
    """The link itself: a one-unit Shopify cart permalink pinned to a declared, served market."""
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
    if pin not in market_registry.served_markets():
        return "market_not_served"
    return None


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    """A timestamp READ from the database; a naive one is stored UTC."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _require_aware(now: Optional[datetime]) -> datetime:
    """A caller's `now` must say its zone: a naive one taken as UTC would stretch a verification by
    the caller's offset."""
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("the deal decision needs an aware `now`")
    return now


def _finite(value: Any) -> Optional[Decimal]:
    """A finite Decimal, or None (None, garbage, NaN and infinities alike)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


_AUTOMATIC_TYPES = ("line", "order")


def automatic_total(value: Any, currency: Optional[str]) -> Optional[Decimal]:
    """The sum of the reference cart's automatic allocations, or None when they are not a list of
    line or order allocations with finite, non-negative amounts in `currency` (absent = the cart's).
    Fail closed: an unreadable list could hide a discount, and a shipping or foreign-currency
    amount must never be taken off the unit price."""
    if not isinstance(value, list):
        return None
    total = Decimal(0)
    for item in value:
        if not isinstance(item, Mapping) or item.get("type") not in _AUTOMATIC_TYPES:
            return None
        if item.get("currency") is not None and item.get("currency") != currency:
            return None
        amount = _finite(item.get("amount"))
        if amount is None or amount < 0:
            return None
        total += amount
    return total


def _ttl_ceiling(verified_at: datetime) -> datetime:
    return verified_at + timedelta(hours=settings.deal_verify_ttl_hours)


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
    # The TTL is a ceiling the stored expiry cannot extend, and lowering it reaches stored rows.
    if now >= _ttl_ceiling(verified_at):
        return "verification_past_ttl"
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
    unit, subtotal, code_amount = (
        _finite(candidate.ref_unit_price), _finite(candidate.ref_subtotal), _finite(candidate.ref_discount_amount)
    )
    automatic = automatic_total(candidate.ref_automatic_discounts, candidate.ref_currency)
    if unit is None or subtotal is None or code_amount is None or automatic is None:
        return "reference_cart_incomplete"
    effective = unit - code_amount - automatic
    if not (code_amount > 0 and effective > 0):
        return "reference_cart_incomplete"
    # One unit: whichever way the platform's subtotal treats allocations, it lies between the
    # effective price and the unit price. Outside that, the cart is not the one-unit cart D3 needs.
    if not (effective <= subtotal <= unit):
        return "reference_cart_inconsistent"
    if require_price_match:
        observed = _finite(ctx.observed_unit_price)
        currency = str(ctx.observed_currency or "").strip().upper()
        if observed is None or not currency:
            return "price_unobserved"
        if currency != candidate.ref_currency or observed != unit:
            return "price_changed"
    return None


def _decision(candidate: DealCandidate) -> DealDecision:
    """Only for a candidate `candidate_reason` passed."""
    verified_at = _aware(candidate.verified_at)
    ceilings = [_aware(candidate.expires_at), _ttl_ceiling(verified_at)]
    if candidate.ends_at is not None:
        ceilings.append(_aware(candidate.ends_at))
    unit = _finite(candidate.ref_unit_price)
    code_amount = _finite(candidate.ref_discount_amount)
    automatic = automatic_total(candidate.ref_automatic_discounts, candidate.ref_currency)
    return DealDecision(
        deal_id=candidate.deal_id,
        variant_id=candidate.variant_id,
        market=market_registry.normalize_market(candidate.market),
        code=str(candidate.code).strip(),
        kind=candidate.kind,
        currency=str(candidate.ref_currency),
        ref_unit_price=unit,
        discount_amount=code_amount,
        automatic_amount=automatic,
        estimated_effective_price=unit - code_amount - automatic,
        verified_at=verified_at,
        valid_until=min(ceilings),
    )


def _rank(decision: DealDecision):
    """The buyer's price first (Q6): the lowest effective price, then the larger code allocation,
    then the later valid_until, then the fresher verification; deal_id only as the last resort.
    (The network lane's preference for our assigned code, Q14, is its own rule, not this one.)"""
    return (
        decision.estimated_effective_price,
        -decision.discount_amount,
        -decision.valid_until.timestamp(),
        -decision.verified_at.timestamp(),
        decision.deal_id,
    )


def decide(
    candidates: Iterable[DealCandidate],
    ctx: DealContext,
    switches: DealSwitches,
    now: datetime,
    *,
    require_price_match: bool,
    unreadable_rows: int = 0,
) -> Tuple[Optional[DealDecision], str]:
    """The pure decision: (decision, "ok") or (None, reason). `unreadable_rows` counts rows the read
    could not parse; they are reported as `row_unreadable`, never silently dropped."""
    now = _require_aware(now)
    for reason in (_flags_reason(switches), _lane_reason(ctx)):
        if reason:
            return None, reason
    if switches.market_stopped:
        return None, "market_stopped"
    if switches.seller_opted_out:
        return None, "seller_opted_out"
    passing: List[DealDecision] = []
    rejected = {"row_unreadable"} if unreadable_rows else set()
    for candidate in candidates:
        reason = candidate_reason(candidate, ctx, now, require_price_match=require_price_match)
        if reason is None:
            passing.append(_decision(candidate))
        else:
            rejected.add(reason)
    if not passing:
        return None, ("no_emittable_deal:" + ",".join(sorted(rejected))) if rejected else "no_deal"
    return min(passing, key=_rank), "ok"


# ---- the reads ------------------------------------------------------------------------------------


def _datetime(value: Any) -> Optional[datetime]:
    if value is None or isinstance(value, datetime):
        return _aware(value)
    try:
        return _aware(datetime.fromisoformat(str(value)))
    except ValueError:
        return None


def _json(value: Any) -> Any:
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


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
        ref_unit_price=_finite(row["ref_unit_price"]),
        ref_subtotal=_finite(row["ref_subtotal"]),
        ref_discount_amount=_finite(row["ref_discount_amount"]),
        ref_currency=row["ref_currency"],
        ref_automatic_discounts=_json(row["ref_automatic_discounts"]),
    )


async def _read_candidate_rows(
    *, seller_id: str, variant_id: str, market: str, deal_id: Optional[str] = None
) -> List[Mapping[str, Any]]:
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
        v.c.ref_automatic_discounts,
    ).select_from(o.join(v, o.c.deal_id == v.c.deal_id)).where(and_(*conditions))
    return list(await database.fetch_all(query))


def _parse_rows(rows: Iterable[Mapping[str, Any]]) -> Tuple[List[DealCandidate], int]:
    """Rows as candidates, OUTSIDE the read's error handling: a row this code cannot parse (a
    renamed column, a bad value) is counted as unreadable and logged, never a silent read failure."""
    candidates: List[DealCandidate] = []
    unreadable = 0
    for row in rows:
        try:
            candidates.append(_candidate(row))
        except Exception as exc:  # noqa: BLE001 - one bad row must not hide the others
            unreadable += 1
            logger.warning("deal decision: unreadable deal row: %s", type(exc).__name__)
    return candidates, unreadable


async def _stopped_or_opted_out(*, market: str, seller_id: str) -> Tuple[bool, bool]:
    """The market's active stop row and the seller's opt-out, in one round trip."""
    stopped = exists().where(and_(deal_lane_stops.c.market == market, deal_lane_stops.c.cleared_at.is_(None)))
    opted_out = exists().where(deal_lane_optout.c.seller_id == seller_id)
    row = await database.fetch_one(select(stopped.label("stopped"), opted_out.label("opted_out")))
    return bool(row["stopped"]), bool(row["opted_out"])


async def _resolve(
    ctx: DealContext, *, now: Optional[datetime], require_price_match: bool, deal_id: Optional[str] = None
) -> Tuple[Optional[DealDecision], str]:
    now = _require_aware(now)
    # Everything that needs no read first: with a flag off, or a link this lane cannot carry, the
    # database is never touched.
    for reason in (_flags_reason(current_switches()), _lane_reason(ctx)):
        if reason:
            return None, reason
    seller_id = str(ctx.seller_id).strip()
    market = market_registry.normalize_market(ctx.pin_country)
    try:
        stopped, opted_out = await _stopped_or_opted_out(market=market, seller_id=seller_id)
        rows: List[Mapping[str, Any]] = []
        if not (stopped or opted_out):
            rows = await _read_candidate_rows(
                seller_id=seller_id, variant_id=str(ctx.variant_id).strip(), market=market, deal_id=deal_id
            )
    except Exception as exc:  # noqa: BLE001 - a failed read is no deal, never a failed click-out
        logger.warning("deal decision: read failed, no deal emitted: %s", type(exc).__name__)
        return None, "read_failed"
    candidates, unreadable = _parse_rows(rows)
    switches = current_switches(market_stopped=stopped, seller_opted_out=opted_out)
    return decide(
        candidates, ctx, switches, now, require_price_match=require_price_match, unreadable_rows=unreadable
    )


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


# ---- the link ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DealOffer:
    """What a mint site knows about the offer, for `_make_external_redirect_url` to resolve a deal
    against the link it actually composes (its variant, quantity and pin)."""

    # ADR-009: the seller of record, i.e. catalog_merchants.merchant_id (a connected store's own
    # merchant id). The verifier writes the same key as observed_store_offers.seller_id.
    seller_id: Optional[str]
    # The market the BUYER's request named, raw (None when it named none).
    buyer_market: Any
    observed_unit_price: Any = None
    observed_currency: Optional[str] = None


_CART_PATH = re.compile(r"^/cart/(\d+):1$")


def cart_dest_matches(dest: str, *, variant_id: str, market: str) -> bool:
    """True when `dest` is the Shopify cart permalink for ONE unit of `variant_id`, pinned with
    `country=` to `market` and carrying no discount yet: the only destination `/r` appends a
    deal-lane code to. Never a product page, never a warm-handoff continue_url."""
    try:
        parsed = urlparse(str(dest or ""))
    except ValueError:
        return False
    if parsed.scheme != "https" or not parsed.netloc:
        return False
    path = _CART_PATH.match(parsed.path or "")
    if not path or path.group(1) != str(variant_id or "").strip():
        return False
    params = parse_qs(parsed.query, keep_blank_values=True)
    if "discount" in params:
        return False
    return params.get("country") == [market_registry.normalize_market(market)] and bool(market)


def append_discount(dest: str, code: str) -> str:
    """`discount=<code>` on a cart permalink, the code percent-encoded."""
    sep = "&" if "?" in dest else "?"
    return f"{dest}{sep}discount={quote(str(code).strip(), safe='')}"


@dataclass(frozen=True)
class ClickDeal:
    dest: str
    carried: bool
    reason: str


async def deal_destination_at_click(payload: Mapping[str, Any], dest: str) -> ClickDeal:
    """`/r`'s re-check: the signed destination with `discount=` appended when the token's deal
    reference still passes every rule, else the signed destination unchanged. Never raises."""
    try:
        ref = parse_deal_reference(payload.get(TOKEN_DEAL_REF_KEY))
        if ref is None:
            return ClickDeal(dest, False, "bad_reference")
        pin = market_registry.normalize_market(payload.get("cart_country"))
        if not cart_dest_matches(dest, variant_id=ref["variant_id"], market=pin):
            return ClickDeal(dest, False, "dest_not_the_verified_cart")
        ctx = payload.get("ctx") if isinstance(payload.get("ctx"), Mapping) else {}
        deal_ctx = DealContext(
            seller_id=str(ctx.get("merchant_id") or "").strip() or None,
            variant_id=ref["variant_id"],
            platform="shopify",  # the destination is a Shopify cart permalink (checked above)
            quantity=1,
            pin_country=pin,
        )
        decision, reason = await recheck_deal_reference(ref, deal_ctx)
        if decision is None:
            return ClickDeal(dest, False, reason)
        return ClickDeal(append_discount(dest, decision.code), True, "ok")
    except Exception as exc:  # noqa: BLE001 - the buyer always gets the signed destination
        logger.warning("deal decision: click-time re-check failed: %s", type(exc).__name__)
        return ClickDeal(dest, False, "recheck_failed")
