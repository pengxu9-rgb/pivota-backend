"""The SWEEP that gathers merchant purchasability facts (WP6).

It renders one merchant's landed checkout, reads whether that checkout offers a CARD and what it
charges, and records the fact. `db/merchant_purchasability.py` owns the rule; this module owns
the LOOP and its BOUNDS. Nothing here decides anything.

    services/shopify_cart_link_preflight.preflight  the one thing that touches a merchant.
    db/merchant_purchasability.record_check         the one thing that writes a fact.
    python -m jobs.merchant_purchasability_sweep    runs ONE sweep and exits (see `main`).
    infra/gcp/setup_merchant_purchasability_sweep_job.sh
                                                    provisions that as a Cloud Run Job + an
                                                    hourly Cloud Scheduler trigger on pivota-crawl.

── WHERE IT RUNS: THE CRAWL EGRESS, NEVER THE PAYMENT NAT ───────────────────────────────────

Until 2026-09-27 this was a `services/audit_scheduler` interval job on the `worker` service. Two
measured problems moved it out:

  * THE WORKER'S EGRESS IS THE DEFAULT NAT (8.231.167.230), THE ADDRESS PAYMENT PARTNERS
    ALLOWLIST. Every check renders a merchant checkout from it; NAT port exhaustion is per-IP, and
    ~50 requests over 37 Cloudflare-fronted domains in ~1 minute once tripped a cross-domain,
    IP-level 429 for ~15 minutes (infra/gcp/setup_tierb_cart_link_eligibility_job.sh). With the
    population growing from 2 to ~37 merchants that stops being hypothetical.
  * AN INTERVAL JOB ON A SERVICE THAT REDEPLOYS HOURLY STARVES. Every on-merge worker deploy
    restarts APScheduler's interval clock; on 2026-09-27 the 3600 s tick was pre-empted three
    times in four hours (worker revisions 02:10, 04:03, 04:21) and one report landed.

It is now a standalone Cloud Run Job on subnet `pivota-crawl` (NAT 34.82.199.35), fired by Cloud
Scheduler — a cron trigger no deploy resets. `services/audit_scheduler` must NEVER register it
again; tests/test_merchant_purchasability.py holds that from the live registry.

── THE POPULATION, AND WHY IT IS THIS ONE ───────────────────────────────────────────────────

Surveyed 2026-09-22. There is NO single merchant x market table in this repo. Three candidates:

  * `execution_routes` (the UCP probe lane) — REJECTED. It has no market column at all, and it
    is not a purchase allowlist; `routes/store_audit_ops.checkout_tier_coverage` exists precisely
    to report that its merchant coverage is zero.
  * `reap_agentic_eligibility` — the VARIANT lane's allowlist, keyed
    (merchant_domain, market_country, product_key, variant_key). The merchant-grain row is
    `product_key = ''`, and this job does not spell that predicate itself: it calls
    `db.reap_agentic_ledger.list_enabled_merchant_markets`, which is the SET form of the exact
    predicate `routes/agent_commerce_reap` decides eligibility with. A population narrower than
    the door's allowlist is a merchant the gate never sees.
  * `tierb_cart_link_eligibility` — the CART-LINK lane's allowlist, keyed (shop_domain, market),
    and it already carries a confirmed `variant_id`.

THE POPULATION IS THE UNION OF THE TWO REAP ALLOWLISTS, AT MERCHANT GRAIN, PLUS THE CONNECTED
SHOPIFY STORES. The two allowlists are EXACTLY what `routes/agent_commerce_reap.py` consults
before it will start a purchase — the variant lane through `_eligibility`, the cart-link lane
through `is_cart_link_eligible` — and nothing else in this repo can make the door offer to buy.

The third lane exists because the fact gained a second consumer. The product-card cart gate
(`routes/agent_shop_gateway._CartPurchasabilityGate`, on `_attach_connected_product_redirects`)
mints a connected Shopify card's prefilled cart only on a fresh positive fact for the cart's host
x the buyer's market. A connected store is in neither allowlist, so without this lane it was
never checked and, under enforcement, its cards could never carry a cart again.

  * THE STORE is the one `services.merchant_store_service.get_merchant_active_stores` hands the
    card lane: a live (`active`/`connected`) `merchant_stores` row, else the merchant's legacy
    `merchant_onboarding.mcp_shop_domain` — Shopify only, since Wix / WooCommerce cards never
    mint a cart and the preflight is Shopify-only — and only for a merchant with a cached
    product, because a store that serves no card makes a fact that gates nothing.
  * THE HOST is `normalize_shop_host(domain)` — the function `shopify_cart_base_url` builds the
    card's cart on — then the same `_population_key` every lane goes through.
  * THE MARKET is the merchant's declared `merchant_onboarding.region`, and ONLY when it is an
    ISO-2 country: the column is a free-text signup field whose own comment says "US, EU,
    APAC", so "EU" (two letters, not a country) and "UK" (Shopify's code is GB) are refused
    with "APAC" and counted as `population_skipped_market_unknown` — never defaulted, never
    mapped. Declared, not measured: a store that cannot sell there comes back negative or
    unverifiable, which is the answer an absent fact already gives, so a wrong region can
    only cost one abandoned checkout, never open a cart.

  Measured 2026-09-28: `region` reads US 40, "shopify" 12, APAC 6, CA 3, Other 1, NULL 1 across
  merchant_onboarding. The lane admits two hosts today — ijaqit-v9.myshopify.com (two live store
  rows) and i9j3i0-kj.myshopify.com (legacy), both US — and counts the three legacy stores whose
  region reads "shopify" as market-unknown.

One representative variant per merchant x market: the Tier B row's confirmed `variant_id` when we
have one, else our catalog's variant for that domain, else NONE — and with none the preflight
picks the storefront's first available variant itself, reading availability with
`?country=<market>` pinned. The expected price is OUR indexed unit price for that variant when
we hold one, so that a drift is a statement about our index and not about an unrelated SKU.

── THE GATE IS INSIDE THE JOB ───────────────────────────────────────────────────────────────

`MERCHANT_PURCHASABILITY_SWEEP_ENABLED`, read PER RUN, gates the whole run — and unlike the Reap
poller, gating the whole run is correct here, because this job has no sweep that must keep
running while the rail is off. It holds no PII, expires nothing and promises nothing to a buyer;
with the dial off it should touch no merchant at all, because every fetch it makes CREATES AN
ABANDONED CHECKOUT on that merchant's store. Provisioning the job dark is therefore inert,
exactly like the Tier B job's gate: the setup script sets the dial on the JOB (`--enable`), and
an execution started by hand against a dark job exits 0 having contacted nobody.

IT IS A DIFFERENT DIAL FROM THE CONSUMERS' (`MERCHANT_PURCHASABILITY_ENFORCE`), and that is not
tidiness. The sweep runs only inside its own Cloud Run Job, and a backend deploy neither creates,
re-images nor arms that job — so one shared dial, armed on `web`, would switch the refusals on
while this job never ran anywhere, and every merchant in the catalogue would answer a permanent
409. Sweep first, verify coverage, enforce second: see
`db.merchant_purchasability.is_enforcement_enabled` and the runbook.

── VANTAGE ──────────────────────────────────────────────────────────────────────────────────

Every direct check is recorded under vantage `worker` (`facts.WORKER_VANTAGE`). SINCE THE MOVE TO
THE CRAWL SUBNET "worker" MEANS "OUR CRAWL EGRESS" (pivota-crawl, NAT 34.82.199.35), not the
worker service and not the payment NAT. The name was kept ON PURPOSE, and it is the only choice
that needs no data migration and no door change:

  * `is_purchasable` reads the vantage named by MERCHANT_PURCHASABILITY_BUYER_VANTAGE, which is
    UNSET on prod `web` (so `worker`) while MERCHANT_PURCHASABILITY_ENFORCE=1 there (read
    2026-09-27). A new vantage name would need that dial moved on `web` AND a complete crawl pass
    under the new name first, or every merchant reads browse_only and the Reap rail answers 409;
    and the old `worker` rows would linger in the ops read until somebody deleted them.
  * Under the same name the crawl job's upserts land on the SAME primary-key rows. A positive
    window carries over and is refreshed by the first run; a merchant the crawl egress cannot
    verify keeps its window (rule 3 in db/merchant_purchasability.py) and ages out over the TTL,
    so a reachability regression shows as `unverifiable` in the report with 72 h to act on it.
  * Neither of our egresses is the buyer's — the Reap partner pays from its own network — so the
    old name was never "the buyer vantage" either; renaming would buy no truth. And the crawl
    egress is the one merchant storefronts are known to answer: the Tier B lane's ELIGIBLE rows —
    the cart-link half of this population, and most of its merchants — were observed FROM
    pivota-crawl, by the Tier B job that runs there.

When `VANTAGE_PROXY_URL` is set the same merchant is ALSO checked through that HTTPS proxy and
recorded under vantage `proxy`, so a buyer-market vantage can be configured without code.
REACHABILITY IS EGRESS-DEPENDENT and that is measured, not assumed: judydoll.com resets direct
TCP from one of our egresses while answering through another.

── PACING ───────────────────────────────────────────────────────────────────────────────────

Every request of a run — every merchant, every vantage — goes through ONE `RequestPacer`, the
Tier B job's: request STARTS are at least `MIN_REQUEST_INTERVAL_S` (1.5 s) apart, the same floor
that job holds on the same crawl address. Before, only merchants were spaced (the pause dial) and
one merchant's catalog pages and permalink hops went back to back. The pause dial still applies,
between merchants, on top.

── PII ──────────────────────────────────────────────────────────────────────────────────────

`SweepReport` carries COUNTS AND A DURATION. No domains, no variant ids, nothing from a row. The
preflight is called with `buyer=None` — the Reap path carries no buyer data in the link — so
there is no buyer anywhere on this path to log or store.

── WHAT ONE RUN COSTS A MERCHANT ────────────────────────────────────────────────────────────

One abandoned Shopify checkout per merchant per vantage, the same side effect every UCP probe
already has. That is why the batch is bounded, the run is budgeted, and the population is
least-recently-checked first. NOTE the ordering is a rotation, not a TTL filter: a population no
larger than the batch is checked IN FULL on every run. The per-merchant arithmetic is in
docs/runbooks/merchant_purchasability.md ("Politeness").
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

import db.merchant_purchasability as facts
import db.reap_agentic_ledger as ledger
from db.database import database
# THE ONE merchant-host canonicaliser — the same function `routes/agent_commerce_reap` validates
# the purchase request with and `tierb_cart_link_eligibility` keys its rows by. See
# `_population_key` for how it is used here.
from services.tierb_cart_link_merchants import canonical_merchant_domain
# THE host a connected Shopify card's cart permalink is built on (`shopify_cart_base_url` reduces
# the connected store domain with it), so a connected-store fact is keyed on the host the card
# gate asks about. See `_CONNECTED_LANE_SQL`.
from services.outbound_links_service import normalize_shop_host
from services.shopify_cart_link_preflight import (
    REQUEST_TIMEOUT_S,
    USER_AGENT,
    PreflightResult,
    preflight,
)
# THE Tier B job's pacer, not a second one: both jobs share the crawl address, and one spacing
# rule for it is the point. See "PACING" above.
from jobs.tierb_cart_link_eligibility import MIN_REQUEST_INTERVAL_S, PacedTransport, RequestPacer
from utils.logger import logger as operator_logger

logger = logging.getLogger(__name__)

# THE OPERATOR LINES GO THROUGH THE "pivota" LOGGER, NOT THE MODULE LOGGER. Measured in prod on
# 2026-09-23: /__scheduler_health showed `runs.merchant_purchasability_sweep.runs_ok=1` on the
# worker and Cloud Logging held ZERO matching lines. Nothing in this process configures the root
# logger — `middleware.structured_logging` configures only the `structured_logs` logger and
# uvicorn only `uvicorn.*` — so root sits at Python's default WARNING and a module logger's INFO
# is dropped at the logger before any handler sees it. `utils.logger` is the one logger in this
# repo that carries its own INFO level and its own stdout handler (propagate=False, format
# `[ts] LEVEL - msg`), which is why services/merchant_order_gap_alert.py's ticks land and this
# job's did not. ONLY the per-run report and the two lines the runbook tells an operator to look
# for go this way; everything else (dial warnings, per-check errors) stays on the module logger.
# Reconfiguring root instead would flood prod with INFO from every module and change the
# uvicorn access-log redaction path — see main.install_uvicorn_access_log_redaction.

__all__ = [
    "EXIT_OK",
    "EXIT_POPULATION_UNREADABLE",
    "EXIT_ERRORS",
    "SweepReport",
    "exit_code_for",
    "is_enabled",
    "load_population",
    "main",
    "run_merchant_purchasability_sweep",
]


# ── dials ───────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Dial:
    env: str
    default: int
    minimum: int
    maximum: int


#: Read PER RUN, never cached at import. The rail's own switch is not here: it is a boolean and
#: `is_enabled()` is its one authoritative reader.
DIALS: Dict[str, _Dial] = {
    # Merchants per run. Each one is a full redirect chain plus up to 20 catalog pages, and each
    # leaves an abandoned checkout behind, so this is a politeness bound as much as a time bound.
    "batch": _Dial("MERCHANT_PURCHASABILITY_BATCH", 20, 1, 200),
    # Wall-clock budget for one run. It stops the job STARTING a new merchant; a merchant already
    # in flight runs to completion, so a run can exceed this by one merchant's worth of fetches.
    # The Cloud Run task timeout in infra/gcp/setup_merchant_purchasability_sweep_job.sh is sized
    # as budget + that one merchant, and that script pins this dial so the two cannot drift.
    "budget_seconds": _Dial("MERCHANT_PURCHASABILITY_BUDGET_SECONDS", 600, 30, 3600),
    # Seconds to wait between merchants. One store at a time, unhurried: this rail has no
    # latency requirement and a burst of checkout creations against one platform does not help us.
    "pause_ms": _Dial("MERCHANT_PURCHASABILITY_PAUSE_MS", 1500, 0, 60000),
}

_WARNED: set = set()


def _env_int(dial: _Dial) -> int:
    """One dial's value, or its default WITH A WARNING NAMING THE VARIABLE (once per process).
    Never raises and never silently zeroes."""
    raw = (os.getenv(dial.env) or "").strip()
    if not raw:
        return dial.default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        _warn_once(dial.env, "merchant_purchasability_sweep: %s is not an integer (%r); using %d",
                   dial.env, raw, dial.default)
        return dial.default
    if value < dial.minimum or value > dial.maximum:
        _warn_once(dial.env, "merchant_purchasability_sweep: %s=%d is outside %d..%d; using %d",
                   dial.env, value, dial.minimum, dial.maximum, dial.default)
        return dial.default
    return value


def _warn_once(key: str, message: str, *args: Any) -> None:
    if key in _WARNED:
        return
    _WARNED.add(key)
    logger.warning(message, *args)


def _reset_dial_warnings() -> None:
    """Test seam."""
    _WARNED.clear()


def is_enabled() -> bool:
    """THE SWEEP DIAL (`MERCHANT_PURCHASABILITY_SWEEP_ENABLED`), and ONLY that one.

    DELEGATED to `db.merchant_purchasability.is_sweep_enabled` rather than re-read here, so the
    dial has one authoritative reader. It is a DIFFERENT dial from the one the consumers read
    (`MERCHANT_PURCHASABILITY_ENFORCE`), and that separation is the whole point: this job runs
    only in its own Cloud Run Job, so arming enforcement without first arming and completing a
    sweep would refuse every merchant in the catalogue. See `is_enforcement_enabled` for the
    arming order.

    Read per run and NOTHING caches or shortcuts around it: with it off this job must touch no
    merchant, because every check it makes creates an abandoned checkout on a live store."""
    return facts.is_sweep_enabled()


def proxy_url() -> Optional[str]:
    """The optional second vantage. An https/http proxy URL; anything else is ignored rather than
    handed to httpx, which would raise inside the run."""
    raw = (os.getenv("VANTAGE_PROXY_URL") or "").strip()
    return raw if raw.startswith(("http://", "https://")) else None


# ── population ──────────────────────────────────────────────────────────────────────────────
#
# Module-level constants, no f-strings: tests/test_repo_sql_prepare_postgres.py resolves a
# `database.*` first argument only when it is a literal or a module-level name.

#: The CART-LINK lane's allowlist, with the variant it already confirmed on the storefront.
#: `verdict = 'ELIGIBLE'` matches what `is_cart_link_eligible` requires before the door will use
#: this lane; a row in any other verdict cannot produce a purchase, so a fact about it gates
#: nothing.
_CART_LANE_SQL = """
SELECT shop_domain AS domain, market AS market, variant_id
  FROM tierb_cart_link_eligibility
 WHERE verdict = 'ELIGIBLE'
