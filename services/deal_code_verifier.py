"""The crawled-code verifier (ADR-027 D3, Q17-Q20; coupon discovery design §3.3): tests the codes the crawl wrote
(services/deal_code_capture_writer.py) on each store's own UCP door, one reference cart per code, and records what
the store said in `observed_store_offer_verifications`.

A step at the end of each refresh run (jobs/external_referral_refresh.py), after retention. It runs ONLY when all of
these hold, and otherwise reads nothing:
  - DEAL_VERIFY_JOB_ENABLED (the job's kill switch) and DEAL_VERIFY_NONCONNECTED_ENABLED (a crawled store has no
    relationship with us: Q2's approval, given 2026-11-07 (Q17), is what authorizes the test: `authorized_by` is
    `q2_approval`) are both "true";
  - the gateway route's key (DEAL_VERIFY_INTERNAL_KEY) is at least MIN_KEY_CHARS long, as the route requires, and
    its URL (DEAL_VERIFY_ROUTE_URL, no default) is https.

THE CART. The gateway's internal route (`POST /internal/ucp/discount-code/verify`, gw#2552) runs the SAME discovery,
create_cart, cart reader and discount reader the buyer's checkout door uses, on its own door (no bearer credential,
no retry, `create_cart` only). One unit of one store variant, the code, the deal's market and that market's currency.
Never a checkout, never a payment; the store keeps one anonymous cart (accepted with Q17).

WHAT IS TESTED (`due`): crawl rows only (the writer's `dlc_` prefix), with a code, a served market and a kind a
one-unit cart can show (`percent`, `amount`; free shipping needs an address, gift and BXGY a cart shape). Never a
seller the opt-out denylist covers (the writer's `read_denyset`: fails closed), never a code the store refused (a
`failed` verification, or its hash in `deal_code_refusals` within REFUSAL_HASH_DAYS), never a deal that has ended.
Order: never-checked first, then verified rows that expire within REVERIFY_WITHIN, then retries after their backoff.

LIMITS (D3): at most DEAL_VERIFY_MAX_CARTS_PER_RUN carts per run, one per host per run, and a slot in
`deal_verify_host_daily_carts` reserved BEFORE each call (one atomic statement that only counts while the host has
carts left today, DEAL_VERIFY_MAX_CARTS_PER_HOST_PER_DAY). No retries inside a run; a spent slot is never returned.

THE PRODUCT IT TESTS ON (`reference_variant`): the cheapest in-stock variant, at quantity 1, of an active seed on the
seller's host whose own currency serves the deal's market (services.seed_serving.seed_row_market; the seed's `market`
column is a storage partition, 'US' for every seed, and is never read): a numeric store variant id, its own price in
the market's currency and at least the code's minimum spend. Each host's seeds are read once per run. Ties go to the lowest store id, so repeated tests land on the same variant. With none, no
cart is built and nothing is written (the verification row is keyed by the variant): counted.

WHAT IS RECORDED, per answer:
  - applied        `checkout_verified`, with the reference cart (unit price, subtotal, the code's amount, the
                   automatic allocations, currency) in major units, `verified_via = ucp_checkout`,
                   `expires_at` = the earlier of the deal's end and now + DEAL_VERIFY_TTL_HOURS;
  - rejected       `failed` with the store's `discount_code_*` reason, plus the refusal hash. Never retried;
  - not_confirmed, not_evaluated, not_priced: not a verdict on the code. A row never verified stays `unverified`
                   with the reason and is retried after its backoff; a verified row keeps its verification (serving
                   stops at its expiry anyway) and records the reason;
  - the route refused the request (400, or `invalid`): `unverified` with `route:<reason>`, backed off 7 days, and the
                   run goes on;
  - the answer was lost (a transport error) or cannot be read (a 200 that is not JSON, or an unknown result); the
                   gateway may have built a cart: the same, and the run stops;
                   (in both route cases a verified row keeps its verification and records only the reason;)
  - 401, 404 or 5xx: the route's configuration or health, not the row's. Nothing is written, the slot stays spent,
                   and the run stops.
  - an applied answer whose cart is not whole and consistent (unit >= subtotal >= unit - code - automatic >= 0) is
                   recorded as not confirmed.
TIME: no cart is started with less than CART_HEADROOM_SECONDS left before the caller's deadline (the refresh job's
task timeout less a margin), nor after RUN_BUDGET_SECONDS.
Every write sets `updated_at`, and a later write never clears `verified_at`, `expires_at` or the reference amounts
(retention's contract: services/deal_code_retention.applied_history).

Counts only are returned and logged: never a code, a seller or a URL. Never raises: verifying can never fail the
refresh run.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import urlparse

from sqlalchemy import and_, func, or_, select

from config.settings import settings
from db.database import database
from db.deal_lane import deal_code_refusals, observed_store_offer_verifications as v, observed_store_offers as o
from services import deal_code_capture_writer as writer
from utils.availability_vocabulary import normalize_availability

logger = logging.getLogger(__name__)

MIN_KEY_CHARS = 43  # the route's own minimum (src/services/ucpDiscountVerifyInternalRoute.js MIN_KEY_CHARS)
VERIFIABLE_KINDS = ("percent", "amount")
# Minor-unit exponents of the currencies a served market prices in. A currency not listed is never verified.
MINOR_EXPONENT = {"USD": 2, "SGD": 2}
# The markets codes are verified in: the writer's market evidence (US/USD, SG/SGD), and only where the currency's
# minor unit is known here, so a market added there later is never sent a cart whose answer cannot be read (P3-3).
MARKET_CURRENCY = {m: c for m, c in writer._MARKET_CURRENCY.items() if c in MINOR_EXPONENT}
REVERIFY_WITHIN = timedelta(hours=3)
RETRY_AFTER = timedelta(hours=12)
# The store does not advertise the discount capability, or the route refused the request as malformed or answered
# nothing (a transport failure may still have built a cart): asking again soon will not change that.
NOT_EVALUATED_RETRY_AFTER = timedelta(days=7)
_LONG_BACKOFF_PREFIXES = ("not_evaluated", "route:")
REFUSAL_HASH_DAYS = 30
VERIFIED_VIA = "ucp_checkout"
AUTHORIZED_BY = "q2_approval"
# The route's own budget is 6 s (VERIFY_BUDGET_MS); a little more for the hop to the gateway.
CALL_TIMEOUT_SECONDS = 15.0
# Gap between two carts of one run (each on another host): the gateway's egress is shared with buyer checkouts.
CALL_SPACING_SECONDS = 1.0
# The whole step stops starting carts after this long.
RUN_BUDGET_SECONDS = 240.0
# A cart is started only while this much time is left before the caller's deadline: one call's timeout, the spacing,
# and the write after it.
CART_HEADROOM_SECONDS = CALL_TIMEOUT_SECONDS + CALL_SPACING_SECONDS + 15.0
# Candidate and seed reads are bounded.
MAX_CANDIDATES = 2000
MAX_SEEDS_PER_HOST = 200
_RESULTS = ("applied", "rejected", "not_confirmed", "not_evaluated", "not_priced")
_REASON_RE = re.compile(r"^[a-z0-9_:.-]{1,64}$")
_VARIANT_ID_RE = re.compile(r"^\d{6,20}$")


def enabled() -> bool:
    """Both flags, a full-length key and an https route URL: there is no default URL, so a staging job with the
    flags on can never send its key to the prod gateway (review P3-9)."""
    return (settings.deal_verify_job_enabled and settings.deal_verify_nonconnected_enabled
            and len(settings.deal_verify_internal_key) >= MIN_KEY_CHARS
            and settings.deal_verify_route_url.startswith("https://"))


def refusal_hash(seller: str, market: str, code: str) -> str:
    """sha256 over (seller, market, code), the code upper-cased as stores match it. Never the code itself."""
    raw = "deal_code_refusal:v1|{}|{}|{}".format(writer.normalize_host(seller), str(market or "").strip().upper(),
                                                str(code or "").strip().upper())
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _aware(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _decimal(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def from_minor(amount: Any, currency: str) -> Optional[Decimal]:
    """A UCP minor-unit integer amount in major units, or None (not an integer, negative, or an unknown currency)."""
    exponent = MINOR_EXPONENT.get(str(currency or ""))
    if exponent is None or isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
        return None
    return (Decimal(amount) / (Decimal(10) ** exponent)).quantize(Decimal(1) / (Decimal(10) ** exponent))


# ---- which codes are due ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    deal_id: str
    seller: str
    market: str
    code: str
    kind: str
    currency: Optional[str]
    min_subtotal: Optional[Decimal]
    ends_at: Optional[datetime]
    priority: int  # 0 never checked, 1 verified and expiring, 2 a retry
    order_at: datetime


def due(offer: Mapping[str, Any], verifications: Iterable[Mapping[str, Any]], now: datetime) -> Optional[Tuple[int, datetime]]:
    """(priority, order key) when this crawl row is due for a cart now, else None. Pure."""
    if str(offer.get("kind") or "") not in VERIFIABLE_KINDS:
        return None
    market = str(offer.get("market") or "")
    if market not in MARKET_CURRENCY or not str(offer.get("code") or "").strip():
        return None
    currency = offer.get("currency")
    if currency is not None and currency != MARKET_CURRENCY[market]:
        return None  # a stated amount or minimum in another currency is not this market's deal
    ends_at = _aware(offer.get("ends_at"))
    if ends_at is not None and ends_at <= now:
        return None
    starts_at = _aware(offer.get("starts_at"))
    if starts_at is not None and starts_at > now:
        return None
    rows = list(verifications)
    if not rows:
        return 0, _aware(offer.get("observed_at")) or now
    if any(r.get("state") == "failed" for r in rows):
        return None  # the store refused it: never retried
    live = [r for r in rows if r.get("state") == "checkout_verified" and _aware(r.get("expires_at"))]
    if live and max(_aware(r["expires_at"]) for r in live) > now + REVERIFY_WITHIN:
        return None
    last_attempt = max((_aware(r.get("updated_at")) or now for r in rows), default=now)
    reasons = [str(r.get("failure_reason") or "") for r in rows]
    backoff = NOT_EVALUATED_RETRY_AFTER if any(x.startswith(_LONG_BACKOFF_PREFIXES) for x in reasons) else RETRY_AFTER
    if live:
        # A verified row near its expiry is re-checked, unless the last re-check did not get an answer recently.
        if any(x for x in reasons) and last_attempt > now - backoff:
            return None
        return 1, min(_aware(r["expires_at"]) for r in live)
    if last_attempt > now - backoff:
        return None
    return 2, last_attempt


async def _not_verifiable_market_count(now: datetime) -> int:
    """Crawl rows the verifier would test (a code of a verifiable kind, not ended) but for their market: none, or one
    this verifier cannot test (GB, DE, ...). Counted so the summary names them (review N-6); rows it would skip
    anyway (free shipping, ended) are not counted, so the number is what the market rule alone leaves out."""
    row = await database.fetch_one(
        select(func.count()).select_from(o)
        .where(and_(writer.crawl_rows(), o.c.code.isnot(None), o.c.kind.in_(VERIFIABLE_KINDS),
                    or_(o.c.ends_at.is_(None), o.c.ends_at > now),
                    or_(o.c.market.is_(None), o.c.market.notin_(sorted(MARKET_CURRENCY))))))
    return int(row[0] or 0) if row is not None else 0


async def _candidates(now: datetime) -> List[Candidate]:
    rows = await database.fetch_all(
        select(o.c.deal_id, o.c.seller_id, o.c.market, o.c.code, o.c.kind, o.c.currency, o.c.min_subtotal,
               o.c.starts_at, o.c.ends_at, o.c.observed_at)
        .where(and_(writer.crawl_rows(), o.c.code.isnot(None), o.c.market.in_(sorted(MARKET_CURRENCY)),
                    o.c.kind.in_(VERIFIABLE_KINDS)))
        .order_by(o.c.observed_at.desc(), o.c.deal_id)
        .limit(MAX_CANDIDATES)
    )
    offers = [dict(r) for r in rows]
    if not offers:
        return []
    by_deal: Dict[str, List[Dict[str, Any]]] = {}
    ids = [r["deal_id"] for r in offers]
    for k in range(0, len(ids), 500):
        found = await database.fetch_all(
            select(v.c.deal_id, v.c.state, v.c.expires_at, v.c.updated_at, v.c.failure_reason)
            .where(v.c.deal_id.in_(ids[k:k + 500])))
        for r in found:
            by_deal.setdefault(r["deal_id"], []).append(dict(r))
    out: List[Candidate] = []
    for r in offers:
        if not str(r["deal_id"]).startswith(writer.CRAWL_DEAL_ID_PREFIX):
            continue
        when = due(r, by_deal.get(r["deal_id"], []), now)
        if when is None:
            continue
        out.append(Candidate(
            deal_id=r["deal_id"], seller=writer.normalize_host(r["seller_id"]), market=r["market"], code=r["code"],
            kind=r["kind"], currency=r["currency"], min_subtotal=_decimal(r["min_subtotal"]),
            ends_at=_aware(r["ends_at"]), priority=when[0], order_at=when[1]))
    # Never-checked: newest sighting first; expiring: soonest first; retries: oldest attempt first.
    out.sort(key=lambda c: (c.priority, -c.order_at.timestamp() if c.priority == 0 else c.order_at.timestamp(), c.deal_id))
    return out


# ---- the product the code is tested on -----------------------------------------------------------------------------

@dataclass(frozen=True)
class ReferenceVariant:
    variant_id: str
    unit_price: Decimal
    discovery_host: Optional[str]


# By host only: the seed's `market` column is a storage partition ('US' for every seed, SGD ones included), never
# its market. The market a seed serves is its own currency's (services.seed_serving.seed_row_market), checked per
# row in `pick_reference_variant` (review P1-1).
_SEEDS_SQL = (
    "SELECT id, domain, destination_url, canonical_url, price_currency, availability, seed_data "
    "FROM external_product_seeds "
    "WHERE status = 'active' "
    "AND (lower(domain) IN (:host, :www) "
    "     OR destination_url LIKE :p1 ESCAPE '\\' OR destination_url LIKE :p2 ESCAPE '\\' "
    "     OR canonical_url LIKE :p1 ESCAPE '\\' OR canonical_url LIKE :p2 ESCAPE '\\') "
    "ORDER BY id LIMIT :lim"
)


def _like(text_: str) -> str:
    return text_.replace("\\", "\\\\").replace("_", "\\_").replace("%", "\\%")


def _seed_host_form(seed: Mapping[str, Any], seller: str) -> Optional[str]:
    """The host form ("www." or bare) the seed's own URL uses on the seller's host, or None when it is another host."""
    for key in ("canonical_url", "destination_url"):
        raw = (urlparse(str(seed.get(key) or "")).hostname or "").lower().rstrip(".")
        if raw and writer.normalize_host(raw) == seller:
            return raw
    return None


def _seed_variants(seed_data: Any) -> List[Dict[str, Any]]:
    from services.catalog_variant_promoter import _extract_variants_from_seed

    return _extract_variants_from_seed(seed_data)


def pick_reference_variant(seeds: Iterable[Mapping[str, Any]], *, seller: str, market: str,
                           min_subtotal: Optional[Decimal]) -> Tuple[Optional[ReferenceVariant], str]:
    """The cheapest in-stock store variant on the seller's host for this market, or (None, why). Pure.

    A seed counts for the market its own currency serves (services.seed_serving.seed_row_market: USD -> US,
    SGD -> SG, never the partition column), and each variant's own currency, when it states one, must be that
    market's too."""
    from services.catalog_enrichment_agent.ingestion import variant_own_price
    from services.seed_serving import seed_row_currency, seed_row_market

    currency = MARKET_CURRENCY.get(market)
    best: Optional[Tuple[Decimal, int, str, Optional[str]]] = None
    priced_below_minimum = False
    for seed in seeds:
        form = _seed_host_form(seed, seller)
        if form is None:
            continue
        if currency is None or seed_row_market(seed) != market:
            continue
        seed_currency = seed_row_currency(seed)
        for variant in _seed_variants(seed.get("seed_data")):
            vid = str(variant.get("shopify_variant_id") or "").strip()
            if not _VARIANT_ID_RE.match(vid):
                continue
            v_currency = str(variant.get("price_currency") or variant.get("currency") or seed_currency or "").strip().upper()
            if v_currency != currency:
                continue
            stock = normalize_availability(variant.get("availability"))
            if stock is None:
                stock = normalize_availability(seed.get("availability"))
            if stock != "in_stock":
                continue
            price = variant_own_price(variant)
            if price is None:
                continue
            amount = _decimal(price)
            if amount is None or amount <= 0:
                continue
            if min_subtotal is not None and amount < min_subtotal:
                priced_below_minimum = True
                continue
            key = (amount, len(vid), vid, form if form != seller else None)
            if best is None or key[:3] < best[:3]:
                best = key
    if best is None:
        return None, ("min_subtotal_unmet" if priced_below_minimum else "no_reference_variant")
    return ReferenceVariant(variant_id=best[2], unit_price=best[0], discovery_host=best[3]), ""


