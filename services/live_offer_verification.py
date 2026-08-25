"""Live-verify the top-K shortlist against the merchant, before we hand a buyer off.

WHAT THIS IS FOR. The audit's headline finding: on a 90-SKU live sample, **31.1% of index records
would produce a wrong or unexecutable spec** — 12.3% price mismatch, 11.1% listed-but-out-of-stock,
6.7% dead PDP (a later 1,000-row measurement put dead handles at 14.5%). Every other freshness
lever is a cadence we cannot run often enough: 43% of live PDPs carry an active markdown, so list
price is wrong on nearly half of them at any moment. The only fix that survives that arithmetic is
to stop serving a remembered price at the moment it matters and ask the merchant.

WHAT IT DELIBERATELY IS NOT. `verifyPrice` in the reco lane calls Pivota's OWN `get_pdp_v2`, so
`price_verified: true` there means "consistent with our own projection", not "matches the
merchant". This module asks the merchant's storefront. The two must not be confused: one of them
can be true while the buyer is charged something else.

BUDGET, and why partial results matter. Top-K only (K=3), in parallel, hard-capped at 1.5s. The cap
is enforced with `asyncio.wait`, NOT `wait_for` around a `gather`: a gather that times out cancels
every task, so one slow merchant would cost the verdicts of two fast ones. Whatever finished inside
the budget is kept.

WHAT IT VERIFIES, AND WHAT IT DELIBERATELY DOES NOT. Shopify's `/products/<handle>.js` returns the
shop's DEFAULT-currency amount in minor units and carries NO currency code. So this source can
establish EXISTENCE and STOCK — both currency-free facts — but it cannot establish PRICE: writing
its number onto an offer quoted in another currency publishes ¥4500 as $4500. That is the
amount-without-its-currency class this repo has already fixed twice (see `_observed_currency` in
agent_shop_gateway). Verifying price needs a currency-bearing source, which is UCP `create_checkout`
— audit item 9. Until then the live amount is carried as INFORMATIONAL only and never replaces the
quoted one. That still addresses the larger half of the 31.1%: 11.1% out-of-stock plus the dead PDPs.

DEGRADATION IS NEVER SILENT (audit F3):
  * `verified`   — the merchant answered and the item is buyable. Stock is replaced; the price is
                   NOT (see above).
  * `gone`       — 404, or the variant is absent or out of stock. DROP the offer and take the
                   next-best merchant; we have duplicate coverage of many products.
  * `unverified` — timed out, blocked, or not a storefront we can read. Keep the snapshot, but the
                   caller must demote it and must not return it as rank 1.

REQUEST-PATH TRAFFIC ON THE SHARED CRAWL IP. This is the primary consumer of the egress isolation
in §3.2, so every fetch goes through `crawl_politeness` with the BOUNDED wait — never `max_wait=0`.
An unbounded pace wait here would be #1854's P1 re-introduced on a live path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

from services import crawl_politeness
from services.shopify_variant_identity import (
    parse_product_js,
    product_js_url,
    storefront_is_shopify,
)

logger = logging.getLogger(__name__)

VERIFIED = "verified"
UNVERIFIED = "unverified"
GONE = "gone"

_DEFAULT_TOP_K = 3
_DEFAULT_DEADLINE_S = 1.5
_DEFAULT_CACHE_TTL_S = 90.0          # audit F2 says 60-120s
_DEFAULT_FETCH_TIMEOUT_S = 1.2       # inside the batch deadline, so one fetch cannot eat it all
_MAX_REDIRECTS = 3
_USER_AGENT = os.getenv("EXTERNAL_OFFER_USER_AGENT") or "Mozilla/5.0 (compatible; PivotaBot/1.0; +https://pivota.cc)"

# A price is "the same" if it rounds to the same cent. Anything looser would let a real markdown
# read as noise, which is the exact error this module exists to catch.
_PRICE_EPSILON = Decimal("0.005")


def _f(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.getenv(name) or default))
    except (TypeError, ValueError):
        return default


def is_enabled() -> bool:
    """Default OFF. This adds request-path egress to third parties; it gets armed deliberately."""
    return str(os.getenv("LIVE_OFFER_VERIFICATION_ENABLED", "false")).strip().lower() in {
        "1", "true", "yes", "on",
    }


@dataclass
class Verdict:
    status: str
    reason: str
    source: str = "shopify_product_js"
    live_price: Optional[Decimal] = None
    live_currency: Optional[str] = None
    in_stock: Optional[bool] = None
    price_changed: bool = False
    # Separate from `status` on purpose. A verdict can establish STOCK and not PRICE — that is the
    # normal case when the shop's currency differs from the one the offer quoted, or when
    # /meta.json did not answer. Folding the two into one flag is what produced the ¥4500-as-$4500
    # bug in the first cut.
    price_verified: bool = False
    checked_at: float = field(default_factory=time.time)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "source": self.source,
            "live_price": (float(self.live_price) if self.live_price is not None else None),
            "live_currency": self.live_currency,
            "in_stock": self.in_stock,
            "price_changed": self.price_changed,
            "price_verified": self.price_verified,
        }


# --- cache ------------------------------------------------------------------------------------
#
# Per-URL, short-TTL, shared where possible. Memorystore is the right home on GCP because the
# service autoscales — a per-process cache would give each instance its own miss rate and multiply
# the outbound requests this module is trying to keep small. The in-process map is the fallback,
# not the design.

_LOCAL: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_LOCAL_MAX = 5_000


def _cache_key(url: str) -> str:
    """Keyed by URL ALONE.

    The fetched document contains EVERY variant, so keying per variant made three variants of one
    product cost three identical fetches — and the external builder routinely emits exactly that
    shape (one offer per variant of one seed). Keying per URL also removes any chance of one
    variant's facts being served for another, because the variant is selected from the cached
    document per offer rather than baked into the key.
    """
    return f"lov:doc:{url}"


async def _cache_get(key: str) -> Optional[Dict[str, Any]]:
    try:
        from utils.redis_client import get_redis_client

        client = get_redis_client()
        if client is not None:
            raw = await client.get(key)
            if raw:
                return json.loads(raw)
            return None
    except Exception as exc:  # noqa: BLE001 - a cache outage must never fail a verification
        logger.debug("live-verify cache read failed (%s); falling back to local", exc)

    hit = _LOCAL.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    return None


async def _cache_put(key: str, payload: Dict[str, Any], *, ttl: Optional[float] = None) -> None:
    ttl = ttl if ttl is not None else _f(
        "LIVE_OFFER_VERIFICATION_CACHE_TTL_SECONDS", _DEFAULT_CACHE_TTL_S
    )
    if ttl <= 0:
        return
    try:
        from utils.redis_client import get_redis_client

        client = get_redis_client()
        if client is not None:
            await client.set(key, json.dumps(payload), ex=int(ttl))
            return
    except Exception as exc:  # noqa: BLE001
        logger.debug("live-verify cache write failed (%s); falling back to local", exc)

    if len(_LOCAL) > _LOCAL_MAX:
        _LOCAL.clear()
    _LOCAL[key] = (time.monotonic() + ttl, payload)


def reset_for_tests() -> None:
    _LOCAL.clear()
    _CURRENCY_REFRESHING.clear()


# --- the shop's currency ----------------------------------------------------------------------
#
# `/products/<handle>.js` carries an amount with NO currency code, which is why the first cut of
# this module refused to verify price at all. Shopify does publish the shop's default currency,
# cheaply and statically, at `/meta.json`.
#
# MEASURED before relying on it (2026-08-25, live): celimax.jp -> JPY, arencia.jp -> USD despite
# the .jp TLD, goongbe.us -> USD with country KR. So the domain is NOT a currency signal and
# `/meta.json` is — which is exactly why guessing from the TLD was never an option.
#
# ALSO MEASURED, because the parser divides by 100: Shopify stores JPY in hundredths like every
# other currency. celimax.jp's `.js` says 319000, the PDP renders ¥3,190 and its JSON-LD says
# JPY 3190. The existing conversion is correct for a zero-decimal currency; the concern that it
# might not be was unfounded, and is recorded here so nobody re-opens it from first principles.

_DEFAULT_CURRENCY_TTL_S = 86_400.0    # a shop's default currency changes ~never
# A host that cannot answer is remembered too, but briefly. Without this an unanswerable host is
# re-asked once per offer per turn, forever — and those retries feed the shared per-host backoff,
# so a 429 on an endpoint that contributes NOTHING to stock verification drove product
# verification to zero. Short enough that a fixed shop recovers without a deploy.
_DEFAULT_CURRENCY_MISS_TTL_S = 600.0

# Hosts with a currency refresh already in flight, so N offers on one domain spawn ONE.
_CURRENCY_REFRESHING: set = set()


def _currency_ttl() -> float:
    return _f("LIVE_OFFER_VERIFICATION_CURRENCY_TTL_SECONDS", _DEFAULT_CURRENCY_TTL_S)


def _currency_miss_ttl() -> float:
    return _f("LIVE_OFFER_VERIFICATION_CURRENCY_MISS_TTL_SECONDS", _DEFAULT_CURRENCY_MISS_TTL_S)


async def _fetch_shop_currency(host: str) -> None:
    """Populate the currency cache for `host`. Runs OFF the request's critical path.

    THE COST MODEL IS THE WHOLE POINT. A shop's default currency is a per-DOMAIN fact that changes
    ~never, and fetching it inside a per-OFFER task bounded at 1.5s was measured to make this
    feature net-negative: one offer then needed two 1s-paced requests against a 1.5s cap, so on a
    cold cache the currency leg never completed AND it stole the budget the product fetch needed —
    stock verification fell from 11/12 to 7/12 while price verification never fired once.

    So the verdict never waits for this. A cold cache simply means "price unverified this turn",
    which is exactly #1868's behaviour — the floor is no-regression — and the next turn for that
    domain can compare. Refreshes are deduplicated per host so a shortlist of N offers on one
    domain spawns one.
    """
    url = f"https://{host}/meta.json"
    currency: Optional[str] = None
    try:
        # Bounded, like every other outbound request here — never `max_wait=0`.
        await crawl_politeness.before_request(url, user_agent=_USER_AGENT)
        timeout = _f("LIVE_OFFER_VERIFICATION_FETCH_TIMEOUT_SECONDS", _DEFAULT_FETCH_TIMEOUT_S)
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, max_redirects=_MAX_REDIRECTS
        ) as client:
            resp = await client.get(
                url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"}
            )
        crawl_politeness.note_response(
            url, resp.status_code, retry_after=resp.headers.get("retry-after")
        )
        if resp.status_code == 200:
            raw = str((resp.json() or {}).get("currency") or "").strip().upper()
            if len(raw) == 3 and raw.isalpha():
                currency = raw
    except Exception as exc:  # noqa: BLE001 - never surfaces; the verdict does not depend on it
        logger.info("shop currency unavailable for %s: %s", host, exc)
    finally:
        # RELEASED IN A `finally`, including on cancellation. The claim is what stops N offers
        # spawning N fetches; leaking it would leave the host claimed forever, so its currency
        # would never be looked up again and price verification for that shop would be
        # permanently off — a silent, unrecoverable state.
        _CURRENCY_REFRESHING.discard(host)

    # A miss is cached too, briefly. See _DEFAULT_CURRENCY_MISS_TTL_S.
    await _cache_put(
        f"lov:cur:{host}",
        {"currency": currency},
        ttl=_currency_ttl() if currency else _currency_miss_ttl(),
    )


async def _shop_currency(host: str) -> Optional[str]:
    """The shop's currency IF we already know it. Never blocks; schedules a refresh on a miss."""
    cached = await _cache_get(f"lov:cur:{host}")
    if cached is not None:
        return cached.get("currency")

    # The host is claimed SYNCHRONOUSLY, before the task is created. `ensure_future` only
    # schedules the coroutine — its body does not run until the loop yields — so a check made
    # inside `_fetch_shop_currency` let every offer in the same tick spawn its own fetch. Measured:
    # 3 offers on one domain produced 3 identical /meta.json requests, which then consumed the
    # host's pacing slots and starved the product fetches they were supposed to complement.
    if host in _CURRENCY_REFRESHING:
        return None
    _CURRENCY_REFRESHING.add(host)

    # Fire and forget, deliberately outside the batch deadline. The done-callback consumes the
    # result so a failure cannot surface as "exception was never retrieved".
    try:
        task = asyncio.ensure_future(_fetch_shop_currency(host))
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
    except RuntimeError:  # pragma: no cover - no running loop
        _CURRENCY_REFRESHING.discard(host)
    return None