"""

#: The CONNECTED-STORE lane: the Shopify store domain the card lane would build a cart on, per
#: merchant, with the merchant's declared region. The two halves are
#: `services.merchant_store_service.get_merchant_active_stores`, branch for branch: its live
#: `merchant_stores` rows, else — only for a merchant with NO live row of any platform — the
#: legacy `merchant_onboarding.mcp_*` store, which it returns whatever `mcp_connected` says and
#: `_attach_connected_product_redirects` uses without reading status. An empty domain is dropped
#: here because the card lane drops it too. The host and market rules are in the module docstring.
#:
#: ONLY A MERCHANT WITH A CACHED PRODUCT: the card lane stamps cards built from `products_cache`,
#: so a store with none serves no card, a fact about it gates nothing, and checking it would only
#: leave an abandoned checkout behind.
_CONNECTED_LANE_SQL = """
SELECT s.domain AS domain, o.region AS region
  FROM merchant_stores s
  JOIN merchant_onboarding o ON o.merchant_id = s.merchant_id
 WHERE s.status IN ('active', 'connected')
   AND lower(s.platform) = 'shopify'
   AND COALESCE(s.domain, '') <> ''
   AND EXISTS (SELECT 1 FROM products_cache pc WHERE pc.merchant_id = s.merchant_id)