async def _host_seeds(host: str) -> List[Dict[str, Any]]:
    params = {"host": host, "www": "www." + host,
              "p1": f"https://{_like(host)}/%", "p2": f"https://www.{_like(host)}/%", "lim": MAX_SEEDS_PER_HOST}
    rows = await database.fetch_all(_SEEDS_SQL, params)
    seeds = []
    for r in rows:
        seed = dict(r)
        data = seed.get("seed_data")
        if isinstance(data, str):
            try:
                seed["seed_data"] = json.loads(data)
            except ValueError:
                seed["seed_data"] = {}
        seeds.append(seed)
    return seeds


async def reference_variant(c: Candidate, cache: Optional[Dict[str, List[Dict[str, Any]]]] = None
                            ) -> Tuple[Optional[ReferenceVariant], str]:
    """The reference variant for a candidate. `cache` holds each host's seeds for one run (review P3-6)."""
    if cache is not None and c.seller in cache:
        seeds = cache[c.seller]
    else:
        seeds = await _host_seeds(c.seller)
        if cache is not None:
            cache[c.seller] = seeds
    return pick_reference_variant(seeds, seller=c.seller, market=c.market, min_subtotal=c.min_subtotal)


# ---- the daily cart cap --------------------------------------------------------------------------------------------

_RESERVE_SQL = (
    "INSERT INTO deal_verify_host_daily_carts (host, utc_day, carts, updated_at) VALUES (:host, :day, 1, :now) "
    "ON CONFLICT (host, utc_day) DO UPDATE SET carts = deal_verify_host_daily_carts.carts + 1, updated_at = :now "
    "WHERE deal_verify_host_daily_carts.carts < :cap "
    "RETURNING carts"
)


