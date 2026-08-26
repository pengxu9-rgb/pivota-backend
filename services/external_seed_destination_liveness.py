"""Does a seed's published destination URL still resolve? — observe it, record it, act on it.

`external_product_seeds` stores a third-party `/products/<handle>` that we publish through
`offers.resolve` (`affiliate_url` / `execution_spec.pdp_url`), through the seed →
`catalog_products` mirror (`merchant_canonical_url`), and through the gateway's own discovery
provider. Until this module existed nothing ever re-read that URL: `stale_snapshot` inferred
freshness from `updated_at` (which any writer bumps), and the refresh turned a 404 into an
anonymous `{"status": "degraded"}`. Measured 2026-08-25, 10.4% of the seeds whose brand
catalogue could be read publish a link that is already broken —
`docs/external-seed-dead-pdp-link-audit.md`.

## The two stages, and why there are two

`read_brand_catalogue` reads a brand's own `/products.json` once and yields every handle it
lists. One request covers every seed on that host — 44 requests covered 3,951 seeds in the
audit — so it is the CANDIDATE FINDER.

It is not evidence. A handle missing from `products.json` can still serve a full product page
(on cosrx.com, 5 of 12 did, absent from the sitemap too). Only `probe_destination` — which
fetches the PDP and looks at the status and the FINAL url — decides what a shopper gets.

## The rule that makes this safe to automate

`unverifiable` is a first-class outcome and it must never buy a retirement. A Cloudflare bot
challenge arrives as HTTP 429 with `cf-mitigated: challenge` on every path including
robots.txt, and 213 of 286 brand hosts answered that way during the audit. A reaper that
folded "cannot verify" into "dead" would have retired most of the corpus on its first run.

So: the failure streak advances ONLY on a confirmed-dead observation, and only when the
previous confirmed observation is at least `RETIREMENT_MIN_GAP` old — one bad night cannot
retire anything, and a host that stops talking to us freezes rather than decays.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse

import httpx

from db.database import database
from services import crawl_politeness
from services.external_offer_dual_write import MIRROR_SOURCE_SYSTEM
from services.outbound_warm_handoff import extract_product_handle

logger = logging.getLogger("external_seed_destination_liveness")

USER_AGENT = (
    "PivotaAuditBot/1.0 (+https://pivota.cc/about/audit-bot; "
    "checks that published product links still resolve)"
)

# --- verdict vocabulary (mirrored by ck_external_product_seeds_destination_verdict) ---------
VERDICT_LIVE = "live"
VERDICT_LIVE_DELISTED = "live_delisted"
VERDICT_REDIRECTED_TO_PRODUCT = "redirected_to_product"
VERDICT_REDIRECTED_OFF_PRODUCT = "redirected_off_product"
VERDICT_DEAD_404 = "dead_404"
VERDICT_UNVERIFIABLE = "unverifiable"

ALL_VERDICTS: Tuple[str, ...] = (
    VERDICT_LIVE,
    VERDICT_LIVE_DELISTED,
    VERDICT_REDIRECTED_TO_PRODUCT,
    VERDICT_REDIRECTED_OFF_PRODUCT,
    VERDICT_DEAD_404,
    VERDICT_UNVERIFIABLE,
)

# The only two verdicts that mean "a shopper following this link does not reach the product".
# `live_delisted` is a live page the brand has unlisted; `redirected_to_product` is a rename we
# can repair. Neither is a reason to withdraw the row.
CONFIRMED_DEAD_VERDICTS = frozenset({VERDICT_DEAD_404, VERDICT_REDIRECTED_OFF_PRODUCT})

# A verdict that is an ANSWER FROM THE ORIGIN, and therefore may stamp destination_checked_at.
OBSERVED_VERDICTS = frozenset(set(ALL_VERDICTS) - {VERDICT_UNVERIFIABLE})

RETIREMENT_STREAK = 2
RETIREMENT_MIN_GAP = timedelta(hours=24)
SUPPRESSION_REASON = "external_seed_destination_dead"

PAGE_LIMIT = 250
MAX_CATALOGUE_PAGES = 80
# Hosts run concurrently; the politeness gate paces PER HOST, so this widens the sweep without
# hitting any single storefront harder. Serial hosts made a full pass a sum-of-waits, which a
# 3600s Cloud Run task timeout does not comfortably hold.
SWEEP_HOST_CONCURRENCY = 4

# Catalogue-read outcomes. Only `ok` is a catalogue.
CATALOGUE_OK = "ok"
CATALOGUE_INCOMPLETE = "incomplete"
CATALOGUE_BOT_CHALLENGE = "bot_challenge"
# 200 with an empty product list — answered, but not a catalogue we can join against.
CATALOGUE_EMPTY = "empty"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- classification

@dataclass(frozen=True)
class DestinationObservation:
    verdict: str
    http_status: Optional[int]
    final_url: Optional[str]
    note: str = ""
    # Did STAGE 1 independently agree the product is gone — i.e. did we read this brand's
    # `/products.json` successfully and find the handle absent from it?
    #
    # THIS IS WHAT MAKES A RETIREMENT SAFE, and a probe on its own does not have it.
    # `/products/<slug>` is a URL SHAPE, not a platform, and a WAF that answers 404 to an
    # unfamiliar client is indistinguishable from a deleted product by looking at the status
    # code alone — see the same argument in services/live_offer_verification._check_one, which
    # refuses to call a 404 `gone` without positive evidence of a Shopify storefront. A
    # successful catalogue read IS that evidence, and the missing handle is a second,
    # independent witness. Repetition cannot substitute: a WAF policy is MORE persistent than
    # a dead product, so it clears the 24h gap by construction.
    corroborated: bool = False

    @property
    def confirmed_dead(self) -> bool:
        return self.verdict in CONFIRMED_DEAD_VERDICTS

    @property
    def reached_origin(self) -> bool:
        return self.verdict in OBSERVED_VERDICTS


def classify_destination(
    *,
    requested_url: str,
    status_code: Optional[int],
    final_url: Optional[str] = None,
    bot_challenged: bool = False,
    transport_error: Optional[str] = None,
    listed_in_catalogue: Optional[bool] = None,
) -> DestinationObservation:
    """What would a shopper following `requested_url` actually get?

    `listed_in_catalogue` is the stage-1 answer when there is one. It only ever downgrades a
    same-handle 200 from `live` to `live_delisted`; it can never turn a live page into a dead
    one, because a missing handle is not evidence of a missing page.
    """
    # Stage 1 said the handle is absent from a catalogue it could actually read. Only that
    # combination may ever advance the failure streak — see `DestinationObservation`.
    corroborated = listed_in_catalogue is False

    if transport_error:
        return DestinationObservation(VERDICT_UNVERIFIABLE, None, None, transport_error)
    if bot_challenged:
        return DestinationObservation(VERDICT_UNVERIFIABLE, status_code, final_url, "bot_challenge")
    if status_code is None:
        return DestinationObservation(VERDICT_UNVERIFIABLE, None, final_url, "no status")

    if status_code in (404, 410):
        return DestinationObservation(
            VERDICT_DEAD_404, status_code, final_url, f"http_{status_code}", corroborated=corroborated
        )
    if status_code >= 400:
        # 403/429/5xx are the origin refusing or failing, NOT the product being gone. Calling
        # these dead is how a reaper eats a corpus.
        return DestinationObservation(
            VERDICT_UNVERIFIABLE, status_code, final_url, f"http_{status_code}"
        )

    wanted = (extract_product_handle(requested_url) or "").lower()
    landed = (extract_product_handle(final_url or requested_url) or "").lower()
    if not wanted:
        # THE SEED NEVER NAMED A PRODUCT HANDLE, so "did we land on the same handle" has no
        # answer and its absence is not evidence of anything. Without this, a perfectly healthy
        # 200 on a non-Shopify-shaped URL (`/p/<sku>`, `/store/products/<x>`, `/shop/<x>.html`)
        # fell straight into `redirected_off_product` — a CONFIRMED-DEAD verdict — and two
        # observations a day apart retired a live product. 682 of the 11,352 active seeds carry
        # such a URL, and the sweep never sees them (`group_by_host` drops handle-less rows), so
        # this was reachable only from the refresh route, where nothing else stood in the way.
        return DestinationObservation(
            VERDICT_UNVERIFIABLE,
            status_code,
            final_url,
            "destination is not product-shaped",
            corroborated=False,
        )
    if not landed:
        return DestinationObservation(
            VERDICT_REDIRECTED_OFF_PRODUCT,
            status_code,
            final_url,
            "left /products/",
            corroborated=corroborated,
        )
    if landed != wanted:
        return DestinationObservation(
            VERDICT_REDIRECTED_TO_PRODUCT, status_code, final_url, f"-> {landed}"
        )
    if listed_in_catalogue is False:
        return DestinationObservation(
            VERDICT_LIVE_DELISTED, status_code, final_url, "absent from products.json"
        )
    return DestinationObservation(VERDICT_LIVE, status_code, final_url, "")


def should_retire(verdict: str, failure_streak: int) -> bool:
    """Retire only on repeated CONFIRMED death. See the module docstring."""
    return verdict in CONFIRMED_DEAD_VERDICTS and int(failure_streak or 0) >= RETIREMENT_STREAK


# --------------------------------------------------------------------------- stage 2: probe

async def probe_destination(
    client: httpx.AsyncClient,
    url: str,
    *,
    listed_in_catalogue: Optional[bool] = None,
    max_wait: Optional[float] = 0,
) -> DestinationObservation:
    """ONE politeness-gated fetch of a seed destination. Never raises."""
    try:
        await crawl_politeness.before_request(url, user_agent=USER_AGENT, max_wait=max_wait)
    except crawl_politeness.RobotsDisallowed:
        return classify_destination(
            requested_url=url, status_code=None, transport_error="robots_disallowed"
        )
    except Exception as exc:  # noqa: BLE001
        return classify_destination(
            requested_url=url, status_code=None, transport_error=type(exc).__name__
        )

    try:
        resp = await client.get(url, headers={"User-Agent": USER_AGENT})
    except Exception as exc:  # noqa: BLE001
        return classify_destination(
            requested_url=url, status_code=None, transport_error=type(exc).__name__
        )

    crawl_politeness.note_response(
        url, resp.status_code, retry_after=resp.headers.get("retry-after")
    )
    return classify_destination(
        requested_url=url,
        status_code=resp.status_code,
        final_url=str(resp.url),
        bot_challenged=bool(resp.headers.get("cf-mitigated")),
        listed_in_catalogue=listed_in_catalogue,
    )


# --------------------------------------------------------------------------- stage 1: catalogue

@dataclass
class CatalogueRead:
    status: str
    handles: Set[str] = field(default_factory=set)
    product_count: int = 0
    note: str = ""

    @property
    def usable(self) -> bool:
        return self.status == CATALOGUE_OK


async def read_brand_catalogue(
    client: httpx.AsyncClient, host: str, *, attempts: int = 3
) -> CatalogueRead:
    """Every handle a Shopify storefront lists, or an honest reason we could not read it.

    A read that broke partway returns NO handles. Crediting the pages that did arrive turns
    every unread page into a fabricated dead handle — it produced 285 of them on one host
    before this was fixed — so a truncated read is discarded and its host leaves the
    denominator entirely.
    """
    handles: Set[str] = set()
    total = 0
    for page in range(1, MAX_CATALOGUE_PAGES + 1):
        url = f"https://{host}/products.json?limit={PAGE_LIMIT}&page={page}"
        kind, payload = await _get_catalogue_page(client, url, attempts)
        if kind != "products":
            note = payload if isinstance(payload, str) else str(payload)
            if page == 1:
                status = note if kind == "http" else kind
                return CatalogueRead(status, set(), total, note)
            return CatalogueRead(CATALOGUE_INCOMPLETE, set(), total, f"broke at page {page}: {note}")
        if not payload:
            if page == 1:
                # ZERO PRODUCTS ON PAGE 1 IS NOT A CATALOGUE OF ZERO PRODUCTS. A storefront
                # that has genuinely sold nothing is vanishingly rare next to one that gates
                # `/products.json` behind a market, a password, or a bot rule while still
                # answering 200. Treating it as usable marks every seed on the host delisted
                # and turns "one request per host" into one PDP fetch per seed — aimed at
                # exactly the hosts most likely to be refusing us.
                return CatalogueRead(CATALOGUE_EMPTY, set(), 0, "page 1 listed no products")
            return CatalogueRead(CATALOGUE_OK, handles, total, f"{page - 1} page(s)")
        total += len(payload)
        for product in payload:
            handle = str((product or {}).get("handle") or "").strip().lower()
            if handle:
                handles.add(handle)
        if len(payload) < PAGE_LIMIT:
            return CatalogueRead(CATALOGUE_OK, handles, total, f"{page} page(s)")
    return CatalogueRead(
        CATALOGUE_INCOMPLETE, set(), total, f"hit MAX_CATALOGUE_PAGES={MAX_CATALOGUE_PAGES}"
    )


async def _get_catalogue_page(
    client: httpx.AsyncClient, url: str, attempts: int
) -> Tuple[str, Any]:
    last = ""
    for attempt in range(max(1, attempts)):
        try:
            # max_wait=0 is UNBOUNDED, which is what a batch wants: the default ceiling makes
            # the backoff curve above ~16s unreachable and records a refusal instead of waiting.
            await crawl_politeness.before_request(url, user_agent=USER_AGENT, max_wait=0)
        except crawl_politeness.RobotsDisallowed:
            return "robots_disallowed", "robots.txt"
        except Exception as exc:  # noqa: BLE001
            return "gate_error", f"{type(exc).__name__}: {exc}"

        try:
            resp = await client.get(url, headers={"User-Agent": USER_AGENT})
        except Exception as exc:  # noqa: BLE001
            last = type(exc).__name__
            await asyncio.sleep(2 * (attempt + 1))
            continue

        crawl_politeness.note_response(
            url, resp.status_code, retry_after=resp.headers.get("retry-after")
        )
        mitigated = resp.headers.get("cf-mitigated")
        if mitigated:
            # A challenge is a refusal, not a pacing signal — retrying can only stall.
            return CATALOGUE_BOT_CHALLENGE, f"cf-mitigated={mitigated} http_{resp.status_code}"
        if resp.status_code == 429 or resp.status_code >= 500:
            last = f"http_{resp.status_code}"
            await asyncio.sleep(3 * (attempt + 1))
            continue
        if resp.status_code != 200:
            return "http", f"http_{resp.status_code}"
        if "json" not in (resp.headers.get("content-type") or "").lower():
            return "not_json", (resp.headers.get("content-type") or "")[:60]
        try:
            return "products", (resp.json() or {}).get("products") or []
        except Exception:  # noqa: BLE001
            return "bad_json", ""
    return "exhausted", last or "retries exhausted"


# --------------------------------------------------------------------------- persistence

async def record_destination_observation(
    seed_id: str,
    observation: DestinationObservation,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Write one observation. Returns what the row now says.

    Four rules, all of them load-bearing:

    * an `unverifiable` observation writes NO FACT ABOUT THE DESTINATION — not the verdict,
      not the status, not the clock. "We could not look" must never read as "we looked and it
      was fine", and it must never read as "we looked and it was alive" either: writing the
      verdict alone was enough to clear the `destination_dead` blocker on a seed sitting at a
      confirmed 404 with a full streak, handing its dead link straight back to the serving
      lane. The row keeps its last CONCLUSIVE answer until a new one arrives.
    * the failure streak advances only on a confirmed-dead verdict that stage 1 CORROBORATED
      (see `DestinationObservation.corroborated`), and only when the previous observation is
      at least `RETIREMENT_MIN_GAP` old — two probes in one run cannot retire a seed, which is
      what makes a sweep re-run free.
    * an uncorroborated confirmed-dead verdict is recorded but HOLDS the streak. It is a real
      observation and the serving lane may act on it; it is just not, on its own, enough to
      withdraw a row.
    * any non-dead ANSWER resets the streak to 0. An `unverifiable` does not: it is not
      evidence the link came back.
    """
    stamp = now or _now()
    advance_streak = observation.confirmed_dead
    gap_cutoff = stamp - RETIREMENT_MIN_GAP

    row = await database.fetch_one(
        """
        SELECT destination_checked_at, destination_verdict, destination_failure_streak, status
        FROM external_product_seeds
        WHERE id = :id
        """,
        {"id": seed_id},
    )
    if not row:
        return {"seed_id": seed_id, "status": "missing"}
    current = dict(row)
    previous_checked_at = current.get("destination_checked_at")
    streak = int(current.get("destination_failure_streak") or 0)

    if not observation.reached_origin:
        next_streak = streak
    elif not advance_streak:
        next_streak = 0
    elif not observation.corroborated:
        # A CONFIRMED-DEAD PROBE WITH NO SECOND WITNESS HOLDS THE STREAK WHERE IT IS.
        # It is recorded (the verdict is real and worth serving on) but it may not push the
        # seed toward retirement on its own, because a 404 from a WAF and a 404 from a deleted
        # product are the same bytes. Only the sweep, which has just read this brand's
        # catalogue and found the handle missing, sets `corroborated`. See
        # `DestinationObservation.corroborated` for why repetition is not a substitute.
        next_streak = streak
    else:
        # A second look inside the gap is not a second observation.
        within_gap = previous_checked_at is not None and _as_utc(previous_checked_at) > gap_cutoff
        next_streak = streak if within_gap else streak + 1

    await database.execute(
        """
        UPDATE external_product_seeds
        SET destination_verdict = CASE
                WHEN CAST(:reached_origin AS BOOLEAN) THEN :verdict
                ELSE destination_verdict
            END,
            destination_http_status = CASE
                WHEN CAST(:reached_origin AS BOOLEAN) THEN :http_status
                ELSE destination_http_status
            END,
            destination_failure_streak = :streak,
            destination_checked_at = CASE
                WHEN CAST(:reached_origin AS BOOLEAN) THEN :checked_at
                ELSE destination_checked_at
            END
        WHERE id = :id
        """,
        {
            "id": seed_id,
            "verdict": observation.verdict,
            "http_status": observation.http_status,
            "streak": next_streak,
            "reached_origin": bool(observation.reached_origin),
            "checked_at": stamp,
        },
    )
    return {
        "seed_id": seed_id,
        "verdict": observation.verdict,
        "http_status": observation.http_status,
        "failure_streak": next_streak,
        "checked_at": stamp.isoformat() if observation.reached_origin else None,
        "retire": should_retire(observation.verdict, next_streak),
    }


