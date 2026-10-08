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
SHOPIFY STORES, PLUS EVERY HOST THE CART MINTER BUILDS A SEED CART ON (`collect_population`). The
two allowlists are EXACTLY what `routes/agent_commerce_reap.py` consults before it will start a
purchase — the variant lane through `_eligibility`, the cart-link lane through
`is_cart_link_eligible` — and nothing else in this repo can make the door offer to buy.

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
  * NOT A TEST MERCHANT: a merchant `services.test_merchant_policy.get_excluded_merchant_ids`
    excludes (the static rig ids plus every `pivota-review-demo*` store) is skipped and counted
    as `population_skipped_test_merchant`. That is the set search already hides from buyers, so
    a fact about it gates no card anyone is served, and checking it only leaves an abandoned
    checkout on our own store. Skipped BEFORE the host and market rules, so a rig's junk region
    is not reported as `population_skipped_market_unknown`.
  * THE HOST is `normalize_shop_host(domain)` — the function `shopify_cart_base_url` builds the
    card's cart on — then the same `_population_key` every lane goes through.
  * THE MARKET is the merchant's declared `merchant_onboarding.region`, and ONLY when it is an
    ISO-2 country: the column is a free-text signup field whose own comment says "US, EU,
    APAC", so "EU" (two letters, not a country) and "UK" (Shopify's code is GB) are refused
    with "APAC" and counted as `population_skipped_market_unknown` — never defaulted, never
    mapped. Declared, not measured: a store that cannot sell there comes back negative or
    unverifiable, which is the answer an absent fact already gives, so a wrong region can
    only cost one abandoned checkout, never open a cart.

  EVERY CONNECTED STORE IS A TEST STORE TODAY (Peng, 2026-09-29): there is no real
  outside-merchant connection yet, and the lane exists so the first one is covered on day one.
  Measured on prod 2026-09-29, the policy's own rules over the lane's own rows: 6 rows, 4 of them
  listed test merchants and now skipped — merch_efbc46b4619cfbdf (ijaqit-v9),
  merch_bbd34645bc1950cc (i9j3i0-kj) and the two `pivota-review-demo*` legacy stores. Two are NOT
  on the list: "Pivota Live Demo Store" (merch_c5e24a8d3738d73b, ijaqit-v9, region US) is still
  swept, and the legacy mec3xu-zd.myshopify.com (merch_shopify_0c74768217e098809ab3, region
  "shopify") is still counted as market-unknown. Listing either hides it from search too.

THE FOURTH LANE IS THE CART MINTER ITSELF. The same card gate, and `offers.resolve`, mint an
EXTERNAL SEED's prefilled cart only on a fresh positive fact for the cart host x the buyer's
market. The #2407 census found 432 cart offers on 53 hosts, 358 of them on 47 hosts nobody had
ever checked, and the census was a floor (see `_cart_mint_lane`). So the population now includes
every host the cart minter WOULD build a cart on, computed BY THE MINTER'S OWN FUNCTIONS over every
active seed — `_external_seed_redirect_identity`, `HandoverVariantResolver.choose` and
`resolve_cart_permalink` in routes/agent_shop_gateway, the chain every seed lane runs — and never
by a predicate restated here, so the population cannot be narrower than what mints a cart.
tests/test_merchant_purchasability.py drives the real mint endpoint over the same rows and holds
that every cart host it mints is in the population.

  * THE HOST is `facts.normalize_domain(<cart base url>)`, which is what
    `_CartPurchasabilityGate.allows_cart` asks the fact for.
  * THE MARKET is the seed row's `market`, the market it is listed and served in (every cart seed
    in the census was US). The gate asks the BUYER's market, which the population cannot know; a
    buyer in another market gets no cart, which is the answer no fact already gives. Never
    defaulted: a seed with no ISO-2 market is counted in `population_skipped_market_unknown`.
  * SCANNED AT MOST ONCE A DAY. The scan reads every active seed on the prod primary, so it runs
    on the first sweep of each 05:00Z slot and the other runs read the cached result
    (db/merchant_purchasability_cart_mint_scans.py); see `_cart_mint_population` for how a
    failed scan falls back without paging every hour.
  * NO VARIANT. The seed's cart variant is not confirmed available the way a Tier B variant is, so
    where the Tier B lane confirmed one IT STILL WINS, and elsewhere the catalog hint or the
    preflight's own pick is used, exactly as for the connected lane.

Wix and WooCommerce connected stores stay out on purpose: no card lane mints a cart for them
(`_attach_connected_product_redirects` gives only Shopify a `shop_domain`), and the preflight is
Shopify-only, so a check could only ever come back unverifiable.

One representative variant per merchant x market: the Tier B row's confirmed `variant_id` when we
have one, else — FOR A KEY A REAP LANE NAMES — our catalog's variant for that domain, else NONE,
and with none the preflight picks the storefront's first available variant itself, reading
availability with `?country=<market>` pinned. The expected price is OUR indexed unit price for
that variant when we hold one, so that a drift is a statement about our index and not about an
unrelated SKU.

The catalog hint is for the REAP lanes only, because only the Reap rail buys the variant our
index names. A key only the connected-store or cart-mint lane names gets NONE: its fact answers
"does this store's checkout take a card in this market", and a hinted id that is stale or not a
Shopify variant id at all (the shortest `source_variant_id` on a seed host can be a SKU) turns
that into VARIANT_GONE — a CONFIRMED NEGATIVE that demotes the store — or INVALID_INPUT, which
can never become positive. Either would block the very carts that lane exists to open.

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