# --- the check ---------------------------------------------------------------------------------


def _quoted_price(offer: Dict[str, Any]) -> Optional[Decimal]:
    raw = offer.get("price")
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except Exception:  # noqa: BLE001
        return None


def _price_moved(live: Optional[Decimal], offer: Dict[str, Any]) -> bool:
    """Did the merchant's price move away from what THIS offer quoted?

    Per-offer by construction: two offers can name the same product at different quoted prices,
    so this can never be cached alongside the product facts.
    """
    quoted = _quoted_price(offer)
    if live is None or quoted is None:
        return False
    return abs(live - quoted) > _PRICE_EPSILON


def _target(offer: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """(product .js url, numeric variant id) for an offer, or (None, None) if unverifiable.

    Read from `execution_spec` when present — that is the contract we published to the agent, so
    verifying anything else would check a different claim from the one we made.
    """
    spec = offer.get("execution_spec")
    page_url = None
    variant_id = None
    if isinstance(spec, dict):
        page_url = spec.get("pdp_url")
        variant_id = spec.get("variant_id")
    source = offer.get("source")
    if not page_url and isinstance(source, dict):
        page_url = source.get("canonical_url") or source.get("destination_url")
    if not page_url:
        return None, None
    # The published pdp_url carries our own tracking params. `product_js_url` already derives the
    # endpoint from the path alone, so it is what actually prevents the leak — this strip is
    # defence-in-depth against that changing, not the guard itself. (Stated precisely because a
    # mutation run showed removing the strip changes nothing: the protection is one layer down.)
    return product_js_url(str(page_url).split("?", 1)[0]), (str(variant_id) if variant_id else None)


async def _check_one(
    offer: Dict[str, Any], *, max_wait: Optional[float] = None
) -> Verdict:
    js_url, variant_id = _target(offer)
    if not js_url:
        return Verdict(UNVERIFIED, "no_verifiable_url")

    # S3: `gone` DELETES a merchant from the shortlist, so it may only be concluded from positive
    # evidence that this is a Shopify storefront. `/products/<slug>` in a path is a URL SHAPE, not
    # a platform: headless Hydrogen, Squarespace `/store/products/`, SFCC `/products/x.html` and
    # any WAF that 404s an unknown UA all match it and none serve the `.js` route. Without
    # evidence a 404 means "we could not ask", not "it is gone".
    seed = offer.get("source") if isinstance(offer.get("source"), dict) else {}
    shopify_evidenced = storefront_is_shopify(seed.get("seed_data") or seed)

    doc = await _cache_get(_cache_key(js_url))
    if doc is None:
        try:
            # S1: the gate is given the CALLER'S remaining budget, not its own 10s default.
            # `await_slot` refuses BEFORE reserving, so an over-budget host answers `paced_out`
            # immediately instead of reserving a slot and then being cancelled at the deadline —
            # which left the reservation consumed and pushed every later turn further out. Three
            # 1s-paced requests cannot fit a 1.5s batch, so under the defaults that backlog grew
            # monotonically and verification collapsed to ~8%.
            await crawl_politeness.before_request(
                js_url, user_agent=_USER_AGENT, max_wait=max_wait
            )
        except crawl_politeness.RobotsDisallowed:
            return Verdict(UNVERIFIED, "robots_disallowed")
        except crawl_politeness.CrawlPaced:
            return Verdict(UNVERIFIED, "paced_out")

        timeout = _f("LIVE_OFFER_VERIFICATION_FETCH_TIMEOUT_SECONDS", _DEFAULT_FETCH_TIMEOUT_S)
        try:
            # `max_redirects` capped for the same reason crawl_politeness caps it: the gate is
            # consulted ONCE, before hop 1, so httpx's default of 20 would turn one paced request
            # into up to 21 unpaced ones from the shared crawl NAT IP — and no intermediate hop
            # reaches `note_response`, so a 429 mid-chain never feeds the backoff.
            async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=True, max_redirects=_MAX_REDIRECTS
            ) as client:
                resp = await client.get(
                    js_url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"}
                )
            crawl_politeness.note_response(
                js_url, resp.status_code, retry_after=resp.headers.get("retry-after")
            )
        except Exception as exc:  # noqa: BLE001 - a failed check degrades, it never raises
            logger.info("live-verify fetch failed for %s: %s", js_url, exc)
            return Verdict(UNVERIFIED, "fetch_failed")

        if resp.status_code == 404:
            if not shopify_evidenced:
                return Verdict(UNVERIFIED, "not_a_known_shopify_storefront")
            doc = {"status": 404, "variants": []}
        elif resp.status_code != 200:
            # NOT cached: a 5xx is transient, and caching it would keep a recovering merchant
            # out of the shortlist for the whole TTL.
            return Verdict(UNVERIFIED, f"http_{resp.status_code}")
        else:
            try:
                doc = {"status": 200, "variants": parse_product_js(resp.json())}
            except Exception:  # noqa: BLE001
                return Verdict(UNVERIFIED, "unparseable")
        await _cache_put(_cache_key(js_url), doc)

    if doc.get("status") == 404:
        return Verdict(GONE, "pdp_404")

    variants = doc.get("variants") or []
    if not variants:
        return Verdict(UNVERIFIED, "no_variants")

    if variant_id:
        chosen = next((v for v in variants if v.get("shopify_variant_id") == variant_id), None)
        if chosen is None:
            if not shopify_evidenced:
                return Verdict(UNVERIFIED, "not_a_known_shopify_storefront")
            # We published a variant id the storefront no longer lists: the cart permalink we
            # handed out cannot be built any more.
            return Verdict(GONE, "variant_absent")
    elif len(variants) == 1:
        chosen = variants[0]
    else:
        # No variant id and more than one option: we cannot say WHICH one we quoted, so we do not
        # get to claim it is in stock. Guessing here is how a buyer lands on the wrong shade.
        return Verdict(UNVERIFIED, "ambiguous_variant")

    if not chosen.get("available"):
        return Verdict(GONE, "out_of_stock", in_stock=False)

    live_price = chosen.get("price_amount")
    live = Decimal(str(live_price)) if live_price is not None else None

    # The amount is only a PRICE once we know its unit. `/products/<handle>.js` carries no
    # currency code, so the shop's default is read separately (one cached fetch per domain) and
    # the comparison is made only when it matches the currency this offer quoted. A mismatch is
    # not an error — a JPY shop and a USD-presentment offer are both correct — it simply means
    # this source cannot speak to that offer's price.
    host = crawl_politeness.host_of(js_url)
    shop_currency = await _shop_currency(host) if host else None
    offer_currency = str(offer.get("currency") or "").strip().upper() or None
    comparable = bool(
        live is not None and shop_currency and offer_currency and shop_currency == offer_currency
    )

    return Verdict(
        VERIFIED, "ok",
        live_price=live,
        live_currency=shop_currency,
        in_stock=True,
        price_changed=_price_moved(live, offer) if comparable else False,
        price_verified=comparable,
    )