UNION ALL
SELECT o.mcp_shop_domain AS domain, o.region AS region
  FROM merchant_onboarding o
 WHERE lower(COALESCE(o.mcp_platform, '')) = 'shopify'
   AND COALESCE(o.mcp_shop_domain, '') <> ''
   AND NOT EXISTS (SELECT 1 FROM merchant_stores s
                    WHERE s.merchant_id = o.merchant_id AND s.status IN ('active', 'connected'))
   AND EXISTS (SELECT 1 FROM products_cache pc WHERE pc.merchant_id = o.merchant_id)
"""

#: Two-letter values of `merchant_onboarding.region` that are NOT countries a buyer can be in:
#: "EU" is on the signup form's own list, and "UK" is how people spell GB. Anything else that
#: passes `facts.normalize_market` is taken as the country it names.
_REGION_NOT_A_COUNTRY = frozenset({"EU", "UK"})


def _connected_market(region: Any) -> Optional[str]:
    """A connected store's declared region as a market, or None (skipped and counted)."""
    market = facts.normalize_market(region)
    return None if market in _REGION_NOT_A_COUNTRY else market

#: `source_domain` IS FOLDED, BY THE ROUTE'S EXPRESSION, CHARACTER FOR CHARACTER. `:domain` is a
#: population key, which is already canonical (lower case, one leading `www.` removed); the
#: Shopify sync writes `source_domain` as Shopify's `shop_domain`, which in production is
#: `www.Brand.com`. A bare `lower()` compare therefore found NO variant and NO price for exactly
#: the `www.`-hosted Shopify merchants the purchase gate exists for — the preflight then ran with
#: no variant hint and no expected price, measuring a representative variant it chose itself and
#: no drift at all. The text is identical to `routes/agent_commerce_reap._PRODUCT_SQL`'s and
#: identical on both dialects (SQLite has no `regexp_replace`), and it is a literal here so the
#: PREPARE sweep plans it.
#:
#: OUR indexed unit price for a variant on a domain. `catalog_offers.market` is NOT a conjunct:
#: it is a NOT NULL DEFAULT 'US' that the external-seed writer never sets, so it claims 'US' for
#: rows that are not, and the base cart-link loader (`_CART_OFFER_SQL`) already excludes it for
#: that reason. Joining on it would silently price zero merchants.
_CATALOG_PRICE_SQL = """
SELECT s.source_variant_id AS variant_id,
       o.currency AS currency,
       CAST(COALESCE(o.merchant_effective_price, o.estimated_best_price, o.list_price) AS TEXT) AS price
  FROM catalog_products p
  JOIN catalog_skus s ON s.product_key = p.product_key
  JOIN catalog_offers o ON o.sku_key = s.sku_key
 WHERE CASE WHEN lower(p.source_domain) LIKE 'www.%'
            THEN substr(lower(p.source_domain), 5)
            ELSE lower(p.source_domain) END = :domain
   AND s.source_variant_id = :variant_id
   AND COALESCE(o.merchant_effective_price, o.estimated_best_price, o.list_price) IS NOT NULL
 ORDER BY COALESCE(o.merchant_effective_price, o.estimated_best_price, o.list_price) ASC
 LIMIT 1
"""

