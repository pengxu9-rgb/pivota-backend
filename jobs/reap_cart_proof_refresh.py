"""Refresh the Reap cart-link storefront proofs on a schedule: one lane per proof writer.

    python -m jobs.reap_cart_proof_refresh mirror     --on-crawl-egress --budget-seconds 10800
    python -m jobs.reap_cart_proof_refresh enrichment --on-crawl-egress --budget-seconds 3600

WHAT IT IS. The Reap cart-link lane refuses a row without a fresh storefront proof, and two writers
author those proofs, each one --domain at a time:

  * `mirror`      scripts/backfill_shopify_variant_ids.py (option 1, #2459) writes
                  `seed_data.snapshot.shopify_cart_proof` on external-seed mirror rows. Valid 7 days
                  (services.shopify_variant_identity.CART_PROOF_MAX_AGE).
  * `enrichment`  jobs/enrichment_cart_variant_proof.py (option 2, #2464) writes
                  `enrichment_cart_variant_proofs`. Valid 72 hours
                  (services.reap_enrichment_cart_proof.MAX_PROOF_AGE).

This module owns NO proof logic. It picks and orders the domains, walks each one page by page by
calling the writer's own entry point on the writer's own cursor, remembers where each domain got to,
bounds the whole pass with a wall-clock budget, and prints ONE report line. Every rule about what a
proof is, how a storefront is read and paced inside a page, and what is written, stays in the writer.

THE DOMAINS, AND THEIR ORDER.
  * mirror: every domain on config/tierb_cart_link_merchants.json. That list IS the set of stores the
    cart-link lane can buy from (it refuses a merchant without a fresh Tier B eligibility row, and
    only this list gets one), so a mirror proof anywhere else serves no purchase. STALEST FIRST:
    stores no run has ever walked to the end go first, then by the earlier of (a) the oldest
    still-valid proof among the store's active seeds (one SQL read, `SELECT_OLDEST_VALID_MIRROR_PROOF_SQL`)
    and (b) when a run last walked the store to its end. A store the budget cut is resumed from its
    stored cursor (below), so a large store's tail is reached on the next run instead of never.
    Domains are compared exactly as the backfill's `--domain` filter compares them (case-sensitive,
    exact or a `.`-subdomain), so the order only counts seeds the backfill will actually walk.
  * enrichment: `ENRICHMENT_DOMAINS` below, the five stores #2464's census found enrichment rows on.
    Deliberately a pinned constant, not a query: #2464 refuses a default population, and so does this.
    Each must also be on the Tier B list (the writer's `plan_domains` refuses it otherwise, exit 2).
    Ordered by their worst-case `.js` cost, cheapest first, so MAC (~1,870 handles) is last.
  * BOTH lanes: a store whose last run ended `aborted_on_block` is BACKED OFF (skipped, `backed_off`)
    until its stored `blocked_until` (`block_backoff(lane)`: 3 days for mirror, whose proofs live 7
    days; 1 day for enrichment, whose proofs live 72 h, so a store that blocked us once is retried
    before its proofs lapse), and walked LAST on the first run after that. One store that always
    blocks us therefore costs one threshold's worth of requests per back-off, and never the other
    stores' refresh. A store that was aborted inside an IP-level block (the pass stopped) is NOT
    backed off: that block was our address's, not the store's.

WHERE EACH DOMAIN GOT TO (db/reap_cart_proof_refresh_cursors.py, migration 250). One row per (lane,
domain): the writer cursor to resume from (NULL = the first page), how the last run ended there, and
when a run last walked it to its end, the block back-off, and a crash count. An APPLY run writes it
after every page and when the domain ends; a DRY RUN reads it (so it walks what the next apply would)
and writes nothing. A store that crashes at the SAME resume cursor on two runs in a row has its cursor
reset to NULL (logged, `crash_cursor_reset`), so the rows before the poison page are walked again
instead of never.

APPLY OR DRY RUN: THE GATE. `REAP_CART_PROOF_APPLY` (1/true/yes/on, any case) makes the run write;
anything else, unset included, is a DRY RUN. A dry run still fetches every storefront, exactly as
hard (both writers gate the write, not the crawl). This is deliberately NOT the Tier B job's "dark
means contact nobody": the dark job is also the dry-run vehicle, so `gcloud run jobs execute` on a
dark job is the dry run on the real job definition (image, subnet, secret) before anything is armed.

THE EGRESS. Both writers fetch merchant storefronts, so this runs ONLY on the crawl subnet
(`pivota-crawl`, NAT 34.82.199.35), never the default NAT whose address payment partners allowlist.
A container cannot see its own subnet, so `--on-crawl-egress` is REQUIRED (the enrichment writer's
own rule, applied to both lanes): without it this exits 2 before touching the database or the net.

PAGES AND PACING. Mirror: `backfill.run(limit=25)` per page. Enrichment: the writer's `run_domain`
with `limit=250` products per page, so its writes (made at the end of each call) commit page by page
and the budget can stop between pages of one store. Inside a page the writer paces itself. Between
pages this module waits `inter_call_gap_s()`, the writer's own slowest spacing, because a mirror call
builds a fresh pacer that never saw the previous page's last request; the enrichment lane passes ONE
shared pacer to every call as well.

THE BUDGET is checked before every writer call: once spent, no new page starts. A page already
running finishes, which is why the task timeout in the setup script is the budget PLUS one page's
worst case. A domain the budget cut is `budget_stopped` (walked partway; resumed next run) or
`not_reached`. If the task timeout's SIGTERM arrives anyway, the partial report is printed first
(`terminated`), and every page already checkpointed stays checkpointed.

BLOCKS, AND THE IP BREAKER.
  * THE STORE: a store is aborted (`aborted_on_block`, backed off as above) when its consecutive
    block-shaped answers reach the writer's own threshold T (mirror 8, enrichment 5; a clean answer
    resets the count, a challenge page or a request never sent does not touch it), OR when it is
    walked to its end having had at least one block and NO clean answer at all (a store we never
    actually read is not `done`, and gets no `last_completed_at`). The pass then MOVES ON.
  * THE ADDRESS: one `services.crawl_ip_throttle.IpThrottleBreaker` (#2473) per run, KEYED BY STORE
    (`store_breaker`: a store's apex, `www.` twin and subdomains are one key), installed for the run
    and fed with every response's headers through `crawl_politeness.note_response`. A store COUNTS
    toward a trip once ITS OWN run of 429s (or 503 + Retry-After) reaches `LANE_STORE_STREAK` in a row
    (a clean answer from it ends the run: one transient error is not a store blocking us; an abort
    alone does not make a store count). It trips when `LANE_IP_TRIP_HOSTS` counting stores within
    `LANE_IP_TRIP_WINDOW_S` include one that WAS HEALTHY -- it answered cleanly this run before its
    blocks, or the cursor table says a previous run walked it cleanly and its last run did not blame
    it -- AND no store outside them has answered cleanly since they began (a store still answering
    says the address is fine). A store the table already blames (`aborted_on_block` last time, back-off
    forgiven or not) is no evidence, so the stores `defer_blocked` walks last, back to back, cannot trip
    it. Four FIRST-CONTACT stores (no cursor row) counting with nothing answering since also trip it: a
    lane meeting stores for the first time from a blocked address. A run that starts blocked with no
    health on record anywhere and no first-contact stores walks every store to its own threshold and
    backs each off (`StoreStreakWindow`; thresholds for a lane that walks stores ONE AT A TIME, see the
    constants). A trip STOPS THE WHOLE
    PASS (the store in flight is `ip_throttled`/`pass_abort`, the rest `not_reached`) and FORGIVES the
    back-off of every store this run aborted within the breaker window before the trip (`ip_block`):
    during an IP-level throttle -- including the 2026-09-30 pattern, a rate throttle that lets
    occasional 200s through -- a store that "blocked us" was the address's problem, not the store's.
    A store that blocked us well before the throttle keeps its back-off.
  * THE OTHER BLOCK SHAPES: 403s, 5xx and transport errors never trip #2473's breaker (it counts
    429 / 503 + Retry-After only), yet an IP-level block looks like that too (the 2026-08-21 shape,
    the 2026-09-28 NAT drops). `StoreBlockBreaker`, store-keyed with the same rule, counts them; its
    trip stops the pass exactly like the throttle breaker's (`LaneBreaker` holds both, over one
    `LaneHealth` fed with the lanes' clean answers).
  * HELD ROWS: a request crawl_politeness does not release in time (a Retry-After / backoff hold, a
    Crawl-delay over the cap, the shared edge slot) is not sent and is COUNTED. A page with any held
    request stops its store WITHOUT advancing the cursor past it (`held_by_politeness`, exit 4): the
    store is neither `done` (nothing was read) nor `aborted_on_block` (nothing refused us), is not
    backed off, and the "no clean answer" rule ignores held requests.
  Mirror: the backfill keeps its counter inside one `run()`, so the client handed to it
  (`BlockStreakClient`) classifies every answer with the backfill's OWN `fetch_product_js` and
  `_is_block`, carries the store count across calls, and once the store has tripped or the breaker
  has, answers 429 locally without touching the network. Enrichment: the writer's own `block_state`,
  one per store (`ObservedStreak`), and the writer's `should_stop` hook, so it stops asking the
  moment the breaker trips.

THE SHARED SHOPIFY-EDGE PACER (#2474). Both lanes' every request goes through
`crawl_politeness.before_request` (robots.txt, the host's own slot, its Retry-After / backoff hold,
and the shared Shopify-edge slot -- both slots or neither for these bounded callers): the enrichment
writer's own call, and the mirror lane's `BlockStreakClient`, which the backfill's plain
`client.get` now reaches. Both lanes mark their hosts
as Shopify-served (every one is a Tier B Shopify store's products.js / products.json / meta.json)
and teach the pacer from response headers, so with `CRAWL_SHOPIFY_EDGE_PACER_ENABLED` (set on both
jobs by the setup script) every request also takes a slot of the aggregate budget all crawl jobs on
the crawl IP share. A request the gate will not let out within `MIRROR_MAX_POLITE_WAIT_S` is NOT
sent: the mirror client answers it locally as a non-answer (the backfill sees `not_json`: neutral,
nothing written for that row). NOTE: a pacer-enabled run leases slots from `crawl_egress_pacer`
(migration 251) -- a dry run writes THAT shared row, and nothing else.

A CRASH (an exception out of the writer, or out of summing its report) is recorded against its domain
and the pass moves on: one store's unreadable data must not cost the other stores their refresh.

THE REPORT: one line, `REAP_CART_PROOF_REPORT {json}` (a text prefix, so it lands in textPayload and
`scripts/ops/run_oneoff_job.sh` shows it). A `REAP_CART_PROOF_PROGRESS` line is printed as each
domain ends.

EXIT CODES. #2464's four, plus one for the budget:
  0  every domain walked to its end (a `backed_off` store does not count against it: it is reported)
  1  the PASS stopped: the IP-throttle breaker tripped (`ip_throttled`)
  2  bad arguments, no --on-crawl-egress, a missing merchant list, or a domain list the writer
     refuses; nothing attempted
  3  a writer crashed on at least one domain (the others were still attempted), a cursor did not
     move, or the pass could not run at all (`REAP_CART_PROOF_CRASH`, no report)
  4  a store was left unwalked: the budget, a SIGTERM (always at least 4, even with nothing walked),
     or one store that blocked us (`aborted_on_block`, backed off from now on)
2 is returned before anything runs; otherwise 1 outranks 3, which outranks 4.

Provisioned by infra/gcp/setup_reap_cart_proof_jobs.sh (dark unless --enable); runbook
docs/runbooks/reap_cart_proofs.md. Not registered with services/audit_scheduler, not run by CI or
by any deploy script; tests/test_reap_cart_proof_refresh.py pins that.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import reap_cart_proof_refresh_cursors as cursor_store  # noqa: E402

logger = logging.getLogger(__name__)

LANES = ("mirror", "enrichment")
GATE_ENV = "REAP_CART_PROOF_APPLY"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: The stores #2464's census (2026-09-29) found enrichment rows on, cheapest worst case first.
ENRICHMENT_DOMAINS = (
    "stilacosmetics.com",
    "jsmbeauty.sg",
    "bluemercury.com",
    "tartecosmetics.com",
    "maccosmetics.com",
)

#: Mirror candidates per writer call. The writer's own default is 100; 50 halves the worst case of
#: the one page the budget cannot stop (see the setup script's task timeout).
MIRROR_PAGE_SIZE = 25
#: Enrichment products per writer call (the writer's default is 2,000). The writer writes at the end of
#: each call, so this is also the most work a SIGTERM can cost.
ENRICHMENT_PAGE_PRODUCTS = 250
#: How long a store that blocked us is skipped, per lane: shorter than the lane's proof life, so one block
#: never guarantees a lapse (mirror proofs live 7 days, enrichment proofs 72 h). The store is walked LAST
#: on the first run after its back-off.
MIRROR_BLOCK_BACKOFF = timedelta(days=3)
ENRICHMENT_BLOCK_BACKOFF = timedelta(days=1)


def block_backoff(lane: str) -> timedelta:
    return MIRROR_BLOCK_BACKOFF if lane == "mirror" else ENRICHMENT_BLOCK_BACKOFF
#: The IP breaker for these lanes (#2473's `IpThrottleBreaker`, with lane thresholds). Its defaults (10 distinct
#: hosts in 60 s) are calibrated on the external-referral refresh, which reaches hundreds of hosts at ~4 req/s;
#: these lanes walk ONE store at a time at ~1 request / 3 s, so a store contributes at most one distinct host
#: per ~minute and 10-in-60s could never trip. Three distinct stores throttling within 15 minutes: during an
#: IP-level throttle every store 429s within its first few requests (three stores in a few minutes).
#: A store counts only after `LANE_STORE_STREAK` blocks in a row, and a trip needs evidence the address went
#: bad (health before the blocks, this run or on the cursor table, and nothing else still answering), or
#: four first-contact stores with nothing answering: see `StoreStreakWindow`. A false trip costs only deferral: the
#: pass stops, nothing is backed off, the next run resumes; a false NON-trip costs a back-off per store.
LANE_IP_TRIP_HOSTS = 3
LANE_IP_TRIP_WINDOW_S = 900.0
#: A store counts toward either breaker only once ITS OWN run of block-shaped answers reaches this many in a
#: row (a clean answer from it resets the run): one transient error (a ReadTimeout, a 502) on each of three
#: healthy stores is not an IP block, and neither is a tiny store the never-read rule aborted after one. Below both writers' own abort
#: thresholds (mirror 8, enrichment 5), so a store blocked outright counts before it is aborted.
LANE_STORE_STREAK = 3
#: The longest the mirror lane waits for `crawl_politeness` to let a request out (a Retry-After hold, a
#: Crawl-delay, the shared Shopify-edge slot). Longer, and the request is not sent. The enrichment writer's
#: own `ENRICHMENT_PROOF_MAX_POLITE_WAIT_S` has the same default.
MIRROR_MAX_POLITE_WAIT_S = 60.0
#: Redirect hops the mirror client follows BY HAND (each one gated, paced and reported); the backfill's
#: own `follow_redirects=True` is overridden, because httpx would follow hops nothing gates.
MIRROR_MAX_REDIRECTS = 3
#: Synthetic statuses of the mirror client's LOCAL answers, so the backfill's report names them
#: (`http_425`, `http_451`) instead of folding them into `not_json` / `most_blocked_domains`. Neither
#: is in the backfill's BLOCK_OUTCOMES; the backfill writes nothing for either.
LOCAL_HELD_STATUS = 425        # crawl_politeness held the request past our patience: not sent, retried
LOCAL_REFUSED_STATUS = 451     # robots.txt Disallow or a Crawl-delay over the cap: permanent, not sent
#: A redirect OFF the requested storefront (or off https): the hop is never requested, and the backfill
#: reads `http_421` (Misdirected Request), the writer's `host_redirected`: not a block, not `not_json`,
#: so it stays out of `most_blocked_domains`; the store DID answer. Counted as `off_storefront_redirects`.
LOCAL_OFF_STOREFRONT_STATUS = 421
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
#: Crashes at the same resume cursor, in consecutive runs, before that cursor is reset to NULL.
CRASH_RESET_AFTER = 2

EXIT_OK = 0
EXIT_ABORTED_ON_BLOCK = 1
EXIT_BAD_ARGS = 2
EXIT_CRASHED = 3
EXIT_BUDGET = 4

REPORT_PREFIX = "REAP_CART_PROOF_REPORT "
PROGRESS_PREFIX = "REAP_CART_PROOF_PROGRESS "

DONE = "done"
ABORTED = "aborted_on_block"
CRASHED = "crashed"
BUDGET_STOPPED = "budget_stopped"
NOT_REACHED = "not_reached"
CURSOR_STUCK = "cursor_stuck"
TERMINATED = "terminated"
IN_PROGRESS = "in_progress"
BACKED_OFF = "backed_off"
IP_THROTTLED = "ip_throttled"
#: crawl_politeness would not release a request in time (a Retry-After / backoff hold, a Crawl-delay, the
#: shared edge slot): the rows it held were NOT read. The store stops here without advancing its cursor
#: past them -- never `done`, never `aborted_on_block`, no back-off; the next run resumes before them.
HELD = "held_by_politeness"

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def apply_enabled(environ: Optional[Mapping[str, str]] = None) -> bool:
    """Strict: exactly 1/true/yes/on after trimming, any case. Everything else is a dry run."""
    env = os.environ if environ is None else environ
    raw = env.get(GATE_ENV)
    if raw is None:
        return False
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value:
        logger.warning("%s=%r is not a recognised truthy value; this run is a DRY RUN", GATE_ENV, raw)
    return False


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


# ── the mirror lane's order: stalest first ──────────────────────────────────────────────────────

# Per seed domain, the oldest proof that is still VALID (checked within the proof's life). A lapsed proof
# is already useless and must not pin its store to the front forever. `checked_at` is compared as text:
# the backfill writes every one as `datetime.now(timezone.utc).isoformat()`, and `:cutoff` is built the
# same way, so the strings order as the instants do.
SELECT_OLDEST_VALID_MIRROR_PROOF_SQL = """
    SELECT domain,
           min(seed_data->'snapshot'->'shopify_cart_proof'->>'checked_at') AS oldest_valid_proof
      FROM external_product_seeds
     WHERE status = 'active'
       AND jsonb_typeof(seed_data->'snapshot'->'shopify_cart_proof') = 'object'
       AND seed_data->'snapshot'->'shopify_cart_proof'->>'checked_at' >= :cutoff
     GROUP BY domain