async def reserve_cart(host: str, now: datetime) -> bool:
    """True when a cart slot for this host today was taken. One statement: two runs cannot both take the last slot."""
    cap = settings.deal_verify_max_carts_per_host_per_day
    row = await database.fetch_one(_RESERVE_SQL, {"host": host, "day": now.date(), "now": now, "cap": cap})
    return row is not None


# ---- the call ------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Answer:
    # one of _RESULTS; "refused_request" (the route refused this row's request); "unreadable_answer" (a 200 this
    # verifier cannot read); "transport_error"; or "error"
    result: str
    reason: str
    body: Dict[str, Any]


def _token(value: Any) -> str:
    return value if isinstance(value, str) and _REASON_RE.match(value) else ""


def read_answer(status: int, body: Any) -> Answer:
    """The route's answer. Pure.
      - a 200 with a known result: that result;
      - a 400, or a 200 answering `invalid`: `refused_request`, the route's reason kept. The request for THIS row is
        what it refused (a host it never contacts, a code it cannot send), so the row backs off (review P2-1);
      - a 200 whose body is not a JSON object, or whose result is unknown (a gateway newer than this verifier):
        `unreadable_answer`. The gateway answered, so a cart was likely built: the row is recorded and backs off,
        and the run stops (review N-1);
      - anything else (401, 404, 5xx): `error`. That is the route's configuration or health, not the row's: nothing
        is written and the run stops."""
    data = body if isinstance(body, dict) else {}
    if status == 400 or (status == 200 and data.get("result") == "invalid"):
        return Answer("refused_request", _token(data.get("reason")) or "invalid_request", {})
    if status != 200:
        return Answer("error", f"http_{status}", {})
    if not isinstance(body, dict):
        return Answer("unreadable_answer", "unreadable", {})
    result = body.get("result")
    if result not in _RESULTS:
        return Answer("unreadable_answer", "unknown_result", {})
    return Answer(result, _token(body.get("reason")), body)