#: A representative variant for a domain when the Tier B lane has none. Deterministic ordering so
#: two runs pick the same variant and their prices are comparable. Folded exactly as above.
_CATALOG_VARIANT_SQL = """
SELECT s.source_variant_id AS variant_id
  FROM catalog_products p
  JOIN catalog_skus s ON s.product_key = p.product_key
 WHERE CASE WHEN lower(p.source_domain) LIKE 'www.%'
            THEN substr(lower(p.source_domain), 5)
            ELSE lower(p.source_domain) END = :domain
   AND s.source_variant_id IS NOT NULL
   AND s.source_variant_id <> ''
 ORDER BY length(s.source_variant_id) ASC, s.source_variant_id ASC
 LIMIT 1
"""


@dataclass(frozen=True)
class Target:
    """One merchant x market to check. `variant_id` may be None — the preflight then picks a
    representative available variant itself, for the right market."""

    domain: str
    market: str
    variant_id: Optional[str] = None
    expected_price_minor: Optional[int] = None
    expected_currency: Optional[str] = None


async def _rows(statement: str) -> List[Dict[str, Any]]:
    """One population query. RAISES on a read failure; `load_population` is what fails soft,
    and it COUNTS the lane it could not read (`UNREADABLE_TALLY`) so the difference between "this
    lane is empty" and "this lane could not be read" reaches the report and the exit code."""
    return [dict(r) for r in await database.fetch_all(statement)]