"""


async def select_oldest_valid_mirror_proofs(db: Any, *, now: datetime, max_age: timedelta) -> Dict[str, datetime]:
    cutoff = (now.astimezone(timezone.utc) - max_age).isoformat()
    out: Dict[str, datetime] = {}
    for raw in await db.fetch_all(SELECT_OLDEST_VALID_MIRROR_PROOF_SQL, {"cutoff": cutoff}) or []:
        row = dict(raw)
        try:
            stamp = _aware(datetime.fromisoformat(str(row["oldest_valid_proof"])))
        except (TypeError, ValueError):
            continue
        out[str(row["domain"])] = stamp
    return out


def _seed_domain_is(seed_domain: str, domain: str) -> bool:
    """The backfill's own `--domain` rule, EXACTLY as its SQL applies it to the `domain` column:
    `domain = :d OR domain LIKE '%.' || :d` -- case-sensitive, no trimming. A seed the backfill will
    not select must not move a store in the order either."""
    return seed_domain == domain or seed_domain.endswith("." + domain)


def order_mirror_domains(domains: Sequence[str], oldest_valid: Mapping[str, datetime],
                         cursors: Mapping[str, "cursor_store.CursorRow"]) -> List[str]:
    """Never-completed stores first; then by the earlier of the oldest valid proof and the last
    completed walk; then by name."""
    def key(domain: str):
        proofs = [ts for seed_domain, ts in oldest_valid.items() if _seed_domain_is(seed_domain, domain)]
        oldest = min(proofs) if proofs else None
        row = cursors.get(domain)
        completed = _aware(row.last_completed_at) if row is not None else None
        stamps = [t for t in (oldest, completed) if t is not None]
        return (completed is not None, min(stamps) if stamps else _EPOCH, domain)

    return sorted(dict.fromkeys(domains), key=key)


def defer_blocked(domains: Sequence[str], cursors: Mapping[str, "cursor_store.CursorRow"],
                  now: datetime) -> "tuple[List[str], List[str]]":
    """(walk order, backed off). A store still inside its block back-off is not walked this run; a
    store whose last run ended `aborted_on_block` but whose back-off has passed is walked LAST. Every
    other store keeps its place."""
    fresh: List[str] = []
    retry: List[str] = []
    skipped: List[str] = []
    for domain in domains:
        row = cursors.get(domain)
        until = _aware(row.blocked_until) if row is not None else None
        if until is not None and until > now:
            skipped.append(domain)
        elif row is not None and row.last_status == ABORTED:
            retry.append(domain)
        else:
            fresh.append(domain)
    return fresh + retry, skipped


# ── the driver: domains -> pages -> one report ──────────────────────────────────────────────────


@dataclass
class Page:
    """One writer call's result. `next_cursor` None means the domain is walked to its end."""
    report: Dict[str, Any]
    next_cursor: Optional[str]
    #: this STORE blocked us: record it, back it off, move on
    aborted: bool = False
    #: the IP breaker tripped during this page: stop the whole pass, back nothing off
    abort_pass: bool = False
    #: requests in this page crawl_politeness did not release (not sent): stop the store, keep the cursor
    held: int = 0