async def call_route(c: Candidate, ref: ReferenceVariant, *, client=None) -> Answer:
    import httpx

    payload = {"seller_host": c.seller, "variant_gid": ref.variant_id, "market": c.market, "code": c.code,
               "expected_currency": MARKET_CURRENCY[c.market]}
    if ref.discovery_host:
        payload["discovery_host"] = ref.discovery_host
    headers = {"X-Internal-Key": settings.deal_verify_internal_key, "Content-Type": "application/json"}

    async def _post(cl) -> Answer:
        resp = await cl.post(settings.deal_verify_route_url, json=payload, headers=headers)
        try:
            body = resp.json()
        except ValueError:
            body = None
        return read_answer(resp.status_code, body)

    try:
        if client is not None:
            return await _post(client)
        async with httpx.AsyncClient(timeout=CALL_TIMEOUT_SECONDS, follow_redirects=False) as owned:
            return await _post(owned)
    except Exception as exc:  # noqa: BLE001 - a failed call is counted, never raised
        # The gateway may have built a cart before the answer was lost: recorded on the row, which backs off.
        return Answer("transport_error", "transport_" + type(exc).__name__.lower()[:40], {})


# ---- the record ----------------------------------------------------------------------------------------------------

def verified_values(answer: Answer, c: Candidate, ref: ReferenceVariant, now: datetime) -> Tuple[Optional[dict], str]:
    """The `checkout_verified` columns for an applied answer, or (None, why) when the answer does not carry a whole
    reference cart in the market's currency (then it is recorded as not confirmed). Pure."""
    body = answer.body
    currency = MARKET_CURRENCY[c.market]
    if body.get("currency") != currency:
        return None, "currency_not_market"
    totals = body.get("totals") if isinstance(body.get("totals"), dict) else {}
    unit = from_minor(body.get("unit_price"), currency)
    subtotal = from_minor(totals.get("subtotal"), currency)
    discount = body.get("discount") if isinstance(body.get("discount"), dict) else {}
    amount = from_minor(discount.get("amount"), currency)
    if unit is None or subtotal is None or amount is None or unit <= 0:
        return None, "applied_incomplete"
    if amount <= 0:
        return None, "applied_without_amount"
    automatic = []
    automatic_amount = Decimal(0)
    for item in body.get("automatic") if isinstance(body.get("automatic"), list) else []:
        a = from_minor(item.get("amount") if isinstance(item, dict) else None, currency)
        if a is None:
            return None, "automatic_unreadable"
        # One unit: a line and an order allocation both come off that unit. The route does not say which, and the
        # deal decision reads either the same way (services/deal_lane_decision.automatic_total).
        automatic.append({"title": "", "type": "line", "amount": str(a), "currency": currency})
        automatic_amount += a
    # The deal decision's own check (services/deal_lane_decision: a subtotal outside [effective price, unit price]
    # is refused): a cart that fails it is not recorded as verified (review P3-2), e.g. a discount above the subtotal.
    effective = unit - amount - automatic_amount
    if effective < 0 or not (effective <= subtotal <= unit):
        return None, "reference_cart_inconsistent"
    ttl = timedelta(hours=settings.deal_verify_ttl_hours)
    expires = now + ttl if c.ends_at is None else min(c.ends_at, now + ttl)
    if expires <= now:
        return None, "deal_ended"
    return {
        "state": "checkout_verified", "verified_at": now, "expires_at": expires, "verified_via": VERIFIED_VIA,
        "authorized_by": AUTHORIZED_BY, "ref_quantity": 1, "ref_unit_price": unit, "ref_compare_at_price": None,
        "ref_subtotal": subtotal, "ref_discount_amount": amount, "ref_currency": currency,
        "ref_automatic_discounts": automatic, "failure_reason": None,
    }, ""