def _population_key(value: Any) -> str:
    """One allowlist row's domain as the canonical merchant, or `""` (skipped by the caller).

    THE ROUTE'S CANONICALISER, so a population key is exactly the merchant key the purchase route
    matches a request under, and exactly what `_CATALOG_*_SQL`'s fold produces from a
    `source_domain`. It agrees with `facts.normalize_domain` (the fact table's own key, applied
    again by `record_check`) on every bare host name; where they differ is a row that is NOT a
    bare host — `https://brand.com/`, a port, a path, a trailing dot — which
    `facts.normalize_domain` would reduce to a host and this refuses (and `load_population`
    counts, for `SweepReport.population_skipped_unusable`). Such a row can never be admitted by the route (its folded
    spelling never equals a validated request key), so a fact about it would gate nothing, and
    sweeping it would spend a preflight on a merchant nobody can buy from.
    """
    try:
        return canonical_merchant_domain(value)
    except ValueError:
        return ""


#: The `SweepReport` count of allowlist rows `_population_key` refused. A COUNT, never the rows.
UNUSABLE_TALLY = "population_skipped_unusable"

#: The `SweepReport` count of allowlist rows whose MARKET the one ISO-2 rule could not key. Both
#: allowlist tables CHECK their market column today, so this is expected to read 0 — it exists so
#: that a row which does get past them is visible rather than silently dropped (or, as before
#: 2026-09-26, silently swept under a truncated market).
MARKET_UNKNOWN_TALLY = "population_skipped_market_unknown"

#: The `SweepReport` count of population LANES (the two allowlists and the connected stores) whose
#: read raised. The run still sweeps whatever the other lanes returned — one unreadable table
#: should not stop the merchants the others name from being refreshed — but the population is
#: incomplete, and
#: `exit_code_for` turns any non-zero count into a FAILED job execution. Before this count the
#: two lanes failed soft and silently: a database that answered nothing produced
#: `population=0 ... errors=0`, which read as a clean run.
UNREADABLE_TALLY = "population_unreadable"