PageFn = Callable[[str, Optional[str]], Awaitable[Page]]
MergeFn = Callable[[Dict[str, Any], Dict[str, Any]], None]


@dataclass
class DomainResult:
    status: str = NOT_REACHED
    pages: int = 0
    elapsed_s: float = 0.0
    start_cursor: Optional[str] = None
    last_cursor: Optional[str] = None
    error: Optional[str] = None
    checkpoint_error: Optional[str] = None
    #: this store's abort is the one that stopped the whole pass
    pass_abort: bool = False
    #: aborted in a run whose IP breaker then tripped: its back-off is forgiven (the address's block)
    ip_block: bool = False
    #: this run reset a cursor that crashed twice in a row
    crash_cursor_reset: bool = False
    writer: Dict[str, Any] = field(default_factory=dict)

    def resume_cursor(self) -> Optional[str]:
        """Where the next run starts this domain, unless it ended here."""
        return self.last_cursor if self.last_cursor is not None else self.start_cursor

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"status": self.status, "pages": self.pages,
                               "elapsed_s": round(self.elapsed_s, 1), "writer": self.writer}
        for key in ("start_cursor", "last_cursor", "error", "checkpoint_error"):
            if getattr(self, key) is not None:
                out[key] = getattr(self, key)
        for key in ("pass_abort", "ip_block", "crash_cursor_reset"):
            if getattr(self, key):
                out[key] = True
        return out


#: `checkpoint(domain, result, final)`: after every page that returned a cursor (final False) and when the
#: domain ends (final True). Never called for a domain that was not reached.
CheckpointFn = Callable[[str, DomainResult, bool], Awaitable[None]]


async def _checkpoint(checkpoint: Optional[CheckpointFn], domain: str, result: DomainResult, final: bool) -> None:
    if checkpoint is None:
        return
    try:
        await checkpoint(domain, result, final)
    except Exception as exc:  # noqa: BLE001 - a lost cursor costs a re-walk, never the pass
        logger.exception("reap cart proof refresh: checkpoint for %s failed", domain)
        result.checkpoint_error = f"{type(exc).__name__}: {str(exc)[:200]}"