async def verify_offers(
    offers: List[Dict[str, Any]],
    *,
    top_k: Optional[int] = None,
    deadline_s: Optional[float] = None,
) -> Dict[int, Verdict]:
    """Verify the first `top_k` offers in parallel within `deadline_s`. Keyed by list index.

    Offers past `top_k` are not checked at all and simply get no verdict — the caller decides what
    an absent verdict means, which is deliberately not the same as `unverified`.
    """
    if not offers or not is_enabled():
        return {}

    k = int(top_k if top_k is not None else _f("LIVE_OFFER_VERIFICATION_TOP_K", _DEFAULT_TOP_K))
    budget = float(
        deadline_s if deadline_s is not None
        else _f("LIVE_OFFER_VERIFICATION_DEADLINE_SECONDS", _DEFAULT_DEADLINE_S)
    )
    if k <= 0 or budget <= 0:
        return {}

    targets = list(enumerate(offers))[:k]
    # The gate is handed the BATCH budget, not its own 10s default. `await_slot` refuses before
    # reserving, so a host that cannot be served inside this turn says so immediately instead of
    # consuming a slot it will be cancelled out of.
    tasks = {
        asyncio.ensure_future(_check_one(o, max_wait=budget)): i for i, o in targets
    }

    # `asyncio.wait`, not `wait_for(gather(...))`. A gather that times out cancels EVERY task, so
    # one slow merchant would throw away the verdicts of the two fast ones. Partial results are
    # the whole point of a hard cap.
    done, pending = await asyncio.wait(tasks.keys(), timeout=budget)

    out: Dict[int, Verdict] = {}
    for task in done:
        index = tasks[task]
        try:
            out[index] = task.result()
        except Exception as exc:  # noqa: BLE001
            logger.info("live-verify task failed: %s", exc)
            out[index] = Verdict(UNVERIFIED, "check_errored")
    for task in pending:
        task.cancel()
        out[tasks[task]] = Verdict(UNVERIFIED, "deadline_exceeded")

    return out