async def load_population(
    limit: int, *, tally: Optional[Dict[str, int]] = None
) -> List[Target]:
    """The merchant x market set to sweep, bounded and deduped.

    `tally`, when given, has `UNUSABLE_TALLY` incremented once per allowlist ROW whose domain is
    not a bare host name and was therefore left out. Counted rather than silently dropped: such a
    row is an operator typo that makes a merchant unbuyable, and the run report is where it shows.
    `MARKET_UNKNOWN_TALLY` counts the same way for a market, and `UNREADABLE_TALLY` once per
    LANE whose read raised (that lane then contributes nothing; the other is still swept).

    The three lanes are unioned on (domain, market). WHERE THEY OVERLAP THE CART LANE WINS the
    variant, because its `variant_id` was confirmed available on the storefront for that market
    by the Tier B job — a stronger fact than anything the catalog holds. The connected-store
    lane carries no variant of its own.
    """
    merged: Dict[Tuple[str, str], Optional[str]] = {}
    # THE VARIANT LANE'S SET COMES FROM THE LANE ITSELF, not from a second copy of its predicate
    # written here. `routes/agent_commerce_reap` decides eligibility with the same sentinel this
    # helper filters on, so the population cannot be narrower than what the door accepts — which
    # it was, until a reviewer measured a merchant the route admitted and the sweep never visited
    # (the sweep additionally required `variant_key = ''`; the route never has).
    def _count(name: str) -> None:
        if tally is not None:
            tally[name] = tally.get(name, 0) + 1

    # THE MARKET IS NEVER DEFAULTED. `facts.normalize_market` is the one ISO-2 rule and answers
    # None for anything it cannot key; such a row is SKIPPED and COUNTED (MARKET_UNKNOWN_TALLY),
    # never swept as "US" and never truncated ("USA" is not "US"). Counted independently of the
    # domain tally, so a row wrong in both ways shows up in both counts.
    def _key(domain: Any, market: Any) -> Optional[Tuple[str, str]]:
        key_domain = _population_key(domain)
        key_market = facts.normalize_market(market)
        if not key_domain:
            _count(UNUSABLE_TALLY)
        if key_market is None:
            _count(MARKET_UNKNOWN_TALLY)
        if not key_domain or key_market is None:
            return None
        return (key_domain, key_market)

    # FAIL SOFT PER LANE, BUT COUNTED. See UNREADABLE_TALLY. `strict=True` because the ledger's
    # default form swallows the error into an empty list, which is exactly the ambiguity the
    # count exists to remove.
    async def _lane(read: Callable[[], Any], name: str) -> List[Dict[str, Any]]:
        try:
            return list(await read())
        except Exception as exc:  # noqa: BLE001 — never BaseException; cancellation travels
            _count(UNREADABLE_TALLY)
            logger.warning(
                "merchant_purchasability_sweep: the %s population lane could not be read "
                "(error_type=%s)", name, type(exc).__name__,
            )
            return []

    for row in await _lane(lambda: ledger.list_enabled_merchant_markets(strict=True), "variant"):
        key = _key(row.get("merchant_domain"), row.get("market_country"))
        if key is not None:
            merged.setdefault(key, None)
    for row in await _lane(lambda: _rows(_CART_LANE_SQL), "cart-link"):
        key = _key(row.get("domain"), row.get("market"))
        variant = str(row.get("variant_id") or "").strip() or None
        if key is not None and (variant or key not in merged):
            merged[key] = variant
    for row in await _lane(lambda: _rows(_CONNECTED_LANE_SQL), "connected-store"):
        key = _key(normalize_shop_host(row.get("domain")), _connected_market(row.get("region")))
        if key is not None:
            merged.setdefault(key, None)

    # Least-recently-checked FIRST, and never-checked first of all — ordered before the catalog
    # lookups so that a big population does not spend its budget pricing merchants this run will
    # not reach. Sorted in Python against what `list_due` reports rather than in SQL, because the
    # population comes from two OTHER tables and cannot be joined to this one portably.
    seen: Dict[Tuple[str, str], Any] = {}
    for row in await facts.list_due(500, vantage=facts.WORKER_VANTAGE):
        key = (str(row.get("merchant_domain") or ""), str(row.get("market_country") or ""))
        seen[key] = row.get("checked_at")

    def _staleness(key: Tuple[str, str]) -> Tuple[int, str]:
        checked = seen.get(key)
        # (0, "") sorts a never-checked merchant ahead of every checked one; the ISO string
        # orders the rest oldest-first without comparing an aware datetime to a naive one.
        return (1, checked.isoformat()) if checked is not None else (0, "")

    ordered = sorted(merged.items(), key=lambda item: (_staleness(item[0]), item[0]))

    targets: List[Target] = []
    for (domain, market), variant in ordered[: max(1, int(limit))]:
        if variant is None:
            variant = await _catalog_variant(domain)
        price_minor, currency = await _catalog_price(domain, variant) if variant else (None, None)
        targets.append(Target(domain, market, variant, price_minor, currency))
    return targets


async def _catalog_variant(domain: str) -> Optional[str]:
    try:
        row = await database.fetch_one(_CATALOG_VARIANT_SQL, {"domain": domain})
    except Exception:  # noqa: BLE001
        return None
    return (str(row["variant_id"]).strip() or None) if row is not None else None


async def _catalog_price(domain: str, variant_id: str) -> Tuple[Optional[int], Optional[str]]:
    """OUR indexed unit price, in exact minor units. `(None, None)` whenever it cannot be read
    EXACTLY — a price we had to round is not a price a drift may be computed from."""
    from services.shopify_cart_link_preflight import amount_to_minor

    try:
        row = await database.fetch_one(_CATALOG_PRICE_SQL, {"domain": domain, "variant_id": variant_id})
    except Exception:  # noqa: BLE001
        return None, None
    if row is None:
        return None, None
    currency = str(row["currency"] or "").strip().upper() or None
    if not currency:
        return None, None
    return amount_to_minor(str(row["price"] or "").strip(), currency), currency


# ── the run ─────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SweepReport:
    """What one run did. COUNTS ONLY — no domains, no variant ids, no rows.

    `skipped_disabled` is 1 when the dial was off. `checked` counts merchant x vantage pairs
    actually fetched; `positive` / `negative` / `unverifiable` partition them by what the store
    said. `errors` is the only count that should page anyone.
    """

    population: int = 0
    #: Allowlist rows left out of the population because their domain is not a bare host name
    #: (a URL, a port, a trailing dot...). The purchase route can never admit such a row, so each
    #: one is a merchant an operator meant to enable and did not.
    population_skipped_unusable: int = 0
    #: Allowlist rows left out because their MARKET is not ISO-2 (absent, blank, "USA"...). The
    #: market is never defaulted or truncated, so such a row is not swept; see
    #: `MARKET_UNKNOWN_TALLY`.
    population_skipped_market_unknown: int = 0
    #: Population lanes (0..2) whose read RAISED, or 1 when the population could not be built at
    #: all. Non-zero means this run swept an incomplete population; the CLI exits
    #: EXIT_POPULATION_UNREADABLE for it. See `UNREADABLE_TALLY`.
    population_unreadable: int = 0
    checked: int = 0
    positive: int = 0
    negative: int = 0
    unverifiable: int = 0
    written: int = 0
    abandoned_budget: int = 0
    errors: int = 0
    skipped_disabled: int = 0
    duration_ms: int = 0


_COUNTS = (
    "population", "population_skipped_unusable", "population_skipped_market_unknown",
    "population_unreadable", "checked", "positive", "negative", "unverifiable", "written",
    "abandoned_budget", "errors", "skipped_disabled",
)

#: Indirected so a test can make the budget elapse without sleeping. MONOTONIC: the budget must
#: survive an NTP step.
_monotonic: Callable[[], float] = time.monotonic

#: The one thing that touches a merchant. Indirected so tests inject a fake fetcher and no test
#: ever opens a socket — the rail's autouse forbid-httpx fixture would fail them if they did.
_preflight = preflight