async def _existing(deal_id: str, variant_id: str):
    return await database.fetch_one(
        select(v.c.state).where(and_(v.c.deal_id == deal_id, v.c.variant_id == variant_id)))


async def _write(deal_id: str, variant_id: str, values: dict, now: datetime) -> None:
    # Every row a cart produced records the route and the permission that allowed that cart (ADR-027 D6), whatever
    # the store answered.
    values = {**values, "verified_via": VERIFIED_VIA, "authorized_by": AUTHORIZED_BY, "updated_at": now}
    existing = await _existing(deal_id, variant_id)
    if existing is None:
        try:
            await database.execute(v.insert().values(deal_id=deal_id, variant_id=variant_id, created_at=now, **values))
            return
        except Exception:  # noqa: BLE001 - another writer inserted the same (deal, variant) first: update that row
            if await _existing(deal_id, variant_id) is None:
                raise
    await database.execute(v.update().where(and_(v.c.deal_id == deal_id, v.c.variant_id == variant_id)).values(**values))


async def record(answer: Answer, c: Candidate, ref: ReferenceVariant, now: datetime) -> str:
    """Write what the store (or the route) said. Returns the count name."""
    if answer.result in ("refused_request", "transport_error", "unreadable_answer"):
        # Not the store's verdict: the row backs off 7 days ("route:" prefix, `due`), and a verified row keeps its
        # verification as on any other no-answer.
        answer = Answer("route", answer.reason, {})
    if answer.result == "applied":
        values, why = verified_values(answer, c, ref, now)
        if values is not None:
            await _write(c.deal_id, ref.variant_id, values, now)
            return "applied"
        answer = Answer("not_confirmed", why, {})
    if answer.result == "rejected":
        reason = ("rejected:" + answer.reason) if answer.reason else "rejected"
        await _write(c.deal_id, ref.variant_id, {"state": "failed", "failure_reason": reason[:128]}, now)
        await _remember_refusal(c, reason, now)
        return "rejected"
    reason = (answer.result + ":" + answer.reason) if answer.reason else answer.result
    if answer.result == "route":
        reason = "route:" + (answer.reason or "refused")
    existing = await _existing(c.deal_id, ref.variant_id)
    if existing is not None and existing["state"] == "checkout_verified":
        # Not a verdict: the verification stands until its own expiry; the attempt is recorded.
        await _write(c.deal_id, ref.variant_id, {"failure_reason": reason[:128]}, now)
    else:
        await _write(c.deal_id, ref.variant_id, {"state": "unverified", "failure_reason": reason[:128]}, now)
    return "route_" + answer.reason[:48] if answer.result == "route" else answer.result