_EXPECTED_TOTAL_TTL_S = 300   # audit F3: a verified spec carries a 5-minute expiry


def _expiry_iso() -> str:
    """When the verified total stops being a claim we stand behind.

    Short on purpose: 43% of live PDPs carry an active markdown, so a total that outlived its
    window would be exactly the stale number this hop exists to replace.
    """
    return (
        datetime.now(tz=timezone.utc) + timedelta(seconds=_EXPECTED_TOTAL_TTL_S)
    ).isoformat().replace("+00:00", "Z")


def apply_verdicts(
    offers: List[Dict[str, Any]], verdicts: Dict[int, Verdict]
) -> List[Dict[str, Any]]:
    """Audit F3, applied. Returns the surviving offers, verified ones first.

    A `gone` offer is DROPPED — we have duplicate coverage of many products, so taking the
    next-best merchant is strictly better than handing a buyer a dead link. An `unverified` offer
    keeps its snapshot but is demoted and must never be rank 1, so it is marked and sorted after
    everything that was actually checked.
    """
    kept: List[Tuple[int, Dict[str, Any]]] = []
    for index, offer in enumerate(offers):
        verdict = verdicts.get(index)
        if verdict is None:
            # Never checked (outside top-K). Not a claim either way.
            kept.append((2, offer))
            continue
        if verdict.status == GONE:
            logger.info("live-verify dropped offer %s: %s", offer.get("offer_id"), verdict.reason)
            continue

        enriched = dict(offer)
        enriched["verification"] = verdict.as_dict()
        if verdict.status == VERIFIED:
            # STOCK and PRICE are reported separately because they are established separately.
            enriched["stock_verified"] = True
            if verdict.in_stock is not None:
                enriched["in_stock"] = bool(verdict.in_stock)

            if verdict.price_verified and verdict.live_price is not None:
                # Amount AND currency move together, always. This branch is only reached when the
                # shop's own declared currency matches the one this offer quoted, so the number
                # and the label agree by construction rather than by assumption.
                enriched["price"] = float(verdict.live_price)
                # `merchant_price_verified`, NOT `price_verified`. The gateway's reco lane
                # already publishes `price_verified` from its own `get_pdp_v2` loopback, which
                # means "consistent with our own projection" — the opposite provenance. Two
                # agent-visible surfaces with one key and contradictory meanings is the
                # reads-like-one-thing-measures-another class the audit itself flags.
                enriched["merchant_price_verified"] = True
                if verdict.live_currency:
                    enriched["currency"] = verdict.live_currency

                # Audit F3: a verified offer carries an expected total and an expiry.
                #
                # ITEM total, not grand total. Shipping and tax need a checkout, which is item 9 —
                # naming this `expected_total` would promise a landed cost we have not computed,
                # and the whole point of the field is that an agent can abort on a mismatch.
                spec = enriched.get("execution_spec")
                if isinstance(spec, dict):
                    spec = dict(spec)
                    spec["expected_item_total"] = float(verdict.live_price)
                    spec["expected_currency"] = verdict.live_currency
                    # Explicit, because the total is only right for the quantity the cart encodes.
                    # `compose_attributed_destinations` defaults to 1 and no caller overrides it
                    # today, but `quantity` is threaded live through four functions and a test
                    # already exercises 2 — nothing else links it to this number.
                    spec["expected_quantity"] = 1
                    spec["expected_total_expires_at"] = _expiry_iso()
                    enriched["execution_spec"] = spec
            else:
                # Checked, but this source could not speak to the price — a currency mismatch, or
                # a shop currency we do not know YET (the lookup is off the critical path, so the
                # first turn for a domain lands here by design). The quoted price stands
                # untouched and unclaimed.
                enriched["merchant_price_verified"] = False
            kept.append((0, enriched))
        else:
            enriched["stock_verified"] = False
            # S7: a DISTINCT key. Both offer builders publish a numeric `confidence` (0.6/0.8/1.0)
            # and `_merit` calls float() on it, so writing a string there would be a silent
            # type-contract change on a published field with no response_model to catch it.
            enriched["verification_confidence"] = "unverified"
            # F3: expected totals must not be asserted for something we could not check.
            spec = enriched.get("execution_spec")
            if isinstance(spec, dict):
                spec = dict(spec)
                # The keys this module actually publishes. Nulling `expected_total` — which
                # nothing sets — left a live `expected_item_total` standing on an offer we could
                # not verify.
                spec["expected_item_total"] = None
                spec["expected_currency"] = None
                spec["expected_total_expires_at"] = None
                enriched["execution_spec"] = spec
            kept.append((1, enriched))

    kept.sort(key=lambda pair: pair[0])
    out = [offer for _rank, offer in kept]

    # S6: the guarantee is ABSOLUTE, not relative. A relative demotion says nothing when the
    # WHOLE batch is unverified — which is the outage case the rule was written for, and the one
    # where an agent is most likely to act on a stale price. We cannot invent a verified offer,
    # so the honest move is to say so on the offer itself: the caller can then refuse to present
    # it as a confident rank 1 rather than discovering the fact from a dashboard.
    if out and out[0].get("stock_verified") is False:
        out[0]["rank_one_unverified"] = True
    return out