def _as_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


async def retire_seed_for_dead_destination(
    seed_id: str, observation: DestinationObservation, *, now: Optional[datetime] = None
) -> Dict[str, Any]:
    """Withdraw a seed whose destination is confirmed gone, and its mirrored catalog rows.

    The mirror is withdrawn through the EXISTING `suppressed_at` control that
    `routes/pivota_canonical_routes` already honours, rather than a new serving flag — the row
    stops being served and the sig answers 404. Deliberately NOT added to
    `_TERMINAL_SUPPRESSION_REASONS`, because a brand can republish a product and this must not
    be announced to the world as a permanent 410.

    ⚠️ THAT IS A STATEMENT ABOUT THE HTTP STATUS WE SERVE, NOT A CLAIM THAT THE LANE UNDOES
    ITSELF. Today it does not, and calling it "reversible" without saying so reads as a safety
    property that is not implemented:

      * `get_sweep_candidates` selects `WHERE status = 'active'`, so a retired seed is never
        looked at again — if the brand puts the product back, nothing here notices;
      * nothing clears `suppressed_at` / `suppression_reason` for `SUPPRESSION_REASON`.
        Compare `services/identity_resolution.REVERT_ROWS_SQL`, which exists precisely so a
        suppression can be lifted.

    So reversal is an operator action (reactivate the seed, clear the mirror suppression), and
    the arithmetic that makes that acceptable is the same one that governs the retirement
    itself: two corroborated confirmed-dead observations a day apart are wrong far less often
    than a resurrection is common. Giving this lane its own un-retire path is a follow-up, and
    it needs care — an automatic un-retire is a resurrection primitive, and it would run on the
    same evidence that a bot challenge can forge.
    """
    stamp = now or _now()
    note = (
        f"auto-retired {stamp.date().isoformat()}: destination {observation.verdict}"
        f" (http {observation.http_status})"
    )
    await database.execute(
        """
        UPDATE external_product_seeds
        SET status = 'inactive',
            notes = CASE
                WHEN notes IS NULL OR TRIM(notes) = '' THEN :note
                ELSE notes || E'\n' || :note
            END,
            updated_at = NOW()
        WHERE id = :id
          AND status = 'active'
        """,
        {"id": seed_id, "note": note},
    )
    await database.execute(
        """
        UPDATE catalog_products
        SET suppressed_at = :stamp,
            suppression_reason = :reason,
            updated_at = NOW()
        WHERE source_ref = :id
          AND source_system = :source_system
          AND suppressed_at IS NULL
        """,
        {
            "id": seed_id,
            "stamp": stamp,
            "reason": SUPPRESSION_REASON,
            # `source_ref` ALONE IS NOT THE LINK. services/external_offer_dual_write states the
            # contract: "catalog_products.source_ref = external_product_seeds.id WITH THIS
            # source_system — that pair is the stable seed->product link", and
            # `resolve_mirror_product` queries on both. Matching on source_ref alone would
            # suppress any row from another door that happens to carry the same value.
            "source_system": MIRROR_SOURCE_SYSTEM,
            # `updated_at` matches services/identity_resolution.SUPPRESS_SQL: a consumer doing
            # incremental work off catalog_products.updated_at must be able to see a withdrawal.
        },
    )
    logger.info(
        "external seed retired for dead destination",
        extra={"seed_id": seed_id, "verdict": observation.verdict},
    )
    return {"seed_id": seed_id, "retired": True, "verdict": observation.verdict}