async def _remember_refusal(c: Candidate, reason: str, now: datetime) -> None:
    h = refusal_hash(c.seller, c.market, c.code)
    if await database.fetch_one(select(deal_code_refusals.c.refusal_hash).where(deal_code_refusals.c.refusal_hash == h)):
        await database.execute(deal_code_refusals.update().where(deal_code_refusals.c.refusal_hash == h)
                               .values(reason=reason[:128], refused_at=now))
        return
    try:
        await database.execute(deal_code_refusals.insert().values(refusal_hash=h, reason=reason[:128], refused_at=now))
    except Exception:  # noqa: BLE001 - inserted meanwhile: refresh it
        await database.execute(deal_code_refusals.update().where(deal_code_refusals.c.refusal_hash == h)
                               .values(reason=reason[:128], refused_at=now))


async def _refused_recently(c: Candidate, now: datetime) -> bool:
    h = refusal_hash(c.seller, c.market, c.code)
    row = await database.fetch_one(
        select(deal_code_refusals.c.refused_at).where(and_(
            deal_code_refusals.c.refusal_hash == h,
            deal_code_refusals.c.refused_at >= now - timedelta(days=REFUSAL_HASH_DAYS))))
    return row is not None


# ---- the step ------------------------------------------------------------------------------------------------------