async def drive(domains: Sequence[str], run_page: PageFn, merge: MergeFn, *, budget_s: float,
                gap_s: float, clock: Callable[[], float] = time.monotonic,
                sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
                emit: Optional[Callable[[str], None]] = None,
                start_cursors: Optional[Mapping[str, Optional[str]]] = None,
                checkpoint: Optional[CheckpointFn] = None,
                results: Optional[Dict[str, DomainResult]] = None,
                on_domain_start: Optional[Callable[[str], None]] = None,
                stop_signal: Optional[Callable[[], bool]] = None,
                forgive_window_s: Optional[float] = None) -> Dict[str, DomainResult]:
    """Walk each domain page by page, in order, inside one budget. See the module docstring.

    `results`, when given, is filled in place as the pass goes, so a caller interrupted mid-pass (the
    SIGTERM path) still holds every finished domain. `stop_signal` (the IP breaker's `tripped`) is
    checked before every writer call: once true, the pass stops, and every back-off this run recorded
    within `forgive_window_s` of the trip (None: all of them) is forgiven -- those stores were blocked
    inside the throttle the breaker saw; an earlier, unrelated block keeps its back-off."""
    if not budget_s > 0:
        raise ValueError("budget_s must be positive")
    started = clock()
    results = results if results is not None else {}
    for domain in domains:
        results[domain] = DomainResult(start_cursor=(start_cursors or {}).get(domain))
    first_call = True
    stop_all = False
    #: (store, clock at its abort) for every store this run backed off: forgiven if the breaker trips soon after
    backed_off_this_run: List[tuple] = []
    for domain in domains:
        if stop_all:
            break
        result = results[domain]
        domain_started = clock()
        after: Optional[str] = result.start_cursor
        if on_domain_start is not None:
            on_domain_start(domain)
        try:
            while True:
                if stop_signal is not None and stop_signal():
                    # The IP breaker tripped (in an earlier store, or between pages of this one).
                    result.status = IP_THROTTLED
                    result.pass_abort = True
                    stop_all = True
                    break
                if clock() - started >= budget_s:
                    # Every later domain meets this same check first and is recorded `not_reached`.
                    result.status = BUDGET_STOPPED if result.pages else NOT_REACHED
                    break
                if not first_call:
                    await sleep(gap_s)
                first_call = False
                try:
                    page = await run_page(domain, after)
                    result.pages += 1
                    merge(result.writer, page.report)
                except Exception as exc:  # noqa: BLE001 - recorded against the domain; the pass goes on
                    logger.exception("reap cart proof refresh: %s crashed", domain)
                    result.status = CRASHED
                    result.error = f"{type(exc).__name__}: {str(exc)[:300]}"
                    break
                if page.abort_pass:
                    # The IP breaker tripped during this page: the address is throttled, not the store.
                    result.status = IP_THROTTLED
                    result.pass_abort = True
                    stop_all = True
                    break
                if page.aborted:
                    # This store blocked us: it is backed off, and the next store gets its turn.
                    result.status = ABORTED
                    break
                if page.held:
                    # Rows in this page were not sent: do NOT advance past them. The store stops here
                    # and resumes from the cursor it had before this page.
                    result.status = HELD
                    break
                if page.next_cursor is None:
                    result.status = DONE
                    break
                if page.next_cursor == after:
                    # The writer handed back the cursor it was given: another call would read the same
                    # page forever. Never expected; stop the domain rather than spin until the timeout.
                    result.status = CURSOR_STUCK
                    break
                after = page.next_cursor
                result.last_cursor = after
                await _checkpoint(checkpoint, domain, result, False)
        except asyncio.CancelledError:
            result.status = TERMINATED
            result.elapsed_s = clock() - domain_started
            raise
        result.elapsed_s = clock() - domain_started
        if result.pass_abort:
            # The IP breaker tripped: every store this run backed off hit the address's throttle, not
            # its own. Re-record each without its back-off (its first checkpoint gave it one).
            tripped_at = clock()
            for earlier, aborted_at in backed_off_this_run:
                if forgive_window_s is not None and tripped_at - aborted_at > forgive_window_s:
                    continue  # blocked us well before the throttle began: its back-off stands
                results[earlier].ip_block = True
                await _checkpoint(checkpoint, earlier, results[earlier], True)
        elif result.status == ABORTED:
            backed_off_this_run.append((domain, clock()))
        if result.status != NOT_REACHED:
            await _checkpoint(checkpoint, domain, result, True)
            if emit is not None:
                emit(PROGRESS_PREFIX + json.dumps({"domain": domain, **result.as_dict()}, sort_keys=True,
                                                  default=str))
    return results


def exit_code(results: Mapping[str, DomainResult], *, terminated: bool = False) -> int:
    statuses = {r.status for r in results.values()}
    # Every `ip_throttled` store carries pass_abort: the breaker stopped the pass there.
    if any(r.pass_abort for r in results.values()):
        return EXIT_ABORTED_ON_BLOCK
    if statuses & {CRASHED, CURSOR_STUCK}:
        return EXIT_CRASHED
    if terminated or statuses & {BUDGET_STOPPED, NOT_REACHED, TERMINATED, ABORTED, HELD}:
        return EXIT_BUDGET
    return EXIT_OK


def cursor_row_for(result: DomainResult, final: bool, now: datetime,
                   prior: Optional["cursor_store.CursorRow"] = None,
                   backoff: timedelta = MIRROR_BLOCK_BACKOFF) -> Dict[str, Any]:
    """What `checkpoint` stores for one domain. `prior` is the row as this run found it."""
    row: Dict[str, Any] = {"next_cursor": result.resume_cursor(), "last_status": result.status,
                           "completed_at": None, "blocked_until": None, "crash_count": 0}
    if not final:
        row.update(next_cursor=result.last_cursor, last_status=IN_PROGRESS)
    elif result.status == DONE:
        row.update(next_cursor=None, completed_at=now)
    elif result.status == CURSOR_STUCK:
        row.update(next_cursor=None)
    elif result.status == ABORTED and not (result.pass_abort or result.ip_block):
        # This store blocked us: skip it for a while, then walk it last. Not in a run whose IP breaker
        # tripped -- that throttle was the address's.
        row.update(blocked_until=now + backoff)
    elif result.status == CRASHED:
        cursor = result.resume_cursor()
        again = prior is not None and prior.last_status == CRASHED and prior.next_cursor == cursor
        count = (prior.crash_count if again else 0) + 1
        if count >= CRASH_RESET_AFTER:
            logger.warning("reap cart proof refresh: crashed %d runs in a row at cursor %r; resetting "
                           "the cursor so the rows before it are walked again", count, cursor)
            result.crash_cursor_reset = True
            row.update(next_cursor=None, crash_count=0)
        else:
            row.update(crash_count=count)
    return row


# ── merging a writer's per-page report into the domain's ────────────────────────────────────────

#: The mirror writer's report keys that are plain counts or count maps, summed over pages.
_MIRROR_COUNTS = ("candidates", "rows_with_new_ids", "variant_ids_stamped", "write_conflicts")
_MIRROR_COUNT_MAPS = ("fetch_outcomes", "match_reasons", "cart_proofs", "proof_currency", "json_price_fetches",
                      "most_blocked_domains")


def merge_mirror(total: Dict[str, Any], page: Dict[str, Any]) -> None:
    for key in _MIRROR_COUNTS:
        total[key] = total.get(key, 0) + int(page.get(key) or 0)
    for key in _MIRROR_COUNT_MAPS:
        bucket = total.setdefault(key, {})
        for name, count in (page.get(key) or {}).items():
            bucket[name] = bucket.get(name, 0) + int(count or 0)


def merge_enrichment(total: Dict[str, Any], page: Dict[str, Any]) -> None:
    """The enrichment writer's per-call report is already a full account of its page; pages are kept
    side by side rather than summed."""
    total.setdefault("pages", []).append(page)


# ── the mirror lane's block streak, carried across calls ────────────────────────────────────────


class _Replay:
    """A client that answers one GET with a response (or an exception) already received."""

    def __init__(self, result: Any) -> None:
        self._result = result

    async def get(self, url: str, **_kwargs: Any) -> Any:
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


#: What the cursor table says about a store before this run (`prior_health`). A store with no row is
#: FIRST CONTACT; a HEALTHY one was walked cleanly on a previous run and was not blamed on its last.
PRIOR_HEALTHY = "healthy"
PRIOR_BLOCKED = "blocked"
PRIOR_WALKED = "walked"


def prior_health(row: Optional["cursor_store.CursorRow"]) -> Optional[str]:
    """None (first contact: no row), PRIOR_BLOCKED (its last run ended `aborted_on_block`, back-off
    forgiven or not), PRIOR_HEALTHY (a previous run walked it to its end -- `done`, or a
    `last_completed_at` the table keeps -- and its last run did not blame it), else PRIOR_WALKED."""
    if row is None:
        return None
    if row.last_status == ABORTED:
        return PRIOR_BLOCKED
    if row.last_status == DONE or row.last_completed_at is not None:
        return PRIOR_HEALTHY
    return PRIOR_WALKED


class LaneHealth:
    """Which stores answered CLEANLY, on one sequence shared by the lane's two breakers: this run's
    clean answers, fed by the lanes' own classification of each answer (`LaneBreaker.observe_clean`:
    the mirror client's non-block, non-`not_json`, non-redirect answers, the enrichment writer's streak
    resets), and what the cursor table said about each store before the run (`prior`, store ->
    `prior_health`). `seq` orders every clean answer and every store's start of counting, so "BEFORE
    its blocks" and "SINCE these blocks began" are exact, whatever the clock."""

    def __init__(self, prior: Optional[Mapping[str, Optional[str]]] = None) -> None:
        self.seq = 0
        #: store -> what the cursor table said before this run (absent: first contact)
        self.prior: Dict[str, str] = {k: v for k, v in (prior or {}).items() if v is not None}
        #: store -> seq of its first clean answer this run
        self.first_clean: Dict[str, int] = {}
        #: store -> seq of its latest clean answer this run
        self.last_clean_of: Dict[str, int] = {}
        #: seq of the latest clean answer from ANY store (0: none yet)
        self.last_clean = 0

    def tick(self) -> int:
        self.seq += 1
        return self.seq

    def clean(self, store: str) -> None:
        seq = self.tick()
        self.first_clean.setdefault(store, seq)
        self.last_clean_of[store] = seq
        self.last_clean = seq