def _new_pacer() -> RequestPacer:
    """ONE pacer per run, shared by every merchant and every vantage. Indirected so a test can
    hand in a fake clock and sleep and prove the spacing without waiting."""
    return RequestPacer(MIN_REQUEST_INTERVAL_S)


def _inner_transport(via: Optional[str]) -> httpx.AsyncBaseTransport:
    """The real transport under the pacer. `via` is the proxy vantage's URL, or None for the
    direct (crawl-egress) vantage. Passing our own transport switches off httpx's environment-
    proxy lookup, so a process HTTPS proxy (an operator's laptop) is honoured here explicitly for
    the direct vantage, exactly as before. Indirected so a test can mount a mock transport."""
    proxy = via or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or None
    return httpx.AsyncHTTPTransport(proxy=proxy)


def _click_id() -> str:
    """A fresh click id per check. It is written as a cart attribute on the merchant's abandoned
    checkout, so it is deliberately a throwaway that ties to no buyer and no campaign."""
    return f"clk_mpx_{uuid.uuid4().hex[:16]}"


async def _check_one(
    target: Target, *, vantage: str, client: Optional[httpx.AsyncClient]
) -> Optional[PreflightResult]:
    """One merchant, one vantage. `buyer=None` ALWAYS: the Reap path carries no buyer data in the
    link, and this sweep has no buyer to carry."""
    return await _preflight(
        target.domain,
        market=target.market,
        variant_id=target.variant_id,
        quantity=1,
        buyer=None,
        click_id=_click_id(),
        check_card=True,
        expected_price_minor=target.expected_price_minor,
        expected_currency=target.expected_currency,
        client=client,
    )


async def run_merchant_purchasability_sweep() -> SweepReport:
    """One sweep. Returns a `SweepReport` and never raises for anything a merchant did.

    It DOES let `asyncio.CancelledError` through: `except Exception`, never `except BaseException`,
    so a caller that cancels (a test, an embedding event loop) actually stops it. The job's hard
    stop is NOT a cancellation — Cloud Run's task timeout kills the container — which is why the
    budget, not the timeout, is meant to end a run (see the setup script).
    """
    started = _monotonic()
    counts = {name: 0 for name in _COUNTS}

    def _report() -> SweepReport:
        return SweepReport(duration_ms=int((_monotonic() - started) * 1000), **counts)

    def _budget_spent() -> bool:
        return (_monotonic() - started) >= budget_seconds

    budget_seconds = _env_int(DIALS["budget_seconds"])

    # THE GATE, INSIDE THE JOB. See the module header: with it off this touches no merchant.
    if not is_enabled():
        counts["skipped_disabled"] = 1
        # INFO, on the operator logger: the runbook's rollback step says "the sweep then
        # returns skipped_disabled=1", and at the hourly interval this is the one line that
        # shows the worker still ticking with the dial off. Counts-only, like the report.
        operator_logger.info("merchant_purchasability_sweep: disabled; no merchant was contacted")
        return _report()

    batch = _env_int(DIALS["batch"])
    pause_s = _env_int(DIALS["pause_ms"]) / 1000.0

    tally: Dict[str, int] = {}
    try:
        targets = await load_population(batch, tally=tally)
    except Exception:  # noqa: BLE001 — a population read must not end the run with a traceback
        counts["errors"] += 1
        counts["population_unreadable"] = max(1, tally.get(UNREADABLE_TALLY, 0))
        logger.exception("merchant_purchasability_sweep: the population could not be built")
        report = _report()
        operator_logger.info("merchant_purchasability_sweep: %s", report)
        return report
    counts["population"] = len(targets)
    counts["population_unreadable"] = tally.get(UNREADABLE_TALLY, 0)
    if counts["population_unreadable"]:
        operator_logger.warning(
            "merchant_purchasability_sweep: %d population lane(s) could not be read; this run's "
            "population is incomplete", counts["population_unreadable"],
        )
    counts["population_skipped_unusable"] = tally.get(UNUSABLE_TALLY, 0)
    if counts["population_skipped_unusable"]:
        # ONCE PER RUN, BY COUNT. Not the domains: the report rule is counts only, and the
        # runbook's census query names the rows for whoever reads this.
        operator_logger.warning(
            "merchant_purchasability_sweep: %d allowlist row(s) skipped: domain is not a bare "
            "host name", counts["population_skipped_unusable"],
        )
    counts["population_skipped_market_unknown"] = tally.get(MARKET_UNKNOWN_TALLY, 0)
    if counts["population_skipped_market_unknown"]:
        operator_logger.warning(
            "merchant_purchasability_sweep: %d allowlist row(s) skipped: market is not an ISO-2 "
            "code (never defaulted)", counts["population_skipped_market_unknown"],
        )

    vantages: List[Tuple[str, Optional[str]]] = [(facts.WORKER_VANTAGE, None)]
    proxy = proxy_url()
    if proxy:
        vantages.append((facts.PROXY_VANTAGE, proxy))

    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
    pacer = _new_pacer()
    for target in targets:
        if _budget_spent():
            # Stop STARTING merchants. The rest are picked up next tick, least-recently-checked
            # first, so nothing is starved — and no merchant is contacted for a run we cannot
            # finish, which on this rail means no abandoned checkout we did not need.
            counts["abandoned_budget"] += 1
            continue
        for vantage, via in vantages:
            # PER-MERCHANT, PER-VANTAGE try/except. One store that hangs, 403s or returns
            # something nobody has classified must not end the batch behind it.
            try:
                transport = PacedTransport(_inner_transport(via), pacer)
                async with httpx.AsyncClient(
                    headers=headers, timeout=REQUEST_TIMEOUT_S, transport=transport
                ) as client:
                    result = await _check_one(target, vantage=vantage, client=client)
            except Exception as exc:  # noqa: BLE001 — never BaseException; see the docstring
                counts["errors"] += 1
                # THE TYPE AND THE VANTAGE, AND NOTHING ELSE. A driver or client exception's
                # message carries the URL, which carries the merchant.
                logger.error(
                    "merchant_purchasability_sweep: a check raised (vantage=%s error_type=%s)",
                    vantage, type(exc).__name__,
                )
                continue
            if result is None:
                counts["errors"] += 1
                continue
            counts["checked"] += 1
            if facts.is_positive(result):
                counts["positive"] += 1
            elif facts.is_negative(result):
                counts["negative"] += 1
            else:
                counts["unverifiable"] += 1
            try:
                written = await facts.record_check(
                    target.domain, target.market, result,
                    vantage=vantage, expected_price_minor=target.expected_price_minor,
                )
            except Exception as exc:  # noqa: BLE001
                counts["errors"] += 1
                logger.error(
                    "merchant_purchasability_sweep: a fact could not be written "
                    "(vantage=%s error_type=%s)", vantage, type(exc).__name__,
                )
                continue
            if written is None:
                counts["errors"] += 1
            else:
                counts["written"] += 1
        if pause_s:
            await asyncio.sleep(pause_s)

    report = _report()
    # THE PROOF LINE. Lands on the worker's stdout as
    # `[ts] INFO - merchant_purchasability_sweep: SweepReport(...)`; see the logger note at the
    # top of the module for why it cannot go through `logger`.
    operator_logger.info("merchant_purchasability_sweep: %s", report)
    return report


