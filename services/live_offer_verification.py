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


async def _cache_put(key: str, payload: Dict[str, Any]) -> None:
    ttl = _f("LIVE_OFFER_VERIFICATION_CACHE_TTL_SECONDS", _DEFAULT_CACHE_TTL_S)
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

    # S2: the amount is INFORMATIONAL. `/products/<handle>.js` carries no currency code and
    # returns the SHOP'S DEFAULT currency, so this number cannot be compared with, or substituted
    # for, an offer quoted in another currency. `price_moved` is therefore only asserted when the
    # comparison is meaningful, and the caller never replaces the published price from it.
    return Verdict(
        VERIFIED, "ok", live_price=live, live_currency=None, in_stock=True,
        price_changed=_price_moved(live, offer),
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
            # STOCK is verified; PRICE is not, and the two must not be conflated. The source
            # carries no currency code, so replacing `price` from it would publish a shop-currency
            # amount under the offer's own currency label — the amount-without-its-currency class
            # this repo has fixed twice. `stock_verified` says exactly what was established.
            enriched["stock_verified"] = True
            if verdict.in_stock is not None:
                enriched["in_stock"] = bool(verdict.in_stock)
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
                spec["expected_total"] = None
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