class StoreStreakWindow:
    """The trip rule both lane breakers share (#2476 reviews of 7250c3e9d and 61fb15f78).

    A store COUNTS toward a trip only once ITS OWN run of block-shaped answers reaches
    `LANE_STORE_STREAK` in a row (a clean answer from it resets the run). One transient error -- a
    ReadTimeout, a 502 -- is not a store blocking us, and neither is a tiny store the lane aborted after
    one (an abort does not make a store count). A counting store stays in the window for `window_s`
    after its latest block.

    `blocks_after_health`: `LANE_IP_TRIP_HOSTS` counting stores in the window trip it when
      * at least one of them WAS HEALTHY -- it answered cleanly this run before the run of blocks that
        made it count, or the cursor table says a previous run walked it cleanly and did not blame it
        on its last (`PRIOR_HEALTHY`) -- so the blocks are new; and
      * no store OUTSIDE the window answered cleanly since the first of them began counting: a store
        that is still answering says the address is fine and these stores are refusing us themselves.
      A store the table already blames (`aborted_on_block` last time, forgiven or not) or never saw
      walked cleanly is no evidence: that is what keeps stores `defer_blocked` walks last, back to back,
      from tripping it night after night and having their back-offs forgiven.
    `nothing_answering`: ONE MORE than `LANE_IP_TRIP_HOSTS` FIRST-CONTACT stores (no cursor row at all)
      count with no clean answer from any store since the first of them: a lane meeting stores for the
      first time from a blocked address. A first-contact store has a row after this run, so this can
      trip at most once for any store, ever."""

    def __init__(self, health: LaneHealth, *, streak_k: Optional[int] = None,
                 trip_stores: int = LANE_IP_TRIP_HOSTS, window_s: float = LANE_IP_TRIP_WINDOW_S,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.health = health
        self.streak_k = int(LANE_STORE_STREAK if streak_k is None else streak_k)
        self.trip_stores = int(trip_stores)
        self.window_s = float(window_s)
        self._clock = clock
        #: store -> its current run of block-shaped answers
        self.streak: Dict[str, int] = {}
        #: store -> (seq it began counting, clock at its latest block)
        self.counting: Dict[str, Tuple[int, float]] = {}

    def reset(self, store: str) -> None:
        self.streak.pop(store, None)

    def block(self, store: str) -> Optional[str]:
        run = self.streak.get(store, 0) + 1
        self.streak[store] = run
        if run >= self.streak_k:
            self._count(store, fresh=run == self.streak_k)
        return self.verdict()

    def _count(self, store: str, *, fresh: bool) -> None:
        now = self._clock()
        seq = self.health.tick() if fresh or store not in self.counting else self.counting[store][0]
        self.counting[store] = (seq, now)
        for other in [s for s, (_q, t) in self.counting.items() if now - t > self.window_s]:
            del self.counting[other]

    def verdict(self) -> Optional[str]:
        """Why it trips now, or None."""
        window = self.counting
        if len(window) < self.trip_stores:
            return None
        health = self.health
        was_healthy = any(health.first_clean.get(store, seq) < seq or health.prior.get(store) == PRIOR_HEALTHY
                          for store, (seq, _t) in window.items())
        began = min(seq for seq, _t in window.values())
        still_answering = any(seq > began for store, seq in health.last_clean_of.items() if store not in window)
        if was_healthy and not still_answering:
            return "blocks_after_health"
        first_contact = [seq for store, (seq, _t) in window.items() if store not in health.prior]
        if len(first_contact) > self.trip_stores and health.last_clean < min(first_contact):
            return "nothing_answering"
        return None


def store_breaker(stores: Sequence[str], *, health: LaneHealth) -> Any:
    """#2473's `IpThrottleBreaker`, keyed by STORE and not by hostname, with the lanes' trip rule.

    * STORE, NOT HOST: the breaker counts distinct keys, and one store answers from its apex and its
      `www.` twin (and a subdomain the seed names). Keyed by hostname, one store's two hosts plus a
      single stray 429 elsewhere would make "3 distinct hosts" and stop the pass. Every host is folded
      to its lane store (the backfill's own rule: exact or a `.`-subdomain), else its bare host.
    * THE TRIP is `StoreStreakWindow`'s over 429s (and 503 + Retry-After): a store counts only after
      `LANE_STORE_STREAK` of them in a row, and a trip needs evidence the address went bad (see
      `StoreStreakWindow`).

    Built as a subclass so its diagnostics (`summary()`, the 429 header histograms) stay #2473's. The
    trip itself is decided here: the parent's trip is disarmed around each `observe`, and the same
    fields it sets on a trip are set when this one trips."""
    from services.crawl_ip_throttle import IpThrottleBreaker, is_ip_throttle_signal

    lane_stores = tuple(dict.fromkeys(stores))

    class StoreThrottleBreaker(IpThrottleBreaker):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.streaks = StoreStreakWindow(health, trip_stores=self.trip_hosts, window_s=self.window_seconds)
            self.trip_reason: Optional[str] = None

        def store_of(self, host: str) -> str:
            key = str(host or "").strip().lower().rstrip(".")
            for store in lane_stores:
                if key == store or key.endswith("." + store):
                    return store
            return key[4:] if key.startswith("www.") else key

        def observe(self, host: str, status_code: int, diag: Mapping[str, str]) -> None:
            store = self.store_of(host)
            armed = self.enabled
            self.enabled = False  # the parent records the window and diagnostics, never trips
            try:
                super().observe(store, status_code, diag)
            finally:
                self.enabled = armed
            if not armed or self.tripped or not store or not is_ip_throttle_signal(int(status_code), diag):
                return
            reason = self.streaks.block(store)
            if reason is not None:
                self.tripped = True
                self.tripped_at = self.last_throttle_at
                self.trip_reason = reason
                self.trip_host_count = len(self.streaks.counting)
                self.trip_shopify_host_count = len(self._recent_shopify)
                logger.warning("reap cart proof refresh: %d stores each answered %d+ 429s in a row within "
                               "%.0fs (%s); the crawl IP is being throttled",
                               self.trip_host_count, self.streaks.streak_k, self.window_seconds, reason)

        def summary(self) -> Dict[str, Any]:
            out = super().summary()
            out.update(ip_throttle_trip_reason=self.trip_reason, ip_throttle_store_streak=self.streaks.streak_k)
            return out

    return StoreThrottleBreaker(trip_hosts=LANE_IP_TRIP_HOSTS, window_seconds=LANE_IP_TRIP_WINDOW_S, enabled=True)


class StoreBlockBreaker:
    """The run-level stop for block shapes that are NOT a throttle: 403s, 5xx, challenge-less refusals
    and transport errors (connection resets, timeouts). #2473's breaker counts only 429 / 503 +
    Retry-After, but an IP-level block also looks like the 2026-08-21 shape (403s interleaved with
    resets; scripts/backfill_shopify_variant_ids.py) or the 2026-09-28 NAT drops. Same trip rule as
    the throttle breaker (`StoreStreakWindow`), over these shapes: one store that genuinely 403s
    everything is aborted and backed off on its own, while stores that had been answering and then
    block one after another stop the pass."""

    def __init__(self, store_of: Callable[[str], str], *, health: LaneHealth,
                 trip_stores: int = LANE_IP_TRIP_HOSTS, window_s: float = LANE_IP_TRIP_WINDOW_S,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._store_of = store_of
        self.streaks = StoreStreakWindow(health, trip_stores=trip_stores, window_s=window_s, clock=clock)
        self.tripped = False
        self.tripped_at: Optional[str] = None
        self.trip_reason: Optional[str] = None
        self.trip_store_count = 0
        self.blocks = 0
        self.by_outcome: Dict[str, int] = {}

    def observe_block(self, host: str, outcome: str) -> None:
        store = self._store_of(host)
        if not store:
            return
        self.blocks += 1
        key = outcome if outcome in self.by_outcome or len(self.by_outcome) < 16 else "other"
        self.by_outcome[key] = self.by_outcome.get(key, 0) + 1
        self._decide(self.streaks.block(store))

    def _decide(self, reason: Optional[str]) -> None:
        if self.tripped or reason is None:
            return
        self.tripped = True
        self.tripped_at = datetime.now(timezone.utc).isoformat()
        self.trip_reason = reason
        self.trip_store_count = len(self.streaks.counting)
        logger.warning("reap cart proof refresh: %d stores each answered %d+ block-shaped (non-429) answers "
                       "in a row, or were aborted, within %.0fs (%s); the crawl IP is blocked",
                       self.trip_store_count, self.streaks.streak_k, self.streaks.window_s, reason)

    def summary(self) -> Dict[str, Any]:
        return {"block_breaker_tripped": self.tripped, "tripped_at": self.tripped_at,
                "trip_reason": self.trip_reason, "trip_store_count": self.trip_store_count,
                "store_streak": self.streaks.streak_k, "blocks": self.blocks,
                "by_outcome": dict(self.by_outcome)}


class LaneBreaker:
    """Both run-level stops for a lane: `throttle` (#2473's breaker, store-keyed: 429 / 503 +
    Retry-After, fed through `crawl_politeness.note_response`) and `blocks` (`StoreBlockBreaker`: every
    other block shape and every aborted store, fed by the lanes), over ONE `LaneHealth` (clean answers,
    fed by the lanes). Either one tripping stops the pass WITHOUT backing off stores and forgives the
    back-offs recorded within the window. Installable with `crawl_ip_throttle.installed()` (it
    forwards `observe` to the throttle breaker)."""

    def __init__(self, stores: Sequence[str], *, prior: Optional[Mapping[str, Optional[str]]] = None) -> None:
        self.health = LaneHealth(prior)
        self.throttle = store_breaker(stores, health=self.health)
        self.blocks = StoreBlockBreaker(self.throttle.store_of, health=self.health)

    @property
    def tripped(self) -> bool:
        return bool(self.throttle.tripped or self.blocks.tripped)

    def store_of(self, host: str) -> str:
        return self.throttle.store_of(host)

    def observe(self, host: str, status_code: int, diag: Mapping[str, str]) -> None:
        self.throttle.observe(host, status_code, diag)

    def observe_block(self, host: str, outcome: str) -> None:
        """A block-shaped answer. A 429 is the throttle breaker's (fed with its headers elsewhere)."""
        if outcome != "rate_limited":
            self.blocks.observe_block(host, outcome)

    def observe_clean(self, host: str) -> None:
        """A clean answer (the lane's own classification): the store is talking to us. Ends its runs."""
        store = self.store_of(host)
        if not store:
            return
        self.health.clean(store)
        self.throttle.streaks.reset(store)
        self.blocks.streaks.reset(store)


def lane_breaker(stores: Sequence[str], *, prior: Optional[Mapping[str, Optional[str]]] = None) -> LaneBreaker:
    return LaneBreaker(stores, prior=prior)


class BlockStreakClient:
    """The client handed to the backfill. For every GET the backfill makes it

      1. answers 429 LOCALLY (nothing sent) once this store's block count has reached the backfill's
         `CONSECUTIVE_BLOCK_ABORT` or the run's IP breaker has tripped;
      2. marks the host Shopify-served (every mirror target is a Tier B Shopify store's products.js)
         and waits for `crawl_politeness.before_request`: robots.txt, the host's own interval, any
         Retry-After / backoff hold `note_response` armed, and the shared Shopify-edge slot (#2474; a
         bounded caller gets both slots or neither). A request robots disallows, or one not released
         within `max_wait`, is NOT sent: answered locally as a non-answer the backfill reads as
         `not_json` (neutral; nothing written for that row). A HELD request is counted
         (`not_sent`, `store_not_sent`): the page stops the store without advancing its cursor. A
         robots refusal is permanent, so it is counted apart (`robots_disallowed`) and does not;
      3. sends it on the real client, then reports the answer with its HEADERS to
         `crawl_politeness.note_response` (per-host backoff, and the installed IP breaker) and to
         `shopify_edge_pacer.learn_from_response`;
      4. classifies it with the backfill's OWN `fetch_product_js` + `_is_block` into this store's
         count: a block adds one, `not_json` touches nothing, anything else resets it.

    `start_store()` resets the per-store counts. The backfill's logic is untouched: it still paces,
    classifies and aborts exactly as before; this only decides what its `client.get` returns."""

    def __init__(self, client: Any, backfill: Any, *, breaker: Any = None,
                 max_wait: float = MIRROR_MAX_POLITE_WAIT_S) -> None:
        self._client = client
        self._backfill = backfill
        self.breaker = breaker
        self.max_wait = max_wait
        self.state = {"store": 0}
        self.limit = int(backfill.CONSECUTIVE_BLOCK_ABORT)
        self.short_circuited = 0
        self.not_sent = 0
        self.robots_disallowed = 0
        self.crawl_delay_too_long = 0
        self.store_blocks = 0
        self.store_clean = 0
        self.store_not_sent = 0
        self.off_storefront_redirects = 0

    def start_store(self, _domain: str = "") -> None:
        self.state["store"] = 0
        self.store_blocks = 0
        self.store_clean = 0
        self.store_not_sent = 0

    @property
    def store_tripped(self) -> bool:
        return self.state["store"] >= self.limit

    @property
    def ip_tripped(self) -> bool:
        return bool(getattr(self.breaker, "tripped", False))

    def _local(self, url: str, status: int, why: str) -> Any:
        return httpx.Response(status, headers={"content-type": "text/plain"}, text=f"not sent: {why}",
                              request=httpx.Request("GET", url))

    async def get(self, url: str, **kwargs: Any) -> Any:
        if self.store_tripped or self.ip_tripped:
            self.short_circuited += 1
            return httpx.Response(429, request=httpx.Request("GET", url))
        from services import crawl_politeness, shopify_edge_pacer
        from services.curated_brand_feed import _same_storefront_host

        user_agent = str(getattr(self._backfill, "USER_AGENT", "") or "")
        # Hops are followed HERE, one by one, each gated, paced and reported like the first request;
        # httpx following them would send requests nothing gates (#2476 review P1-2).
        kwargs = {**kwargs, "follow_redirects": False}
        requested_host = urlsplit(url).hostname or ""
        # The lane's patience is a DEADLINE for the whole request, hops included.
        deadline = time.monotonic() + self.max_wait
        current = url
        result: Any = None
        off_storefront = False
        for _hop in range(MIRROR_MAX_REDIRECTS + 1):
            shopify_edge_pacer.mark_shopify_host(current)
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise crawl_politeness.CrawlPaced("the request's patience ran out between hops")
                await crawl_politeness.before_request(current, user_agent=user_agent, max_wait=remaining)
            except crawl_politeness.RobotsDisallowed:
                self.robots_disallowed += 1
                return self._local(current, LOCAL_REFUSED_STATUS, "robots")
            except crawl_politeness.CrawlDelayTooLong:
                # The host asks for a Crawl-delay over the cap: permanent for this run, not a hold.
                self.crawl_delay_too_long += 1
                return self._local(current, LOCAL_REFUSED_STATUS, "crawl-delay")
            except crawl_politeness.CrawlPaced as exc:  # EdgePaced included
                self.not_sent += 1
                self.store_not_sent += 1
                return self._local(current, LOCAL_HELD_STATUS, type(exc).__name__)
            try:
                result = await self._client.get(current, **kwargs)
            except Exception as exc:  # noqa: BLE001 - re-raised below, after it is counted
                result = exc
                break
            headers = getattr(result, "headers", None)
            crawl_politeness.note_response(
                current, result.status_code,
                retry_after=headers.get("retry-after") if headers is not None else None, headers=headers)
            shopify_edge_pacer.learn_from_response(current, headers)
            if result.status_code not in _REDIRECT_STATUSES:
                break
            location = headers.get("location") if headers is not None else None
            if not location:
                break
            nxt = urlsplit(urljoin(current, location))
            if nxt.scheme != "https" or not _same_storefront_host(requested_host, nxt.hostname or ""):
                # Off this storefront or off https: never requested. Its own outcome, not the 3xx the
                # backfill would read as `not_json` and count into `most_blocked_domains`.
                self.off_storefront_redirects += 1
                off_storefront = True
                result = self._local(current, LOCAL_OFF_STOREFRONT_STATUS, "redirect off the storefront")
                break
            current = nxt.geturl()
        _payload, outcome = await self._backfill.fetch_product_js(_Replay(result), url)
        if self._backfill._is_block(outcome):
            self.state["store"] += 1
            self.store_blocks += 1
            observe = getattr(self.breaker, "observe_block", None)
            if observe is not None:
                observe(requested_host, outcome)
        elif outcome != "not_json" and not off_storefront:
            # An off-storefront redirect is NEUTRAL here (neither a block nor a clean answer): the store
            # answered, but not with its product page.
            self.state["store"] = 0
            self.store_clean += 1
            observe_clean = getattr(self.breaker, "observe_clean", None)
            if observe_clean is not None:
                observe_clean(requested_host)
        if isinstance(result, Exception):
            raise result
        return result


class ObservedStreak(dict):
    """The enrichment writer's `block_state` for ONE store, carried across that store's pages, with its
    blocks and clean answers counted. The writer adds one per block (`block_state["consecutive"] += 1`),
    sets 0 on a clean answer and leaves it alone for a neutral one."""

    def __init__(self, on_clean: Optional[Callable[[], None]] = None) -> None:
        super().__init__(consecutive=0)
        self.blocks = 0
        self.clean = 0
        self._on_clean = on_clean

    def __setitem__(self, key: str, value: int) -> None:
        if key == "consecutive":
            old = self.get("consecutive", 0)
            if value > old:
                self.blocks += value - old
            elif value == 0:
                self.clean += 1
                if self._on_clean is not None:
                    self._on_clean()
        super().__setitem__(key, value)


# ── the two lanes: one writer call per page ─────────────────────────────────────────────────────


def mirror_domains(merchants_path: Optional[str] = None) -> List[str]:
    from services.tierb_cart_link_merchants import load_merchants

    return sorted({m.domain for m in load_merchants(merchants_path)})


def mirror_page_fn(backfill: Any, client: Any, *, apply: bool, page_size: int = MIRROR_PAGE_SIZE) -> PageFn:
    async def run_page(domain: str, after: Optional[str]) -> Page:
        held_before = getattr(client, "not_sent", 0)
        report = await backfill.run(limit=page_size, domain=domain, apply=apply, client=client, after=after)
        held = getattr(client, "not_sent", 0) - held_before
        # Fewer candidates than asked for: the domain is walked to its end.
        exhausted = int(report.get("candidates") or 0) < page_size
        # A store walked to its end with blocks and not ONE clean answer was never actually read -- unless
        # requests were HELD: those were never asked, so they are no evidence against the store.
        never_read = (exhausted and getattr(client, "store_blocks", 0) > 0
                      and getattr(client, "store_clean", 0) == 0 and getattr(client, "store_not_sent", 0) == 0)
        # The backfill's own abort, OR the store count carried across this store's calls reaching the
        # same threshold (the backfill's per-call counter cannot see blocks from earlier calls).
        aborted = (bool(report.get("aborted_on_block")) or bool(getattr(client, "store_tripped", False))
                   or never_read)
        abort_pass = bool(getattr(client, "ip_tripped", False))
        cursor = None if (aborted or abort_pass or held or exhausted) else report.get("next_cursor")
        return Page(report=report, next_cursor=cursor, aborted=aborted, abort_pass=abort_pass, held=held)

    return run_page


#: The enrichment writer's fetch outcome for a request crawl_politeness held past its patience. Its
#: `robots_disallowed` and `crawl_delay_too_long` are permanent refusals, not holds, and are not here.
ENRICHMENT_HELD_OUTCOMES = ("crawl_paced",)


def enrichment_page_fn(job: Any, db: Any, client: Any, plans: Mapping[str, Any], *, apply: bool, pacer: Any,
                       breaker: Any = None, page_products: int = ENRICHMENT_PAGE_PRODUCTS) -> PageFn:
    """One `run_domain` call per page. Each store gets its own `ObservedStreak` (the writer's store
    count, carried across that store's pages). The writer's `should_stop` is the IP breaker, so it
    stops asking the moment the breaker trips."""
    per_store: Dict[str, ObservedStreak] = {}

    def ip_tripped() -> bool:
        return bool(getattr(breaker, "tripped", False))

    async def run_page(domain: str, after: Optional[str]) -> Page:
        limit = job.abort_after_blocks()
        if domain not in per_store:
            observe_clean = getattr(breaker, "observe_clean", None)
            per_store[domain] = ObservedStreak(
                on_clean=(lambda: observe_clean(domain)) if observe_clean is not None else None)
        streak = per_store[domain]

        def on_block(outcome: str) -> None:
            observe = getattr(breaker, "observe_block", None)
            if observe is not None:
                observe(domain, outcome)

        report = await job.run_domain(db, client, plans[domain], apply=apply, source_mode="auto",
                                      limit=page_products, after=after, pacer=pacer,
                                      block_limit=limit, block_state=streak, should_stop=ip_tripped,
                                      on_block=on_block)
        exhausted = bool(report.get("exhausted"))
        fetches = report.get("fetches") or {}
        # Requests crawl_politeness did not release (a hold, a Crawl-delay over the cap): not asked.
        held = sum(int(fetches.get(outcome) or 0) for outcome in ENRICHMENT_HELD_OUTCOMES)
        never_read = exhausted and streak.blocks > 0 and streak.clean == 0 and not held
        abort_pass = ip_tripped()
        aborted = (bool(report.get("aborted_on_block")) or never_read) and not abort_pass
        cursor = None if (aborted or abort_pass or held or exhausted) else report.get("next_cursor")
        return Page(report=dict(report), next_cursor=cursor, aborted=aborted, abort_pass=abort_pass, held=held)

    return run_page


def inter_call_gap_s(lane: str, *, backfill: Any = None, job: Any = None) -> float:
    """The writer's own slowest spacing, applied between two calls."""
    if lane == "mirror":
        return max(float(backfill.GLOBAL_MIN_INTERVAL_S), float(backfill.PER_DOMAIN_MIN_GAP_S))
    return float(job.request_gap_s())


# ── planning and running a lane ─────────────────────────────────────────────────────────────────


@dataclass
class LanePlan:
    """Everything a lane needs, resolved BEFORE the database or the network is touched, so a
    domain list the writer refuses is exit 2 with nothing attempted."""
    lane: str
    domains: List[str]
    writer: Any
    gap_s: float
    plans: Dict[str, Any] = field(default_factory=dict)
    #: enrichment: the proof table's `ensure_table` (the writer's `run()` calls it; `run_domain` does not).
    ensure_proof_table: Optional[Callable[[], Awaitable[bool]]] = None
    #: mirror: how long a proof is valid (the stalest-first order ignores lapsed proofs).
    proof_max_age: Optional[timedelta] = None


def plan_lane(lane: str, now: datetime) -> LanePlan:
    if lane == "mirror":
        from scripts import backfill_shopify_variant_ids as backfill
        from services.shopify_variant_identity import CART_PROOF_MAX_AGE

        return LanePlan(lane=lane, domains=mirror_domains(), writer=backfill,
                        gap_s=inter_call_gap_s(lane, backfill=backfill), proof_max_age=CART_PROOF_MAX_AGE)
    import jobs.enrichment_cart_variant_proof as job
    from db.enrichment_cart_variant_proofs import ensure_table

    plans = {p.domain: p for p in job.plan_domains(list(ENRICHMENT_DOMAINS))}
    return LanePlan(lane=lane, domains=list(plans), writer=job, gap_s=inter_call_gap_s(lane, job=job),
                    plans=plans, ensure_proof_table=ensure_table)


@dataclass
class RunState:
    """Filled as the pass goes, so the SIGTERM path can report what was done."""
    results: Dict[str, DomainResult] = field(default_factory=dict)
    info: Dict[str, Any] = field(default_factory=dict)
    terminated: Optional[str] = None
    #: prints the report; called on the SIGTERM path before the database is disconnected
    on_terminate: Optional[Callable[[], None]] = None
    reported: bool = False


async def run_lane(plan: LanePlan, *, apply: bool, budget_s: float, emit: Callable[[str], None],
                   state: Optional[RunState] = None, db: Any = None,
                   now: Callable[[], datetime] = _utcnow) -> Dict[str, Any]:
    if db is None:
        from db.database import database as db
    state = state if state is not None else RunState()
    info = state.info
    info.update(domains_order=list(plan.domains), inter_call_gap_s=plan.gap_s)
    cursors: Dict[str, cursor_store.CursorRow] = {}

    async def checkpoint(domain: str, result: DomainResult, final: bool) -> None:
        row = cursor_row_for(result, final, now(), cursors.get(domain), backoff=block_backoff(plan.lane))
        await cursor_store.save(db, lane=plan.lane, domain=domain, next_cursor=row["next_cursor"],
                                last_status=row["last_status"], completed_at=row["completed_at"], now=now(),
                                blocked_until=row["blocked_until"], crash_count=row["crash_count"])

    await db.connect()
    try:
        if apply and not await cursor_store.ensure_table(db):
            raise RuntimeError(f"could not ensure {cursor_store.TABLE}")
        cursors.update(await cursor_store.load(db, plan.lane, table_must_exist=apply))
        start = {d: row.next_cursor for d, row in cursors.items() if row.next_cursor is not None}
        info["resumed"] = {d: c for d, c in start.items() if d in plan.domains}
        kwargs = dict(budget_s=budget_s, gap_s=plan.gap_s, emit=emit, start_cursors=start,
                      checkpoint=checkpoint if apply else None, results=state.results)
        domains = list(plan.domains)
        if plan.lane == "mirror":
            oldest = await select_oldest_valid_mirror_proofs(db, now=now(), max_age=plan.proof_max_age)
            domains = order_mirror_domains(domains, oldest, cursors)
        domains, backed_off = defer_blocked(domains, cursors, now())
        info["domains_order"] = domains
        info["backed_off"] = {d: str(cursors[d].blocked_until) for d in backed_off}
        for domain in backed_off:
            state.results[domain] = DomainResult(status=BACKED_OFF)
        from services import crawl_identity, crawl_ip_throttle, shopify_edge_pacer

        # What every store request of this run carries: off | signed | unsigned_<reason>.
        info["web_bot_auth"] = crawl_identity.status()

        # What the cursor table says about each store: health from a previous run is evidence the
        # address went bad; a store it already blames is not (`StoreStreakWindow`).
        breaker = lane_breaker(plan.domains, prior={d: prior_health(row) for d, row in cursors.items()})
        kwargs["stop_signal"] = lambda: breaker.tripped
        kwargs["forgive_window_s"] = LANE_IP_TRIP_WINDOW_S
        try:
            with crawl_ip_throttle.installed(breaker):
                if plan.lane == "mirror":
                    # No cookie rides any request (a hop's cart_currency/localization would steer the
                    # presentment currency the proofs now record): services/shopify_presentment.py.
                    from services.shopify_presentment import no_cookie_client

                    async with no_cookie_client() as raw_client:
                        client = BlockStreakClient(raw_client, plan.writer, breaker=breaker)
                        try:
                            await drive(domains, mirror_page_fn(plan.writer, client, apply=apply), merge_mirror,
                                        on_domain_start=client.start_store, **kwargs)
                        finally:
                            info["block_streak_short_circuited"] = client.short_circuited
                            info["not_sent_by_politeness"] = client.not_sent
                            info["robots_disallowed"] = client.robots_disallowed
                            info["crawl_delay_too_long"] = client.crawl_delay_too_long
                            info["off_storefront_redirects"] = client.off_storefront_redirects
                else:
                    job = plan.writer
                    if apply and not await plan.ensure_proof_table():
                        raise RuntimeError("could not ensure the enrichment proof table")
                    pacer = job.Pacer(job.request_gap_s())
                    async with job.no_cookie_client() as client:
                        try:
                            await drive(domains,
                                        enrichment_page_fn(job, db, client, plan.plans, apply=apply, pacer=pacer,
                                                           breaker=breaker),
                                        merge_enrichment, **kwargs)
                        finally:
                            info["requests"] = pacer.requests
        finally:
            info["ip_throttle"] = breaker.throttle.summary()
            info["block_breaker"] = breaker.blocks.summary()
            info["shopify_edge_pacer"] = shopify_edge_pacer.stats()
    finally:
        # A SIGTERM lands here first: print the partial report while the process is still ours and
        # BEFORE the disconnect, which can itself take the grace period.
        if state.terminated is not None and state.on_terminate is not None:
            state.on_terminate()
        await db.disconnect()
    return info


# ── main ────────────────────────────────────────────────────────────────────────────────────────


def _parse(argv: Optional[List[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("lane", choices=LANES)
    parser.add_argument("--on-crawl-egress", action="store_true",
                        help="REQUIRED: this process egresses by the crawl subnet (pivota-crawl)")
    parser.add_argument("--budget-seconds", type=float, required=True,
                        help="wall-clock budget; once spent no new page starts")
    parser.add_argument("--only", action="append", default=None, metavar="DOMAIN",
                        help="walk only this store (repeatable); it must be on the lane's list. For the "
                             "first dry run, which is ONE small store (docs/runbooks/reap_cart_proofs.md)")
    return parser.parse_args(argv)


def restrict(plan: LanePlan, only: Optional[Sequence[str]]) -> LanePlan:
    """The plan limited to `only`, in the plan's own order. A domain not on the lane's list is refused
    (MerchantListError: exit 2, nothing attempted), never silently dropped."""
    if not only:
        return plan
    from services.tierb_cart_link_merchants import MerchantListError, normalize_domain

    try:
        wanted = {normalize_domain(d) for d in only}
    except ValueError as exc:
        raise MerchantListError(f"--only: {exc}") from None
    missing = sorted(wanted - set(plan.domains))
    if missing:
        raise MerchantListError(f"--only names stores not on the {plan.lane} lane's list: {missing}")
    plan.domains = [d for d in plan.domains if d in wanted]
    plan.plans = {d: p for d, p in plan.plans.items() if d in wanted}
    return plan


def _emit(line: str) -> None:
    print(line, flush=True)


async def _run_with_sigterm(plan: LanePlan, *, apply: bool, budget_s: float, emit: Callable[[str], None],
                            state: RunState) -> None:
    """Run the lane; a SIGTERM (Cloud Run's task timeout, or a cancel) cancels it and marks the state, so
    main still prints the partial report."""
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()

    def on_sigterm() -> None:
        state.terminated = "SIGTERM"
        task.cancel()

    installed = False
    try:
        loop.add_signal_handler(signal.SIGTERM, on_sigterm)
        installed = True
    except (NotImplementedError, RuntimeError, ValueError):  # not the main thread / no signal support
        logger.warning("could not install a SIGTERM handler; a timeout will leave no partial report")
    try:
        await run_lane(plan, apply=apply, budget_s=budget_s, emit=emit, state=state)
    except asyncio.CancelledError:
        if state.terminated is None:
            raise
    finally:
        if installed:
            loop.remove_signal_handler(signal.SIGTERM)


def main(argv: Optional[List[str]] = None, *, environ: Optional[Mapping[str, str]] = None,
         emit: Callable[[str], None] = _emit) -> int:
    try:
        args = _parse(argv)
    except SystemExit as exc:  # argparse: a usage error is a bad argument, never a crash
        return EXIT_BAD_ARGS if exc.code else EXIT_OK
    if not args.on_crawl_egress:
        print("REAP_CART_PROOF_ERROR refusing to crawl without --on-crawl-egress: run on the "
              "pivota-crawl subnet and say so", file=sys.stderr, flush=True)
        return EXIT_BAD_ARGS
    if not (math.isfinite(args.budget_seconds) and args.budget_seconds > 0):
        print("REAP_CART_PROOF_ERROR --budget-seconds must be a positive number", file=sys.stderr, flush=True)
        return EXIT_BAD_ARGS
    apply = apply_enabled(environ)
    now = _utcnow()
    started = time.monotonic()
    try:
        from services.tierb_cart_link_merchants import MerchantListError

        plan = restrict(plan_lane(args.lane, now), args.only)
    except (MerchantListError, FileNotFoundError) as exc:
        print(f"REAP_CART_PROOF_ERROR {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return EXIT_BAD_ARGS
    except Exception as exc:  # noqa: BLE001 - an import or planning failure is a crash, never "a block"
        logger.exception("reap cart proof refresh could not plan the %s lane", args.lane)
        print(f"REAP_CART_PROOF_CRASH {type(exc).__name__}: {str(exc)[:300]}", file=sys.stderr, flush=True)
        return EXIT_CRASHED
    state = RunState()

    def report() -> int:
        """Build and print the one report line (once), and return the exit code. A SIGTERM always
        exits non-zero, even when it arrived before any store finished."""
        code = exit_code(state.results, terminated=state.terminated is not None)
        if not state.reported:
            state.reported = True
            body = {
                "lane": args.lane, "mode": "apply" if apply else "dry_run", "gate_env": GATE_ENV,
                "budget_s": args.budget_seconds, "elapsed_s": round(time.monotonic() - started, 1),
                "started_at": now.isoformat(), "exit_code": code, "terminated": state.terminated,
                "status_counts": _status_counts(state.results),
                "domains": {d: r.as_dict() for d, r in state.results.items()},
                **state.info,
            }
            emit(REPORT_PREFIX + json.dumps(body, sort_keys=True, default=str))
        return code

    state.on_terminate = report
    try:
        asyncio.run(_run_with_sigterm(plan, apply=apply, budget_s=args.budget_seconds, emit=emit, state=state))
    except Exception as exc:  # noqa: BLE001 - the pass itself could not run (e.g. the DB is unreachable)
        logger.exception("reap cart proof refresh crashed")
        print(f"REAP_CART_PROOF_CRASH {type(exc).__name__}: {str(exc)[:300]}", file=sys.stderr, flush=True)
        if state.terminated is not None:
            return report()
        return EXIT_CRASHED
    return report()


def _status_counts(results: Mapping[str, DomainResult]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for result in results.values():
        counts[result.status] = counts.get(result.status, 0) + 1
    return counts


if __name__ == "__main__":
    raise SystemExit(main())
