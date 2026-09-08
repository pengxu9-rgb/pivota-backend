"""Ask the merchant whether the thing we are about to sell is still real, at checkout time.

WHY, AND WHY NOT AT INDEX TIME. `services/live_offer_verification` already asks the merchant's
storefront — but at SEARCH time, on the top 3 results, inside a 1.5s budget, and its `unverified`
verdict only DEMOTES a result. Those are the right trade-offs for a shortlist and the wrong ones
for a purchase. At checkout there is exactly one item, the latency budget is larger because a
buyer is committing rather than browsing, and "we could not ask the merchant" must not resolve to
"proceed". This module is that asymmetry and nothing else: it reuses live_offer_verification's
fetch, cache and politeness machinery rather than growing a second crawler.

WHAT IT SHIPS AS. **Shadow.** The audit behind live_offer_verification measured 31.1% of index
records producing a wrong or unexecutable spec (12.3% price mismatch, 11.1% out-of-stock, 6.7%
dead PDP) on a 90-SKU sample. A fail-closed gate on that number refuses roughly a third of
purchases, and that sample predates the variant-identity backfill that has since given 3,279
products merchant-issued identity. So the honest sequence is: compute the verdict, record what it
WOULD have refused, measure the real rate against today's catalog, then arm enforcement on
evidence. `would_block` on every observation row is that measurement; `outcome` is what actually
happened to the buyer.

THE MODE IS THE WHOLE SAFETY STORY, so it is one value and not a scatter of booleans:
  off      (default) — nothing runs, no egress, no rows. A checkout is untouched.
  shadow             — verdict computed and recorded; the checkout ALWAYS proceeds.
  enforce            — `block` refuses the checkout. Not to be armed without the shadow numbers.

FAIL-CLOSED, AND WHAT THAT MEANS PRECISELY. In `enforce`, only a positive `ok` proceeds. A
merchant that times out, blocks us, or is not a storefront we can read yields `unverifiable`,
which blocks — because the alternative is to charge someone for a thing we could not confirm
exists. That is the opposite of the search path's choice, deliberately.

PRICE IS NOT VERIFIED HERE, AND SAYS SO. Shopify's `/products/<handle>.js` returns the shop's
default-currency amount in minor units and carries NO currency code, so it establishes existence
and stock — currency-free facts — and cannot establish price. Writing its number onto an offer
quoted in another currency publishes ¥4500 as $4500. `price_verified` is therefore its own field,
never folded into the outcome, and a price mismatch is recorded as INFORMATIONAL. Verifying price
needs a currency-bearing source (UCP `create_checkout`); until that exists this module must not
claim the larger half of the 31.1%. Claiming it would be worse than not checking, because it
would look like the check had been done.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from db.database import database
from services import live_offer_verification
from services.variant_identity import MERCHANT_ISSUED, variant_id_provenance

logger = logging.getLogger("checkout_preflight")

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ENFORCE = "enforce"
_MODES = (MODE_OFF, MODE_SHADOW, MODE_ENFORCE)

OK = "ok"
BLOCK = "block"
UNVERIFIABLE = "unverifiable"

#: Reasons. Closed set, because they are grouped in the shadow report and a free-text reason
#: makes that report unaggregatable.
R_OK = "ok"
R_GONE = "merchant_says_gone"
R_OUT_OF_STOCK = "out_of_stock"
R_UNVERIFIABLE = "could_not_ask_merchant"
R_NO_MERCHANT_VARIANT = "no_merchant_issued_variant_id"
R_SUPPRESSED = "suppressed"
R_DISABLED = "preflight_off"

_DEFAULT_DEADLINE_S = 4.0


def mode() -> str:
    """`off` unless deliberately set. Reads the env every call so the flag can be flipped on a
    running service without a deploy — the enforcement decision should not require a roll."""
    raw = str(os.getenv("CHECKOUT_PREFLIGHT_MODE", MODE_OFF)).strip().lower()
    return raw if raw in _MODES else MODE_OFF


def is_enabled() -> bool:
    return mode() != MODE_OFF


def deadline_seconds() -> float:
    try:
        return max(0.5, float(os.getenv("CHECKOUT_PREFLIGHT_DEADLINE_SECONDS") or _DEFAULT_DEADLINE_S))
    except (TypeError, ValueError):
        return _DEFAULT_DEADLINE_S


@dataclass
class PreflightVerdict:
    outcome: str
    reason: str
    would_block: bool
    #: Separate from the outcome on purpose — see the module docstring. A verdict can establish
    #: stock without establishing price, and this module never establishes price today.
    price_verified: bool = False
    live_status: Optional[str] = None
    in_stock: Optional[bool] = None
    live_price: Optional[Decimal] = None
    live_currency: Optional[str] = None
    quoted_price: Optional[Decimal] = None
    quoted_currency: Optional[str] = None
    price_moved: bool = False
    latency_ms: int = 0
    detail: Dict[str, Any] = field(default_factory=dict)
    #: The mode this verdict was COMPUTED under, captured at creation.
    #:
    #: `allows_checkout` used to read mode() live, which meant a verdict's meaning could change
    #: after it was produced: the flag is read from the environment on every call so it can be
    #: flipped without a deploy, so a flip between computing a verdict and acting on it would
    #: have silently converted a shadow observation into a refusal (or the reverse). A verdict
    #: has to be self-consistent — it is a record of a decision already made.
    mode: str = MODE_OFF

    @property
    def allows_checkout(self) -> bool:
        """What the CALLER acts on. In shadow this is always True however bad the verdict —
        that is what shadow means — so a caller can be wired in before enforcement is armed and
        does not change behaviour when it is."""
        if self.mode == MODE_ENFORCE:
            return self.outcome == OK
        return True


def _decimal(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _quoted(offer: Dict[str, Any]) -> tuple:
    price = _decimal(
        offer.get("merchant_effective_price")
        or offer.get("list_price")
        or offer.get("estimated_best_price")
    )
    return price, (str(offer.get("currency") or "").strip().upper() or None)


def _variant_of(offer: Dict[str, Any]) -> str:
    spec = offer.get("execution_spec")
    if isinstance(spec, dict) and spec.get("variant_id"):
        return str(spec["variant_id"]).strip()
    return str(offer.get("source_variant_id") or "").strip()


async def preflight(offer: Dict[str, Any]) -> PreflightVerdict:
    """Verify ONE offer against the merchant. Never raises: a preflight that throws would take
    down the checkout it is supposed to protect, which in shadow would be absurd."""
    started = time.monotonic()
    # Read ONCE. Every decision in this call, and the verdict it returns, refer to the same mode.
    current_mode = mode()
    quoted_price, quoted_currency = _quoted(offer)

    def _finish(outcome: str, reason: str, **kw) -> PreflightVerdict:
        v = PreflightVerdict(
            outcome=outcome,
            reason=reason,
            # The fail-closed decision, computed identically in every mode. Recording what
            # enforcement WOULD do is the entire purpose of shadow.
            would_block=(outcome != OK),
            quoted_price=quoted_price,
            quoted_currency=quoted_currency,
            latency_ms=int((time.monotonic() - started) * 1000),
            mode=current_mode,
            **kw,
        )
        return v

    if current_mode == MODE_OFF:
        return _finish(OK, R_DISABLED)

    try:
        # 1. Identity, before spending a request. An id we minted ourselves cannot be verified
        #    against the merchant by definition — asking would only tell us the PRODUCT exists.
        variant_id = _variant_of(offer)
        provenance = variant_id_provenance(
            variant_id,
            product_id=offer.get("source_product_id"),
            product_key=offer.get("product_key"),
        )
        if provenance != MERCHANT_ISSUED:
            return _finish(
                BLOCK, R_NO_MERCHANT_VARIANT,
                detail={"variant_id_provenance": provenance},
            )

        # 2. Suppression, also free. A suppressed row must never reach a buyer regardless of
        #    what the merchant would say about it.
        if offer.get("suppressed_at") or offer.get("suppression_reason"):
            return _finish(BLOCK, R_SUPPRESSED)

        # 3. Ask the merchant. Reuses the search path's checker so there is one crawler, one
        #    cache and one politeness budget — but the verdict is read with checkout's rules.
        # `max_wait` bounds only the politeness stall inside `_check_one`; the robots fetch
        # (5 s), the pacing wait (4 s) and a redirect-chasing fetch (1.2 s per operation, up to
        # 3 redirects) are each bounded separately, so together they could hold the money
        # path for 10-14 s. The search lane never sees that because its batch `asyncio.wait`
        # caps the whole call; calling `_check_one` bare dropped that bound. `TimeoutError`
        # is an `Exception`, so it lands on UNVERIFIABLE below -- fail-closed.
        budget = deadline_seconds()
        verdict = await asyncio.wait_for(
            live_offer_verification._check_one(offer, max_wait=budget), timeout=budget
        )
    except Exception as exc:  # noqa: BLE001
        # Fail-closed on our OWN failure too. An exception here means we did not establish the
        # thing we came to establish, which is the same epistemic position as a timeout.
        logger.warning("checkout preflight errored: %s", repr(exc)[:200])
        return _finish(UNVERIFIABLE, R_UNVERIFIABLE, detail={"error": repr(exc)[:200]})

    common = {
        "live_status": verdict.status,
        "in_stock": verdict.in_stock,
        "live_price": verdict.live_price,
        "live_currency": verdict.live_currency,
        # Pinned False at the pass-through, whatever `_check_one` said. The search checker sets
        # it True when /meta.json's shop currency matches the offer's; that is a currency
        # INFERENCE about `/products/<handle>.js`'s minor-unit amount, not a quote from the
        # merchant's checkout, and this module's contract is that price is established only
        # by a currency-bearing source (UCP create_checkout). Claiming it here would look like
        # the check had been done.
        "price_verified": False,
        "price_moved": bool(verdict.price_changed),
        "detail": {"source": verdict.source, "verify_reason": verdict.reason},
    }

    if verdict.status == live_offer_verification.GONE:
        reason = R_OUT_OF_STOCK if verdict.in_stock is False else R_GONE
        return _finish(BLOCK, reason, **common)
    if verdict.status == live_offer_verification.VERIFIED:
        if verdict.in_stock is False:
            return _finish(BLOCK, R_OUT_OF_STOCK, **common)
        return _finish(OK, R_OK, **common)
    # `unverified` — the merchant did not answer, or is not a storefront we can read. At search
    # time this demotes; here it blocks.
    return _finish(UNVERIFIABLE, R_UNVERIFIABLE, **common)


INSERT_OBSERVATION_SQL = """
    INSERT INTO checkout_preflight_observations (
        observation_id, mode, outcome, would_block, reason,
        merchant_id, product_key, sku_key, offer_id,
        live_status, in_stock, price_verified,
        quoted_price, quoted_currency, live_price, live_currency,
        latency_ms, detail
    ) VALUES (
        :observation_id, :mode, :outcome, :would_block, :reason,
        :merchant_id, :product_key, :sku_key, :offer_id,
        :live_status, :in_stock, :price_verified,
        :quoted_price, :quoted_currency, :live_price, :live_currency,
        :latency_ms, CAST(:detail AS jsonb)
    )
