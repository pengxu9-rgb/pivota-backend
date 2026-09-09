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

#: WHERE an observation came from. Live rows are biased toward what agents actually ask for; sweep
#: rows are unbiased over the catalog but say nothing about demand. Averaging them answers neither
#: question, and "the merchants refuse 20% of what buyers ask for" is the only one of the two that
#: justifies arming a gate on the checkout path.
SOURCE_LIVE = "live"
SOURCE_SWEEP = "measurement_sweep"
#: NOT `R_UNVERIFIABLE`. Both block, and conflating them would make the shadow report unreadable:
#: "could_not_ask_merchant" is a fact about the MERCHANT, this is a fact about US — nobody has
#: warmed this URL yet. Week one of shadow is otherwise a wall of merchant-shaped refusals that
#: are really our own empty cache, which is exactly the "denominator is empty by construction"
#: mistake this gate already made once.
R_NOT_YET_CHECKED = "not_yet_checked"
#: Split out of R_UNVERIFIABLE for the same reason R_NOT_YET_CHECKED was: no merchant was
#: contacted, and no merchant could have been. `_target` returns no URL for a PDP that is not
#: `/products/<handle>`-shaped, or for a seed carrying no url at all, and that happens BEFORE the
#: cache read. Left inside `could_not_ask_merchant` it was the ONLY reason in the answered
#: denominator under the shipping config, so the rate built to escape a by-construction 1.0 read
#: 1.0 by construction — on rows where nobody was asked.
R_NO_VERIFIABLE_URL = "no_verifiable_url"

#: Every reason `preflight` can return WITHOUT a merchant ever being contacted. This is a set, not
#: a single constant, because review caught the answered rate excluding only `not_yet_checked` and
#: still calling itself "the merchant's verdict": `no_merchant_issued_variant_id` is decided at
#: step 1 before any request, and on the union population it dominates. Measured on the shape prod
#: will produce in week one, the difference is 0.99 against a true merchant refusal rate of 0.20 —
#: the same unreadable-by-construction defect one class down, and the reader who sees 0.99 vetoes
#: enforcement. Anything added here must be a reason that CANNOT have touched a merchant.
NO_CONTACT_REASONS = frozenset({
    R_NOT_YET_CHECKED,        # the fence held; our cache was cold
    R_NO_MERCHANT_VARIANT,    # refused at step 1, no request made
    R_NO_VERIFIABLE_URL,      # no `/products/<handle>` url to ask about; decided before the cache
    R_SUPPRESSED,             # refused at step 2, no request made
    R_DISABLED,               # the gate was off — and `record` drops these before they land, so
                              # this member is dead in the table today and kept for completeness
})
#: Deliberately OUT, and each for a reason worth stating because the next person will be tempted:
#: R_GONE, R_OUT_OF_STOCK and R_OK all require a DOCUMENT, and under a closed fence a document
#: means somebody contacted that merchant. R_UNVERIFIABLE stays in the answered denominator too:
#: after this split it means the merchant was asked and did not usefully answer, except on the
#: exception path, where contact is genuinely ambiguous — the conservative side of that is to
#: count it as a merchant failure rather than silently shrink the denominator.

_DEFAULT_DEADLINE_S = 4.0


def mode() -> str:
    """`off` unless deliberately set. Reads the env every call so the flag can be flipped on a
    running service without a deploy — the enforcement decision should not require a roll."""
    raw = str(os.getenv("CHECKOUT_PREFLIGHT_MODE", MODE_OFF)).strip().lower()
    return raw if raw in _MODES else MODE_OFF


def is_enabled() -> bool:
    return mode() != MODE_OFF