async def run(now: Optional[datetime] = None, *, client=None, sleep=asyncio.sleep,
              monotonic=time.monotonic, deadline: Optional[float] = None) -> Dict[str, int]:
    """Verify the due codes once. Never raises. Returns counts ({} when the verifier is off).

    `deadline` is a `monotonic()` time the step must be done by (the refresh job passes its task timeout less a
    margin, review P2-2): no cart is started with less than CART_HEADROOM_SECONDS left before it."""
    if not enabled():
        return {}
    counts: Dict[str, int] = {}

    def bump(name: str, n: int = 1) -> None:
        counts[name] = counts.get(name, 0) + n

    try:
        now = now or datetime.now(timezone.utc)
        try:
            await database.fetch_one(select(deal_code_refusals.c.refusal_hash).limit(1))
        except Exception as exc:  # noqa: BLE001 - without the refusal record a dead code could be tested again
            bump("refusals_unreadable")
            logger.warning("deal code verifier: refusal table unreadable: %s", type(exc).__name__)
            return counts
        try:
            deny = await writer.read_denyset()
        except Exception as exc:  # noqa: BLE001 - an opt-out we cannot read means no cart
            bump("optout_unreadable")
            logger.warning("deal code verifier: opt-out check failed: %s", type(exc).__name__)
            return counts
        candidates = await _candidates(now)
        bump("due", len(candidates))
        out_of_market = await _not_verifiable_market_count(now)
        if out_of_market:
            bump("skipped_market_not_verifiable", out_of_market)
        per_run = settings.deal_verify_max_carts_per_run
        carts = 0
        hosts_done: set = set()
        started = monotonic()
        seeds_by_host: Dict[str, List[Dict[str, Any]]] = {}
        stopped = False
        for c in candidates:
            if stopped:
                bump("left_route_stopped")
                continue
            if deadline is not None and monotonic() + CART_HEADROOM_SECONDS > deadline:
                bump("left_job_deadline")
                continue
            if carts >= per_run:
                bump("left_for_next_run")
                continue
            if monotonic() - started > RUN_BUDGET_SECONDS:
                bump("left_over_budget")
                continue
            if c.seller in hosts_done:
                bump("skipped_host_this_run")
                continue
            try:
                if deny.blocks(c.seller):
                    bump("skipped_optout")
                    continue
                if await _refused_recently(c, now):
                    bump("skipped_refused_recently")
                    continue
                ref, why = await reference_variant(c, seeds_by_host)
                if ref is None:
                    bump("skipped_" + why)
                    continue
                if not await reserve_cart(c.seller, now):
                    bump("skipped_host_cap")
                    hosts_done.add(c.seller)
                    continue
                hosts_done.add(c.seller)
                if carts:
                    await sleep(CALL_SPACING_SECONDS)
                carts += 1
                answer = await call_route(c, ref, client=client)
                if answer.result == "error":
                    # 401, 404, 5xx: the route's configuration or health, not this row. Nothing is written, and the
                    # run stops: spending the next hosts' slots would not help (review P2-1).
                    bump("route_errors")
                    bump("route_" + answer.reason[:48])
                    stopped = True
                    continue
                if answer.result in ("transport_error", "unreadable_answer"):
                    bump("route_errors")
                    stopped = True  # recorded below (a cart may exist), and then no more calls this run
                bump(await record(answer, c, ref, now))
            except Exception as exc:  # noqa: BLE001 - one code's failure never costs the others or the run
                bump("errors")
                logger.warning("deal code verifier failed: %s", type(exc).__name__)
        bump("carts", carts)
    except Exception as exc:  # noqa: BLE001 - verifying never fails the refresh run
        bump("errors")
        logger.warning("deal code verifier failed: %s", type(exc).__name__)
    if counts:
        logger.warning("deal code verifier: %s", {k: n for k, n in counts.items() if n})
    return counts