# ── CLI: the Cloud Run Job's entry point ───────────────────────────────────────────────────
#
# `python -m jobs.merchant_purchasability_sweep` runs ONE sweep and exits; Cloud Scheduler is the
# clock (infra/gcp/setup_merchant_purchasability_sweep_job.sh). The exit code is the verdict of
# the execution: non-zero fails it and trips the "Cloud Run job failing" alert, and the job has
# --max-retries 0, so a failed run is not re-run (a re-run is another round of abandoned
# checkouts; a human decides).

#: Done. Includes a run with the gate off (nobody contacted), a run whose population was empty,
#: and a run the budget cut short — the rest are first in line next hour.
EXIT_OK = 0
#: A population lane could not be read, the population could not be built at all, or the database
#: could not be connected to (nothing was read). The run swept whatever it could read, but a
#: merchant on an unread allowlist was not refreshed, and with MERCHANT_PURCHASABILITY_ENFORCE on
#: its fact is ageing out towards a 409. (Python also exits 1 on an uncaught traceback; the log
#: tells the cases apart, and every one of them means "the population was not read".)
EXIT_POPULATION_UNREADABLE = 1
#: The population was read, but at least one check raised or one fact could not be written —
#: `SweepReport.errors`, "the only count that should page anyone" (the runbook). Numbered like
#: the Tier B job's EXIT_RECORD_FAILED.
EXIT_ERRORS = 4


def exit_code_for(report: SweepReport) -> int:
    """The execution's exit code, from the report alone. An unreadable population outranks
    per-merchant errors: it is the one that says the run did not see what it had to."""
    if report.population_unreadable:
        return EXIT_POPULATION_UNREADABLE
    if report.errors:
        return EXIT_ERRORS
    return EXIT_OK


async def amain() -> int:
    """One sweep against the process database, then its exit code. `main` minus the event loop,
    so a test can drive the CLI's whole path on the test's own loop and connection.

    With the gate off the database is never connected: the run returns `skipped_disabled=1`
    before it would read anything, and a dark job must not even open a connection."""
    if not is_enabled():
        return exit_code_for(await run_merchant_purchasability_sweep())
    connected_here = not database.is_connected
    if connected_here:
        try:
            await database.connect()
        except Exception as exc:  # noqa: BLE001 — mapped to an exit code, not a traceback
            operator_logger.error(
                "merchant_purchasability_sweep: could not connect to the database (error_type=%s); "
                "no population was read", type(exc).__name__,
            )
            return EXIT_POPULATION_UNREADABLE
    try:
        report = await run_merchant_purchasability_sweep()
    finally:
        if connected_here:
            try:
                await database.disconnect()
            except Exception as exc:  # noqa: BLE001 — the run's verdict is already decided
                logger.warning(
                    "merchant_purchasability_sweep: disconnect failed (error_type=%s)",
                    type(exc).__name__,
                )
    return exit_code_for(report)


def main(argv: Optional[List[str]] = None) -> int:
    """No options: every knob is an env dial (DIALS, read per run), which the setup script pins
    on the job. `--help` still works, so an operator can ask."""
    import argparse  # noqa: PLC0415 — only the CLI needs it

    argparse.ArgumentParser(
        prog="python -m jobs.merchant_purchasability_sweep",
        description="Run ONE merchant purchasability sweep and exit "
        f"({EXIT_OK}=ok, {EXIT_POPULATION_UNREADABLE}=population unreadable, "
        f"{EXIT_ERRORS}=a check or a write failed).",
    ).parse_args(argv)
    return asyncio.run(amain())


if __name__ == "__main__":
    raise SystemExit(main())