With `CRAWL_SHOPIFY_EDGE_PACER_ENABLED` (set on the job by the setup script, as on the Reap proof
jobs) every direct-vantage request also takes a slot of the aggregate Shopify-edge budget shared by
every crawl job that sets the flag (services/shopify_edge_pacer.py, #2474): `PacedTransport(...,
shopify_edge=True)`. Every merchant here is a Shopify store, so no host learning is needed. This
job's pacer has NO deadline, so a shared wait is bounded by the edge pacer's horizon (~60 s at
lease 2), not by the run budget; and its own spacing is already under the shared rate, so the flag
mostly keeps the other jobs' share honest.

── AN IP THROTTLE STOPS THE RUN ─────────────────────────────────────────────────────────────

Measured on prod, 2026-10-07 07:08Z onward (and 10-05 10:07Z -> 10-06 00:08Z): 17-20 of the 20
merchants unverifiable every hour, 163 of ~196 checks in 24 h answering
`products_json_page_1_status_429` across unrelated stores: an edge limiter in front of every
store, not 20 stores each deciding (the 2026-09-30 signature, services/crawl_ip_throttle.py). And
the sweep kept asking all 20 stores every hour, adding to the volume the limiter counts.

NOT THE ADDRESS'S HISTORY. At 23:56Z on 10-07 the crawl NAT was swapped to a spare IP
(34.82.242.65); the 00:07Z sweep was still 17/20 unverifiable, and a one-off probe from the fresh
address, with this module's exact headers, got `429`, `server: cloudflare`, `retry-after: 60`,
text/plain `local_rate_limited`, no `cf-mitigated`, on its FIRST request to flowerknows.co and
dermalogica.com. So a new IP does not help, and the only lever is ours: ask less, and stop asking
once the limiter is evidently on. (Changing how the crawler presents itself is out of scope.)
"IP_THROTTLE" is the shared name of the line and the breaker; what it measures is "many distinct
stores throttling THIS crawler at once", whatever the limiter keys on. Two changes:

  * THE PREFLIGHT NAMES A THROTTLE (`shopify_cart_link_preflight.throttle_signal`): a 429, a 503
    with Retry-After, or a Cloudflare `cf-mitigated` challenge, on ANY request, comes back
    retryable=True, `throttled=True`, verdict TRANSPORT_ERROR (a challenged CHECKOUT keeps its old
    BLOCKED_UNKNOWN, which the Tier B lane needs definite). Before, a throttled catalog page was
    VARIANT_UNVERIFIED with retryable=False. A login or password wall reached first still wins:
    LOGIN_REQUIRED stays the confirmed negative it is. Both are unverifiable, so rule 3 of
    db/merchant_purchasability.py already held -- no throttle ever advanced
    `consecutive_failures` or cleared `positive_until` -- and it still does; what changes is that
    the row says what happened. NOTE what rule 3 cannot do: a positive window is only RENEWED by a
    positive check, so a throttle that lasts longer than the TTL (72 h) still ages every merchant
    out to browse_only. Stopping the hammering is what shortens the throttle.
  * ONE BREAKER PER RUN (`SweepThrottleBreaker`, #2473's `IpThrottleBreaker`), installed for the
    loop and fed every answer with its headers through `crawl_politeness.note_response`. It trips
    when `MERCHANT_PURCHASABILITY_IP_THROTTLE_TRIP_HOSTS` (3) DISTINCT hosts (`www.` folded) are throttled
    within `..._WINDOW_SECONDS` (1200 s, the task timeout: "in one run"). #2473's default of 10
    in 60 s is calibrated on a lane reaching hundreds of hosts at ~4 req/s; this one reaches ~20
    stores one at a time, ~3 s apart, and on 10-07 that rule would have let 10 of the 20 through
    before stopping. A trip STOPS THE RUN: no further merchant is contacted, nothing is written
    for them (`skipped_ip_throttle`), the run prints `IP_THROTTLE {json}` (the refresh's fields,
    so one log filter alerts on both) and EXITS 0. A false trip costs one hour's renewal for the
    merchants not reached, against a 72 h TTL.

WHY A TRIP EXITS 0, unlike the nightly external-referral refresh's 3. "prod: Cloud Run job
failing" (infra/gcp/setup_monitoring.sh) fires on ANY failed task, per job, in 300 s windows, and
this job runs HOURLY: the throttle windows measured so far lasted 14-17 h, so a non-zero trip
would page every hour of every window -- for a condition nothing on our side can fix in the hour.
That policy's own description sets the convention: an outcome the job RECORDS exits 0 and pages
through its own policy. The trip is recorded (the `IP_THROTTLE` line, `ip_throttled=1` in the
report); the page is "prod: purchasability sweep IP-throttled" on that line (setup_monitoring.sh),
one incident per window, not the job's exit. Non-zero stays for
what IS broken: an unreadable population (1) and a check or write that failed (4).

THE CHECKS THAT DID COMPLETE ARE WRITTEN, throttled ones included, as unverifiable rows. That is
deliberate. A throttled row keeps its window and its failure count (rule 3), so writing it costs
the merchant nothing, and it moves the row's `checked_at`, which is what rotates the due list.
Left unwritten, a few stores that throttle us ON THEIR OWN (a store-level Cloudflare rule) would
stay first in line forever, trip the breaker at the head of every run, and starve every merchant
behind them until their windows lapsed. Written, they sink to the back, and next hour the run
starts with the merchants this one did not reach.

── PII ──────────────────────────────────────────────────────────────────────────────────────

`SweepReport` carries COUNTS AND A DURATION. No domains, no variant ids, nothing from a row. The
preflight is called with `buyer=None` — the Reap path carries no buyer data in the link — so
there is no buyer anywhere on this path to log or store.

── WHAT ONE RUN COSTS A MERCHANT ────────────────────────────────────────────────────────────

One abandoned Shopify checkout per merchant per vantage, the same side effect every UCP probe
already has. That is why the batch is bounded, the run is budgeted, and the population is
least-recently-checked first. NOTE the ordering is a rotation, not a TTL filter: a population no
larger than the batch is checked IN FULL on every run, and a larger one is covered every
ceil(population_total / batch) runs, never-checked merchants first. The per-merchant and
freshness arithmetic is in docs/runbooks/merchant_purchasability.md ("Politeness" and
"Capacity").
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

import db.merchant_purchasability as facts
import db.merchant_purchasability_cart_mint_scans as mint_scans
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
# THE test-merchant policy search already serves by (the find_products_multi rig filter). One owner:
# the connected-store lane skips exactly the merchants it excludes. See `_CONNECTED_LANE_SQL`.
from services.test_merchant_policy import get_excluded_merchant_ids
from services.shopify_cart_link_preflight import (
    REQUEST_TIMEOUT_S,
    USER_AGENT,
    PreflightResult,
    preflight,
    throttle_signal,
)
# #2473's IP breaker and the response funnel that feeds it. See "AN IP THROTTLE STOPS THE RUN".
from services import crawl_ip_throttle, crawl_politeness
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
    "collect_population",
    "load_population",
    "merge_lanes",
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
    # THE IP BREAKER (see "AN IP THROTTLE STOPS THE RUN"): this many DISTINCT hosts throttled
    # within the window stops the run. 0 disarms it (diagnostics and the IP_THROTTLE line stay);
    # so does CRAWL_IP_THROTTLE_BREAKER_ENABLED=false, the switch every crawl lane shares.
    "ip_throttle_trip_hosts": _Dial("MERCHANT_PURCHASABILITY_IP_THROTTLE_TRIP_HOSTS", 3, 0, 50),
    # The default is the job's task timeout (setup script), i.e. "within one run".
    "ip_throttle_window_seconds": _Dial("MERCHANT_PURCHASABILITY_IP_THROTTLE_WINDOW_SECONDS", 1200, 60, 3600),
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
SELECT s.merchant_id AS merchant_id, s.domain AS domain, o.region AS region
  FROM merchant_stores s
  JOIN merchant_onboarding o ON o.merchant_id = s.merchant_id
 WHERE s.status IN ('active', 'connected')
   AND lower(s.platform) = 'shopify'
   AND COALESCE(s.domain, '') <> ''
   AND EXISTS (SELECT 1 FROM products_cache pc WHERE pc.merchant_id = s.merchant_id)
UNION ALL
SELECT o.merchant_id AS merchant_id, o.mcp_shop_domain AS domain, o.region AS region
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


#: The CART-MINT lane's read: every ACTIVE seed (the only status any seed lane serves), keyset
#: paged on the primary key, carrying exactly the columns the minter's identity chain reads.
#: Deliberately NO predicate beyond `status`: anything narrower would be a restatement of when a
#: cart is built, and the point of this lane is that the minter decides that.
_SEED_PAGE_SQL = """
SELECT id, domain, market, attached_product_key, attached_variant_id, external_product_id,
       destination_url, canonical_url, seller_ref, seed_kind, seed_data
  FROM external_product_seeds
 WHERE status = 'active'
   AND id > :after
 ORDER BY id
 LIMIT :page_rows
"""

#: Rows per page, and the product keys one `HandoverVariantResolver` is primed with. ONE
#: RESOLVER PER PAGE, sized to the page: a resolver stops priming at `max_keys` (default 400),
#: and past that every product key answers `not_primed` — no catalog variant, so no cart on the
#: evidence branch. The #2407 census ran ONE resolver over all 23,854 seeds and so resolved at
#: most 400 product keys; its 432 cart offers / 53 hosts are a floor, not the population.
_SEED_PAGE_ROWS = 500
#: A pause between pages, so an hourly full scan of the seed table is a trickle on the 2-vCPU
#: primary rather than a burst. Indirected so tests run it at 0.
_SEED_PAGE_PAUSE_S = 0.1
#: The resolver's lookup timeout for this lane. The serving default (0.5 s) bounds a BUYER's
#: request; nobody waits here, and a lookup that times out is a catalog variant we did not see,
#: i.e. a cart host silently missing from the population (it is counted, see below).
_SEED_HANDOVER_TIMEOUT_S = 20.0
#: Wall-clock bound on the whole scan, so a slow database cannot spend the run's budget before
#: the first merchant is checked. Stopping early leaves the lane INCOMPLETE, and that is counted.
_SEED_LANE_BUDGET_S = 300.0

#: The resolver outcomes that mean "the catalog was not consulted", as opposed to "consulted and
#: it named no variant". Either one can hide a cart the minter WOULD build with a fresh lookup.
_HANDOVER_NOT_CONSULTED = ("handover_lookup_failed", "handover_not_primed")


@dataclass
class CartMintLane:
    """What the cart-mint lane read. COUNTS and host keys; no seed ids, no URLs.

    `hosts` maps (normalised cart host, the seed row's raw `market`) to the number of seeds that
    mint a cart there; the key is validated by `load_population`'s one `_key` rule like every
    other lane's. `complete` is False when the scan stopped early or a catalog lookup failed —
    the lane then under-reports, and `load_population` counts it as unreadable."""

    hosts: Dict[Tuple[str, str], int]
    seeds_scanned: int = 0
    cart_seeds: int = 0
    complete: bool = True
    incomplete_reason: str = ""
    #: The scan's wall clock. It is spent from the run's budget, so it is logged every run.
    elapsed_ms: int = 0


def _seed_cart_hosts(gw: Any, resolver: Any, row: Dict[str, Any]) -> set:
    """Every host the cart minter would build a prefilled cart on for ONE seed row.

    THE MINTER'S OWN CHAIN — `HandoverVariantResolver.choose`, `_external_seed_redirect_identity`,
    `resolve_cart_permalink` — called over the UNION of the inputs its lanes differ on, so this is
    never narrower than any one of them:
      * every stored variant, PLUS THE EMPTY VARIANT. offers.resolve falls back to `[{}]`, and a
        card the gateway built may carry no variant id at all. The empty variant is also what
        makes the `named_variant_id` difference between the lanes irrelevant here: a name can
        only REFUSE a hand-over that the empty variant (no name, no claim) resolves anyway — a
        sole catalog row, or the seed's own stamp — while an exact match resolves whatever the
        claim. So `choose` is called in the card lanes' form, without it; the host a cart is
        built on never depends on which variant the cart names;
      * the product id from the row, from the snapshot, and none at all (the lanes read one or
        the other, and a gateway-built card may carry none); it decides whether a seed stamp
        that restates the product id is refused, and with none nothing is refused;
      * both destination spellings (offers.resolve hands `resolve_cart_permalink` the canonical
        URL, the card lanes the destination URL). The host depends on it only when the seed
        carries no shop domain at all.
    A seed with no http(s) URL mints no link in any lane, so it is skipped.
    """
    seed_data = gw._ensure_seed_data_obj(row.get("seed_data"))
    destination_url = str(row.get("destination_url") or seed_data.get("destination_url") or "")
    canonical_url = str(row.get("canonical_url") or seed_data.get("canonical_url") or destination_url)
    dests = [u for u in dict.fromkeys((canonical_url, destination_url)) if u.startswith(("http://", "https://"))]
    if not dests:
        return set()
    product_key = gw._handover_product_key(row, seed_data)
    product_ids = [*dict.fromkeys(
        str(p).strip() for p in (row.get("external_product_id"), seed_data.get("external_product_id"))
        if p is not None and str(p).strip()
    ), None]
    hosts: set = set()
    for variant in [*gw._seed_variants(seed_data), {}]:
        offer_variant_id = gw._seed_offer_variant_id(variant) or None
        for product_id in product_ids:
            handover = resolver.choose(
                product_key=product_key, product_id=product_id, seed_data=seed_data,
                offer_variant_id=offer_variant_id,
            )
            identity = gw._external_seed_redirect_identity(
                row=row, seed_data=seed_data, offer_variant_id=offer_variant_id, handover=handover,
            )
            for dest in dests:
                cart = gw.resolve_cart_permalink(
                    destination_url=dest,
                    shop_domain=identity.get("shop_domain"),
                    platform=identity.get("platform"),
                    cart_variant_id=identity.get("cart_variant_id"),
                )
                if cart:
                    # THE GATE'S KEY: `_CartPurchasabilityGate.allows_cart` asks the fact for
                    # `normalize_domain(cart_base_url)`.
                    hosts.add(facts.normalize_domain(cart))
    hosts.discard("")
    return hosts


async def _cart_mint_lane() -> CartMintLane:
    """Every (cart host, seed market) the cart minter would build a prefilled cart on.

    RAISES if a page cannot be read (`load_population` fails the lane soft and counts it). A
    catalog lookup that failed or never ran, or a scan cut short by `_SEED_LANE_BUDGET_S`, does
    not raise: what was found is kept and `complete` is set False, which is counted too.
    """
    # Imported HERE, not at module import: the route module is large, and a failure to import it
    # must be this lane's counted failure, not a job that cannot start.
    import routes.agent_shop_gateway as gw
    from services.handover_variant_identity import HandoverVariantResolver

    lane = CartMintLane(hosts={})
    started = _monotonic()
    after = ""
    while True:
        if (_monotonic() - started) >= _SEED_LANE_BUDGET_S:
            lane.complete, lane.incomplete_reason = False, "budget"
            break
        records = await database.fetch_all(
            _SEED_PAGE_SQL, {"after": after, "page_rows": _SEED_PAGE_ROWS}
        )
        if not records:
            break
        rows = []
        for record in records:
            row = dict(gw._row_to_dict(record))
            row["seed_data"] = gw._ensure_seed_data_obj(row.get("seed_data"))
            rows.append(row)
        after = str(rows[-1]["id"])
        resolver = HandoverVariantResolver(
            timeout_s=_SEED_HANDOVER_TIMEOUT_S, max_keys=max(1, len(rows))
        )
        await resolver.prime(gw._handover_product_key(row, row["seed_data"]) for row in rows)
        for row in rows:
            lane.seeds_scanned += 1
            hosts = _seed_cart_hosts(gw, resolver, row)
            if hosts:
                lane.cart_seeds += 1
            market = str(row.get("market") or "")
            for host in hosts:
                lane.hosts[(host, market)] = lane.hosts.get((host, market), 0) + 1
        if any(resolver.stats.get(name) for name in _HANDOVER_NOT_CONSULTED):
            lane.complete, lane.incomplete_reason = False, "catalog_lookup"
        if len(records) < _SEED_PAGE_ROWS:
            break
        if _SEED_PAGE_PAUSE_S:
            await asyncio.sleep(_SEED_PAGE_PAUSE_S)
    lane.elapsed_ms = int((_monotonic() - started) * 1000)
    return lane


# ── the cart-mint lane is scanned at most ONCE A DAY ─────────────────────────────────────────
#
# A scan reads every active seed's `seed_data` on the 2-vCPU prod primary, and the sweep runs 21
# times a day; database load there caused serving timeouts on 2026-09-26/27. So a scan runs on
# the first sweep of each UTC day's 05:00 slot (05:07, after the nightly crawls that share the
# database and the crawl address: see the setup script) and every other run reads the last
# complete scan from db/merchant_purchasability_cart_mint_scans.py. The seed table changes by
# nightly refresh and ingest, so a day-old population misses at most one day of new cart hosts —
# which a missing fact already answers safely (no cart).
#
# A FAILED OR INCOMPLETE SCAN DOES NOT PAGE EVERY HOUR. It is recorded, retried no sooner than
# `_SCAN_RETRY_S` later, and meanwhile the last complete scan is used as long as it is no older
# than `_SCAN_MAX_AGE_S` (whatever the failed scan did find is added to it). Only when there is
# no complete scan that young is the lane counted unreadable (exit 1): the population can then be
# missing hosts that have minted carts for days.
#
# AN UNREADABLE CACHE MEANS NO SCAN. If the table cannot be created or read, the lane cannot tell
# whether it scanned today, and scanning anyway would turn a broken table into an hourly full
# scan — the load this cache exists to prevent. It is counted unreadable instead.

#: The UTC hour whose slot a scan belongs to; a complete scan at or after the latest one is fresh.
_SCAN_SLOT_HOUR_UTC = 5
#: The least time between two scan ATTEMPTS after one failed or came back incomplete.
_SCAN_RETRY_S = 6 * 3600
#: The oldest complete scan a run may fall back to without counting the lane unreadable. The
#: fact TTL: past it, hosts minting carts since then may have no fact at all.
_SCAN_MAX_AGE_S = 72 * 3600

#: Indirected so tests move the clock across a slot without sleeping. An AWARE UTC datetime: it is
#: written as `scanned_at`, and a naive one binds as local wall time under asyncpg.
_utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc)  # noqa: E731

#: The `SweepReport` count (added to `errors`) of scans whose result could not be written to the
#: cache. The run still sweeps what it scanned, but the next run will scan again.
CACHE_WRITE_FAILED_TALLY = "cart_mint_cache_write_failed"


class CartMintCacheUnavailable(RuntimeError):
    """The scan cache could not be created or read, so no scan was attempted (see above)."""


@dataclass
class CartMintPopulation:
    """The cart-mint lane as a run uses it: `lane` is what to sweep, `source` where it came from
    ("scan", "cache", or "cache+partial-scan"), `age_s` the age of the scan it rests on (0 for
    this run's own), and `usable` False when it must be counted unreadable."""

    lane: CartMintLane
    source: str
    age_s: int
    usable: bool
    reason: str = ""
    cache_write_failed: bool = False


def _scan_slot_start(now: datetime) -> datetime:
    """The latest `_SCAN_SLOT_HOUR_UTC`:00 UTC at or before `now`."""
    start = now.replace(hour=_SCAN_SLOT_HOUR_UTC, minute=0, second=0, microsecond=0)
    return start if start <= now else start - timedelta(days=1)


def _from_scan_row(row: Dict[str, Any]) -> CartMintLane:
    return CartMintLane(
        hosts=dict(row["hosts"]), seeds_scanned=int(row.get("seeds_scanned") or 0),
        cart_seeds=int(row.get("cart_seeds") or 0), complete=True,
        elapsed_ms=int(row.get("elapsed_ms") or 0),
    )


async def _cart_mint_population(*, fresh_scan: bool = False) -> CartMintPopulation:
    """The cart-mint lane under the once-a-day policy above. `fresh_scan=True` (the census) scans
    now and neither reads nor writes the cache. RAISES `CartMintCacheUnavailable` (no scan was
    attempted) or, with `fresh_scan`, whatever the scan raised."""
    if fresh_scan:
        lane = await _cart_mint_lane()
        return CartMintPopulation(lane, "scan", 0, lane.complete, lane.incomplete_reason)

    try:
        ready = await mint_scans.ensure_table()
        cached = await mint_scans.latest(complete_only=True) if ready else None
        last = await mint_scans.latest(complete_only=False) if ready else None
    except Exception as exc:  # noqa: BLE001
        raise CartMintCacheUnavailable(type(exc).__name__) from exc
    if not ready:
        raise CartMintCacheUnavailable("table_not_ready")

    now = _utcnow()
    if cached is not None and cached["scanned_at"] >= _scan_slot_start(now):
        return CartMintPopulation(
            _from_scan_row(cached), "cache", int((now - cached["scanned_at"]).total_seconds()), True
        )

    lane: Optional[CartMintLane] = None
    reason = ""
    write_failed = False
    backing_off = (
        last is not None and not last["complete"]
        and (now - last["scanned_at"]).total_seconds() < _SCAN_RETRY_S
    )
    if backing_off:
        reason = "retry_backoff"
    else:
        try:
            lane = await _cart_mint_lane()
            reason = lane.incomplete_reason
        except Exception as exc:  # noqa: BLE001 — recorded and counted below, never a traceback
            reason = f"error:{type(exc).__name__}"
        try:
            await mint_scans.record(
                scan_id=f"cms_{uuid.uuid4().hex[:16]}", scanned_at=now,
                complete=bool(lane is not None and lane.complete), reason=reason,
                seeds_scanned=lane.seeds_scanned if lane else 0,
                cart_seeds=lane.cart_seeds if lane else 0,
                elapsed_ms=lane.elapsed_ms if lane else 0,
                hosts=lane.hosts if lane else {},
            )
        except Exception as exc:  # noqa: BLE001
            write_failed = True
            logger.warning(
                "merchant_purchasability_sweep: the cart-mint scan could not be cached "
                "(error_type=%s); the next run will scan again", type(exc).__name__,
            )
        if lane is not None and lane.complete:
            return CartMintPopulation(lane, "scan", 0, True, "", write_failed)

    # Today's scan failed, came back incomplete, or is backing off: the last complete scan, plus
    # whatever a partial scan found. Counted unreadable only when that scan is too old to trust.
    hosts: Dict[Tuple[str, str], int] = dict(cached["hosts"]) if cached is not None else {}
    for key, seeds in (lane.hosts.items() if lane is not None else ()):
        hosts[key] = max(hosts.get(key, 0), seeds)
    base = _from_scan_row(cached) if cached is not None else CartMintLane(hosts={}, complete=False)
    base.hosts = hosts
    age_s = int((now - cached["scanned_at"]).total_seconds()) if cached is not None else -1
    usable = cached is not None and age_s <= _SCAN_MAX_AGE_S
    source = "cache+partial-scan" if lane is not None and lane.hosts else "cache"
    return CartMintPopulation(base, source, age_s, usable, reason, write_failed)

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

#: The `SweepReport` count of connected-store rows skipped because their merchant is a TEST
#: merchant (`services.test_merchant_policy`). Expected and not an alert: every connected store
#: was a test store on 2026-09-29. A COUNT, never the merchant ids.
TEST_MERCHANT_TALLY = "population_skipped_test_merchant"

#: The `SweepReport` count of population reads that failed: once per LANE (the two allowlists, the
#: connected stores, the cart minter) whose read raised or — the cart-mint lane only — came back
#: incomplete, and once when the staleness read (`facts.list_due`) raised. The run still sweeps
#: whatever the other lanes returned — one unreadable table should not stop the merchants the
#: others name from being refreshed — but the population is incomplete (or, for the staleness
#: read, its rotation order is), and `exit_code_for` turns any non-zero count into a FAILED job
#: execution. Before this count the lanes failed soft and silently: a database that answered
#: nothing produced `population=0 ... errors=0`, which read as a clean run.
UNREADABLE_TALLY = "population_unreadable"

#: `SweepReport` sizes, handed back through `load_population(sizes=...)`: the deduped union of
#: every lane BEFORE the batch bound, and how many of those have no fact row from our vantage. With a population
#: larger than the batch these are what tell an operator whether the rotation keeps up with the
#: TTL (see "Capacity" in the runbook); `population` alone is capped at the batch.
TOTAL_TALLY = "population_total"
NEVER_CHECKED_TALLY = "population_never_checked"
#: And the age, in minutes, of the cart-mint scan the run used (0 = scanned this run; -1 = none).
CART_MINT_AGE_TALLY = "cart_mint_population_age_min"

#: The lanes, in the order they are unioned. Only the cart-link lane carries a variant, and
#: `merge_lanes` never replaces a variant with None, so the Tier B lane's CONFIRMED variant lands
#: on a key whichever other lanes also name it.
LANES = ("variant", "cart-link", "connected-store", "cart-mint")


async def collect_population(
    *,
    tally: Optional[Dict[str, int]] = None,
    cart_mint: Optional[Dict[str, Any]] = None,
    fresh_cart_mint_scan: bool = False,
) -> Dict[str, Dict[Tuple[str, str], Optional[str]]]:
    """Every lane's admitted (domain, market) keys, by lane name, each with its variant.

    `tally`, when given, has `UNUSABLE_TALLY` incremented once per lane ROW whose domain is not a
    bare host name and was therefore left out. Counted rather than silently dropped: such a row
    is an operator typo that makes a merchant unbuyable, and the run report is where it shows.
    `MARKET_UNKNOWN_TALLY` counts the same way for a market, and `UNREADABLE_TALLY` once per LANE
    whose read raised or came back incomplete (a lane that raised contributes nothing; the others
    are still swept).

    `cart_mint`, when given, receives the cart-mint lane's counts (`seeds_scanned`, `cart_seeds`,
    `complete`, `source`, `age_s`, and `seeds_by_key` — seeds per admitted key). The cart-mint lane
    follows the once-a-day scan policy (`_cart_mint_population`); `fresh_cart_mint_scan=True`
    scans now and touches no cache, which is what the read-only census does. This function is the
    ONE place the population is decided: the sweep and scripts/merchant_purchasability_census.py
    both read it.
    """
    lanes: Dict[str, Dict[Tuple[str, str], Optional[str]]] = {name: {} for name in LANES}

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
    async def _lane(read: Callable[[], Any], name: str) -> Any:
        try:
            return await read()
        except Exception as exc:  # noqa: BLE001 — never BaseException; cancellation travels
            _count(UNREADABLE_TALLY)
            logger.warning(
                "merchant_purchasability_sweep: the %s population lane could not be read "
                "(error_type=%s)", name, type(exc).__name__,
            )
            return None

    # THE VARIANT LANE'S SET COMES FROM THE LANE ITSELF, not from a second copy of its predicate
    # written here. `routes/agent_commerce_reap` decides eligibility with the same sentinel this
    # helper filters on, so the population cannot be narrower than what the door accepts — which
    # it was, until a reviewer measured a merchant the route admitted and the sweep never visited
    # (the sweep additionally required `variant_key = ''`; the route never has).
    for row in await _lane(lambda: ledger.list_enabled_merchant_markets(strict=True), "variant") or []:
        key = _key(row.get("merchant_domain"), row.get("market_country"))
        if key is not None:
            lanes["variant"].setdefault(key, None)
    for row in await _lane(lambda: _rows(_CART_LANE_SQL), "cart-link") or []:
        key = _key(row.get("domain"), row.get("market"))
        variant = str(row.get("variant_id") or "").strip() or None
        if key is not None and (variant or key not in lanes["cart-link"]):
            lanes["cart-link"][key] = variant
    connected_rows = await _lane(lambda: _rows(_CONNECTED_LANE_SQL), "connected-store") or []
    # Read only when there are rows to filter. It fails SOFT to the static rig ids (its own
    # contract, the one search relies on), never to "exclude nobody".
    test_merchants = await get_excluded_merchant_ids(database) if connected_rows else set()
    for row in connected_rows:
        if str(row.get("merchant_id") or "").strip() in test_merchants:
            _count(TEST_MERCHANT_TALLY)
            continue
        key = _key(normalize_shop_host(row.get("domain")), _connected_market(row.get("region")))
        if key is not None:
            lanes["connected-store"].setdefault(key, None)

    population: Optional[CartMintPopulation] = await _lane(
        lambda: _cart_mint_population(fresh_scan=fresh_cart_mint_scan), "cart-mint"
    )
    minted: Optional[CartMintLane] = population.lane if population is not None else None
    if population is not None:
        if population.cache_write_failed:
            _count(CACHE_WRITE_FAILED_TALLY)
        if not population.usable:
            # Under-reports with nothing young enough to stand in: a catalog lookup it could not
            # make, a scan the lane budget cut short, or a scan that raised, and no complete scan
            # within `_SCAN_MAX_AGE_S`. Counted like a read that raised; what WAS found is swept.
            _count(UNREADABLE_TALLY)
            logger.warning(
                "merchant_purchasability_sweep: the cart-mint population lane is incomplete "
                "(reason=%s)", population.reason,
            )
        elif population.reason:
            # Today's scan failed or is backing off, and a young-enough scan stands in: logged,
            # NOT counted, so a slow database does not fail (and page) every run of the day.
            operator_logger.warning(
                "merchant_purchasability_sweep: today's cart-mint scan did not complete "
                "(reason=%s); using the scan from %d min ago", population.reason,
                population.age_s // 60,
            )
    if minted is not None:
        seeds_by_key: Dict[Tuple[str, str], int] = {}
        for (host, market), seeds in sorted(minted.hosts.items()):
            key = _key(host, market)
            if key is not None:
                lanes["cart-mint"].setdefault(key, None)
                seeds_by_key[key] = seeds_by_key.get(key, 0) + seeds
        operator_logger.info(
            "merchant_purchasability_sweep: cart-mint lane: source=%s age_min=%d seeds_scanned=%d "
            "cart_seeds=%d keys=%d complete=%s elapsed_ms=%d", population.source,
            population.age_s // 60 if population.age_s >= 0 else -1, minted.seeds_scanned,
            minted.cart_seeds, len(lanes["cart-mint"]), minted.complete, minted.elapsed_ms,
        )
        if cart_mint is not None:
            cart_mint.update(
                seeds_scanned=minted.seeds_scanned, cart_seeds=minted.cart_seeds,
                complete=population.usable and minted.complete, incomplete_reason=population.reason,
                source=population.source, age_s=population.age_s,
                elapsed_ms=minted.elapsed_ms, seeds_by_key=seeds_by_key,
            )
    return lanes


def merge_lanes(
    lanes: Dict[str, Dict[Tuple[str, str], Optional[str]]]
) -> Dict[Tuple[str, str], Optional[str]]:
    """The union on (domain, market). WHERE LANES OVERLAP THE CART-LINK LANE WINS the variant,
    because its `variant_id` was confirmed available on the storefront for that market by the
    Tier B job — a stronger fact than anything the catalog holds. No other lane carries one."""
    merged: Dict[Tuple[str, str], Optional[str]] = {}
    for name in LANES:
        for key, variant in lanes.get(name, {}).items():
            if variant or key not in merged:
                merged[key] = variant
    return merged


async def load_population(
    limit: int,
    *,
    tally: Optional[Dict[str, int]] = None,
    sizes: Optional[Dict[str, int]] = None,
) -> List[Target]:
    """The merchant x market set to sweep THIS RUN: `collect_population`'s union, least recently
    checked first, bounded by `limit`, with each target's variant and price hint.

    `tally` receives `collect_population`'s counts, plus one `UNREADABLE_TALLY` if the staleness
    read fails (the run then sweeps in key order). `sizes` receives `TOTAL_TALLY` and
    `NEVER_CHECKED_TALLY`.
    """
    cart_mint: Dict[str, Any] = {}
    lanes = await collect_population(tally=tally, cart_mint=cart_mint)
    if sizes is not None:
        age_s = cart_mint.get("age_s")
        sizes[CART_MINT_AGE_TALLY] = int(age_s) // 60 if isinstance(age_s, int) and age_s >= 0 else -1
    merged = merge_lanes(lanes)
    # The keys that may take the catalog's variant hint: see "One representative variant" in the
    # module docstring.
    reap_keys = set(lanes["variant"]) | set(lanes["cart-link"])

    # Least-recently-checked FIRST, and never-checked first of all — ordered before the catalog
    # lookups so that a big population does not spend its budget pricing merchants this run will
    # not reach. Sorted in Python against what `list_due` reports rather than in SQL, because the
    # population comes from OTHER tables and cannot be joined to this one portably.
    #
    # THIS ORDER IS WHAT KEEPS A POPULATION LARGER THAN THE BATCH FRESH: each run takes the
    # `limit` stalest keys, so every key is re-checked once every ceil(total / limit) runs. It is
    # also why a failed staleness read is COUNTED: `list_due` used to swallow it into [], every
    # key then read as never-checked, and the run would sweep the alphabetically-first `limit`
    # keys every hour while the rest aged out of the TTL, with nothing in the report.
    seen: Dict[Tuple[str, str], Any] = {}
    try:
        due = await facts.list_due(facts.LIST_DUE_MAX, vantage=facts.WORKER_VANTAGE, strict=True)
    except Exception as exc:  # noqa: BLE001
        due = []
        if tally is not None:
            tally[UNREADABLE_TALLY] = tally.get(UNREADABLE_TALLY, 0) + 1
        logger.warning(
            "merchant_purchasability_sweep: the staleness read failed; this run sweeps in key "
            "order (error_type=%s)", type(exc).__name__,
        )
    for row in due:
        key = (str(row.get("merchant_domain") or ""), str(row.get("market_country") or ""))
        seen[key] = row.get("checked_at")

    def _staleness(key: Tuple[str, str]) -> Tuple[int, str]:
        checked = seen.get(key)
        # (0, "") sorts a never-checked merchant ahead of every checked one; the ISO string
        # orders the rest oldest-first without comparing an aware datetime to a naive one.
        return (1, checked.isoformat()) if checked is not None else (0, "")

    ordered = sorted(merged.items(), key=lambda item: (_staleness(item[0]), item[0]))
    if sizes is not None:
        sizes[TOTAL_TALLY] = len(ordered)
        sizes[NEVER_CHECKED_TALLY] = sum(1 for key, _ in ordered if key not in seen)

    targets: List[Target] = []
    for (domain, market), variant in ordered[: max(1, int(limit))]:
        if variant is None and (domain, market) in reap_keys:
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
    #: Connected-store rows left out because the merchant is a TEST merchant; see
    #: `TEST_MERCHANT_TALLY`. Expected, not an alert.
    population_skipped_test_merchant: int = 0
    #: Population reads (the four lanes and the staleness read, 0..5) that RAISED or, for the
    #: cart-mint lane, came back incomplete; or 1 when the population could not be built at all.
    #: Non-zero means this run swept an incomplete population (or in the wrong order); the CLI
    #: exits EXIT_POPULATION_UNREADABLE for it. See `UNREADABLE_TALLY`.
    population_unreadable: int = 0
    #: The deduped union of every lane BEFORE the batch bound (`population` is capped at the
    #: batch). A run covers `population` of these; all of them are covered every
    #: ceil(population_total / batch) runs.
    population_total: int = 0
    #: Of `population_total`, how many have no fact row from our vantage yet. They are swept
    #: first; after the first full rotation this should read ~0, and a key that STAYS here is one
    #: the rotation is not reaching.
    population_never_checked: int = 0
    #: Minutes since the cart-mint scan this run used was taken: 0 when this run scanned, up to
    #: ~24 h on a normal day, -1 when there was none. Above ~1,440 today's scan failed and an
    #: older one stands in (see `_cart_mint_population`); above 4,320 the lane is counted
    #: unreadable.
    cart_mint_population_age_min: int = -1
    checked: int = 0
    positive: int = 0
    negative: int = 0
    unverifiable: int = 0
    written: int = 0
    abandoned_budget: int = 0
    errors: int = 0
    skipped_disabled: int = 0
    #: Of `unverifiable`, the checks the EDGE THROTTLED (`PreflightResult.throttled`: a 429, a 503
    #: with Retry-After, a Cloudflare challenge). Recorded like any unverifiable result (rule 3:
    #: nothing advanced, nothing cleared). Many of these in one run is the ADDRESS, not the stores.
    throttled: int = 0
    #: 1 when the IP breaker tripped and stopped the run. NOT an exit code: see "WHY A TRIP EXITS 0".
    ip_throttled: int = 0
    #: Merchant x vantage pairs NOT contacted because the breaker had tripped. Nothing is written
    #: for them; they are first in line next hour.
    skipped_ip_throttle: int = 0
    duration_ms: int = 0


_COUNTS = (
    "population", "population_skipped_unusable", "population_skipped_market_unknown",
    "population_skipped_test_merchant", "population_unreadable", "population_total", "population_never_checked",
    "cart_mint_population_age_min", "checked",
    "positive", "negative", "unverifiable", "written", "abandoned_budget", "errors",
    "skipped_disabled", "throttled", "ip_throttled", "skipped_ip_throttle",
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


#: The prefix of the run's breaker line, the same one `jobs.external_referral_refresh` prints, so
#: one log filter (`textPayload:"IP_THROTTLE "`) alerts on every crawl lane.
IP_THROTTLE_PREFIX = "IP_THROTTLE "
JOB_NAME = "merchant-purchasability-sweep"


class SweepThrottleBreaker(crawl_ip_throttle.IpThrottleBreaker):
    """#2473's breaker with this job's two differences; everything else (the window, the latch,
    `summary()`, the header histograms) is the parent's.

      * IT COUNTS WHAT THE PREFLIGHT CALLS A THROTTLE (`throttle_signal`), so a Cloudflare
        challenge counts as well as a 429 / 503 + Retry-After. The parent counts only the latter;
        a challenge is handed to it as a 429.
      * ONE MERCHANT IS ONE KEY: `www.` is folded, so a store answering from its apex and its
        `www.` twin cannot count twice.
    """

    def observe(self, host: str, status_code: int, diag: Any) -> None:
        key = str(host or "").strip().lower().rstrip(".")
        if key.startswith("www."):
            key = key[4:]
        status = int(status_code)
        if throttle_signal(status, diag) and not crawl_ip_throttle.is_ip_throttle_signal(status, diag):
            status = 429
        super().observe(key, status, diag)


def _new_breaker() -> SweepThrottleBreaker:
    """ONE breaker per run, from the job's dials. Indirected so a test can shrink the window."""
    # `from_env` with both numbers given reads only the shared kill switch.
    return SweepThrottleBreaker.from_env(
        trip_hosts=_env_int(DIALS["ip_throttle_trip_hosts"]),
        window_seconds=float(_env_int(DIALS["ip_throttle_window_seconds"])),
    )


class IpThrottleTripped(httpx.TransportError):
    """A request the sweep did not send because the IP breaker had tripped. A transport error, so
    the preflight reports TRANSPORT_ERROR for it like any request that never got an answer."""


class _ThrottleWatchingTransport(httpx.AsyncBaseTransport):
    """The outermost transport of every check: refuses a request once the breaker has tripped
    (BEFORE the pacer, so a refused request takes no slot), and reports every answer to
    `crawl_politeness.note_response` WITH ITS HEADERS -- the funnel that feeds the installed
    breaker, and that logs each 429 with its Retry-After / server / cf-mitigated headers.

    `observe=False` for the PROXY vantage: it leaves from another address, so its answers say
    nothing about the crawl IP and must not trip the crawl IP's breaker. It is still refused once
    the breaker has tripped: the run is stopped, not just the crawl IP's half of it."""

    def __init__(
        self, inner: httpx.AsyncBaseTransport, breaker: SweepThrottleBreaker, *, observe: bool = True
    ) -> None:
        self._inner = inner
        self._breaker = breaker
        self._observe = observe
        self.refused = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._breaker.tripped:
            self.refused += 1
            raise IpThrottleTripped("the crawl IP is throttled; request not sent", request=request)
        response = await self._inner.handle_async_request(request)
        if self._observe:
            crawl_politeness.note_response(
                str(request.url), response.status_code,
                retry_after=response.headers.get("retry-after"), headers=response.headers,
            )
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


def ip_throttle_line(breaker: SweepThrottleBreaker, counts: Dict[str, int]) -> Dict[str, Any]:
    """The `IP_THROTTLE` line: the breaker's own fields, exactly as `summary()` names them (the
    external-referral refresh prints the same ones), plus this run's counts."""
    summary = breaker.summary()
    line: Dict[str, Any] = {"job": JOB_NAME, "status": "ip_throttled" if breaker.tripped else "ok"}
    line.update({k: v for k, v in summary.items() if k.startswith("ip_throttle")})
    line["throttle_diagnostics"] = summary.get("throttle_diagnostics")
    line["throttled_checks"] = counts.get("throttled", 0)
    line["skipped_for_ip_throttle"] = counts.get("skipped_ip_throttle", 0)
    return line


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
    counts["cart_mint_population_age_min"] = -1

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
    sizes: Dict[str, int] = {}
    try:
        targets = await load_population(batch, tally=tally, sizes=sizes)
    except Exception:  # noqa: BLE001 — a population read must not end the run with a traceback
        counts["errors"] += 1
        counts["population_unreadable"] = max(1, tally.get(UNREADABLE_TALLY, 0))
        logger.exception("merchant_purchasability_sweep: the population could not be built")
        report = _report()
        operator_logger.info("merchant_purchasability_sweep: %s", report)
        return report
    counts["population"] = len(targets)
    counts["population_unreadable"] = tally.get(UNREADABLE_TALLY, 0)
    counts["population_total"] = sizes.get(TOTAL_TALLY, 0)
    counts["population_never_checked"] = sizes.get(NEVER_CHECKED_TALLY, 0)
    counts["cart_mint_population_age_min"] = sizes.get(CART_MINT_AGE_TALLY, -1)
    # A scan that could not be cached means the next run scans the seed table again: an ERROR
    # (exit 4), because left alone it is exactly the hourly load the cache exists to prevent.
    counts["errors"] += tally.get(CACHE_WRITE_FAILED_TALLY, 0)
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
    # No warning line: skipping a test store is policy, not a fault.
    counts["population_skipped_test_merchant"] = tally.get(TEST_MERCHANT_TALLY, 0)
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
    breaker = _new_breaker()
    with crawl_ip_throttle.installed(breaker):
        await _sweep_targets(targets, vantages, headers, pacer, breaker, counts, pause_s, _budget_spent)
    counts["ip_throttled"] = int(breaker.tripped)

    report = _report()
    # THE BREAKER LINE, every armed run, tripped or not: a text prefix on stdout, so it lands in
    # textPayload (a bare JSON line would land in jsonPayload and read blank in
    # scripts/ops/run_oneoff_job.sh). Counts and timestamps only -- no host is ever listed.
    print(IP_THROTTLE_PREFIX + json.dumps(ip_throttle_line(breaker, counts), separators=(",", ":"),
                                          default=str), flush=True)
    if breaker.tripped:
        operator_logger.warning(
            "merchant_purchasability_sweep: stopped ip_throttled: %d distinct hosts throttled "
            "within %.0fs (first throttle %s, tripped %s); %d merchant x vantage check(s) not "
            "started. Nothing was written for them, and no throttled check demotes or fails a "
            "merchant (rule 3)",
            breaker.trip_host_count, breaker.window_seconds, breaker.first_throttle_at,
            breaker.tripped_at, counts["skipped_ip_throttle"],
        )
    # THE PROOF LINE. Lands on the worker's stdout as
    # `[ts] INFO - merchant_purchasability_sweep: SweepReport(...)`; see the logger note at the
    # top of the module for why it cannot go through `logger`.
    operator_logger.info("merchant_purchasability_sweep: %s", report)
    return report


async def _sweep_targets(
    targets: List[Target],
    vantages: List[Tuple[str, Optional[str]]],
    headers: Dict[str, str],
    pacer: RequestPacer,
    breaker: SweepThrottleBreaker,
    counts: Dict[str, int],
    pause_s: float,
    budget_spent: Callable[[], bool],
) -> None:
    """The per-merchant loop. Mutates `counts`; never raises for anything a merchant did."""
    for target in targets:
        if breaker.tripped:
            # THE RUN IS STOPPED: contact nobody else. Every pair left is counted, nothing is
            # written for it, and it is first in line next hour (its `checked_at` did not move).
            # The ENFORCEMENT is `_ThrottleWatchingTransport`, which refuses every request after a
            # trip (the rest of this merchant's vantages included); this only saves building a
            # client per pair to be refused.
            counts["skipped_ip_throttle"] += len(vantages)
            continue
        if budget_spent():
            # Stop STARTING merchants. The rest are picked up next tick, least-recently-checked
            # first, so nothing is starved — and no merchant is contacted for a run we cannot
            # finish, which on this rail means no abandoned checkout we did not need.
            counts["abandoned_budget"] += 1
            continue
        for vantage, via in vantages:
            watch: Optional[_ThrottleWatchingTransport] = None
            # PER-MERCHANT, PER-VANTAGE try/except. One store that hangs, 403s or returns
            # something nobody has classified must not end the batch behind it.
            try:
                # The shared Shopify-edge budget models the CRAWL egress IP only: the direct
                # vantage (via=None) leaves from it; the proxy vantage does not.
                watch = _ThrottleWatchingTransport(
                    PacedTransport(_inner_transport(via), pacer, shopify_edge=via is None), breaker,
                    observe=via is None,
                )
                async with httpx.AsyncClient(
                    headers=headers, timeout=REQUEST_TIMEOUT_S, transport=watch
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
            if watch is not None and watch.refused:
                # A request of this check was refused locally because the breaker had tripped
                # (a later vantage of the merchant whose answer tripped it, or a trip part way
                # through a check): the result describes our refusal, not the store. Not written.
                counts["skipped_ip_throttle"] += 1
                continue
            counts["checked"] += 1
            # Counted BESIDE positive/negative/unverifiable, not instead: a throttled result is
            # unverifiable (rule 3), and it is recorded as one -- see "AN IP THROTTLE STOPS THE RUN"
            # for why it is written at all.
            counts["throttled"] += int(bool(result.throttled))
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
        if pause_s and not breaker.tripped:
            await asyncio.sleep(pause_s)


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
    per-merchant errors: it is the one that says the run did not see what it had to. A run the IP
    breaker stopped is NOT a failure (`ip_throttled` decides nothing here): see "WHY A TRIP EXITS 0"
    in the module header."""
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