async def get_sweep_candidates(limit: int) -> List[Dict[str, Any]]:
    """Active seeds, least-recently-verified first. NULLS FIRST: that is the whole corpus."""
    rows = await database.fetch_all(
        """
        SELECT id, market, domain, canonical_url, destination_url, destination_checked_at
        FROM external_product_seeds
        WHERE status = 'active'
        ORDER BY destination_checked_at ASC NULLS FIRST, updated_at ASC NULLS FIRST
        LIMIT :limit
        """,
        {"limit": max(1, int(limit or 1))},
    )
    return [dict(row) for row in rows or []]


# --------------------------------------------------------------------------- the sweep

def destination_of(row: Dict[str, Any]) -> str:
    return str(row.get("canonical_url") or row.get("destination_url") or "").strip()


def host_of(url: str) -> str:
    try:
        host = (urlparse(str(url or "")).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""
    return host[4:] if host.startswith("www.") else host


def group_by_host(rows: Sequence[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """A LOCALE STOREFRONT IS ITS OWN CATALOGUE.

    `nl.beautyofjoseon.com` and `beautyofjoseon.com` do not list the same handles, so the host
    is taken verbatim off the destination and never folded to an apex (bar `www.`).
    """
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        dest = destination_of(row)
        if not dest or not extract_product_handle(dest):
            continue
        grouped.setdefault(host_of(dest), []).append(row)
    return grouped


async def run_destination_sweep(
    *,
    limit: int = 2000,
    client: Optional[httpx.AsyncClient] = None,
    retire: bool = True,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Stage 1 per host, then stage 2 only for the handles the brand no longer lists.

    Sizing: a full pass must fit inside the `stale_snapshot` window (7 days). At ~11.4k active
    seeds that is ~1,700/day, and stage 1 makes it a few hundred requests rather than 11,400.
    """
    candidates = await get_sweep_candidates(limit)
    grouped = group_by_host(candidates)

    summary: Dict[str, Any] = {
        "candidates": len(candidates),
        "hosts": len(grouped),
        "hosts_unverifiable": 0,
        "listed": 0,
        "probed": 0,
        "dead_links_found": 0,
        "seeds_retired": 0,
        "verdicts": {v: 0 for v in ALL_VERDICTS},
        "catalogue_status": {},
    }

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=25.0, follow_redirects=True)
    host_slot = asyncio.Semaphore(SWEEP_HOST_CONCURRENCY)

    async def sweep_one_host(host: str, seeds: List[Dict[str, Any]]) -> None:
        async with host_slot:
            catalogue = await read_brand_catalogue(client, host)
            summary["catalogue_status"][catalogue.status] = (
                summary["catalogue_status"].get(catalogue.status, 0) + 1
            )
            if not catalogue.usable:
                # A host we cannot read produces NO verdicts. Recording `unverifiable` for
                # every seed on it would bury the one number that matters — how much of the
                # corpus we can still see — under rows that were never in question.
                summary["hosts_unverifiable"] += 1
                return

            for seed in seeds:
                dest = destination_of(seed)
                handle = (extract_product_handle(dest) or "").lower()

                if handle in catalogue.handles:
                    # THE BRAND ITSELF LISTS THIS HANDLE, and we read that from its origin —
                    # so it IS an observation, and recording it is what makes the sweep able
                    # to verify a whole host for the price of one request. Without this the
                    # only rows ever verified would be the delisted ones, and every healthy
                    # seed would sit at `destination_never_verified` forever.
                    #
                    # `http_status` stays NULL on purpose: we did not fetch the PDP, and
                    # writing 200 would be inventing a response. Measured residual: 1 of 67
                    # listed handles probed directly was a 404, so this is strong evidence,
                    # not proof — at ~1.5% it is not worth one request per seed to find.
                    observation = DestinationObservation(
                        VERDICT_LIVE, None, None, "listed in products.json"
                    )
                    summary["listed"] += 1
                else:
                    observation = await probe_destination(client, dest, listed_in_catalogue=False)
                    summary["probed"] += 1

                summary["verdicts"][observation.verdict] = (
                    summary["verdicts"].get(observation.verdict, 0) + 1
                )
                result = await record_destination_observation(seed["id"], observation, now=now)
                if observation.confirmed_dead:
                    summary["dead_links_found"] += 1
                if retire and result.get("retire"):
                    await retire_seed_for_dead_destination(seed["id"], observation, now=now)
                    summary["seeds_retired"] += 1

    try:
        # One host failing must not void the rest of the pass — the counters above are the
        # deliverable, and a half-swept corpus is strictly better than none.
        results = await asyncio.gather(
            *(sweep_one_host(h, s) for h, s in grouped.items()), return_exceptions=True
        )
        for host, outcome in zip(grouped, results):
            if isinstance(outcome, BaseException):
                summary["hosts_unverifiable"] += 1
                logger.warning(
                    "destination sweep host failed",
                    extra={"host": host, "error": f"{type(outcome).__name__}: {outcome}"},
                )
    finally:
        if owns_client:
            await client.aclose()

    logger.info("external seed destination sweep complete", extra={"summary": summary})
    return summary


def coverage_alarm(summary: Dict[str, Any]) -> Optional[str]:
    """The dial worth paging on: a crawl lane that quietly stops seeing its hosts.

    Returns a message when more hosts were unreadable than readable. During the 2026-08-25
    audit that ratio was 213:44 from a client outside the reserved crawl egress — a sweep
    reporting "0 dead links" from there would have been indistinguishable from a healthy one.
    """
    hosts = int(summary.get("hosts") or 0)
    blind = int(summary.get("hosts_unverifiable") or 0)
    if hosts and blind * 2 > hosts:
        return (
            f"destination sweep could not read {blind} of {hosts} brand hosts — "
            "a 'no dead links' result from this run means nothing"
        )
    return None


__all__: Iterable[str] = (
    "ALL_VERDICTS",
    "CONFIRMED_DEAD_VERDICTS",
    "CatalogueRead",
    "DestinationObservation",
    "RETIREMENT_MIN_GAP",
    "RETIREMENT_STREAK",
    "SUPPRESSION_REASON",
    "VERDICT_DEAD_404",
    "VERDICT_LIVE",
    "VERDICT_LIVE_DELISTED",
    "VERDICT_REDIRECTED_OFF_PRODUCT",
    "VERDICT_REDIRECTED_TO_PRODUCT",
    "VERDICT_UNVERIFIABLE",
    "classify_destination",
    "coverage_alarm",
    "get_sweep_candidates",
    "group_by_host",
    "probe_destination",
    "read_brand_catalogue",
    "record_destination_observation",
    "retire_seed_for_dead_destination",
    "run_destination_sweep",
    "should_retire",
)