"""


async def record(verdict: PreflightVerdict, offer: Dict[str, Any]) -> None:
    """Persist one observation. Swallows its own failures.

    A measurement that can break a checkout is not worth having, and in shadow the checkout is
    proceeding regardless — so a failed INSERT costs a data point, not a purchase. It is a table
    rather than a log line because Cloud Logging drops lines: on 2026-09-08 it dropped a completed
    backfill's entire report while the job exited 0, and a shadow mode that silently under-counts
    is the failure it exists to detect.
    """
    # From the VERDICT, not the live env: the flag is re-read on every call so it can be
    # flipped without a roll, and an off->shadow flip between computing and recording wrote a
    # `preflight_off` pass into the shadow report as if it had been measured -- deflating the
    # one number the enforcement decision rests on; the reverse flip dropped a real row.
    if verdict.mode == MODE_OFF:
        return
    try:
        await database.execute(INSERT_OBSERVATION_SQL, {
            "observation_id": uuid.uuid4().hex,
            "mode": verdict.mode,
            "outcome": verdict.outcome,
            "would_block": bool(verdict.would_block),
            "reason": verdict.reason,
            "merchant_id": str(offer.get("merchant_id") or "")[:64] or None,
            "product_key": str(offer.get("product_key") or "")[:255] or None,
            "sku_key": str(offer.get("sku_key") or "")[:255] or None,
            "offer_id": str(offer.get("offer_id") or "")[:255] or None,
            "live_status": verdict.live_status,
            "in_stock": verdict.in_stock,
            "price_verified": bool(verdict.price_verified),
            "quoted_price": verdict.quoted_price,
            "quoted_currency": verdict.quoted_currency,
            "live_price": verdict.live_price,
            "live_currency": verdict.live_currency,
            "latency_ms": verdict.latency_ms,
            "detail": json.dumps(verdict.detail or {}),
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning("checkout preflight observation not recorded: %s", repr(exc)[:200])


async def preflight_and_record(offer: Dict[str, Any]) -> PreflightVerdict:
    """The call sites use this. One verdict, one row, and the caller reads `allows_checkout`."""
    verdict = await preflight(offer)
    await record(verdict, offer)
    return verdict


SHADOW_REPORT_SQL = """
    SELECT reason,
           count(*) AS n,
           count(*) FILTER (WHERE would_block) AS would_block,
           round(avg(latency_ms)) AS avg_latency_ms
    FROM checkout_preflight_observations
    -- make_interval with an integer, NOT a cast of a text parameter to interval: asyncpg types
    -- a parameter inside such a cast as a real interval and rejects a string. NOTE the comment
    -- itself must not name a bind parameter -- `databases` scans the WHOLE statement including
    -- comments, so a colon-prefixed word here is parsed as a second parameter and the query
    -- fails with "the server expects 1 argument, 2 were passed". Same class as the migration
    -- runner matching CONCURRENTLY inside prose.
    WHERE created_at > NOW() - make_interval(days => :days)
    GROUP BY reason
    ORDER BY n DESC