def egress_allowed() -> bool:
    """May THIS process fetch a merchant to answer a preflight? Default NO.

    `checkout_preflight` runs inside `web`, and `web` is on the `default` subnet, whose NAT holds
    8.231.167.230 — the address payment partners allowlist. Merchant fetches from there share both
    the IP's reputation and its NAT port pool with the payment path, and port exhaustion is
    per-IP. `infra/gcp/setup_egress_nat.sh` records the measurement: ~50 requests over 37
    Cloudflare-fronted domains in about a minute tripped a cross-domain IP-level 429 lasting ~15
    minutes.

    The default is CLOSED, so arming the gate cannot start crawling from the money path by
    accident — that has to be a deliberate, separate act. A process that legitimately egresses
    (a warm/measurement lane running on `pivota-crawl`) sets this true for itself.

    Read per call, like `mode()`, so it can be shut off on a running service without a deploy.
    """
    raw = str(os.getenv("CHECKOUT_PREFLIGHT_ALLOW_EGRESS", "")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


_WARNED_BLIND_ENFORCE = False


def _warn_if_enforcing_blind(current_mode: str) -> None:
    """`enforce` + a closed fence + a cold cache withdraws EVERY cart, on zero merchant evidence.

    The two flags are independent and both re-read per call, so this combination can be reached by
    flipping one variable on a running service. This module's own design note says the mode is the
    whole safety story precisely so it is "one value and not a scatter of booleans"; adding a
    second boolean that can silently null the product needs to say so out loud at least once.

    In the log MESSAGE, not `extra=`: the root logger is WARNING in prod and
    `setup_structured_logging()` is never called, so structured fields are dark.
    """
    global _WARNED_BLIND_ENFORCE
    dangerous = current_mode == MODE_ENFORCE and not egress_allowed()
    if not dangerous:
        # Latch on the STATE, not once-ever. Review found the first version silent forever after
        # one warning, so opening the fence and closing it again re-entered the dangerous state
        # with no signal — and the message text is the only signal there is.
        _WARNED_BLIND_ENFORCE = False
        return
    if _WARNED_BLIND_ENFORCE:
        return
    _WARNED_BLIND_ENFORCE = True
    logger.warning(
        "checkout preflight is ENFORCING with its egress fence CLOSED "
        "(CHECKOUT_PREFLIGHT_ALLOW_EGRESS unset): every hand-over whose document is not already "
        "cached in THIS process will be refused as not_yet_checked and its cart withdrawn, on no "
        "merchant evidence. Warm the cache from a lane that may crawl, or set the mode to shadow."
    )


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
    # Passed the already-read value rather than re-reading: the rule three lines up is the whole
    # reason `current_mode` exists, and a warning that disagreed with the verdict beside it would
    # be worse than no warning.
    try:
        _warn_if_enforcing_blind(current_mode)
    except Exception:  # noqa: BLE001 - a log line may not break the checkout it describes
        pass
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
        # ONLY REACHABLE WITH THE FENCE OPEN. Under the default closed fence `_check_one` makes
        # no outbound request at all, so none of the budgets below are in play; they bound the
        # opted-in lane on `pivota-crawl`. Left in place because that lane is the one that will
        # hit them.
        # `max_wait` bounds only the politeness stall inside `_check_one`; the robots fetch
        # (5 s), the pacing wait (4 s) and a redirect-chasing fetch (1.2 s per operation, up to
        # 3 redirects) are each bounded separately, so together they could hold the money
        # path for 10-14 s. The search lane never sees that because its batch `asyncio.wait`
        # caps the whole call; calling `_check_one` bare dropped that bound. `TimeoutError`
        # is an `Exception`, so it lands on UNVERIFIABLE below -- fail-closed.
        budget = deadline_seconds()
        verdict = await asyncio.wait_for(
            live_offer_verification._check_one(
                offer, max_wait=budget, cache_only=not egress_allowed()
            ),
            timeout=budget,
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
    #
    # ...unless nobody ever asked. Under the egress fence a cold URL is not evidence about the
    # merchant at all, and reporting it as one would inflate `could_not_ask_merchant` with our own
    # unwarmed cache. It still BLOCKS — the gate's asymmetry is unchanged, and an unverified
    # hand-over is refused whatever the reason — but it is counted apart so the shadow report can
    # say how much of the refusal rate is merchants and how much is homework.
    if verdict.reason == live_offer_verification.NO_CACHED_EVIDENCE:
        return _finish(UNVERIFIABLE, R_NOT_YET_CHECKED, **common)
    if verdict.reason == live_offer_verification.NO_VERIFIABLE_URL:
        return _finish(UNVERIFIABLE, R_NO_VERIFIABLE_URL, **common)
    return _finish(UNVERIFIABLE, R_UNVERIFIABLE, **common)


INSERT_OBSERVATION_SQL = """
    INSERT INTO checkout_preflight_observations (
        observation_id, source, run_id, mode, outcome, would_block, reason,
        merchant_id, product_key, sku_key, offer_id,
        live_status, in_stock, price_verified,
        quoted_price, quoted_currency, live_price, live_currency,
        latency_ms, detail
    ) VALUES (
        :observation_id, :source, :run_id, :mode, :outcome, :would_block, :reason,
        :merchant_id, :product_key, :sku_key, :offer_id,
        :live_status, :in_stock, :price_verified,
        :quoted_price, :quoted_currency, :live_price, :live_currency,
        :latency_ms, CAST(:detail AS jsonb)
    )
"""


async def record(
    verdict: PreflightVerdict,
    offer: Dict[str, Any],
    *,
    source: str = SOURCE_LIVE,
    run_id: Optional[str] = None,
) -> None:
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
            "source": str(source or SOURCE_LIVE)[:32],
            "run_id": (str(run_id)[:64] if run_id else None),
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


async def preflight_and_record(
    offer: Dict[str, Any], *, source: str = SOURCE_LIVE, run_id: Optional[str] = None
) -> PreflightVerdict:
    """The call sites use this. One verdict, one row, and the caller reads `allows_checkout`."""
    verdict = await preflight(offer)
    await record(verdict, offer, source=source, run_id=run_id)
    return verdict


SHADOW_REPORT_SQL = """
    SELECT source,
           reason,
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
    GROUP BY source, reason
    ORDER BY n DESC
"""


#: What the observations actually cover. Carried IN the report because a refusal rate is only
#: meaningful with its denominator named, and this one is narrower than "external offers":
#:   * only offers.resolve's external-seed lane is wired today. find_products_multi's seed lane
#:     (search cards), _build_prefetched_external_seed_wrappers and mint_external_seed_links
#:     publish the SAME pre-filled cart_url and are NOT gated — the search lane is the larger
#:     surface, and gating the shared chokepoint is follow-up work;
#:   * within that lane, the asked population is a UNION: handoffs whose merchant-issued
#:     variant we could name, PLUS handoffs that would hand the buyer a cart. #2151 moved this
#:     off "cart-prefilled" alone, because a cart also needs storefront evidence stored on 0 of
#:     11,824 active seeds and keying on it left this denominator empty by construction; it is
#:     a union rather than a swap because the attach lane ships carts built from an
#:     operator-typed `attached_variant_id` for which no `catalog_skus` row exists, and keying
#:     on the resolved id alone would have stopped gating the only carts that exist today.
#:     BOTH HALVES MATTER TO THIS RATE. The second half admits handoffs carrying an id this
#:     backend would NOT call merchant-issued, and those are refused at step 1 of `preflight`
#:     with `no_merchant_issued_variant_id` before any merchant is contacted — so they inflate
#:     `would_block` with a statement about OUR OWN operator data, not about the merchant's
#:     stock. Group by reason before reading the rate;
#:   * the widened population is dominated by storefronts `live_offer_verification._check_one`
#:     STRUCTURALLY CANNOT READ. `storefront_is_shopify` is false on every active seed, so a
#:     host that does not answer a parseable `/products/<handle>.js` yields
#:     `not_a_known_shopify_storefront` -> UNVERIFIED -> UNVERIFIABLE -> would_block=True. Read
#:     `would_block_rate` WITHOUT separating that class out and it approaches 1.0 for a reason
#:     that is about our own evidence, not about the merchant's stock.
#: A reader who takes this rate as "how often an external offer is stale" will be wrong twice.
REPORT_SCOPE = (
    "offers.resolve external-seed lane; the asked population is the UNION of handoffs with a "
    "resolved merchant-issued variant id and handoffs that would hand the buyer a cart "
    "(see #2151) — so it is neither 'cart-prefilled only' nor 'resolved-identity only'; "
    "search/prefetch/mint lanes not gated; "
    "two classes inflate would_block for reasons that are about US, not the merchant: an id we "
    "do not call merchant-issued is blocked at step 1 with no request made, and a storefront we "
    "cannot read answers unverifiable — group by reason before reading the rate; "
    "within a request, the first N distinct (pdp_url, variant) questions in seed-row order — "
    "rate is over questions ASKED, not over candidates offered; "
    "THIRD class, and it dominates until a warm lane runs: with the egress fence closed (the "
    "default) `web` never asks a merchant, so an uncached document answers not_yet_checked and "
    "would_block_rate is 1.0 BY CONSTRUCTION; read would_block_rate_answered, whose denominator "
    "drops EVERY reason decided without contacting a merchant (not_yet_checked, "
    "no_merchant_issued_variant_id, no_verifiable_url, suppressed, preflight_off — see "
    "NO_CONTACT_REASONS) so it is "
    "the merchants' verdict over the questions that actually reached one, while no_contact and "
    "not_yet_checked measure our own coverage"
)


def _summarise_shadow_rows(
    by_reason: List[Dict[str, Any]], *, window_days: int
) -> Dict[str, Any]:
    """The arithmetic, split out from the query so it can be tested without a database.

    Not cosmetic: the interesting part of this report is which rows go in which denominator, and
    while that lived inside the DB call the only way to pin it was a Postgres round trip — so
    dropping a rate, or leaving `not_yet_checked` in the answered denominator, was invisible.
    """
    # FOLD BY REASON FIRST. The query groups by (source, reason), so a reason seen from both
    # sources arrives as two rows and would be listed twice in `by_reason` — a reader scanning the
    # breakdown would see "merchant_says_gone" on two lines and no indication why.
    folded: Dict[str, Dict[str, Any]] = {}
    for r in by_reason:
        key = str(r.get("reason") or "")
        acc = folded.setdefault(
            key, {"reason": key, "n": 0, "would_block": 0, "_lat": 0.0})
        acc["n"] += int(r["n"])
        acc["would_block"] += int(r["would_block"])
        # Weighted, because folding two sources' averages unweighted would let a 3-row sweep
        # bucket outvote a 3000-row live one. Dropping it entirely — which the first fold did —
        # loses the only signal that says whether a refusal was slow or instant.
        if r.get("avg_latency_ms") is not None:
            acc["_lat"] += float(r["avg_latency_ms"]) * int(r["n"])
    for acc in folded.values():
        lat = acc.pop("_lat")
        acc["avg_latency_ms"] = round(lat / acc["n"]) if acc["n"] and lat else None
    by_reason = sorted(folded.values(), key=lambda r: -r["n"])

    total = sum(int(r["n"]) for r in by_reason)
    blocked = sum(int(r["would_block"]) for r in by_reason)
    # TWO RATES, because they answer different questions and the raw one is unreadable on its own
    # while the fence is closed. `not_yet_checked` means WE never asked; counting it as a refusal
    # makes the headline 1.0 and says nothing about any merchant. Dropped from BOTH sides, not
    # just the numerator — leaving it in the denominator would understate the merchant refusal
    # rate by exactly the size of our own cold cache.
    cold = sum(int(r["n"]) for r in by_reason if r.get("reason") == R_NOT_YET_CHECKED)
    # The answered denominator drops EVERY no-contact reason, not just the cold-cache one.
    no_contact = sum(int(r["n"]) for r in by_reason if r.get("reason") in NO_CONTACT_REASONS)
    no_contact_blocked = sum(
        int(r["would_block"]) for r in by_reason if r.get("reason") in NO_CONTACT_REASONS
    )
    answered = total - no_contact
    return {
        "scope": REPORT_SCOPE,
        "window_days": int(window_days),
        "observations": total,
        "would_block": blocked,
        # None, not 0.0, on an empty window: 0/0 must not read as "nothing would be refused",
        # which is precisely the number someone would arm enforcement on. The answered rate needs
        # the same guard for the same reason, and on day one of shadow it is the LIKELY state.
        "would_block_rate": (round(blocked / total, 4) if total else None),
        # The merchant's verdict, over the questions that reached a merchant.
        "answered": answered,
        "would_block_rate_answered": (
            round((blocked - no_contact_blocked) / answered, 4) if answered else None
        ),
        # Everything refused without a request. `not_yet_checked` is the part of it we can fix by
        # warming; the rest is missing identity and suppression.
        "no_contact": no_contact,
        # Our own coverage: the share of gated hand-overs nobody has warmed yet.
        "not_yet_checked": cold,
        "not_yet_checked_rate": (round(cold / total, 4) if total else None),
        "by_reason": by_reason,
    }


async def shadow_report(window_days: int = 7) -> Dict[str, Any]:  # noqa: D401
    """What enforcement WOULD have refused, and why. This is the evidence that decides whether
    `enforce` is armed — read it rather than the 31.1% from the pre-backfill sample."""
    rows = [dict(r) for r in (await database.fetch_all(
        SHADOW_REPORT_SQL, {"days": int(window_days)})) or []]
    # PER SOURCE, and the combined figure is deliberately NOT the headline. Live rows describe
    # demand, sweep rows describe the catalog, and one rate over both answers neither question —
    # the arithmetic would be dominated by whichever sweep ran most recently.
    out = _summarise_shadow_rows(rows, window_days=window_days)
    sources = sorted({str(r.get("source") or SOURCE_LIVE) for r in rows})
    out["by_source"] = {
        src: _summarise_shadow_rows(
            [r for r in rows if str(r.get("source") or SOURCE_LIVE) == src],
            window_days=window_days,
        )
        for src in sources
    }
    out["sources"] = sources
    return out