"""


#: What the observations actually cover. Carried IN the report because a refusal rate is only
#: meaningful with its denominator named, and this one is narrower than "external offers":
#:   * only offers.resolve's external-seed lane is wired today. find_products_multi's seed lane
#:     (search cards), _build_prefetched_external_seed_wrappers and mint_external_seed_links
#:     publish the SAME pre-filled cart_url and are NOT gated — the search lane is the larger
#:     surface, and gating the shared chokepoint is follow-up work;
#:   * within that lane, only CART-PREFILLED handoffs are asked about. Referral-only offers are
#:     left alone deliberately (see the call site), so they appear in no row at all.
#: A reader who takes this rate as "how often an external offer is stale" will be wrong twice.
REPORT_SCOPE = (
    "offers.resolve external-seed lane, cart-prefilled handoffs only; "
    "search/prefetch/mint lanes not gated; "
    "within a request, the first N distinct (pdp_url, variant) questions in seed-row order — "
    "rate is over questions ASKED, not over candidates offered"
)


async def shadow_report(window_days: int = 7) -> Dict[str, Any]:  # noqa: D401
    """What enforcement WOULD have refused, and why. This is the evidence that decides whether
    `enforce` is armed — read it rather than the 31.1% from the pre-backfill sample."""
    rows = await database.fetch_all(SHADOW_REPORT_SQL, {"days": int(window_days)})
    by_reason: List[Dict[str, Any]] = [dict(r) for r in rows or []]
    total = sum(int(r["n"]) for r in by_reason)
    blocked = sum(int(r["would_block"]) for r in by_reason)
    return {
        "scope": REPORT_SCOPE,
        "window_days": int(window_days),
        "observations": total,
        "would_block": blocked,
        # None, not 0.0, on an empty window: 0/0 must not read as "nothing would be refused",
        # which is precisely the number someone would arm enforcement on.
        "would_block_rate": (round(blocked / total, 4) if total else None),
        "by_reason": by_reason,
    }
